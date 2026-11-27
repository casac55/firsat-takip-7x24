from __future__ import annotations

import html as _html
import logging
from typing import Optional

from src.amazon_depo import AmazonDepoDeal, AmazonDepoHunter, build_depo_cart_url
from src.channel_publisher import ChannelPublisher
from src.config import AppConfig
from src.fetchers import HttpFetcher
from src.notifier import TelegramNotifier, format_tl
from src.popular_deals import PopularDeal, _enrich_deal_insights, canonicalize_deal_key
from src.storage import PriceStorage
from src.wakeup import WakeupAlarmManager

logger = logging.getLogger(__name__)


class AmazonDepoRunner:
    """
    %100 Amazon.com.tr Depo (Warehouse Deals — A215JX4S9CANSO) odaklı ana motor.
    Başka hiçbir mağaza veya genel tarayıcı çalıştırarak IP hız sınırını tüketmez.
    """

    def __init__(
        self,
        config: AppConfig,
        storage: PriceStorage,
        notifier: TelegramNotifier,
        wakeup: Optional[WakeupAlarmManager] = None,
    ) -> None:
        self.config = config
        self.storage = storage
        self.notifier = notifier
        self.wakeup = wakeup
        self.fetcher = HttpFetcher(
            timeout_seconds=config.timeout_seconds,
            use_curl_cffi=config.use_curl_cffi,
            use_playwright_fallback=config.use_playwright_fallback,
        )
        self.hunter = AmazonDepoHunter(self.fetcher)
        self.channel_publisher = ChannelPublisher(config, storage, notifier)
        self._cycle_count = 0

    def run_cycle(self, force: bool = False) -> dict:
        if not force and not self.config.depo_enabled:
            return {"found": 0, "removed": 0, "super_alerts": 0}

        self._cycle_count += 1
        watchlist = self.storage.list_depo_watchlist()

        deals, sold_out = self.hunter.hunt(
            categories=self.config.depo_categories,
            keywords=self.config.depo_keywords,
            watchlist=watchlist,
            scan_popular_pool=True,
            min_discount_percent=self.config.depo_min_discount_percent,
            super_discount_percent=self.config.depo_super_discount_percent,
            min_price_tl=self.config.depo_min_price_tl,
            max_price_tl=self.config.depo_max_price_tl,
            max_items=50,
        )

        # Watchlist durumlarını güncelle
        found_map = {d.asin: d for d in deals}
        for w in watchlist:
            w_asin = str(w.get("asin") or "").upper()
            if w_asin in found_map:
                d = found_map[w_asin]
                self.storage.update_depo_watchlist_status(w_asin, d.depo_price_tl, True, note=d.title)
            elif w_asin in sold_out:
                self.storage.update_depo_watchlist_status(w_asin, 0.0, False)

        # Her 2 döngüde bir paneldeki eski ürünlerin AOD stoğunu da doğrula ve bitenleri anında sil
        existing = self.storage.list_depo_deals(min_discount=0, limit=30)
        unchecked_existing = [r for r in existing if str(r.get("asin") or "") not in found_map]
        if unchecked_existing and (force or self._cycle_count % 2 == 0):
            still_alive, gone = self.hunter.verify_existing_deals_stock(
                unchecked_existing,
                super_discount_percent=self.config.depo_super_discount_percent,
                max_check=10,
            )
            sold_out.update(gone)
            if still_alive:
                self.storage.upsert_depo_deals(still_alive)

        removed_cnt = self.storage.remove_depo_asins(sold_out)
        if deals:
            self.storage.upsert_depo_deals(deals)

        super_alerts = 0
        for d in deals:
            if not (d.is_super_steal or d.is_watchlist):
                continue
            if self.storage.was_depo_asin_notified(d.asin, d.depo_price_tl):
                continue

            super_alerts += 1
            if self.config.depo_auto_notify_admin and self.notifier.is_configured:
                self._send_admin_super_alert(d)
                self.storage.mark_depo_asin_notified(d.asin, d.depo_price_tl)

            if self.config.depo_auto_publish_channel and self.channel_publisher.is_enabled:
                self.publish_depo_to_channel(d)

        top_pct = deals[0].discount_percent if deals else 0
        logger.info(
            "[Amazon Depo Sniper] Döngü #%d tamamlandı: %d aktif fırsat, %d tükenen silindi, %d acil alarm (en yüksek: -%%%d).",
            self._cycle_count,
            len(deals),
            removed_cnt,
            super_alerts,
            top_pct,
        )
        return {
            "found": len(deals),
            "removed": removed_cnt,
            "super_alerts": super_alerts,
            "top_discount": top_pct,
        }

    def verify_stock_now(self) -> dict:
        existing = self.storage.list_depo_deals(min_discount=0, limit=40)
        if not existing:
            return {"alive": 0, "removed": 0}
        alive, gone = self.hunter.verify_existing_deals_stock(
            existing,
            super_discount_percent=self.config.depo_super_discount_percent,
            max_check=25,
        )
        removed = self.storage.remove_depo_asins(gone)
        if alive:
            self.storage.upsert_depo_deals(alive)
        return {"alive": len(alive), "removed": removed}

    def _send_admin_super_alert(self, d: AmazonDepoDeal) -> None:
        cart_url = d.cart_url(self.config.amazon_associate_tag)
        oid_badge = "✅ Doğrudan Depo Sepet Kodu (OID)" if d.offer_listing_id else "🔗 Depo Sayfası"
        note_line = f"\n📝 Kusur/Not: <i>{_html.escape(d.condition_note)}</i>" if d.condition_note else ""
        wl_tag = "🎯 <b>ÖZEL NÖBET ÜRÜNÜ YAKALANDI!</b>\n" if d.is_watchlist else ""

        msg = (
            f"{wl_tag}🚨📦 <b>AMAZON DEPO -%{d.discount_percent} DÜŞÜŞ!</b>\n\n"
            f"📦 <b>{_html.escape(d.title)}</b>\n"
            f"💰 Depo Fiyatı: <b>{format_tl(d.depo_price_tl)}</b>\n"
            f"📊 Sıfır Fiyatı: <s>{format_tl(d.market_price_tl)}</s> ({_html.escape(d.price_source)})\n"
            f"🔥 Net Kazanç: <b>{format_tl(d.savings_tl)} (-%{d.discount_percent})</b>\n"
            f"🏷 Kondisyon: <b>{_html.escape(d.condition_label)}</b>{note_line}\n"
            f"🛒 Sepet Modu: <i>{oid_badge}</i>"
        )
        kb = {
            "inline_keyboard": [
                [{"text": "⚡ 1 TIKLA DEPO ADETİNİ SEPETE EKLE", "url": cart_url}],
                [{"text": "🛒 Amazon Depo Teklifini Gör", "url": d.url}],
            ]
        }

        if self.wakeup is not None and d.discount_percent >= self.config.depo_super_discount_percent:
            voice = (
                f"Dikkat! Amazon Depo fırsatı yakalandı. {d.title[:50]}, "
                f"yüzde {d.discount_percent} indirimle {int(round(d.depo_price_tl))} lira."
            )
            self.wakeup.trigger(
                summary_html=msg,
                voice_text=voice,
                reply_markup=kb,
                direct_url=cart_url,
            )
        else:
            self.notifier.send_html(msg, reply_markup=kb)

    def publish_depo_to_channel(self, d: AmazonDepoDeal, force_repost: bool = False) -> bool:
        canon = canonicalize_deal_key(d.url)
        if force_repost:
            self.storage.delete_published_channel_deal(canon)
        cart_url = d.cart_url(self.config.amazon_associate_tag)
        pop = PopularDeal(
            deal_key=canon,
            title=f"[Amazon Depo • {d.condition_label}] {d.title}",
            seller="Amazon.com.tr Depo",
            price_tl=d.depo_price_tl,
            previous_or_second_price_tl=d.market_price_tl,
            discount_percent=max(1, d.discount_percent),
            direct_url=cart_url if d.offer_listing_id else d.url,
            image_url=d.image_url,
            category=f"Amazon Depo • {d.category}",
            is_official_seller=True,
            badge_label=f"🚨📦 AMAZON DEPO -%{d.discount_percent} FIRSATI!",
            market_avg_price_tl=d.market_price_tl,
            is_glitch=(d.discount_percent >= 65),
        )
        _enrich_deal_insights(pop)
        return self.channel_publisher.publish_popular_deal(pop)

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from src.analyzer import analyze_deals, deduplicate_offers
from src.bestseller_tracker import BestsellerTracker
from src.channel_publisher import ChannelPublisher
from src.community_deals import CommunityDealsScanner
from src.config import AppConfig
from src.fetchers import HttpFetcher
from src.keyword_search import search_product_across_sites
from src.models import AlertType, DealAlert, PriceOffer, ScrapeOutcome
from src.notifier import TelegramNotifier, format_tl
from src.popular_deals import PopularDealsHunter
from src.scrapers.registry import build_enabled_scrapers
from src.storage import PriceStorage
from src.url_tracker import fetch_watched_product
from src.wakeup import WakeupAlarmManager
from src.x_publisher import XPublisher

logger = logging.getLogger(__name__)


@dataclass
class RunResult:
    run_id: int
    offers: list[PriceOffer]
    grouped_offers: dict[str, list[PriceOffer]]
    alerts: list[DealAlert]
    outcomes: list[ScrapeOutcome]
    sources_ok: int
    sources_failed: int


class PriceTrackerRunner:
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
        self.channel_publisher = ChannelPublisher(config, storage, notifier)
        self.x_publisher = XPublisher(config)
        self.fetcher = HttpFetcher(
            timeout_seconds=config.timeout_seconds,
            use_curl_cffi=config.use_curl_cffi,
            use_playwright_fallback=config.use_playwright_fallback,
        )
        self._last_popular_ts: float = 0.0
        self._last_intel_ts: float = 0.0

    def run_once(self, force_summary: bool = False) -> RunResult:
        paused_variants = self.storage.get_paused_variants()
        s25_all_paused = "ALL" in paused_variants

        outcomes: list[ScrapeOutcome] = []
        raw_offers: list[PriceOffer] = []

        if not s25_all_paused:
            scrapers = build_enabled_scrapers(self.fetcher, self.config)
            with ThreadPoolExecutor(max_workers=self.config.max_workers) as pool:
                future_map = {pool.submit(self._run_scraper, s): s.name for s in scrapers}
                for fut in as_completed(future_map):
                    outcome = fut.result()
                    outcomes.append(outcome)
                    raw_offers.extend(outcome.offers)

        run_id = self.storage.create_run()
        deduped = deduplicate_offers(raw_offers)
        sources_ok = sum(1 for o in outcomes if o.error is None)
        sources_failed = sum(1 for o in outcomes if o.error is not None)

        self.storage.save_offers(run_id, deduped)
        self.storage.finalize_run(run_id, len(deduped), sources_ok, sources_failed)

        alerts = analyze_deals(deduped, self.config, self.storage, current_run_id=run_id)
        only_official = "ONLY_OFFICIAL" in paused_variants
        grouped = _group_top_offers(
            deduped, self.config.top_offers_per_variant, only_official=only_official
        )

        if alerts and self.notifier.is_configured:
            msg = self.notifier.build_deals_message(alerts)
            has_wakeup = any(a.is_wakeup_worthy for a in alerts)
            first = alerts[0].offer
            kb = self.notifier.build_deal_keyboard(
                [a.offer.variant for a in alerts],
                include_alarm_stop=has_wakeup,
                direct_url=first.url,
            )

            sent = False
            if has_wakeup and self.wakeup is not None:
                voice_txt = (
                    f"Dikkat! Samsung Galaxy S25 Ultra {first.variant} son 6 ayın en düşük fiyatına indi. "
                    f"Fiyat {int(round(first.effective_price_tl))} lira, mağaza {first.seller}."
                )
                self.wakeup.trigger(
                    summary_html=msg,
                    voice_text=voice_txt,
                    reply_markup=kb,
                    direct_url=first.url,
                )
                sent = True
            else:
                sent = self.notifier.send_html(msg, reply_markup=kb)

            if sent:
                for alert in alerts:
                    self.storage.record_alert(
                        variant=alert.offer.variant,
                        seller=alert.offer.seller,
                        price_tl=alert.offer.effective_price_tl,
                        alert_types=alert.alert_types,
                        url=alert.offer.url,
                    )
                    if self.config.auto_publish_s25_deals and self.channel_publisher.is_enabled:
                        self.channel_publisher.publish_s25_alert(alert)
                    pct_est = int(round(alert.below_market_percent or alert.drop_percent or 0))
                    self._maybe_auto_tweet_deal(
                        deal_key=f"s25:{alert.offer.variant}:{alert.offer.seller}:{int(round(alert.offer.effective_price_tl))}",
                        title=f"Samsung Galaxy S25 Ultra {alert.offer.variant}",
                        price_tl=alert.offer.effective_price_tl,
                        seller=alert.offer.seller,
                        direct_url=alert.offer.url,
                        discount_pct=pct_est,
                        coupon_code=alert.offer.coupon_code,
                        force_qualify=True,
                    )

        # Günlük planlı saatlerde veya zorunlu özette veya notify_every_check açıksa özet gönder
        should_send_summary = force_summary or self.config.notify_every_check
        due_slot = self._check_due_daily_slot()
        if due_slot:
            should_send_summary = True

        if should_send_summary and deduped and self.notifier.is_configured and not s25_all_paused:
            summary_msg = self.notifier.build_summary_message(
                grouped,
                sources_ok=sources_ok,
                sources_total=len(outcomes),
                paused_variants=paused_variants,
            )
            if self.notifier.send_html(
                summary_msg,
                reply_markup=self.notifier.build_main_menu_keyboard(only_official=only_official),
            ) and due_slot:
                self.storage.mark_daily_report_sent(due_slot)

        # Katman 1 (Her döngüde: 2-3 dk): Özel takip edilen ürün linkleri, kelime aramaları, topluluk kuponları ve Amazon Depo (%70-%90 düşüş radarı)
        self._check_watched_urls()
        self._check_watched_keywords()
        self._check_community_deals()
        self.scan_amazon_depo(force=False)

        # Katman 2 (10 dakikada bir): Çok satanlar (%60+ fiyat hatası avcısı dahil) ve popüler fırsatlar
        now_ts = time.time()
        if now_ts - self._last_popular_ts >= 600.0:
            self._last_popular_ts = now_ts
            self._check_bestseller_drops()
            if self.config.auto_publish_popular_deals and self.channel_publisher.is_enabled:
                self.publish_next_popular_deals(max_posts=self.config.max_popular_posts_per_scan)

        # Katman 3 (30 dakikada bir): Çark/tıkla-kazan ve sosyal medya influencer arama kodları
        if now_ts - self._last_intel_ts >= 1800.0:
            self._last_intel_ts = now_ts
            self._check_gamified_campaigns()
            self._check_influencer_codes()

        self._check_daily_habit_report()
        self._check_daily_smoke_test(outcomes)

        return RunResult(
            run_id=run_id,
            offers=deduped,
            grouped_offers=grouped,
            alerts=alerts,
            outcomes=sorted(outcomes, key=lambda o: o.source),
            sources_ok=sources_ok,
            sources_failed=sources_failed,
        )

    def _run_scraper(self, scraper) -> ScrapeOutcome:
        start = time.perf_counter()
        try:
            offers = scraper.scrape()
            elapsed = round(time.perf_counter() - start, 2)
            logger.info("[%s] %d teklif bulundu (%.2fs)", scraper.name, len(offers), elapsed)
            return ScrapeOutcome(source=scraper.name, offers=offers, elapsed_seconds=elapsed)
        except Exception as exc:
            elapsed = round(time.perf_counter() - start, 2)
            logger.warning("[%s] Hata (%.2fs): %s", scraper.name, elapsed, exc)
            return ScrapeOutcome(source=scraper.name, error=str(exc), elapsed_seconds=elapsed)

    def _check_due_daily_slot(self) -> str | None:
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        now_minutes = now.hour * 60 + now.minute

        for slot in self.config.daily_summary_times:
            try:
                hh, mm = map(int, slot.split(":"))
            except ValueError:
                continue
            slot_minutes = hh * 60 + mm
            if 0 <= (now_minutes - slot_minutes) <= 45:
                report_key = f"{today}_{slot}"
                if not self.storage.has_daily_report_sent(report_key):
                    return report_key
        return None

    def _check_watched_urls(self) -> None:
        watched = self.storage.list_watched_urls(active_only=True)
        if not watched:
            return

        alerts_to_send: list[dict] = []
        for item in watched:
            watch_id = int(item["id"])
            url = str(item["url"])
            old_price = float(item["last_price_tl"])
            min_price = float(item["min_price_tl"])
            prev_in_stock = bool(item.get("in_stock", 1))
            target_price = float(item["target_price_tl"]) if item.get("target_price_tl") is not None else None
            six_month_low = self.storage.get_watched_url_six_month_low(
                watch_id, days=self.config.six_month_days
            )

            try:
                snap = fetch_watched_product(url, self.fetcher)
            except Exception as exc:
                logger.warning("[watched#%d] Kontrol hatası (%s): %s", watch_id, url, exc)
                continue

            eff_price = snap.effective_price_tl
            self.storage.update_watched_url_price(
                watch_id,
                snap.title,
                snap.price_tl,
                snap.in_stock,
                effective_price_tl=eff_price,
                coupon_info=snap.coupon_info,
                coupon_code=snap.coupon_code,
            )

            if not snap.in_stock or eff_price <= 0:
                continue

            reasons: list[str] = []
            alert_types: list[AlertType] = []

            if not prev_in_stock and snap.in_stock:
                reasons.append("📦🔥 STOĞA GİRDİ!")
                alert_types.append(AlertType.BACK_IN_STOCK)

            if target_price is not None and eff_price <= target_price:
                reasons.append("🎯 Hedef Fiyat Altı")
                alert_types.append(AlertType.TARGET_PRICE)

            if six_month_low is not None and six_month_low > 0 and eff_price < six_month_low - 50.0:
                if snap.coupon_discount_tl > 0 and snap.price_tl >= six_month_low - 50.0:
                    reasons.append("🎟🔥 Kuponla 6 Aylık En Düşüğün Altında")
                    alert_types.append(AlertType.COUPON_SIX_MONTH_LOW)
                else:
                    reasons.append("📉🔥 6 Aylık En Düşüğün Altında")
                    alert_types.append(AlertType.SIX_MONTH_LOW)

            if min_price > 0 and eff_price < min_price - 50.0:
                reasons.append("🏆 Tüm Zamanların En Düşüğü")
                alert_types.append(AlertType.ALL_TIME_LOW)

            if old_price > 0 and eff_price < old_price:
                diff = old_price - eff_price
                pct = (diff / old_price) * 100.0
                if diff >= 100.0 or pct >= 1.5:
                    reasons.append("📉 Fiyat Düştü")
                    alert_types.append(AlertType.PRICE_DROP)

            cooldown_key = f"URL#{watch_id}"
            if reasons and not self.storage.was_recently_alerted(
                cooldown_key, snap.seller, eff_price, self.config.cooldown_hours
            ):
                alerts_to_send.append(
                    {
                        "id": watch_id,
                        "title": snap.title,
                        "seller": snap.seller,
                        "url": snap.best_url,
                        "old_price_tl": old_price,
                        "new_price_tl": eff_price,
                        "list_price_tl": snap.price_tl,
                        "coupon_discount_tl": snap.coupon_discount_tl,
                        "coupon_info": snap.coupon_info,
                        "coupon_code": snap.coupon_code,
                        "six_month_low_tl": six_month_low,
                        "reasons": reasons,
                        "alert_types": alert_types,
                    }
                )

        if alerts_to_send and self.notifier.is_configured:
            msg = self.notifier.build_watched_url_alerts_message(alerts_to_send)
            has_wakeup = any(
                t in (AlertType.SIX_MONTH_LOW, AlertType.COUPON_SIX_MONTH_LOW, AlertType.BACK_IN_STOCK)
                for al in alerts_to_send
                for t in al["alert_types"]
            )
            first = alerts_to_send[0]
            kb = self.notifier.build_watched_url_keyboard(
                [al["id"] for al in alerts_to_send],
                include_alarm_stop=has_wakeup,
                direct_url=first["url"],
            )
            if has_wakeup and self.wakeup is not None:
                voice_txt = (
                    f"Dikkat! Takip ettiğiniz ürün {first['title'][:60]} fırsat fiyatına indi. "
                    f"Yeni fiyat {int(round(first['new_price_tl']))} lira."
                )
                self.wakeup.trigger(
                    summary_html=msg,
                    voice_text=voice_txt,
                    reply_markup=kb,
                    direct_url=first["url"],
                )
                sent = True
            else:
                sent = self.notifier.send_html(msg, reply_markup=kb)

            if sent:
                for al in alerts_to_send:
                    self.storage.record_alert(
                        variant=f"URL#{al['id']}",
                        seller=al["seller"],
                        price_tl=al["new_price_tl"],
                        alert_types=al["alert_types"],
                        url=al["url"],
                    )

    def _check_bestseller_drops(self) -> None:
        if not self.config.bestseller_tracker_enabled:
            return
        try:
            tracker = BestsellerTracker(self.config, self.storage, self.fetcher)
            scanned_count, drops = tracker.scan_and_detect_drops()
            logger.info("[Çok Satanlar] %d ürün tarandı, %d yeni fiyat düşüşü tespit edildi.", scanned_count, len(drops))
            if not drops:
                return

            sent_count = 0
            for d in drops:
                if sent_count >= 3 and not d.is_glitch:
                    break
                it = d.item
                eff = it.effective_price_tl
                cooldown_key = f"BS:{it.product_key}"
                if not d.is_glitch and self.storage.was_recently_alerted(
                    cooldown_key, it.seller, eff, self.config.cooldown_hours
                ):
                    continue

                # PopularDeal şablonuna dönüştürüp anlık ortalama fiyat doğrulaması yap
                from src.popular_deals import PopularDeal, _enrich_deal_insights, fetch_realtime_market_avg_price

                avg_mkt = fetch_realtime_market_avg_price(
                    title=it.title,
                    current_price_tl=eff,
                    fetcher=self.fetcher,
                    fallback_prev_tl=d.prev_price_tl,
                )
                pop = PopularDeal(
                    deal_key=it.product_key,
                    title=it.title,
                    seller=it.seller,
                    price_tl=it.price_tl,
                    previous_or_second_price_tl=d.prev_price_tl,
                    discount_percent=d.drop_percent,
                    direct_url=it.url,
                    image_url=it.image_url,
                    is_official_seller=it.is_official_seller,
                    coupon_discount_tl=it.coupon_discount_tl,
                    coupon_info=it.coupon_info,
                    coupon_code=it.coupon_code,
                    category=it.category,
                    badge_label=f"{d.reason_badge} ({it.category})",
                    is_glitch=d.is_glitch,
                    market_avg_price_tl=avg_mkt,
                )
                _enrich_deal_insights(pop)

                # Özel Telegram sohbetine bildir (Fiyat Hatası ise anında öncelikli uyandırma alarmı!)
                if self.notifier.is_configured:
                    avg_line = f"📊 Piyasa Ortalama Fiyatı: <b>{format_tl(avg_mkt)}</b> <i>(Anlık Kontrol)</i>\n" if avg_mkt else ""
                    priv_msg = (
                        f"🔥 <b>{d.reason_badge}</b>\n"
                        f"📂 <i>{it.category}</i>\n\n"
                        f"📦 <b>{it.title}</b>\n"
                        f"{avg_line}"
                        f"💰 Yeni Fiyat: <b>{format_tl(eff)}</b> "
                        f"(Önceki: <s>{format_tl(d.prev_price_tl or eff)}</s> • <b>-%{d.drop_percent}</b>)\n"
                        f"🏪 Mağaza: <b>{it.seller}</b>\n"
                        f"🔗 <a href=\"{it.url}\">Doğrudan Ürüne Git</a>"
                    )
                    kb_priv = {"inline_keyboard": [[{"text": "🛒 Mağazada Direkt Aç", "url": it.url}]]}
                    if d.is_glitch and self.wakeup is not None:
                        self.wakeup.trigger(
                            summary_html=priv_msg,
                            voice_text=f"Fiyat hatası tespit edildi! {it.title[:50]} yüzde {d.drop_percent} düştü, fiyat {int(round(eff))} lira.",
                            reply_markup=kb_priv,
                            direct_url=it.url,
                        )
                    else:
                        self.notifier.send_html(priv_msg, reply_markup=kb_priv)

                # Kanala yayınla
                if self.config.auto_publish_bestseller_drops and self.channel_publisher.is_enabled:
                    if not self.storage.is_channel_deal_published(pop.deal_key, title=pop.title, price_tl=eff):
                        self.channel_publisher.publish_popular_deal(pop)

                # Büyük düşüşse X'te otomatik paylaş
                self._maybe_auto_tweet_deal(
                    deal_key=pop.deal_key,
                    title=pop.title,
                    price_tl=eff,
                    seller=pop.seller,
                    direct_url=pop.direct_url,
                    discount_pct=pop.discount_percent,
                    coupon_code=pop.coupon_code,
                )

                self.storage.record_alert(cooldown_key, it.seller, eff, [AlertType.PRICE_DROP], it.url)
                sent_count += 1
        except Exception as exc:
            logger.warning("Çok satanlar takip hatası: %s", exc)

    def _check_watched_keywords(self) -> None:
        kws = self.storage.list_watched_keywords(active_only=True)
        if not kws:
            return
        for item in kws:
            kid = int(item["id"])
            q = str(item["query"])
            old_p = float(item.get("last_min_price_tl") or 0)
            low_p = float(item.get("all_time_low_tl") or 0)
            try:
                results = search_product_across_sites(q, self.fetcher, max_results=5)
            except Exception as exc:
                logger.debug("Kelime takip hatası (%s): %s", q, exc)
                continue
            if not results:
                continue
            best = results[0]
            eff = best.effective_price_tl
            self.storage.add_or_update_watched_keyword(q, eff, best.seller, best.url)

            if old_p > 0 and eff < old_p - 100.0 and not self.storage.was_recently_alerted(
                f"KW#{kid}", best.seller, eff, self.config.cooldown_hours
            ):
                msg = (
                    f"🔔 <b>KELİME TAKİP FIRSAT ALARMI: '{q}'</b>\n\n"
                    + self.notifier.build_keyword_search_message(q, results[:3])
                )
                kb = self.notifier.build_keyword_search_keyboard(q, best.url)
                if self.notifier.send_html(msg, reply_markup=kb):
                    self.storage.record_alert(
                        f"KW#{kid}", best.seller, eff, [AlertType.PRICE_DROP], best.url
                    )

    def scan_amazon_depo(self, force: bool = False) -> list:
        """
        Amazon.com.tr Depo ürünlerini tarar, piyasa fiyatının %40-%90 altındaki fırsatları
        veritabanına kaydeder; süper düşüşlerde (%75-%90+) kişisel Telegram'a acil alarm
        ve isteğe bağlı olarak @firsat_takibi kanalına fotoğraflı paylaşım yapar.
        """
        if not force and not self.config.depo_enabled:
            return []
        try:
            from src.amazon_depo import AmazonDepoHunter
            from src.popular_deals import PopularDeal, _enrich_deal_insights, canonicalize_deal_key

            hunter = AmazonDepoHunter(self.fetcher)
            deals = hunter.hunt(
                categories=self.config.depo_categories,
                keywords=self.config.depo_keywords,
                min_discount_percent=self.config.depo_min_discount_percent,
                super_discount_percent=self.config.depo_super_discount_percent,
                min_price_tl=self.config.depo_min_price_tl,
                max_price_tl=self.config.depo_max_price_tl,
                max_items=35,
            )
            if deals:
                self.storage.upsert_depo_deals(deals)
                logger.info(
                    "[Amazon Depo] %d fırsat bulundu (en yüksek indirim: -%%%d).",
                    len(deals),
                    deals[0].discount_percent,
                )

            for d in deals:
                if not d.is_super_steal:
                    continue
                if self.storage.was_depo_asin_notified(d.asin, d.depo_price_tl):
                    continue

                if self.config.depo_auto_notify_admin and self.notifier.is_configured:
                    import html as _html

                    msg = (
                        f"🚨📦 <b>AMAZON DEPO EFSANE DÜŞÜŞ! (-%{d.discount_percent})</b>\n\n"
                        f"📦 <b>{_html.escape(d.title)}</b>\n"
                        f"💰 Depo Fiyatı: <b>{format_tl(d.depo_price_tl)}</b>\n"
                        f"📊 Piyasa Fiyatı: <s>{format_tl(d.market_price_tl)}</s> ({_html.escape(d.price_source)})\n"
                        f"🔥 Kazanç: <b>{format_tl(d.savings_tl)} (-%{d.discount_percent})</b>\n"
                        f"🏷 Durum: <i>{_html.escape(d.condition_label)}</i>\n\n"
                        f'🛒 <a href="{_html.escape(d.url, quote=True)}">HEMEN AMAZON DEPO\'DA AÇ</a>'
                    )
                    self.notifier.send_html(msg)
                    self.storage.mark_depo_asin_notified(d.asin, d.depo_price_tl)

                if self.config.depo_auto_publish_channel and self.channel_publisher.is_enabled:
                    pop = PopularDeal(
                        deal_key=canonicalize_deal_key(d.url),
                        title=f"[Amazon Depo] {d.title}",
                        seller="Amazon.com.tr Depo",
                        price_tl=d.depo_price_tl,
                        previous_or_second_price_tl=d.market_price_tl,
                        discount_percent=d.discount_percent,
                        direct_url=d.url,
                        image_url=d.image_url,
                        category=f"Amazon Depo • {d.category}",
                        is_official_seller=True,
                        badge_label=f"🚨📦 AMAZON DEPO -%{d.discount_percent} FIRSATI!",
                        market_avg_price_tl=d.market_price_tl,
                        is_glitch=(d.discount_percent >= 65),
                    )
                    _enrich_deal_insights(pop)
                    self.channel_publisher.publish_popular_deal(pop)
            return deals
        except Exception as exc:
            logger.debug("Amazon Depo tarama hatası: %s", exc)
            return []

    def _check_community_deals(self) -> None:
        try:
            kws = [str(k["query"]) for k in self.storage.list_watched_keywords(active_only=True)]
            scanner = CommunityDealsScanner(self.fetcher)
            deals = scanner.scan(
                extra_keywords=kws,
                include_all_hot_prices=self.config.auto_publish_community_deals,
            )
            new_deals = [d for d in deals if not self.storage.is_community_deal_seen(d.deal_key)]
            for d in new_deals:
                self.storage.mark_community_deal_seen(d.deal_key, d.title, d.url)
            important = [
                d for d in new_deals
                if d.coupon_code or "s25 ultra" in d.matched_keyword
            ]
            if important and self.notifier.is_configured:
                msg = self.notifier.build_community_deals_message(important[:5])
                self.notifier.send_html(msg, reply_markup=self.notifier.build_main_menu_keyboard())

            if self.config.auto_publish_community_deals and self.channel_publisher.is_enabled:
                from src.deal_stacker import compute_deal_confidence
                from src.popular_deals import PopularDeal, _enrich_deal_insights

                pub_cnt = 0
                for d in new_deals:
                    if pub_cnt >= 3:
                        break
                    if self.storage.is_channel_deal_published(
                        d.deal_key,
                        title=d.title,
                        price_tl=float(d.price_tl) if d.price_tl else None,
                        url=d.url,
                    ):
                        continue

                    # Kupon varsa canlı doğrula; emin olunmayan kuponu ekleme!
                    if d.coupon_code or d.coupon_info:
                        from src.popular_deals import verify_coupon_live

                        c_ok, v_code, v_info = verify_coupon_live(
                            d.coupon_code, d.coupon_info, d.url, self.fetcher
                        )
                        d.coupon_code = v_code
                        d.coupon_info = v_info
                        # Sadece kupon paylaşımıysa ve kupon doğrulanamadıysa tamamen atla
                        if not c_ok and not d.previous_price_tl and not d.is_six_month_low:
                            continue

                    # Sadece fotoğraflı ürün paylaş: görsel eksikse mağaza sayfasından çekmeyi dene
                    if not d.image_url and d.url.startswith("http"):
                        d.image_url, _ = PopularDealsHunter(self.fetcher)._fetch_product_image_and_old_price(
                            d.url, float(d.price_tl or 0.0)
                        )
                    if not (d.image_url and d.image_url.startswith("http")):
                        continue

                    # Görseli çözümlenmiş tam ürün fırsatını PopularDeal formatında (fotoğraflı) yayınla
                    if d.price_tl and (d.previous_price_tl or d.is_six_month_low or d.coupon_code):
                        from src.popular_deals import fetch_realtime_market_avg_price

                        avg_c = fetch_realtime_market_avg_price(
                            title=d.title,
                            current_price_tl=float(d.price_tl),
                            fetcher=self.fetcher,
                            fallback_prev_tl=d.previous_price_tl,
                        )
                        pop = PopularDeal(
                            deal_key=d.deal_key,
                            title=d.title,
                            seller=d.seller or "Mağaza",
                            price_tl=float(d.price_tl),
                            previous_or_second_price_tl=d.previous_price_tl or avg_c,
                            discount_percent=d.discount_percent or (25 if d.is_six_month_low else 15),
                            direct_url=d.url,
                            image_url=d.image_url,
                            coupon_info=d.coupon_info,
                            coupon_code=d.coupon_code,
                            category=d.category,
                            badge_label="SON 6 AYIN EN DÜŞÜK FİYATI!" if d.is_six_month_low else "🔥 ANLIK FIRSAT RADARI!",
                            market_avg_price_tl=avg_c,
                        )
                        _enrich_deal_insights(pop)
                        if self.channel_publisher.publish_popular_deal(pop):
                            pub_cnt += 1
                        continue

                    score, _ = compute_deal_confidence(
                        post_type="community",
                        title=d.title,
                        price_tl=float(d.price_tl or 0.0),
                        coupon_code=d.coupon_code,
                    )
                    if score >= 70:
                        if self.channel_publisher.publish_community_deal(d):
                            pub_cnt += 1
                    elif self._request_channel_approval(
                        deal_key=d.deal_key,
                        post_type="community",
                        title=d.title,
                        seller=d.source,
                        price_tl=float(d.price_tl or 0.0),
                        coupon_code=d.coupon_code,
                        url=d.url,
                    ):
                        pub_cnt += 1
        except Exception as exc:
            logger.debug("Topluluk fırsat kontrol hatası: %s", exc)

    def _request_channel_approval(
        self,
        *,
        deal_key: str,
        post_type: str,
        title: str,
        seller: str,
        price_tl: float,
        coupon_code: str,
        url: str,
    ) -> bool:
        """Düşük güven skorlu (<70) çark/influencer/topluluk gönderilerini önce yönetici onayına düşürür."""
        from src.deal_stacker import compute_deal_confidence

        score, label = compute_deal_confidence(
            post_type=post_type,
            title=title,
            price_tl=price_tl,
            coupon_code=coupon_code,
        )
        aid = self.storage.enqueue_pending_approval(
            deal_key=deal_key,
            post_type=post_type,
            title=title,
            seller=seller,
            price_tl=price_tl,
            coupon_code=coupon_code,
            url=url,
            confidence=score,
        )
        if aid is None:
            return False
        if self.notifier.is_configured:
            c_line = f'\n🎟 Kod: "<b>{coupon_code}</b>"' if coupon_code else ""
            p_line = f"\n💰 Fiyat: <b>{format_tl(price_tl)}</b>" if price_tl > 0 else ""
            msg = (
                f"🛡 <b>KANAL YAYIN ONAYI BEKLİYOR (#{aid})</b>\n"
                f"📊 Güven Skoru: <b>{score}/100</b> — <i>{label}</i>\n\n"
                f"📦 <b>{title}</b>{p_line}{c_line}\n"
                f"🏪 Kaynak: <b>{seller}</b>\n"
                f"🔗 <a href=\"{url}\">Kaynağı Kontrol Et</a>"
            )
            kb = {
                "inline_keyboard": [
                    [
                        {"text": "✅ Kanala Yayınla", "callback_data": f"approve_post:{aid}"},
                        {"text": "❌ Reddet", "callback_data": f"reject_post:{aid}"},
                    ]
                ]
            }
            self.notifier.send_html(msg, reply_markup=kb)
        return True

    def publish_approved_item(self, row: dict) -> bool:
        """Yöneticinin onayladığı bekleyen gönderiyi @firsat_takibi kanalına yayınlar."""
        from types import SimpleNamespace

        ptype = str(row.get("post_type") or "")
        if ptype == "gamified":
            item = SimpleNamespace(
                title=row["title"],
                source_name=row["seller"] or "Kampanya",
                benefit_summary="Onaylı Kampanya",
                coupon_code=row["coupon_code"],
                url=row["url"],
            )
            return self.channel_publisher.publish_gamified_campaign(item)
        if ptype == "influencer":
            item = SimpleNamespace(
                title=row["title"],
                coupon_code=row["coupon_code"],
                url=row["url"],
            )
            return self.channel_publisher.publish_influencer_code(item)
        img_u = ""
        if str(row.get("url") or "").startswith("http"):
            img_u, _ = PopularDealsHunter(self.fetcher)._fetch_product_image_and_old_price(
                str(row["url"]), float(row.get("price_tl") or 0.0)
            )
        item = SimpleNamespace(
            deal_key=row["deal_key"],
            title=row["title"],
            price_tl=float(row.get("price_tl") or 0.0) or None,
            coupon_code=row["coupon_code"],
            url=row["url"],
            image_url=img_u,
        )
        return self.channel_publisher.publish_community_deal(item)

    def _check_gamified_campaigns(self) -> None:
        """Çark çevirme, tıkla kazan ve kazı kazan kampanyalarını tarayıp yönetici onayına sunar."""
        if not self.channel_publisher.is_enabled:
            return
        try:
            from src.market_intel import MarketIntelligenceHunter

            items = MarketIntelligenceHunter(self.fetcher).scan_gamified_campaigns(limit=6)
            sent_cnt = 0
            for it in items:
                if sent_cnt >= 1:
                    break
                key = f"GAME:{it.url.split('?')[0]}"
                if self.storage.is_channel_deal_published(key, title=it.title):
                    continue
                if self._request_channel_approval(
                    deal_key=key,
                    post_type="gamified",
                    title=it.title,
                    seller=it.source_name,
                    price_tl=0.0,
                    coupon_code=it.coupon_code,
                    url=it.url,
                ):
                    sent_cnt += 1
        except Exception as exc:
            logger.debug("Çark/tıkla-kazan tarama hatası: %s", exc)

    def _check_influencer_codes(self) -> None:
        """Sosyal medyadan/webden #reklam #işbirliği influencer indirim kodlarını tarar ve yönetici onayına sunar."""
        if not self.channel_publisher.is_enabled:
            return
        try:
            from src.market_intel import MarketIntelligenceHunter
            from src.popular_deals import verify_coupon_live

            items = MarketIntelligenceHunter(self.fetcher).scan_influencer_hashtag_codes(limit=5)
            sent_cnt = 0
            for it in items:
                if sent_cnt >= 1:
                    break
                c_ok, v_code, _ = verify_coupon_live(it.coupon_code, "", it.url, self.fetcher)
                if not c_ok or not v_code:
                    continue
                it.coupon_code = v_code
                key = f"INF:{it.coupon_code}"
                if self.storage.is_channel_deal_published(key, title=it.title):
                    continue
                if self._request_channel_approval(
                    deal_key=key,
                    post_type="influencer",
                    title=it.title,
                    seller=it.source_name,
                    price_tl=0.0,
                    coupon_code=it.coupon_code,
                    url=it.url,
                ):
                    sent_cnt += 1
        except Exception as exc:
            logger.debug("Influencer kod tarama hatası: %s", exc)

    def _check_daily_habit_report(self) -> None:
        """Günde 1 kez (21:00 sonrası) alışveriş alışkanlıklarını analiz edip gelişim önerileri sunar."""
        try:
            now = datetime.now()
            if now.hour < 21:
                return
            report_key = f"HABIT_REPORT:{now.strftime('%Y-%m-%d')}"
            if self.storage.has_daily_report_sent(report_key):
                return
            from src.habit_analyzer import analyze_shopping_habits, apply_habit_recommendations, format_habit_report_html

            rep = analyze_shopping_habits(self.storage, self.config)
            added = apply_habit_recommendations(self.config, rep)
            msg = format_habit_report_html(rep)
            if added:
                msg += f"\n\n🤖 <b>Otomatik Gelişim:</b> Öğrenilen yoğun saatler takvime eklendi: {', '.join(added)}"
            if self.notifier.is_configured and self.notifier.send_html(msg):
                self.storage.mark_daily_report_sent(report_key)
        except Exception as exc:
            logger.debug("Alışkanlık raporu hatası: %s", exc)

    def _check_daily_smoke_test(self, outcomes: list[ScrapeOutcome] | None = None) -> None:
        """Günde 1 kez tüm aktif kaynaklardan kaç ürün geldiğini raporlar; seçici bozulmasını anında bildirir."""
        try:
            now = datetime.now()
            report_key = f"SMOKE_TEST:{now.strftime('%Y-%m-%d')}"
            if self.storage.has_daily_report_sent(report_key):
                return
            report = self.run_live_smoke_test(existing_outcomes=outcomes, send_telegram=True)
            if report.get("sent") or not self.notifier.is_configured:
                self.storage.mark_daily_report_sent(report_key)
        except Exception as exc:
            logger.debug("Günlük duman testi hatası: %s", exc)

    def run_live_smoke_test(
        self,
        existing_outcomes: list[ScrapeOutcome] | None = None,
        send_telegram: bool = True,
    ) -> dict:
        """Her kaynağı canlı çekip 'kaç ürün geldi / seçici durumu' duman testi raporunu üretir."""
        outcomes = list(existing_outcomes or [])
        if not outcomes:
            scrapers = build_enabled_scrapers(self.fetcher, self.config)
            with ThreadPoolExecutor(max_workers=self.config.max_workers) as pool:
                futs = [pool.submit(self._run_scraper, s) for s in scrapers]
                for f in as_completed(futs):
                    outcomes.append(f.result())
        outcomes.sort(key=lambda o: o.source)

        ok_cnt = 0
        warn_zero: list[str] = []
        failed_list: list[str] = []
        lines = ["🧪 <b>GÜNLÜK CANLI KAYNAK &amp; SEÇİCİ DUMAN TESTİ</b>\n"]
        for o in outcomes:
            cnt = len(o.offers)
            if o.error:
                failed_list.append(o.source)
                lines.append(f"❌ <b>{o.source}</b>: HATA ({o.elapsed_seconds:.1f}s) — <i>{o.error[:70]}</i>")
            elif cnt == 0:
                warn_zero.append(o.source)
                lines.append(f"⚠️ <b>{o.source}</b>: <b>0 ürün</b> ({o.elapsed_seconds:.1f}s) — <i>Seçici/Stok kontrol edilmeli</i>")
            else:
                ok_cnt += 1
                lines.append(f"✅ <b>{o.source}</b>: <b>{cnt} ürün</b> geldi ({o.elapsed_seconds:.1f}s)")

        lines.append("")
        lines.append(
            f"📊 <b>Özet:</b> {ok_cnt} kaynak sağlıklı • {len(warn_zero)} kaynak 0 ürün • {len(failed_list)} kaynak hata"
        )
        msg_html = "\n".join(lines)
        sent = False
        if send_telegram and self.notifier.is_configured:
            sent = self.notifier.send_html(msg_html)
        return {
            "ok": True,
            "healthy": ok_cnt,
            "zero_sources": warn_zero,
            "failed_sources": failed_list,
            "report_html": msg_html,
            "sent": sent,
        }

    def publish_next_popular_deals(self, max_posts: int = 2) -> int:
        """
        Son 6 ayın en düşük fiyatına inen genel popüler ürünleri tarar
        ve henüz kanalda paylaşılmamış olanlardan en fazla `max_posts` adedini @firsat_takibi kanalına yayınlar.
        """
        if not self.channel_publisher.is_enabled:
            return 0
        try:
            hunter = PopularDealsHunter(self.fetcher)
            deals = hunter.hunt(
                min_discount_percent=self.config.min_popular_discount_percent,
                min_savings_tl=self.config.min_popular_savings_tl,
                max_items=max(20, max_posts * 6),
                storage=self.storage,
            )
            from src.popular_deals import verify_and_attach_realtime_avg

            published = 0
            seller_counts: dict[str, int] = {}
            for deal in deals:
                if published >= max_posts:
                    break
                if self.storage.is_channel_deal_published(
                    deal.deal_key, title=deal.title, price_tl=deal.effective_price_tl
                ):
                    continue
                if seller_counts.get(deal.seller, 0) >= 2:
                    continue
                # Yayınlamadan hemen önce anlık piyasa ortalama fiyatını kontrol et ve sahte/birim fiyat hatalarını ele
                if not verify_and_attach_realtime_avg(
                    deal, self.fetcher, min_discount_percent=self.config.min_popular_discount_percent
                ):
                    continue
                if not deal.image_url and deal.direct_url.startswith("http"):
                    deal.image_url, _ = hunter._fetch_product_image_and_old_price(
                        deal.direct_url, deal.effective_price_tl
                    )
                if not (deal.image_url and deal.image_url.startswith("http")):
                    continue
                if self.channel_publisher.publish_popular_deal(deal):
                    published += 1
                    seller_counts[deal.seller] = seller_counts.get(deal.seller, 0) + 1
                    logger.info("[Kanal] Popüler fırsat yayınlandı: %s (%s TL)", deal.title[:50], deal.effective_price_tl)
                    self._maybe_auto_tweet_deal(
                        deal_key=deal.deal_key,
                        title=deal.title,
                        price_tl=deal.effective_price_tl,
                        seller=deal.seller,
                        direct_url=deal.direct_url,
                        discount_pct=deal.discount_percent,
                        coupon_code=deal.coupon_code,
                    )
            return published
        except Exception as exc:
            logger.warning("Popüler fırsat kanal yayını hatası: %s", exc)
            return 0

    def _maybe_auto_tweet_deal(
        self,
        *,
        deal_key: str,
        title: str,
        price_tl: float,
        seller: str,
        direct_url: str,
        discount_pct: int = 0,
        coupon_code: str = "",
        force_qualify: bool = False,
    ) -> bool:
        if not (self.config.x_auto_publish_deals and self.x_publisher.is_configured):
            return False
        if not force_qualify and discount_pct < self.config.x_min_discount_percent:
            return False
        if self.storage.count_x_tweets_today() >= self.config.x_max_tweets_per_day:
            return False
        if self.storage.is_x_deal_posted(deal_key):
            return False

        last_t = self.storage.get_last_x_tweet_time()
        if last_t is not None:
            elapsed_min = (datetime.now() - last_t).total_seconds() / 60.0
            if elapsed_min < self.config.x_min_interval_minutes:
                return False

        ok_x, x_res = self.x_publisher.post_deal_tweet(
            title=title,
            price_tl=price_tl,
            seller=seller,
            direct_url=direct_url,
            discount_pct=discount_pct,
            coupon_code=coupon_code,
        )
        if ok_x:
            self.storage.mark_x_deal_posted(deal_key, x_res)
            logger.info("[X] En iyi fırsat otomatik tweetlendi: %s", x_res)
            return True
        return False


def _group_top_offers(
    offers: list[PriceOffer], limit: int, only_official: bool = False
) -> dict[str, list[PriceOffer]]:
    grouped: dict[str, list[PriceOffer]] = {}
    for off in sorted(offers, key=lambda o: o.effective_price_tl):
        if only_official and not off.is_official_seller:
            continue
        bucket = grouped.setdefault(off.variant, [])
        if len(bucket) < limit:
            bucket.append(off)
    return grouped

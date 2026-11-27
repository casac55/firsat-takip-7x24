from __future__ import annotations

import html
import logging
from urllib.parse import quote

import requests

from src.affiliate import convert_to_affiliate_url
from src.config import AppConfig
from src.models import DealAlert
from src.notifier import TelegramNotifier, format_coupon_badge, format_tl
from src.popular_deals import PopularDeal
from src.storage import PriceStorage

logger = logging.getLogger(__name__)


class ChannelPublisher:
    """
    @firsat_takibi kanalı için:
    - Kanal profil açıklaması ve sabitlenmiş hoş geldiniz mesajı kurar
    - S25 Ultra ve Genel Popüler (6 Ayın En Düşüğü) fırsatlarını affiliate linkli,
      ürün görselli ve viral paylaşım butonlu olarak kanala yayınlar
    - X (Twitter), Instagram ve WhatsApp grupları için hazır üye çekme metinleri üretir
    """

    def __init__(self, config: AppConfig, storage: PriceStorage, notifier: TelegramNotifier) -> None:
        self.config = config
        self.storage = storage
        self.notifier = notifier

    @property
    def channel_id(self) -> str:
        return self.config.telegram_channel_id

    @property
    def is_enabled(self) -> bool:
        return bool(self.config.channel_enabled and self.channel_id and self.notifier.is_configured)

    def _disclosure_suffix(self) -> str:
        if self.config.always_show_ad_disclosure or (
            self.config.show_disclosure_when_affiliate_active and self.config.has_any_affiliate_tag
        ):
            return " • <i>#reklam #işbirliği</i>"
        return ""

    def _build_hashtags(
        self,
        title: str = "",
        seller: str = "",
        category: str = "",
        coupon_code: str = "",
        is_glitch: bool = False,
        extra_tags: tuple[str, ...] = (),
    ) -> str:
        tags: list[str] = ["#indirim", "#fırsat", "#kampanya"]
        low_s = f"{seller} {title}".lower()
        store_map = {
            "amazon": "#amazon #amazontr",
            "trendyol": "#trendyol",
            "hepsiburada": "#hepsiburada",
            "mediamarkt": "#mediamarkt",
            "teknosa": "#teknosa",
            "n11": "#n11",
            "pazarama": "#pazarama",
            "vatan": "#vatanbilgisayar",
            "idefix": "#idefix",
            "migros": "#migros",
            "gratis": "#gratis",
            "watsons": "#watsons",
            "mavi": "#mavi",
            "koton": "#koton",
            "lc waikiki": "#lcwaikiki",
            "dyson": "#dyson",
            "samsung": "#samsung",
        }
        for k, v in store_map.items():
            if k in low_s:
                for t in v.split():
                    if t not in tags:
                        tags.append(t)

        low_t = f"{title} {category}".lower()
        if any(k in low_t for k in ("s25", "galaxy", "iphone", "telefon", "kulaklık", "tv", "laptop", "bilgisayar", "elektronik")):
            for t in ("#teknoloji", "#elektronik"):
                if t not in tags:
                    tags.append(t)
        if "s25" in low_t:
            tags.append("#galaxys25ultra")
        if any(k in low_t for k in ("ruj", "krem", "deodorant", "nemlendirici", "kozmetik", "cilt", "parfüm", "gratis", "watsons")):
            for t in ("#kozmetik", "#ciltbakımı"):
                if t not in tags:
                    tags.append(t)
        if any(k in low_t for k in ("kadın", "elbise", "ceket", "ayakkabı", "çanta", "giyim", "moda", "spor")):
            for t in ("#moda", "#giyim"):
                if t not in tags:
                    tags.append(t)
        if any(k in low_t for k in ("termos", "blender", "süpürge", "kahve", "mutfak", "ev")):
            for t in ("#evyaşam", "#mutfak"):
                if t not in tags:
                    tags.append(t)
        if coupon_code:
            tags.append("#kuponkodu")
        if is_glitch:
            tags.append("#fiyathatası")
        for t in extra_tags:
            if t not in tags:
                tags.append(t)
        tags.append("#alışveriş")
        return " ".join(tags[:10])

    def _format_stack_html(self, stack_summary: str) -> str:
        out_lines: list[str] = []
        for ln in (stack_summary or "").splitlines():
            if ln.startswith("🎯 Net Maliyet:"):
                # "🎯 Net Maliyet: 3.131 TL (Toplam 4.010 TL • -%56 kazanç!)" -> belirgin büyük vurgu
                rest = ln.replace("🎯 Net Maliyet:", "").strip()
                if "(" in rest:
                    price_part, sav_part = rest.split("(", 1)
                    out_lines.append(
                        f"\n🎯 <b><u>NET MALİYET:</u></b> <b><code>{html.escape(price_part.strip())}</code></b> 🔥 "
                        f"<b>({html.escape(sav_part.strip())}</b>"
                    )
                else:
                    out_lines.append(f"\n🎯 <b><u>NET MALİYET:</u></b> <b><code>{html.escape(rest)}</code></b> 🔥")
            else:
                out_lines.append(html.escape(ln))
        return "\n".join(out_lines)

    def build_viral_channel_keyboard(self, affiliate_url: str, share_title: str, price_str: str) -> dict:
        """
        Kanal gönderisinin altındaki butonlarda çoklu platform (Telegram, WhatsApp, X/Twitter)
        'Bu Kanalı Paylaş' butonlarını sunar; ürün linki ise doğrudan mesaj metninde yer alır.
        """
        full_share_msg = (
            f"🔥 {share_title[:70]} — {price_str}!\n"
            f"Anlık fiyat hataları ve 6 ayın en düşük fiyatları için kanala katıl: {self.config.telegram_channel_url}"
        )
        tg_share_link = (
            f"https://t.me/share/url?url={quote(self.config.telegram_channel_url, safe='')}"
            f"&text={quote(f'🔥 {share_title[:70]} — {price_str}! Anlık fırsatlar için katıl:', safe='')}"
        )
        wa_share_link = f"https://api.whatsapp.com/send?text={quote(full_share_msg, safe='')}"
        x_share_link = (
            f"https://twitter.com/intent/tweet?text={quote(f'🔥 {share_title[:65]} — {price_str}!', safe='')}"
            f"&url={quote(self.config.telegram_channel_url, safe='')}"
        )
        rows: list[list[dict]] = [
            [{"text": "📲 Bu Kanalı Paylaş (Telegram)", "url": tg_share_link}],
            [
                {"text": "🟢 Bu Kanalı Paylaş (WhatsApp)", "url": wa_share_link},
                {"text": "🐦 X'te Paylaş", "url": x_share_link},
            ],
        ]
        return {"inline_keyboard": rows}

    def setup_channel_welcome_and_bio(self) -> tuple[bool, str]:
        """Kanal açıklamasını günceller ve sabitlenmiş tanıtım mesajını yayınlar."""
        if not self.is_enabled:
            return False, "Kanal ayarlı değil."

        bio = (
            "🚨 Türkiye'nin Otomatik Fiyat & İndirim Radarı!\n"
            "📉 Son 6 ayın en düşük fiyatına inen ürünler, kupon kodları, fiyat hataları ve sahte indirim filtreli gerçek fırsatlar anlık paylaşılır."
        )
        try:
            requests.post(
                f"{self.notifier.api_base}/setChatDescription",
                json={"chat_id": self.channel_id, "description": bio},
                timeout=10,
            )
        except Exception as exc:
            logger.debug("Kanal açıklaması güncellenemedi: %s", exc)

        welcome_html = (
            "🚀 <b>FIRSAT RADARI (@firsat_takibi) KANALINA HOŞ GELDİNİZ!</b>\n\n"
            "Bu kanalda yapay zeka destekli fiyat takip botumuz tüm büyük mağazaları "
            "(Amazon.com.tr, Hepsiburada, Trendyol, Teknosa, MediaMarkt, İdefix, Migros, N11, Pazarama) "
            "7/24 tarayarak <b>sadece gerçek indirimleri</b> paylaşır:\n\n"
            "✅ <b>Son 6 Ayın En Düşük Fiyatına</b> inen ürünler\n"
            "✅ <b>Sahte İndirim Filtresi</b> (Önce zam yapıp sonra indirim yapanlar elenir!)\n"
            "✅ Tırnak içinde doğrudan kopyalanabilir <b>\"KUPON KODLARI\"</b>\n"
            "✅ Aracı/karşılaştırma sitesi olmadan <b>doğrudan mağaza linki</b>\n\n"
            "🔔 <b>Önemli:</b> Stok ve fiyat hatası fırsatları dakikalar içinde tükendiği için "
            "kanalın bildirimlerini (<b>Sessizden Çıkar / Unmute</b>) açık tutmayı ve kanalımızı "
            "arkadaşlarınızla paylaşmayı unutmayın!"
        )
        kb = self.build_viral_channel_keyboard(
            affiliate_url="",
            share_title="Türkiye'nin Otomatik İndirim & Fiyat Hatası Kanalı",
            price_str="Ücretsiz Katıl",
        )
        try:
            resp = requests.post(
                f"{self.notifier.api_base}/sendMessage",
                json={
                    "chat_id": self.channel_id,
                    "text": welcome_html,
                    "parse_mode": "HTML",
                    "reply_markup": kb,
                },
                timeout=15,
            ).json()
            if resp.get("ok"):
                msg_id = resp["result"]["message_id"]
                requests.post(
                    f"{self.notifier.api_base}/pinChatMessage",
                    json={"chat_id": self.channel_id, "message_id": msg_id, "disable_notification": False},
                    timeout=10,
                )
                return True, f"Hoş geldiniz mesajı {self.channel_id} kanalına gönderildi ve sabitlendi!"
            return False, f"Mesaj gönderilemedi: {resp}"
        except Exception as exc:
            return False, f"Hata: {exc}"

    def publish_popular_deal(self, deal: PopularDeal) -> bool:
        if not self.is_enabled:
            return False
        if any(bad in (deal.direct_url or "").lower() for bad in ("onu.al", "onual.com", "zxro.com")):
            return False

        aff_url = convert_to_affiliate_url(deal.direct_url, self.config)
        off_badge = " ✅ <i>(Resmi Satıcı)</i>" if deal.is_official_seller else ""
        c_badge = format_coupon_badge(deal.coupon_info, deal.coupon_code)

        lines = [
            f"🔥 <b>{html.escape(deal.badge_label)}</b>",
            "",
            f"📦 <b>{html.escape(deal.title)}</b>",
            "",
        ]
        prev_p = deal.previous_or_second_price_tl
        if (not prev_p or prev_p <= deal.effective_price_tl) and deal.discount_percent > 0:
            prev_p = round(deal.effective_price_tl / max(0.25, 1.0 - deal.discount_percent / 100.0), 2)

        if prev_p and prev_p > deal.effective_price_tl:
            diff = prev_p - deal.effective_price_tl
            calc_pct = max(deal.discount_percent, int(round((diff / prev_p) * 100.0)))
            lines.append(f"💸 Önceki Fiyat: <s>{format_tl(prev_p)}</s>")
            lines.append(f"📉 İndirim Oranı: <b>-%{calc_pct}</b> (<b>{format_tl(diff)}</b> kazanç)")
        elif deal.discount_percent > 0:
            lines.append(f"📉 İndirim Oranı: <b>-%{deal.discount_percent}</b>")
        avg_p = getattr(deal, "market_avg_price_tl", None) or prev_p
        if avg_p and avg_p > 0:
            lines.append(f"📊 Piyasa Ortalama Fiyatı: <b>{format_tl(avg_p)}</b> <i>(Anlık Kontrol)</i>")
        lines.append(f"💰 Şimdiki Fiyat: <b>{format_tl(deal.effective_price_tl)}</b>")

        if c_badge:
            lines.append(f"{c_badge}")
        if getattr(deal, "stack_summary", ""):
            lines.append(f"🧮 <b>Üçlü Fırsat Adımları:</b>\n{self._format_stack_html(deal.stack_summary)}")
        if getattr(deal, "multibuy_summary", ""):
            lines.append(f"🧴 <b>Birim Fiyat:</b> {html.escape(deal.multibuy_summary)}")
        if deal.trade_in_info:
            lines.append(f"🔄 Takas Desteği: <b>{html.escape(deal.trade_in_info)}</b>")
        lines.append(f"🏪 Mağaza: <b>{html.escape(deal.seller)}</b>{off_badge}")
        lines.append("")
        lines.append(
            f'🛒 <b><a href="{html.escape(aff_url, quote=True)}">FIRSAT ÜRÜNÜNE DİREKT GİT (MAĞAZA)</a></b>{self._disclosure_suffix()}'
        )
        tags_line = self._build_hashtags(
            title=deal.title,
            seller=deal.seller,
            category=deal.category,
            coupon_code=deal.coupon_code,
            is_glitch=getattr(deal, "is_glitch", False),
        )
        lines.append(f"<i>{html.escape(tags_line)}</i>")

        caption = "\n".join(lines)
        kb = self.build_viral_channel_keyboard(
            affiliate_url=aff_url,
            share_title=deal.title,
            price_str=format_tl(deal.effective_price_tl),
        )

        # Sadece fotoğraflı ürün paylaş (panelden kapatılmadığı sürece fotoğraf zorunlu)
        has_photo = bool(deal.image_url and deal.image_url.startswith("http"))
        if self.config.only_photo_deals and not has_photo:
            logger.info("Fotoğrafsız ürün kanal paylaşımından atlandı: %s", deal.title[:60])
            return False

        # Yayınlamadan hemen önce son kez tekrar kontrolü yap (farklı kaynaklardan aynı anda gelen kopyaları engeller)
        if self.config.prevent_duplicate_deals and self.storage.is_channel_deal_published(
            deal.deal_key,
            title=deal.title,
            price_tl=deal.effective_price_tl,
            url=deal.direct_url,
            repost_discount_pct=self.config.duplicate_repost_discount_pct,
        ):
            logger.info("Tekrar eden ürün kanal paylaşımından engellendi: %s", deal.title[:60])
            return False

        if has_photo:
            sent = self._send_channel_photo_url(deal.image_url, caption, kb)
            if not sent and not self.config.only_photo_deals:
                sent = self.notifier.send_html(caption, chat_id=self.channel_id, reply_markup=kb)
        else:
            sent = self.notifier.send_html(caption, chat_id=self.channel_id, reply_markup=kb)
        if sent:
            self.storage.mark_channel_deal_published(
                deal.deal_key, deal.title, deal.effective_price_tl, aff_url
            )
        return sent

    def publish_s25_alert(self, alert: DealAlert) -> bool:
        if not self.is_enabled:
            return False
        off = alert.offer
        aff_url = convert_to_affiliate_url(off.url, self.config)
        off_badge = " ✅ <i>(Resmi Satıcı)</i>" if off.is_official_seller else ""
        color_tag = f" ({html.escape(off.color)})" if off.color else ""
        c_badge = format_coupon_badge(off.coupon_info, off.coupon_code)

        lines = [
            "🚨 <b>SAMSUNG GALAXY S25 ULTRA FIRSAT ALARMI!</b>",
            "",
            f"📱 <b>Galaxy S25 Ultra — {html.escape(off.variant)}{color_tag}</b>",
        ]
        ref_prev = alert.previous_min_tl or alert.market_median_tl
        if ref_prev and ref_prev > off.effective_price_tl:
            diff = ref_prev - off.effective_price_tl
            pct = max(1, int(round((diff / ref_prev) * 100.0)))
            lines.append(f"💸 Önceki / Piyasa Fiyatı: <s>{format_tl(ref_prev)}</s>")
            lines.append(f"📉 İndirim Oranı: <b>-%{pct}</b> (<b>{format_tl(diff)}</b> daha ucuz)")
        avg_s25 = alert.market_median_tl or ref_prev
        if avg_s25 and avg_s25 > 0:
            lines.append(f"📊 Piyasa Ortalama Fiyatı: <b>{format_tl(avg_s25)}</b> <i>(Anlık Kontrol)</i>")
        lines.append(f"💰 Şimdiki Fiyat: <b>{format_tl(off.effective_price_tl)}</b>")
        if c_badge:
            lines.append(c_badge)
        if off.trade_in_info:
            lines.append(f"🔄 Takas Kampanyası: <b>{html.escape(off.trade_in_info)}</b>")
        lines.append(f"🏪 Mağaza: <b>{html.escape(off.seller)}</b>{off_badge}")
        lines.append("")
        lines.append(
            f'🛒 <b><a href="{html.escape(aff_url, quote=True)}">FIRSAT ÜRÜNÜNE DİREKT GİT (MAĞAZA)</a></b>{self._disclosure_suffix()}'
        )
        tags_line = self._build_hashtags(
            title=f"Samsung Galaxy S25 Ultra {off.variant}",
            seller=off.seller,
            coupon_code=off.coupon_code,
        )
        lines.append(f"<i>{html.escape(tags_line)}</i>")

        kb = self.build_viral_channel_keyboard(
            affiliate_url=aff_url,
            share_title=f"Samsung Galaxy S25 Ultra {off.variant}",
            price_str=format_tl(off.effective_price_tl),
        )
        return self.notifier.send_html("\n".join(lines), chat_id=self.channel_id, reply_markup=kb)

    def publish_community_deal(self, deal) -> bool:
        """DonanımHaber Sıcak Fırsatlar'dan yakalanan kuponlu/fiyatlı sıcak fırsatı kanala (sadece fotoğraflı olarak) yayınlar."""
        if not self.is_enabled:
            return False
        if any(bad in str(getattr(deal, "url", "") or "").lower() for bad in ("onu.al", "onual.com", "zxro.com")):
            return False
        img_url = str(getattr(deal, "image_url", "") or "").strip()
        if not img_url.startswith("http"):
            logger.info("Fotoğrafsız topluluk ürünü kanal paylaşımından atlandı: %s", str(getattr(deal, "title", ""))[:60])
            return False
        if self.storage.is_channel_deal_published(
            deal.deal_key,
            title=deal.title,
            price_tl=float(deal.price_tl) if getattr(deal, "price_tl", None) else None,
            url=str(getattr(deal, "url", "") or ""),
        ):
            logger.info("Tekrar eden topluluk ürünü kanal paylaşımından engellendi: %s", deal.title[:60])
            return False
        p_txt = f"\n💰 Fiyat: <b>{format_tl(deal.price_tl)}</b>" if getattr(deal, "price_tl", None) else ""
        c_txt = f'\n🎟 Kupon Kodu: "<b><code>{html.escape(deal.coupon_code)}</code></b>"' if getattr(deal, "coupon_code", "") else ""
        text = (
            f"⚡ <b>TOPLULUK SICAK FIRSATI!</b>\n\n"
            f"📦 <b>{html.escape(deal.title)}</b>"
            f"{p_txt}{c_txt}\n\n"
            f'🛒 <b><a href="{html.escape(deal.url, quote=True)}">FIRSAT ÜRÜNÜNE DİREKT GİT (MAĞAZA)</a></b>{self._disclosure_suffix()}\n'
            f"<i>{html.escape(self._build_hashtags(title=deal.title, coupon_code=getattr(deal, 'coupon_code', ''), extra_tags=('#sıcakfırsat',)))}</i>"
        )
        kb = self.build_viral_channel_keyboard(
            affiliate_url=deal.url,
            share_title=deal.title,
            price_str=format_tl(deal.price_tl) if getattr(deal, "price_tl", None) else "Sıcak Fırsat",
        )
        sent = self._send_channel_photo_url(img_url, text, kb)
        if sent:
            self.storage.mark_channel_deal_published(
                deal.deal_key, deal.title, float(deal.price_tl or 0.0), deal.url
            )
        return sent

    def publish_gamified_campaign(self, item) -> bool:
        """Çark çevirme, tıkla kazan, kazı kazan ve tık hızı kupon kampanyalarını teşvik edici dille kanala yayınlar."""
        if not self.is_enabled:
            return False
        aff_url = convert_to_affiliate_url(item.url, self.config)
        c_txt = f'\n🎟 Kod: "<b><code>{html.escape(item.coupon_code)}</code></b>"' if getattr(item, "coupon_code", "") else ""
        text = (
            f"🎡 <b>ÇARK ÇEVİR &amp; TIKLA KAZAN FIRSATI!</b>\n\n"
            f"🎁 <b>{html.escape(item.title)}</b>\n"
            f"🏪 Platform: <b>{html.escape(item.source_name)}</b> • <i>{html.escape(item.benefit_summary)}</i>"
            f"{c_txt}\n\n"
            f"🔥 <i>Günlük kupon ve sürpriz indirim hakları sınırlı! Hemen girip şansını dene, kazandığın kuponu %25+ indirimli ürünlerle birleştirip ekstra kâr et!</i>\n\n"
            f'🎡 <b><a href="{html.escape(aff_url, quote=True)}">HEMEN KATIL &amp; KUPONUNU KAP</a></b>{self._disclosure_suffix()}\n'
            f"<i>{html.escape(self._build_hashtags(title=item.title, seller=item.source_name, coupon_code=getattr(item, 'coupon_code', ''), extra_tags=('#çark', '#tıklakazan')))}</i>"
        )
        kb = self.build_viral_channel_keyboard(
            affiliate_url=aff_url,
            share_title=f"🎡 {item.source_name}: {item.title[:50]}",
            price_str="Ücretsiz Dene & Kazan",
        )
        sent = self.notifier.send_html(text, chat_id=self.channel_id, reply_markup=kb)
        if sent:
            deal_key = f"GAME:{item.url.split('?')[0]}"
            self.storage.mark_channel_deal_published(deal_key, item.title, 0.0, aff_url)
        return sent

    def publish_influencer_code(self, item) -> bool:
        """Sosyal medyadan/webden yakalanan doğrulanmış #reklam/#işbirliği influencer kodunu kanala yayınlar."""
        if not self.is_enabled or not getattr(item, "coupon_code", ""):
            return False
        aff_url = convert_to_affiliate_url(item.url, self.config)
        text = (
            f"🏷 <b>SOSYAL MEDYA / INFLUENCER İNDİRİM KODU!</b>\n\n"
            f"📦 <b>{html.escape(item.title)}</b>\n"
            f'🎟 Kupon Kodu: "<b><code>{html.escape(item.coupon_code)}</code></b>"\n\n'
            f"⚡ <i>Süreli veya kota sınırlı influencer kodları hızlı tükenir, sepette hemen deneyin!</i>\n\n"
            f'🛒 <b><a href="{html.escape(aff_url, quote=True)}">FIRSAT ÜRÜNÜNE DİREKT GİT (MAĞAZA)</a></b>{self._disclosure_suffix()}\n'
            f"<i>{html.escape(self._build_hashtags(title=item.title, coupon_code=item.coupon_code, extra_tags=('#indirimkodu', '#influencerkodu')))}</i>"
        )
        kb = self.build_viral_channel_keyboard(
            affiliate_url=aff_url,
            share_title=f"🏷 Kod: {item.coupon_code}",
            price_str="İndirim Kodu",
        )
        sent = self.notifier.send_html(text, chat_id=self.channel_id, reply_markup=kb)
        if sent:
            deal_key = f"INF:{item.coupon_code}"
            self.storage.mark_channel_deal_published(deal_key, item.title, 0.0, aff_url)
        return sent

    def _send_channel_photo_url(self, image_url: str, caption: str, reply_markup: dict) -> bool:
        try:
            resp = requests.post(
                f"{self.notifier.api_base}/sendPhoto",
                json={
                    "chat_id": self.channel_id,
                    "photo": image_url,
                    "caption": caption[:1020],
                    "parse_mode": "HTML",
                    "reply_markup": reply_markup,
                    "protect_content": False,
                },
                timeout=15,
            )
            return resp.status_code == 200
        except Exception:
            return False

    def build_social_media_promo_pack(self, recent_deals: list[PopularDeal]) -> str:
        """
        X (Twitter), Instagram Reels/Story ve WhatsApp/Telegram gruplarından
        @firsat_takibi kanalına organik üye çekmek için kopyala-yapıştır hazır metinler üretir.
        """
        sample_line = "🔥 Son 6 ayın en düşük fiyatına inen elektronik ve market fırsatları!"
        if recent_deals:
            d = recent_deals[0]
            sample_line = f"🔥 {d.title[:55]} şu an %{d.discount_percent} indirimle {format_tl(d.effective_price_tl)}!"

        ch_url = self.config.telegram_channel_url
        return (
            "📣 <b>ÜYE ÇEKME & SOSYAL MEDYA PAYLAŞIM PAKETİ</b>\n\n"
            "Aşağıdaki hazır metinleri kopyalayıp X (Twitter), Instagram Story/Reels, TikTok ve WhatsApp/Telegram gruplarında paylaşarak kanalınıza hızlıca organik üye çekebilirsiniz:\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🐦 <b>1. X (Twitter) Viral Tweet Taslağı:</b>\n"
            f"<code>{html.escape(sample_line)}\n\n"
            "Mağazaların önce zam yapıp sonra 'indirim' diye sunduğu sahte kampanyaları eleyen, sadece son 6 ayın en düşük fiyatlarını ve kupon kodlarını anlık yakalayan Telegram botumuzu ücretsiz kanala bağladık.\n\n"
            f"Anlık bildirimler için 👉 {ch_url}\n"
            "#indirim #fırsat #kampanya #teknoloji</code>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "📱 <b>2. Instagram Story / TikTok / Reels Açıklaması:</b>\n"
            f"<code>Gece 00:00 kuponlarını ve anlık fiyat hatalarını stok bitmeden yakalayan botumuz artık herkese açık! 🤖🔥\n"
            f"Katılmak için link: {ch_url}</code>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "💬 <b>3. WhatsApp & Telegram Arkadaş/Fırsat Grupları Mesajı:</b>\n"
            f"<code>Arkadaşlar tüm mağazaları (Amazon, Hepsiburada, Trendyol, Teknosa, MediaMarkt) 7/24 tarayıp sadece son 6 ayın en düşük fiyatına inen ürünleri ve kupon kodlarını paylaşan temiz bir bot kanalı kurduk, reklam/spam yok:\n{ch_url}</code>"
        )

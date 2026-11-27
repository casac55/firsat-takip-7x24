from __future__ import annotations

import html
import json
import logging
from datetime import datetime
from typing import Optional

import requests

from src.models import AlertType, DealAlert, PriceOffer

logger = logging.getLogger(__name__)


def format_tl(amount: float) -> str:
    """74999.0 -> '74.999 TL', 74999.5 -> '74.999,50 TL'."""
    if abs(amount - round(amount)) < 0.01:
        formatted = f"{int(round(amount)):,}".replace(",", ".")
        return f"{formatted} TL"
    whole, frac = f"{amount:.2f}".split(".")
    whole_tr = f"{int(whole):,}".replace(",", ".")
    return f"{whole_tr},{frac} TL"


def format_coupon_badge(coupon_info: str, coupon_code: str = "") -> str:
    """Kupon bilgisini ve varsa kupon kodunu tek dokunuşla kopyalanabilir (<code>) biçimlendirir."""
    info = html.escape((coupon_info or "").strip())
    code = html.escape((coupon_code or "").strip())
    if info and code:
        return f'🎟 {info} — Kupon Kodu: "<b><code>{code}</code></b>"'
    if code:
        return f'🎟 Kupon Kodu: "<b><code>{code}</code></b>"'
    if info:
        return f"🎟 {info}"
    return ""


_REASON_LABELS = {
    AlertType.TARGET_PRICE: "🎯 Hedef Fiyat Altı",
    AlertType.SIX_MONTH_LOW: "📉🔥 6 Aylık En Düşüğün Altında",
    AlertType.COUPON_SIX_MONTH_LOW: "🎟🔥 Kuponla 6 Aylık En Düşüğün Altında",
    AlertType.ALL_TIME_LOW: "🏆 Tüm Zamanların En Düşüğü",
    AlertType.PRICE_DROP: "📉 Fiyat Düştü",
    AlertType.BELOW_MARKET: "🔥 Piyasa Ortalaması Altı Fırsat",
    AlertType.BACK_IN_STOCK: "📦🔥 STOĞA GİRDİ!",
}


class TelegramNotifier:
    """Telegram Bot API ile HTML, fotoğraf (grafik), belge (Excel/CSV) ve ses gönderici."""

    def __init__(self, bot_token: str, chat_id: str, timeout: int = 18) -> None:
        self.bot_token = bot_token.strip()
        self.chat_id = str(chat_id).strip()
        self.timeout = timeout

    @property
    def is_configured(self) -> bool:
        return bool(
            self.bot_token
            and self.chat_id
            and "buraya_" not in self.bot_token
            and "buraya_" not in self.chat_id
        )

    @property
    def api_base(self) -> str:
        return f"https://api.telegram.org/bot{self.bot_token}"

    def send_html(
        self,
        text: str,
        chat_id: Optional[str] = None,
        reply_markup: Optional[dict] = None,
        disable_notification: bool = False,
    ) -> bool:
        target_chat = chat_id or self.chat_id
        if not self.bot_token or not target_chat:
            logger.warning("Telegram token veya chat_id tanımlı değil, mesaj gönderilmedi.")
            return False

        chunks = _split_message(text, max_len=3900)
        ok = True
        for idx, chunk in enumerate(chunks):
            payload: dict = {
                "chat_id": target_chat,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "disable_notification": disable_notification,
                "protect_content": False,
            }
            if reply_markup and idx == len(chunks) - 1:
                payload["reply_markup"] = reply_markup
            try:
                resp = requests.post(
                    f"{self.api_base}/sendMessage",
                    json=payload,
                    timeout=self.timeout,
                )
                if resp.status_code != 200:
                    logger.error("Telegram hatası (%s): %s", resp.status_code, resp.text)
                    ok = False
            except Exception as exc:
                logger.error("Telegram isteği başarısız: %s", exc)
                ok = False
        return ok

    def send_photo(
        self,
        photo_bytes: bytes,
        caption: str = "",
        chat_id: Optional[str] = None,
        reply_markup: Optional[dict] = None,
    ) -> bool:
        target_chat = chat_id or self.chat_id
        if not self.bot_token or not target_chat:
            return False
        data: dict = {"chat_id": target_chat, "parse_mode": "HTML"}
        if caption:
            data["caption"] = caption[:1020]
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)
        try:
            resp = requests.post(
                f"{self.api_base}/sendPhoto",
                data=data,
                files={"photo": ("fiyat_grafigi.png", photo_bytes, "image/png")},
                timeout=self.timeout,
            )
            return resp.status_code == 200
        except Exception as exc:
            logger.error("Telegram fotoğraf gönderme hatası: %s", exc)
            return False

    def send_document(
        self,
        doc_bytes: bytes,
        filename: str,
        caption: str = "",
        chat_id: Optional[str] = None,
        reply_markup: Optional[dict] = None,
    ) -> bool:
        target_chat = chat_id or self.chat_id
        if not self.bot_token or not target_chat:
            return False
        data: dict = {"chat_id": target_chat, "parse_mode": "HTML"}
        if caption:
            data["caption"] = caption[:1020]
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)
        try:
            resp = requests.post(
                f"{self.api_base}/sendDocument",
                data=data,
                files={"document": (filename, doc_bytes, "text/csv")},
                timeout=self.timeout,
            )
            return resp.status_code == 200
        except Exception as exc:
            logger.error("Telegram dosya gönderme hatası: %s", exc)
            return False

    def send_audio(
        self,
        audio_bytes: bytes,
        filename: str = "alarm.wav",
        title: str = "Fiyat Alarmı",
        caption: str = "",
        chat_id: Optional[str] = None,
    ) -> bool:
        target_chat = chat_id or self.chat_id
        if not self.bot_token or not target_chat:
            return False
        data: dict = {"chat_id": target_chat, "title": title[:60]}
        if caption:
            data["caption"] = caption[:1000]
        try:
            resp = requests.post(
                f"{self.api_base}/sendAudio",
                data=data,
                files={"audio": (filename, audio_bytes, "audio/wav")},
                timeout=self.timeout,
            )
            return resp.status_code == 200
        except Exception as exc:
            logger.debug("Telegram ses gönderme hatası: %s", exc)
            return False

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        try:
            requests.post(
                f"{self.api_base}/answerCallbackQuery",
                json={"callback_query_id": callback_id, "text": text[:190]},
                timeout=8,
            )
        except Exception:
            pass

    def test_connection(self) -> tuple[bool, str]:
        if not self.is_configured:
            return False, ".env dosyasında TELEGRAM_BOT_TOKEN ve TELEGRAM_CHAT_ID ayarlanmalı."
        try:
            me = requests.get(f"{self.api_base}/getMe", timeout=self.timeout).json()
            if not me.get("ok"):
                return False, f"Bot doğrulanamadı: {me}"
            bot_user = me.get("result", {}).get("username", "bot")
            sent = self.send_html(
                "✅ <b>Fiyat Takip Sistemi</b> bağlantısı başarılı!\n"
                "Aşağıdaki butonlardan tüm işlemleri tek dokunuşla yapabilirsiniz.",
                reply_markup=self.build_main_menu_keyboard(),
            )
            if not sent:
                return False, "Bot token geçerli ancak mesaj gönderilemedi (CHAT_ID'yi kontrol edin)."
            return True, f"Bağlantı başarılı (@{bot_user})."
        except Exception as exc:
            return False, f"Bağlantı hatası: {exc}"

    # --- Klavye / Buton Oluşturucular ---

    def remove_reply_keyboard(self, chat_id: Optional[str] = None, text: str = "⌨️ Sabit alt klavye kaldırıldı.") -> bool:
        """Telegram sohbetinin altındaki sabitlenmiş klavyeyi kaldırır."""
        return self.send_html(
            text,
            chat_id=chat_id,
            reply_markup={"remove_keyboard": True},
            disable_notification=True,
        )

    def build_main_menu_keyboard(self, only_official: bool = False) -> dict:
        """Mesaj içi tıklanabilir ana menü butonları."""
        off_lbl = "🛡 Resmi Satıcı: AÇIK ✅" if only_official else "🛡 Resmi Satıcı: TÜMÜ"
        return {
            "inline_keyboard": [
                [
                    {"text": "📢 Kanala Şimdi Fırsat Yayınla", "callback_data": "cmd:publish_channel"},
                    {"text": "🐦 X'te Paylaş / Durum", "callback_data": "cmd:x_menu"},
                ],
                [
                    {"text": "📣 Üye Çekme Paketi", "callback_data": "cmd:promo_pack"},
                    {"text": "🔎 Tüm Siteleri Şimdi Tara", "callback_data": "cmd:scan"},
                ],
                [
                    {"text": "🔥 Sıcak Fırsatlar (Forum)", "callback_data": "cmd:community"},
                    {"text": "🔍 Kelime Takip Listesi", "callback_data": "cmd:keywords"},
                ],
                [
                    {"text": "📈 Fiyat Geçmişi Grafiği", "callback_data": "cmd:charts"},
                    {"text": "📥 Excel / CSV Rapor İndir", "callback_data": "cmd:export_csv"},
                ],
                [
                    {"text": "🎯 S25 Ultra Hedefleri", "callback_data": "cmd:targets"},
                    {"text": "📋 Takip Edilen Linkler", "callback_data": "cmd:list_urls"},
                ],
                [
                    {"text": "⏸ Takip Durdur / Başlat", "callback_data": "cmd:toggle_menu"},
                    {"text": off_lbl, "callback_data": "toggle_official"},
                ],
                [
                    {"text": "📊 Son Fiyat Özeti", "callback_data": "cmd:summary"},
                    {"text": "⏰ Uyandırma Alarmı Test", "callback_data": "cmd:test_alarm"},
                ],
            ]
        }

    def build_charts_keyboard(self, variants: list[str], watched_items: list[dict]) -> dict:
        rows: list[list[dict]] = [
            [{"text": f"📈 {v} Grafiği", "callback_data": f"chart_var:{v}"} for v in variants]
        ]
        for item in watched_items[:6]:
            wid = item["id"]
            short_title = str(item.get("title", ""))[:26]
            rows.append(
                [{"text": f"📈 #{wid} {short_title}", "callback_data": f"chart_url:{wid}"}]
            )
        rows.append([{"text": "🏠 Ana Menü", "callback_data": "cmd:main_menu"}])
        return {"inline_keyboard": rows}

    def build_targets_keyboard(self, variants: list[str]) -> dict:
        rows: list[list[dict]] = []
        for v in variants:
            rows.append(
                [
                    {"text": f"➖ {v} (-2.500)", "callback_data": f"adj_target:{v}:-2500"},
                    {"text": f"➖ {v} (-1.000)", "callback_data": f"adj_target:{v}:-1000"},
                    {"text": f"➕ {v} (+1.000)", "callback_data": f"adj_target:{v}:1000"},
                ]
            )
        rows.append(
            [
                {"text": "⏸ Takip Durdur / Başlat", "callback_data": "cmd:toggle_menu"},
                {"text": "🏠 Ana Menü", "callback_data": "cmd:main_menu"},
            ]
        )
        return {"inline_keyboard": rows}

    def build_toggle_tracking_keyboard(self, variants: list[str], paused: set[str]) -> dict:
        rows: list[list[dict]] = []
        all_paused = "ALL" in paused
        only_official = "ONLY_OFFICIAL" in paused
        for v in variants:
            is_p = all_paused or (v in paused)
            if is_p:
                rows.append([{"text": f"▶️ {v} Takibini Başlat (Şu an Durduruldu)", "callback_data": f"resume_variant:{v}"}])
            else:
                rows.append([{"text": f"⏸ {v} Takibini Durdur (Şu an Aktif)", "callback_data": f"pause_variant:{v}"}])

        off_lbl = "🛡 Sadece Resmi Satıcılar: AÇIK ✅" if only_official else "🛡 Sadece Resmi Satıcılar: KAPALI"
        rows.append([{"text": off_lbl, "callback_data": "toggle_official"}])
        rows.append(
            [
                {"text": "🛑 Tümünü Durdur", "callback_data": "pause_all_s25"},
                {"text": "▶️ Tümünü Başlat", "callback_data": "resume_all_s25"},
            ]
        )
        rows.append([{"text": "🏠 Ana Menü", "callback_data": "cmd:main_menu"}])
        return {"inline_keyboard": rows}

    def build_deal_keyboard(
        self,
        variants: list[str],
        include_alarm_stop: bool = False,
        direct_url: str = "",
    ) -> dict:
        rows: list[list[dict]] = []
        if direct_url and direct_url.startswith("http"):
            rows.append([{"text": "🛒 Mağazada Direkt Aç (Tek Tık)", "url": direct_url}])
        unique_vars = list(dict.fromkeys(variants))
        chart_btns = [{"text": f"📈 {v} Grafiği", "callback_data": f"chart_var:{v}"} for v in unique_vars[:2]]
        if chart_btns:
            rows.append(chart_btns)
        pause_btns = [
            {"text": f"🚫 {v} Takibini Bırak", "callback_data": f"pause_variant:{v}"}
            for v in unique_vars
        ]
        if pause_btns:
            rows.append(pause_btns[:2])
            if len(pause_btns) > 2:
                rows.append(pause_btns[2:])
        rows.append(
            [
                {"text": "🛑 Tüm S25 Takibini Durdur", "callback_data": "pause_all_s25"},
                {"text": "🏠 Ana Menü", "callback_data": "cmd:main_menu"},
            ]
        )
        return {"inline_keyboard": rows}

    def build_watched_url_keyboard(
        self,
        watch_ids: list[int],
        include_alarm_stop: bool = False,
        direct_url: str = "",
    ) -> dict:
        rows: list[list[dict]] = []
        if direct_url and direct_url.startswith("http"):
            rows.append([{"text": "🛒 Mağazada Direkt Aç (Tek Tık)", "url": direct_url}])
        for wid in dict.fromkeys(watch_ids):
            rows.append(
                [
                    {"text": f"📈 #{wid} Grafik", "callback_data": f"chart_url:{wid}"},
                    {"text": f"🎯 #{wid} (%5 Altı)", "callback_data": f"pct_url_target:{wid}:5"},
                    {"text": f"🎯 #{wid} (-1.000)", "callback_data": f"adj_url_target:{wid}:-1000"},
                    {"text": f"🗑 #{wid} Sil", "callback_data": f"unwatch_url:{wid}"},
                ]
            )
        rows.append(
            [
                {"text": "🔎 Şimdi Tara", "callback_data": "cmd:scan"},
                {"text": "📋 Link Listesi", "callback_data": "cmd:list_urls"},
                {"text": "🏠 Ana Menü", "callback_data": "cmd:main_menu"},
            ]
        )
        return {"inline_keyboard": rows}

    # --- Mesaj Şablonları ---

    def build_deals_message(self, alerts: list[DealAlert]) -> str:
        lines = ["🚨 <b>SAMSUNG GALAXY S25 ULTRA FIRSAT ALARMI!</b>", ""]
        for alert in alerts:
            off = alert.offer
            badges = " | ".join(_REASON_LABELS[r] for r in alert.alert_types)
            c_badge = format_coupon_badge(off.coupon_info, off.coupon_code)
            color_tag = f" • 🎨 {html.escape(off.color)}" if off.color else ""
            official_tag = " ✅ <i>(Resmi Satıcı)</i>" if off.is_official_seller else ""

            if off.coupon_discount_tl > 0 or c_badge:
                price_line = (
                    f"📱 <b>{html.escape(off.variant)}</b>{color_tag} — "
                    f"<b>{format_tl(off.effective_price_tl)}</b> "
                    f"(Liste: <s>{format_tl(off.price_tl)}</s> • {c_badge})"
                )
            else:
                price_line = f"📱 <b>{html.escape(off.variant)}</b>{color_tag} — <b>{format_tl(off.price_tl)}</b>"

            lines.append(price_line)
            lines.append(f"🏷 {badges}")
            lines.append(f"🏪 Mağaza: <b>{html.escape(off.seller)}</b>{official_tag}")
            if off.coupon_code:
                lines.append(f'🎟 Kupon Kodu: "<b>{html.escape(off.coupon_code)}</b>"')
            if off.trade_in_info:
                lines.append(f"🔄 Takas Kampanyası: <b>{html.escape(off.trade_in_info)}</b>")
            if alert.fake_discount_warning:
                lines.append(f"⚠️ <b>Sahte İndirim Uyarısı:</b> {html.escape(alert.fake_discount_warning)}")
            lines.append(f"📝 {html.escape(off.title[:110])}")

            details: list[str] = []
            if alert.six_month_low_tl:
                details.append(f"6 aylık taban: {format_tl(alert.six_month_low_tl)}")
            if alert.previous_min_tl and alert.drop_tl:
                details.append(
                    f"Önceki en düşük: {format_tl(alert.previous_min_tl)} (-{format_tl(alert.drop_tl)} / %{alert.drop_percent:.1f})"
                )
            if alert.market_median_tl and alert.below_market_percent:
                details.append(
                    f"Piyasa medyanı: {format_tl(alert.market_median_tl)} (%{alert.below_market_percent:.1f} daha ucuz)"
                )
            if alert.target_price_tl and AlertType.TARGET_PRICE in alert.alert_types:
                details.append(f"Hedefiniz: {format_tl(alert.target_price_tl)}")
            if details:
                lines.append("ℹ️ " + " • ".join(details))

            lines.append(f"🔗 <a href=\"{html.escape(off.url, quote=True)}\">Doğrudan Ürüne Git</a>")
            lines.append("")

        lines.append(f"🕒 {datetime.now().strftime('%d.%m.%Y %H:%M')}")
        return "\n".join(lines)

    def build_summary_message(
        self,
        grouped_offers: dict[str, list[PriceOffer]],
        title: str = "📊 <b>GALAXY S25 ULTRA GÜNCEL EN UCUZ FİYATLAR</b>",
        sources_ok: Optional[int] = None,
        sources_total: Optional[int] = None,
        paused_variants: Optional[set[str]] = None,
    ) -> str:
        lines = [title]
        paused = paused_variants or set()
        if "ONLY_OFFICIAL" in paused:
            lines.append("🛡 <i>Filtre: Sadece Resmi / Güvenilir Satıcılar Gösteriliyor</i>")
        lines.append("")

        if "ALL" in paused:
            lines.append("⏸ <i>Galaxy S25 Ultra takibi şu an durdurulmuş durumda. Aşağıdaki butondan yeniden başlatabilirsiniz.</i>")
            return "\n".join(lines)

        if not grouped_offers:
            lines.append("⚠️ Seçili filtreye uygun geçerli fiyat teklifi bulunamadı.")
            return "\n".join(lines)

        for variant in ("256 GB", "512 GB", "1 TB"):
            if variant in paused:
                lines.append(f"⏸ <b>{variant}</b> — <i>Takip durduruldu</i>")
                lines.append("")
                continue
            offers = grouped_offers.get(variant, [])
            if not offers:
                continue
            lines.append(f"📱 <b>{variant}</b>")
            for idx, off in enumerate(offers, start=1):
                prefix = "🥇" if idx == 1 else ("🥈" if idx == 2 else ("🥉" if idx == 3 else f"{idx}."))
                c_badge = format_coupon_badge(off.coupon_info, off.coupon_code)
                coupon_tag = f" ({c_badge})" if (off.coupon_discount_tl > 0 or off.coupon_code) else ""
                off_badge = " ✅" if off.is_official_seller else ""
                color_badge = f" [{html.escape(off.color)}]" if off.color else ""
                trade_badge = f" (🔄 {html.escape(off.trade_in_info)})" if off.trade_in_info else ""
                lines.append(
                    f"  {prefix} <b>{format_tl(off.effective_price_tl)}</b>{coupon_tag}{trade_badge} — "
                    f"<a href=\"{html.escape(off.url, quote=True)}\">{html.escape(off.seller)}</a>{off_badge}{color_badge}"
                )
            lines.append("")

        meta = f"🕒 {datetime.now().strftime('%d.%m.%Y %H:%M')}"
        if sources_ok is not None and sources_total is not None:
            meta += f" • Kaynak: {sources_ok}/{sources_total} aktif"
        lines.append(meta)
        return "\n".join(lines)

    def build_watched_url_alerts_message(self, alerts: list[dict]) -> str:
        lines = ["🔔 <b>TAKİPLİ ÜRÜN LİNKİ FIRSAT / STOK ALARMI!</b>", ""]
        for item in alerts:
            badges = " | ".join(item["reasons"])
            c_badge = format_coupon_badge(item.get("coupon_info", ""), item.get("coupon_code", ""))
            lines.append(f"📦 <b>#{item['id']} — {html.escape(item['title'][:100])}</b>")
            if item.get("coupon_discount_tl", 0) > 0 or c_badge:
                lines.append(
                    f"💰 Kuponlu Net Fiyat: <b>{format_tl(item['new_price_tl'])}</b> "
                    f"(Liste: <s>{format_tl(item['list_price_tl'])}</s> • {c_badge})"
                )
            else:
                lines.append(f"💰 Yeni Fiyat: <b>{format_tl(item['new_price_tl'])}</b>")
            if item.get("coupon_code"):
                lines.append(f'🎟 Kupon Kodu: "<b>{html.escape(str(item["coupon_code"]))}</b>"')
            lines.append(f"🏷 {badges}")
            if item.get("six_month_low_tl"):
                lines.append(f"📊 Son 6 aylık en düşük: {format_tl(item['six_month_low_tl'])}")
            if item.get("old_price_tl") and item["old_price_tl"] > 0 and item["new_price_tl"] < item["old_price_tl"]:
                diff = item["old_price_tl"] - item["new_price_tl"]
                pct = (diff / item["old_price_tl"]) * 100.0
                lines.append(
                    f"📉 Önceki: {format_tl(item['old_price_tl'])} (-{format_tl(diff)} / %{pct:.1f})"
                )
            lines.append(f"🏪 Mağaza: {html.escape(item['seller'])}")
            lines.append(f"🔗 <a href=\"{html.escape(item['url'], quote=True)}\">Doğrudan Ürüne Git</a>")
            lines.append("")
        lines.append(f"🕒 {datetime.now().strftime('%d.%m.%Y %H:%M')}")
        return "\n".join(lines)

    def build_watched_list_message(self, items: list[dict]) -> str:
        if not items:
            return (
                "📭 <b>Henüz takip edilen özel ürün linkiniz yok.</b>\n\n"
                "Herhangi bir e-ticaret ürün linkini doğrudan sohbete yapıştırarak (ör. <code>https://... 68000</code>) takibe alabilirsiniz!"
            )
        lines = ["📋 <b>TAKİP EDİLEN ÜRÜN LİSTESİ</b>", ""]
        for item in items:
            target = (
                format_tl(float(item["target_price_tl"]))
                if item.get("target_price_tl") is not None
                else "Yok"
            )
            in_stock = bool(item.get("in_stock", 1))
            last_p = float(item["last_price_tl"])
            min_p = float(item["min_price_tl"])
            price_txt = format_tl(last_p) if (in_stock and last_p > 0) else "📦 Stok Bekleniyor"
            min_txt = format_tl(min_p) if min_p > 0 else "-"
            lines.append(f"<b>#{item['id']}</b> — {html.escape(str(item['title'])[:90])}")
            lines.append(
                f"  💰 Durum: <b>{price_txt}</b> | En Düşük: {min_txt} | Hedef: <b>{target}</b>"
            )
            lines.append(f"  🔗 <a href=\"{html.escape(str(item['url']), quote=True)}\">{html.escape(str(item['seller']))}</a>")
            lines.append("")
        lines.append("Aşağıdaki butonlardan grafik görebilir, hedef fiyatı güncelleyebilir veya takibi bırakabilirsiniz:")
        return "\n".join(lines)

    def build_community_deals_message(self, deals: list) -> str:
        if not deals:
            return "🔥 <b>Şu an S25 Ultra / Samsung / Kupon ile eşleşen yeni forum sıcak fırsatı bulunamadı.</b>"
        lines = ["🔥 <b>TOPLULUK & FORUM SICAK FIRSATLARI</b>", ""]
        for idx, d in enumerate(deals[:8], 1):
            p_txt = f" — <b>{format_tl(d.price_tl)}</b>" if getattr(d, "price_tl", None) else ""
            c_txt = f' • 🎟 Kupon Kodu: "<b>{html.escape(d.coupon_code)}</b>"' if getattr(d, "coupon_code", "") else ""
            lines.append(
                f"{idx}. <a href=\"{html.escape(d.url, quote=True)}\">{html.escape(d.title[:110])}</a>{p_txt}{c_txt}"
            )
        lines.append(f"\n🕒 {datetime.now().strftime('%d.%m.%Y %H:%M')}")
        return "\n".join(lines)

    def build_keyword_search_message(self, query: str, offers: list) -> str:
        if not offers:
            return f"⚠️ <b>'{html.escape(query)}'</b> için mağazalarda uygun teklif bulunamadı."
        lines = [f"🔍 <b>'{html.escape(query)}' — En Ucuz Mağaza Fiyatları</b>", ""]
        for idx, off in enumerate(offers, 1):
            prefix = "🥇" if idx == 1 else ("🥈" if idx == 2 else ("🥉" if idx == 3 else f"{idx}."))
            c_badge = format_coupon_badge(off.coupon_info, off.coupon_code)
            c_str = f" ({c_badge})" if c_badge else ""
            t_str = f" (🔄 {html.escape(off.trade_in_info)})" if off.trade_in_info else ""
            off_b = " ✅" if off.is_official_seller else ""
            lines.append(
                f"  {prefix} <b>{format_tl(off.effective_price_tl)}</b>{c_str}{t_str} — "
                f"<a href=\"{html.escape(off.url, quote=True)}\">{html.escape(off.seller)}</a>{off_b}"
            )
            lines.append(f"     <i>{html.escape(off.title[:85])}</i>")
        lines.append("\nBu ürünü sürekli takip listesine eklemek için aşağıdaki butona dokunun:")
        return "\n".join(lines)

    def build_keyword_search_keyboard(self, query: str, best_url: str = "") -> dict:
        rows: list[list[dict]] = []
        if best_url and best_url.startswith("http"):
            rows.append([{"text": "🛒 En Ucuz Mağazada Direkt Aç", "url": best_url}])
        safe_q = query[:38]
        rows.append([{"text": f"➕ '{safe_q}' Sürekli Takibe Al", "callback_data": f"watch_kw:{safe_q}"}])
        rows.append(
            [
                {"text": "🔍 Takip Edilen Aramalar", "callback_data": "cmd:keywords"},
                {"text": "🏠 Ana Menü", "callback_data": "cmd:main_menu"},
            ]
        )
        return {"inline_keyboard": rows}

    def build_watched_keywords_message(self, items: list[dict]) -> tuple[str, dict]:
        if not items:
            txt = (
                "🔍 <b>Henüz takip edilen kelime aramanız yok.</b>\n\n"
                "Sohbete doğrudan istediğiniz ürünün adını yazarak (ör. <code>AirPods Pro 2</code>, <code>PS5 Pro</code>, <code>Dyson V15</code>) tüm mağazalarda aratabilir ve tek butonla sürekli takibe alabilirsiniz!"
            )
            return txt, self.build_main_menu_keyboard()

        lines = ["🔍 <b>TAKİP EDİLEN KELİME ARAMALARI</b>", ""]
        rows: list[list[dict]] = []
        for item in items:
            kid = int(item["id"])
            q = str(item["query"])
            last_p = float(item.get("last_min_price_tl") or 0)
            low_p = float(item.get("all_time_low_tl") or 0)
            seller = str(item.get("last_seller") or "-")
            url = str(item.get("last_url") or "")
            p_str = format_tl(last_p) if last_p > 0 else "-"
            l_str = format_tl(low_p) if low_p > 0 else "-"
            link_part = f"<a href=\"{html.escape(url, quote=True)}\">{html.escape(seller)}</a>" if url else html.escape(seller)
            lines.append(f"<b>#{kid} — {html.escape(q)}</b>")
            lines.append(f"  💰 En Ucuz: <b>{p_str}</b> ({link_part}) | Dip: {l_str}")
            lines.append("")
            btn_row = [{"text": f"🔎 #{kid} '{q[:18]}' Ara", "callback_data": f"run_kw:{kid}"}]
            if url.startswith("http"):
                btn_row.append({"text": "🛒 Mağaza", "url": url})
            btn_row.append({"text": f"🗑 #{kid} Sil", "callback_data": f"unwatch_kw:{kid}"})
            rows.append(btn_row)

        rows.append([{"text": "🏠 Ana Menü", "callback_data": "cmd:main_menu"}])
        lines.append("💡 <i>Yeni ürün aramak için adını doğrudan sohbete yazmanız yeterlidir.</i>")
        return "\n".join(lines), {"inline_keyboard": rows}


def _split_message(text: str, max_len: int = 3900) -> list[str]:
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in text.splitlines(keepends=True):
        if current_len + len(line) > max_len and current:
            chunks.append("".join(current))
            current = [line]
            current_len = len(line)
        else:
            current.append(line)
            current_len += len(line)
    if current:
        chunks.append("".join(current))
    return chunks

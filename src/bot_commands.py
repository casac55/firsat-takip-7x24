from __future__ import annotations

import html
import logging
import re
import threading
import time
from datetime import datetime
from typing import Optional

import requests

from src.charts import render_price_chart_png
from src.community_deals import CommunityDealsScanner
from src.config import AppConfig
from src.exporter import generate_csv_report_bytes
from src.filter import detect_variant, parse_price_tl
from src.keyword_search import search_product_across_sites
from src.notifier import TelegramNotifier, format_coupon_badge, format_tl
from src.popular_deals import PopularDealsHunter
from src.runner import PriceTrackerRunner
from src.storage import PriceStorage
from src.url_tracker import fetch_watched_product
from src.wakeup import WakeupAlarmManager, get_dynamic_interval_minutes

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

_BUTTON_TEXT_MAP = {
    "📢 kanala fırsat yayınla": "/kanalyayinla",
    "📣 üye çekme paketi": "/uyecek",
    "🔎 anlık fiyatları tara": "/tara",
    "📊 son fiyat özeti": "/ozet",
    "🔥 sıcak fırsatlar": "/firsatlar",
    "🔍 takip edilen aramalar": "/aramalar",
    "📈 fiyat grafikleri": "/grafik",
    "📥 excel raporu al": "/rapor",
    "🎯 hedef fiyatları yönet": "/hedefler",
    "📋 takip edilen linkler": "/liste",
    "⏸ takip & resmi satıcı": "/takipmenu",
    "⏸ takip durdur / başlat": "/takipmenu",
    "⏰ alarmı test et": "/testalarm",
    "🔕 alarmı sustur": "/sustur",
    "🏠 ana menü": "/yardim",
}


class TelegramCommandListener:
    """Telegram'dan gelen tüm buton tıklamalarını, komutları ve ürün linklerini dinler."""

    def __init__(
        self,
        config: AppConfig,
        storage: PriceStorage,
        notifier: TelegramNotifier,
        runner: PriceTrackerRunner,
        scan_lock: threading.Lock,
        wakeup: Optional[WakeupAlarmManager] = None,
    ) -> None:
        self.config = config
        self.storage = storage
        self.notifier = notifier
        self.runner = runner
        self.scan_lock = scan_lock
        self.wakeup = wakeup
        self._offset = 0
        self._stop_event = threading.Event()
        self._manual_scan_running = False

    def start_background(self) -> threading.Thread:
        thread = threading.Thread(target=self._poll_loop, daemon=True, name="tg-commands")
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop_event.set()

    def _poll_loop(self) -> None:
        logger.info("Telegram komut ve buton dinleyici başlatıldı.")
        while not self._stop_event.is_set():
            try:
                resp = requests.get(
                    f"{self.notifier.api_base}/getUpdates",
                    params={"offset": self._offset, "timeout": 20},
                    timeout=30,
                )
                if resp.status_code != 200:
                    time.sleep(4)
                    continue
                payload = resp.json()
                for upd in payload.get("result", []):
                    self._offset = max(self._offset, int(upd["update_id"]) + 1)
                    try:
                        if "callback_query" in upd:
                            self._handle_callback(upd["callback_query"])
                        else:
                            self._handle_update(upd)
                    except Exception as inner_exc:
                        logger.error("Telegram mesaj işleme hatası: %s", inner_exc)
            except Exception as exc:
                logger.debug("Telegram polling hatası: %s", exc)
                time.sleep(4)

    def _main_kb(self) -> dict:
        return self.notifier.build_main_menu_keyboard(
            only_official=self.storage.is_only_official_sellers()
        )

    def _handle_callback(self, cb: dict) -> None:
        cb_id = str(cb.get("id", ""))
        data = str(cb.get("data", ""))
        msg = cb.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        if self.config.telegram_chat_id and chat_id != self.config.telegram_chat_id:
            return

        self.notifier.answer_callback(cb_id)

        if data == "stop_alarm":
            if self.wakeup:
                self.wakeup.stop_alarm()
            self.notifier.send_html(
                "🔕 <b>Uyandırma alarmı susturuldu.</b>",
                chat_id=chat_id,
                reply_markup=self._main_kb(),
            )
        elif data == "cmd:main_menu":
            self._send_help(chat_id)
        elif data == "cmd:publish_channel":
            self._handle_publish_channel(chat_id)
        elif data == "cmd:x_menu":
            self._handle_x_menu(chat_id)
        elif data == "cmd:x_post_now":
            self._handle_x_post_now(chat_id)
        elif data == "cmd:promo_pack":
            self._handle_promo_pack(chat_id)
        elif data == "cmd:summary":
            self._send_summary(chat_id)
        elif data == "cmd:scan":
            self._trigger_async_scan(chat_id)
        elif data == "cmd:community":
            self._send_community_deals(chat_id)
        elif data == "cmd:keywords":
            self._send_watched_keywords(chat_id)
        elif data == "cmd:charts":
            self._send_charts_menu(chat_id)
        elif data == "cmd:export_csv":
            self._send_csv_export(chat_id)
        elif data == "cmd:targets":
            self._send_targets_menu(chat_id)
        elif data == "cmd:list_urls":
            self._send_watched_list(chat_id)
        elif data == "cmd:toggle_menu":
            self._send_toggle_menu(chat_id)
        elif data == "cmd:test_alarm":
            self._trigger_test_alarm(chat_id)
        elif data.startswith("watch_kw:"):
            q = data.split(":", 1)[1].strip()
            self._watch_keyword_query(chat_id, q)
        elif data.startswith("approve_post:"):
            aid = int(data.split(":", 1)[1])
            row = self.storage.resolve_pending_approval(aid, approved=True)
            if row and self.runner.publish_approved_item(row):
                self.notifier.send_html(
                    f"✅ <b>#{aid}</b> onaylandı ve <b>@firsat_takibi</b> kanalına yayınlandı!",
                    chat_id=chat_id,
                    reply_markup=self._main_kb(),
                )
            else:
                self.notifier.send_html(f"ℹ️ <b>#{aid}</b> zaten işlenmiş veya bulunamadı.", chat_id=chat_id)
        elif data.startswith("reject_post:"):
            aid = int(data.split(":", 1)[1])
            row = self.storage.resolve_pending_approval(aid, approved=False)
            self.notifier.send_html(
                f"❌ <b>#{aid}</b> reddedildi ve kanal kuyruğundan çıkarıldı." if row else f"ℹ️ <b>#{aid}</b> zaten işlenmiş.",
                chat_id=chat_id,
                reply_markup=self._main_kb(),
            )
        elif data.startswith("run_kw:"):
            kid = int(data.split(":", 1)[1])
            items = [x for x in self.storage.list_watched_keywords() if int(x["id"]) == kid]
            if items:
                self._handle_keyword_search(str(items[0]["query"]), chat_id)
        elif data.startswith("unwatch_kw:"):
            kid = int(data.split(":", 1)[1])
            self.storage.remove_watched_keyword(kid)
            self._send_watched_keywords(chat_id, note=f"🗑 <b>#{kid}</b> numaralı kelime araması silindi.\n\n")
        elif data == "toggle_official":
            new_state = not self.storage.is_only_official_sellers()
            self.storage.set_only_official_sellers(new_state)
            lbl = "AÇIK (Sadece Resmi/Güvenilir Satıcılar)" if new_state else "KAPALI (Tüm Satıcılar)"
            self._send_toggle_menu(
                chat_id,
                note=f"🛡 <b>Resmi Satıcı Filtresi:</b> <b>{lbl}</b> olarak güncellendi.\n\n",
            )
        elif data.startswith("chart_var:"):
            variant = data.split(":", 1)[1]
            self._send_variant_chart(chat_id, variant)
        elif data.startswith("chart_url:"):
            try:
                wid = int(data.split(":", 1)[1])
            except ValueError:
                return
            self._send_url_chart(chat_id, wid)
        elif data.startswith("adj_target:"):
            _, variant, delta_s = data.split(":", 2)
            cur = self.config.target_prices_tl.get(variant, 70000.0)
            new_val = max(10000.0, cur + float(delta_s))
            self.config.save_target_price(variant, new_val)
            self._send_targets_menu(
                chat_id,
                header_note=f"✅ <b>{html.escape(variant)}</b> hedef fiyatı <b>{format_tl(new_val)}</b> yapıldı.\n\n",
            )
        elif data.startswith("unwatch_url:"):
            try:
                watch_id = int(data.split(":", 1)[1])
            except ValueError:
                return
            if self.storage.remove_watched_url(watch_id):
                self.notifier.send_html(
                    f"🗑 <b>#{watch_id}</b> numaralı ürünün fiyat takibi durduruldu.",
                    chat_id=chat_id,
                    reply_markup=self._main_kb(),
                )
            else:
                self.notifier.send_html(
                    f"ℹ️ <b>#{watch_id}</b> numaralı ürün zaten takipten çıkarılmış.",
                    chat_id=chat_id,
                    reply_markup=self._main_kb(),
                )
        elif data.startswith("adj_url_target:"):
            _, wid_s, delta_s = data.split(":", 2)
            wid = int(wid_s)
            item = self.storage.get_watched_url(wid)
            if not item:
                self.notifier.send_html(f"⚠️ #{wid} numaralı aktif ürün bulunamadı.", chat_id=chat_id)
                return
            base = float(item["target_price_tl"] or item["last_price_tl"] or 10000.0)
            new_target = max(10.0, round(base + float(delta_s), 2))
            self.storage.set_watched_url_target(wid, new_target)
            self._send_watched_list(
                chat_id,
                note=f"✅ <b>#{wid}</b> için yeni hedef fiyat: <b>{format_tl(new_target)}</b>\n\n",
            )
        elif data.startswith("pct_url_target:"):
            _, wid_s, pct_s = data.split(":", 2)
            wid = int(wid_s)
            item = self.storage.get_watched_url(wid)
            if not item:
                self.notifier.send_html(f"⚠️ #{wid} numaralı aktif ürün bulunamadı.", chat_id=chat_id)
                return
            last_p = float(item["last_price_tl"] or 0.0)
            if last_p <= 0:
                self.notifier.send_html("⚠️ Ürünün henüz stok fiyatı olmadığı için yüzde hedef hesaplanamadı.", chat_id=chat_id)
                return
            new_target = max(10.0, round(last_p * (1.0 - float(pct_s) / 100.0)))
            self.storage.set_watched_url_target(wid, new_target)
            self._send_watched_list(
                chat_id,
                note=f"✅ <b>#{wid}</b> için yeni hedef fiyat (%{pct_s} altı): <b>{format_tl(new_target)}</b>\n\n",
            )
        elif data.startswith("pause_variant:"):
            variant = data.split(":", 1)[1]
            self.storage.pause_variant(variant)
            self._send_toggle_menu(
                chat_id,
                note=f"⏸ <b>{html.escape(variant)}</b> takibi durduruldu.\n\n",
            )
        elif data.startswith("resume_variant:"):
            variant = data.split(":", 1)[1]
            self.storage.resume_variant("ALL")
            self.storage.resume_variant(variant)
            self._send_toggle_menu(
                chat_id,
                note=f"▶️ <b>{html.escape(variant)}</b> takibi yeniden başlatıldı.\n\n",
            )
        elif data == "pause_all_s25":
            self.storage.pause_variant("ALL")
            self._send_toggle_menu(
                chat_id,
                note="🛑 <b>Tüm Galaxy S25 Ultra</b> takibi durduruldu.\n\n",
            )
        elif data == "resume_all_s25":
            self.storage.resume_variant("ALL")
            for v in self.config.variants:
                self.storage.resume_variant(v)
            self._send_toggle_menu(
                chat_id,
                note="▶️ <b>Tüm Galaxy S25 Ultra</b> varyantları için takip başlatıldı!\n\n",
            )

    def _handle_update(self, upd: dict) -> None:
        msg = upd.get("message") or upd.get("edited_message")
        if not isinstance(msg, dict):
            return
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        if self.config.telegram_chat_id and chat_id != self.config.telegram_chat_id:
            return

        text = str(msg.get("text", "")).strip()
        if not text:
            return

        mapped = _BUTTON_TEXT_MAP.get(text.lower())
        if mapped:
            text = mapped

        if not text.startswith("/"):
            if _URL_RE.search(text):
                self._handle_add_url(text, chat_id)
            elif len(text) >= 3:
                self._handle_keyword_search(text, chat_id)
            else:
                self._send_help(chat_id)
            return

        cmd = text.split()[0].lower().split("@")[0]
        if cmd in ("/start", "/yardim", "/help", "/menu"):
            self._send_help(chat_id)
        elif cmd in ("/kanalyayinla", "/yayinla"):
            self._handle_publish_channel(chat_id)
        elif cmd in ("/uyecek", "/promo"):
            self._handle_promo_pack(chat_id)
        elif cmd in ("/firsatlar", "/sicak"):
            self._send_community_deals(chat_id)
        elif cmd in ("/aramalar", "/kelimeler"):
            self._send_watched_keywords(chat_id)
        elif cmd in ("/ara",):
            parts = text.split(maxsplit=1)
            if len(parts) > 1:
                self._handle_keyword_search(parts[1], chat_id)
            else:
                self._send_watched_keywords(chat_id)
        elif cmd in ("/sustur", "/stopalarm"):
            if self.wakeup:
                self.wakeup.stop_alarm()
            self.notifier.send_html(
                "🔕 <b>Uyandırma alarmı susturuldu.</b>",
                chat_id=chat_id,
                reply_markup=self._main_kb(),
            )
        elif cmd in ("/ozet", "/son"):
            self._send_summary(chat_id)
        elif cmd in ("/dumantesti", "/kaynaktest", "/smoketest"):
            self.notifier.send_html("🧪 <i>Canlı kaynak ve seçici duman testi çalıştırılıyor...</i>", chat_id=chat_id)
            res = self.runner.run_live_smoke_test(send_telegram=False)
            self.notifier.send_html(res["report_html"], chat_id=chat_id, reply_markup=self._main_kb())
        elif cmd in ("/piyasa", "/banka", "/marka"):
            from src.market_intel import MarketIntelligenceHunter, format_market_intel_html

            self.notifier.send_html("🔮 <i>Banka kampanyaları, resmi marka siteleri ve hashtag sinyalleri taranıyor...</i>", chat_id=chat_id)
            rep = MarketIntelligenceHunter(self.runner.fetcher).collect_all()
            self.notifier.send_html(
                format_market_intel_html(rep),
                chat_id=chat_id,
                reply_markup=self._main_kb(),
            )
        elif cmd in ("/cark", "/tiklakazan"):
            from src.market_intel import MarketIntelligenceHunter

            items = MarketIntelligenceHunter(self.runner.fetcher).scan_gamified_campaigns(limit=8)
            if not items:
                self.notifier.send_html(
                    "🎡 Şu an yeni bir çark / tıkla-kazan kampanyası bulunamadı.",
                    chat_id=chat_id,
                    reply_markup=self._main_kb(),
                )
            else:
                lines = ["🎡 <b>AKTİF ÇARK ÇEVİR &amp; TIKLA KAZAN KAMPANYALARI</b>\n"]
                for it in items:
                    lines.append(
                        f"• <b>{html.escape(it.source_name)}:</b> "
                        f"<a href=\"{html.escape(it.url, quote=True)}\">{html.escape(it.title[:85])}</a>"
                    )
                self.notifier.send_html("\n".join(lines), chat_id=chat_id, reply_markup=self._main_kb())
        elif cmd in ("/influencer", "/kodlar"):
            from src.market_intel import MarketIntelligenceHunter

            items = MarketIntelligenceHunter(self.runner.fetcher).scan_influencer_hashtag_codes(limit=8)
            if not items:
                self.notifier.send_html(
                    "🏷 Şu an doğrulanmış yeni #reklam / influencer kodu bulunamadı.",
                    chat_id=chat_id,
                    reply_markup=self._main_kb(),
                )
            else:
                lines = ["🏷 <b>DOĞRULANMIŞ #REKLAM / INFLUENCER KODLARI</b>\n"]
                for it in items:
                    lines.append(
                        f'• Kod: "<b>{html.escape(it.coupon_code)}</b>" — '
                        f"<a href=\"{html.escape(it.url, quote=True)}\">{html.escape(it.title[:75])}</a>"
                    )
                self.notifier.send_html("\n".join(lines), chat_id=chat_id, reply_markup=self._main_kb())
        elif cmd in ("/oneriler", "/aliskanlik", "/iyilestir"):
            from src.habit_analyzer import analyze_shopping_habits, apply_habit_recommendations, format_habit_report_html

            rep = analyze_shopping_habits(self.storage, self.config)
            extra = ""
            if cmd == "/iyilestir":
                added = apply_habit_recommendations(self.config, rep)
                extra = (
                    f"\n\n✨ <b>Otomatik İyileştirme:</b> Yeni yoğun saatler eklendi: {', '.join(added)}"
                    if added
                    else "\n\n✅ Takvim zaten en verimli saatlere göre optimize edilmiş."
                )
            self.notifier.send_html(
                format_habit_report_html(rep) + extra,
                chat_id=chat_id,
                reply_markup=self._main_kb(),
            )
        elif cmd in ("/durum", "/tara", "/fiyat"):
            self._trigger_async_scan(chat_id)
        elif cmd in ("/grafik", "/chart"):
            self._send_charts_menu(chat_id)
        elif cmd in ("/rapor", "/excel", "/csv"):
            self._send_csv_export(chat_id)
        elif cmd in ("/hedefler",):
            self._send_targets_menu(chat_id)
        elif cmd == "/hedef":
            self._handle_set_target(text, chat_id)
        elif cmd in ("/takipmenu",):
            self._send_toggle_menu(chat_id)
        elif cmd in ("/durdur", "/birak"):
            self._handle_pause(text, chat_id)
        elif cmd in ("/baslat", "/devam"):
            self._handle_resume(text, chat_id)
        elif cmd in ("/ekle", "/takip"):
            self._handle_add_url(text, chat_id)
        elif cmd in ("/liste", "/linkler"):
            self._send_watched_list(chat_id)
        elif cmd == "/linkhedef":
            self._handle_link_target(text, chat_id)
        elif cmd == "/sil":
            self._handle_remove_url(text, chat_id)
        elif cmd == "/testalarm":
            self._trigger_test_alarm(chat_id)
        else:
            self._send_help(chat_id)

    def _send_summary(self, chat_id: str) -> None:
        grouped = self.storage.get_latest_offers_by_variant(self.config.top_offers_per_variant)
        reply = self.notifier.build_summary_message(
            grouped,
            title="📊 <b>SON TARAMA ÖZETİ (KAYITLI VERİ)</b>",
            paused_variants=self.storage.get_paused_variants(),
        )
        self.notifier.send_html(
            reply,
            chat_id=chat_id,
            reply_markup=self._main_kb(),
        )

    def _send_charts_menu(self, chat_id: str) -> None:
        watched = self.storage.list_watched_urls(active_only=True)
        kb = self.notifier.build_charts_keyboard(self.config.variants, watched)
        self.notifier.send_html(
            "📈 <b>Fiyat Geçmişi Grafiği</b>\n\n"
            "Grafiğini görmek istediğiniz S25 Ultra varyantını veya takip ettiğiniz ürünü seçin:",
            chat_id=chat_id,
            reply_markup=kb,
        )

    def _send_variant_chart(self, chat_id: str, variant: str) -> None:
        points = self.storage.get_variant_price_series(variant, days=self.config.six_month_days)
        target = self.config.target_prices_tl.get(variant)
        six_m = self.storage.get_six_month_low(variant, days=self.config.six_month_days)
        png = render_price_chart_png(
            title=f"Samsung Galaxy S25 Ultra ({variant}) Fiyat Geçmişi",
            points=points,
            target_price_tl=target,
            six_month_low_tl=six_m,
        )
        watched = self.storage.list_watched_urls(active_only=True)
        caption = (
            f"📈 <b>Galaxy S25 Ultra ({html.escape(variant)})</b> Fiyat Değişim Grafiği\n"
            f"• Kayıtlı Tarama Noktası: {len(points)}"
        )
        self.notifier.send_photo(
            png,
            caption=caption,
            chat_id=chat_id,
            reply_markup=self.notifier.build_charts_keyboard(self.config.variants, watched),
        )

    def _send_url_chart(self, chat_id: str, watch_id: int) -> None:
        item = self.storage.get_watched_url(watch_id)
        if not item:
            self.notifier.send_html(f"⚠️ #{watch_id} numaralı ürün bulunamadı.", chat_id=chat_id)
            return
        points = self.storage.get_watched_url_price_series(watch_id, days=self.config.six_month_days)
        target = float(item["target_price_tl"]) if item.get("target_price_tl") else None
        six_m = self.storage.get_watched_url_six_month_low(watch_id, days=self.config.six_month_days)
        png = render_price_chart_png(
            title=f"#{watch_id} - {item['title'][:45]}",
            points=points,
            target_price_tl=target,
            six_month_low_tl=six_m,
        )
        watched = self.storage.list_watched_urls(active_only=True)
        self.notifier.send_photo(
            png,
            caption=f"📈 <b>#{watch_id} — {html.escape(str(item['title'])[:80])}</b> Fiyat Grafiği",
            chat_id=chat_id,
            reply_markup=self.notifier.build_charts_keyboard(self.config.variants, watched),
        )

    def _send_csv_export(self, chat_id: str) -> None:
        csv_bytes = generate_csv_report_bytes(self.storage)
        fname = f"s25_ultra_fiyat_raporu_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
        self.notifier.send_document(
            csv_bytes,
            filename=fname,
            caption="📥 <b>Excel Uyumlu Fiyat Geçmişi ve Takip Raporu</b>\nDoğrudan Microsoft Excel ile açabilirsiniz.",
            chat_id=chat_id,
            reply_markup=self._main_kb(),
        )

    def _send_community_deals(self, chat_id: str) -> None:
        self.notifier.send_html("🔥 Forum ve topluluk sıcak fırsatları taranıyor...", chat_id=chat_id)
        kws = [str(k["query"]) for k in self.storage.list_watched_keywords(active_only=True)]
        scanner = CommunityDealsScanner(self.runner.fetcher)
        deals = scanner.scan(extra_keywords=kws)
        for d in deals:
            self.storage.mark_community_deal_seen(d.deal_key, d.title, d.url)
        msg = self.notifier.build_community_deals_message(deals)
        self.notifier.send_html(msg, chat_id=chat_id, reply_markup=self._main_kb())

    def _handle_publish_channel(self, chat_id: str) -> None:
        def _worker() -> None:
            self.notifier.send_html(
                f"📢 Son 6 ayın en düşük fiyatlı popüler fırsatları taranıp <b>{html.escape(self.config.telegram_channel_id)}</b> kanalına yayınlanıyor...",
                chat_id=chat_id,
            )
            count = self.runner.publish_next_popular_deals(max_posts=2)
            if count > 0:
                self.notifier.send_html(
                    f"✅ <b>{count} adet</b> yeni popüler fırsat <b>{html.escape(self.config.telegram_channel_id)}</b> kanalına ürün görseli ve viral paylaşım butonlarıyla yayınlandı!\n"
                    f"👉 {self.config.telegram_channel_url}",
                    chat_id=chat_id,
                    reply_markup=self._main_kb(),
                )
            else:
                self.notifier.send_html(
                    "ℹ️ Şu an için paylaşılmamış yeni popüler fırsat kalmadı (sonraki taramada yeni düşenler otomatik yayınlanacak).",
                    chat_id=chat_id,
                    reply_markup=self._main_kb(),
                )

        threading.Thread(target=_worker, daemon=True, name="channel-pub").start()

    def _handle_promo_pack(self, chat_id: str) -> None:
        try:
            hunter = PopularDealsHunter(self.runner.fetcher)
            deals = hunter.hunt(min_discount_percent=5, max_items=1)
        except Exception:
            deals = []
        msg = self.runner.channel_publisher.build_social_media_promo_pack(deals)
        self.notifier.send_html(msg, chat_id=chat_id, reply_markup=self._main_kb())

    def _handle_x_menu(self, chat_id: str) -> None:
        xp = self.runner.x_publisher
        if not xp.is_configured:
            self.notifier.send_html(
                "🐦 <b>X (Twitter) Hesabı Henüz Bağlı Değil</b>\n\n"
                "Bağlamak için:\n"
                "1) <code>developer.x.com</code> adresinden ücretsiz Developer hesabı açıp bir App oluşturun (<b>App permissions: Read and Write</b>).\n"
                "2) Aldığınız 4 anahtarı (<code>X_API_KEY</code>, <code>X_API_SECRET</code>, <code>X_ACCESS_TOKEN</code>, <code>X_ACCESS_TOKEN_SECRET</code>) <code>.env</code> dosyasına yazın.\n"
                "3) Ardından bu butona basarak hesabınızı doğrulayıp tek tıkla tweet atabilirsiniz!",
                chat_id=chat_id,
                reply_markup=self._main_kb(),
            )
            return

        ok, status_msg = xp.test_connection()
        today_cnt = self.storage.count_x_tweets_today()
        kb = {
            "inline_keyboard": [
                [{"text": "🐦 En İyi Fırsatı Şimdi X'te Paylaş", "callback_data": "cmd:x_post_now"}],
                [{"text": "🏠 Ana Menü", "callback_data": "cmd:main_menu"}],
            ]
        }
        self.notifier.send_html(
            f"🐦 <b>X (Twitter) Bağlantı Durumu</b>\n\n"
            f"• Durum: {'✅ ' if ok else '⚠️ '}{html.escape(status_msg)}\n"
            f"• Bugün atılan otomatik tweet: <b>{today_cnt}/{self.config.x_max_tweets_per_day}</b>",
            chat_id=chat_id,
            reply_markup=kb,
        )

    def _handle_x_post_now(self, chat_id: str) -> None:
        def _worker() -> None:
            xp = self.runner.x_publisher
            if not xp.is_configured:
                self._handle_x_menu(chat_id)
                return
            self.notifier.send_html("🐦 En güncel fırsat bulunup X'te paylaşılıyor...", chat_id=chat_id)
            try:
                hunter = PopularDealsHunter(self.runner.fetcher)
                deals = hunter.hunt(min_discount_percent=5, max_items=3)
            except Exception:
                deals = []
            if not deals:
                self.notifier.send_html("⚠️ Şu an X'te paylaşılacak yeni fırsat bulunamadı.", chat_id=chat_id)
                return
            deal = deals[0]
            ok, res = xp.post_deal_tweet(
                title=deal.title,
                price_tl=deal.effective_price_tl,
                seller=deal.seller,
                direct_url=deal.direct_url,
                discount_pct=deal.discount_percent,
                coupon_code=deal.coupon_code,
            )
            if ok:
                self.storage.mark_x_deal_posted(deal.deal_key, res)
                self.notifier.send_html(
                    f"✅ <b>X (Twitter)'da paylaşıldı!</b>\n🔗 {html.escape(res)}",
                    chat_id=chat_id,
                    reply_markup=self._main_kb(),
                )
            else:
                self.notifier.send_html(
                    f"⚠️ X paylaşım hatası: <code>{html.escape(res)}</code>",
                    chat_id=chat_id,
                    reply_markup=self._main_kb(),
                )

        threading.Thread(target=_worker, daemon=True, name="x-post").start()

    def _send_watched_keywords(self, chat_id: str, note: str = "") -> None:
        items = self.storage.list_watched_keywords(active_only=True)
        txt, kb = self.notifier.build_watched_keywords_message(items)
        self.notifier.send_html(note + txt, chat_id=chat_id, reply_markup=kb)

    def _handle_keyword_search(self, query: str, chat_id: str) -> None:
        clean_q = " ".join(query.strip().split())
        self.notifier.send_html(
            f"🔍 <b>'{html.escape(clean_q)}'</b> tüm mağazalarda aranıyor...",
            chat_id=chat_id,
        )
        offers = search_product_across_sites(clean_q, self.runner.fetcher, max_results=5)
        msg = self.notifier.build_keyword_search_message(clean_q, offers)
        best_url = offers[0].url if offers else ""
        kb = self.notifier.build_keyword_search_keyboard(clean_q, best_url)
        self.notifier.send_html(msg, chat_id=chat_id, reply_markup=kb)

    def _watch_keyword_query(self, chat_id: str, query: str) -> None:
        offers = search_product_across_sites(query, self.runner.fetcher, max_results=3)
        if offers:
            best = offers[0]
            kid, _ = self.storage.add_or_update_watched_keyword(
                query, best.effective_price_tl, best.seller, best.url
            )
            note = (
                f"✅ <b>'{html.escape(query)}'</b> (#{kid}) tüm mağazalardan sürekli takibe alındı!\n"
                f"Şu anki en ucuz: <b>{format_tl(best.effective_price_tl)}</b> ({html.escape(best.seller)})\n\n"
            )
        else:
            kid, _ = self.storage.add_or_update_watched_keyword(query, 0.0, "-", "")
            note = f"✅ <b>'{html.escape(query)}'</b> (#{kid}) sürekli takibe alındı!\n\n"
        self._send_watched_keywords(chat_id, note=note)

    def _trigger_async_scan(self, chat_id: str) -> None:
        if self._manual_scan_running:
            self.notifier.send_html(
                "⏳ Şu anda devam eden bir tarama var, tamamlandığında sonuçlar gönderilecek...",
                chat_id=chat_id,
            )
            return

        def _worker() -> None:
            self._manual_scan_running = True
            try:
                self.notifier.send_html("🔎 Tüm siteler ve kayıtlı linkler taranıyor...", chat_id=chat_id)
                with self.scan_lock:
                    res = self.runner.run_once(force_summary=False)
                reply = self.notifier.build_summary_message(
                    res.grouped_offers,
                    title="📊 <b>ANLIK GALAXY S25 ULTRA FİYATLARI</b>",
                    sources_ok=res.sources_ok,
                    sources_total=res.sources_ok + res.sources_failed,
                    paused_variants=self.storage.get_paused_variants(),
                )
                self.notifier.send_html(
                    reply,
                    chat_id=chat_id,
                    reply_markup=self._main_kb(),
                )
            finally:
                self._manual_scan_running = False

        threading.Thread(target=_worker, daemon=True, name="manual-scan").start()

    def _send_targets_menu(self, chat_id: str, header_note: str = "") -> None:
        paused = self.storage.get_paused_variants()
        lines = [header_note + "🎯 <b>S25 Ultra Hedef Fiyat Yönetimi</b>", ""]
        for v in self.config.variants:
            t = self.config.target_prices_tl.get(v)
            st = " <i>(⏸ Durduruldu)</i>" if ("ALL" in paused or v in paused) else " <i>(✅ Aktif)</i>"
            lines.append(f"• <b>{v}</b>: <b>{format_tl(t) if t else 'Tanımsız'}</b>{st}")
        lines.append("\nAşağıdaki butonlarla hedefleri tek dokunuşla değiştirebilirsiniz:")
        self.notifier.send_html(
            "\n".join(lines),
            chat_id=chat_id,
            reply_markup=self.notifier.build_targets_keyboard(self.config.variants),
        )

    def _send_toggle_menu(self, chat_id: str, note: str = "") -> None:
        paused = self.storage.get_paused_variants()
        only_off = "ONLY_OFFICIAL" in paused
        lines = [note + "⏸ <b>Takip & Güvenilir Satıcı Ayarları</b>", ""]
        for v in self.config.variants:
            is_p = ("ALL" in paused) or (v in paused)
            status = "⏸ Durduruldu" if is_p else "✅ Aktif Takip Ediliyor"
            lines.append(f"• <b>{v}</b>: {status}")
        lines.append(
            f"• <b>🛡 Resmi Satıcı Filtresi</b>: {'✅ Sadece Resmi Satıcılar' if only_off else '🌐 Tüm Satıcılar'}"
        )
        lines.append("\nAşağıdaki butonlarla ayarları tek dokunuşla değiştirebilirsiniz:")
        self.notifier.send_html(
            "\n".join(lines),
            chat_id=chat_id,
            reply_markup=self.notifier.build_toggle_tracking_keyboard(self.config.variants, paused),
        )

    def _send_watched_list(self, chat_id: str, note: str = "") -> None:
        items = self.storage.list_watched_urls(active_only=True)
        kb = (
            self.notifier.build_watched_url_keyboard([int(i["id"]) for i in items])
            if items
            else self._main_kb()
        )
        self.notifier.send_html(
            note + self.notifier.build_watched_list_message(items),
            chat_id=chat_id,
            reply_markup=kb,
        )

    def _trigger_test_alarm(self, chat_id: str) -> None:
        if self.wakeup:
            self.wakeup.trigger(
                summary_html="🔔 <b>Test Uyandırma Alarmı!</b>\n6 aylık fiyat altı fırsat veya stok açıldığında bu şekilde uyarılacaksınız.",
                voice_text="Test uyandırma alarmı. Fırsat yakalandığında bu şekilde uyarılacaksınız.",
                reply_markup={"inline_keyboard": [[{"text": "🔕 Alarmı Sustur", "callback_data": "stop_alarm"}]]},
                force=True,
            )
        else:
            self.notifier.send_html("Alarm yöneticisi aktif değil.", chat_id=chat_id)

    def _handle_pause(self, text: str, chat_id: str) -> None:
        parts = text.split(maxsplit=1)
        arg = parts[1].strip().lower() if len(parts) > 1 else "hepsi"
        if arg in ("hepsi", "tümü", "all", "s25"):
            self.storage.pause_variant("ALL")
            self._send_toggle_menu(chat_id, note="🛑 <b>Galaxy S25 Ultra</b> takibi durduruldu.\n\n")
            return
        variant = detect_variant(arg)
        if not variant:
            self._send_toggle_menu(chat_id)
            return
        self.storage.pause_variant(variant)
        self._send_toggle_menu(chat_id, note=f"⏸ <b>{variant}</b> takibi durduruldu.\n\n")

    def _handle_resume(self, text: str, chat_id: str) -> None:
        parts = text.split(maxsplit=1)
        arg = parts[1].strip().lower() if len(parts) > 1 else "hepsi"
        if arg in ("hepsi", "tümü", "all", "s25"):
            self.storage.resume_variant("ALL")
            for v in self.config.variants:
                self.storage.resume_variant(v)
            self._send_toggle_menu(chat_id, note="▶️ <b>Tüm varyantlar</b> için takip başlatıldı!\n\n")
            return
        variant = detect_variant(arg)
        if not variant:
            self._send_toggle_menu(chat_id)
            return
        self.storage.resume_variant("ALL")
        self.storage.resume_variant(variant)
        self._send_toggle_menu(chat_id, note=f"▶️ <b>{variant}</b> takibi yeniden başlatıldı!\n\n")

    def _handle_add_url(self, text: str, chat_id: str) -> None:
        m = _URL_RE.search(text)
        if not m:
            self.notifier.send_html(
                "Kullanım: Ürün linkini doğrudan yapıştırın (ör. <code>https://... 68000</code>).",
                chat_id=chat_id,
                reply_markup=self._main_kb(),
            )
            return

        url = m.group(0).rstrip(".,);")
        remainder = (text[: m.start()] + " " + text[m.end() :]).strip()
        remainder = re.sub(r"^/(ekle|takip)\b", "", remainder, flags=re.IGNORECASE).strip()
        target_price = parse_price_tl(remainder) if remainder else None

        self.notifier.send_html("🔎 Ürün linki inceleniyor, fiyat ve stok okunuyor...", chat_id=chat_id)
        try:
            snap = fetch_watched_product(url, self.runner.fetcher, storage=self.storage)
        except Exception as exc:
            self.notifier.send_html(
                f"⚠️ Bu linkten fiyat okunamadı:\n<code>{html.escape(str(exc))}</code>\n\n"
                "Site bot koruması uyguluyor olabilir veya sayfada fiyat bilgisi bulunamadı.",
                chat_id=chat_id,
                reply_markup=self._main_kb(),
            )
            return

        watch_id, is_new = self.storage.add_or_update_watched_url(
            url=snap.best_url,
            title=snap.title,
            seller=snap.seller,
            price_tl=snap.effective_price_tl,
            target_price_tl=target_price,
            in_stock=snap.in_stock,
        )
        status_txt = "takibe eklendi" if is_new else "güncellendi"
        target_txt = format_tl(target_price) if target_price else "Belirlenmedi"
        c_badge = format_coupon_badge(snap.coupon_info, snap.coupon_code)
        coupon_line = (
            f"\n{c_badge} (Liste: {format_tl(snap.price_tl)})"
            if (snap.coupon_discount_tl > 0 or snap.coupon_code)
            else ""
        )
        price_display = (
            f"<b>{format_tl(snap.effective_price_tl)}</b>{coupon_line}"
            if (snap.in_stock and snap.effective_price_tl > 0)
            else "📦 <b>Stokta Yok — Stoğa Girdiği Anda Uyandırılacaksınız!</b>"
        )
        kb = self.notifier.build_watched_url_keyboard([watch_id])
        self.notifier.send_html(
            f"✅ <b>Ürün #{watch_id} {status_txt}!</b>\n\n"
            f"📦 <b>{html.escape(snap.title)}</b>\n"
            f"💰 Durum/Fiyat: {price_display}\n"
            f"🏪 Mağaza: <a href=\"{html.escape(snap.best_url, quote=True)}\">{html.escape(snap.seller)}</a>\n"
            f"🎯 Hedef Fiyat: <b>{target_txt}</b>\n\n"
            f"Aşağıdaki butonlarla grafik görebilir, hedef fiyatı ayarlayabilir veya takibi bırakabilirsiniz:",
            chat_id=chat_id,
            reply_markup=kb,
        )

    def _handle_link_target(self, text: str, chat_id: str) -> None:
        m = re.match(r"^/linkhedef\s+#?(\d+)\s+(\d[\d\.\,]*)$", text, re.IGNORECASE)
        if not m:
            self._send_watched_list(chat_id)
            return
        watch_id = int(m.group(1))
        price = parse_price_tl(m.group(2))
        if not price or not self.storage.set_watched_url_target(watch_id, price):
            self.notifier.send_html(
                f"⚠️ #{watch_id} numaralı aktif ürün bulunamadı veya fiyat geçersiz.",
                chat_id=chat_id,
            )
            return
        self._send_watched_list(
            chat_id,
            note=f"✅ <b>#{watch_id}</b> numaralı ürün için hedef fiyat <b>{format_tl(price)}</b> olarak ayarlandı.\n\n",
        )

    def _handle_remove_url(self, text: str, chat_id: str) -> None:
        m = re.match(r"^/sil\s+#?(\d+)$", text, re.IGNORECASE)
        if not m:
            self._send_watched_list(chat_id)
            return
        watch_id = int(m.group(1))
        if self.storage.remove_watched_url(watch_id):
            self._send_watched_list(chat_id, note=f"🗑 <b>#{watch_id}</b> numaralı ürün takipten çıkarıldı.\n\n")
        else:
            self._send_watched_list(chat_id, note=f"⚠️ #{watch_id} numaralı aktif ürün bulunamadı.\n\n")

    def _handle_set_target(self, text: str, chat_id: str) -> None:
        m = re.match(r"^/hedef\s+(.+?)\s+(\d[\d\.\,]*)$", text, re.IGNORECASE)
        if not m:
            self._send_targets_menu(chat_id)
            return
        variant = detect_variant(m.group(1))
        price = parse_price_tl(m.group(2))
        if not variant or not price:
            self._send_targets_menu(chat_id)
            return
        self.config.save_target_price(variant, price)
        self._send_targets_menu(
            chat_id,
            header_note=f"✅ <b>{variant}</b> için yeni hedef fiyat <b>{format_tl(price)}</b> olarak kaydedildi.\n\n",
        )

    def _send_help(self, chat_id: str) -> None:
        interval, is_turbo = get_dynamic_interval_minutes(self.config)
        mode_str = f"⚡ Turbo Mod Aktif ({interval} dk'da bir tarama)" if is_turbo else f"🕒 Normal Mod ({interval} dk'da bir tarama — Gece 23:45-01:30 ve 18:00-21:00 arası otomatik 4 dk Turbo Mod)"
        dash_url = f"http://{self.config.dashboard_host}:{self.config.dashboard_port}"
        self.notifier.send_html(
            "🤖 <b>Fiyat Takip & Uyandırma Botu — Kontrol Paneli</b>\n\n"
            f"📡 <b>Tarama Modu:</b> {mode_str}\n"
            f"🌐 <b>Canlı Web Panel:</b> <a href=\"{dash_url}\">{dash_url}</a>\n\n"
            "• <b>Ürün Linki Takibi:</b> Herhangi bir ürün linkini doğrudan sohbete yapıştırın (<code>https://... 68000</code>).\n"
            "• <b>İsimle Ürün Arama & Takip:</b> Sohbete istediğiniz ürünün adını yazın (ör. <code>AirPods Pro 2</code>, <code>PS5 Pro</code>) — tüm mağazalarda arayıp tek butonla takibe alır!",
            chat_id=chat_id,
            reply_markup=self._main_kb(),
        )

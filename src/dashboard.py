from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import platform
import sys
import threading
import time
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from src.amazon_depo import (
    AMAZON_DEPO_MERCHANT_ID,
    DEPO_CATEGORY_FEEDS,
    AmazonDepoDeal,
    AmazonDepoHunter,
    build_depo_cart_url,
    extract_asin,
)
from src.config import AppConfig
from src.exporter import generate_csv_report_bytes
from src.notifier import TelegramNotifier, format_tl
from src.oracle_deployer import build_oracle_bundle_zip, build_sqlite_backup_bytes
from src.storage import PriceStorage

logger = logging.getLogger(__name__)

_BOOT_TS = time.time()


def _compute_auth_token(password: str) -> str:
    return hmac.new(b"firsat-takip-panel-v2", password.encode("utf-8"), hashlib.sha256).hexdigest()


def start_dashboard_server(
    config: AppConfig,
    storage: PriceStorage,
    notifier: Optional[TelegramNotifier] = None,
    runner: Any = None,
    wakeup: Any = None,
    scan_lock: Optional[threading.Lock] = None,
) -> ThreadingHTTPServer | None:
    if not config.dashboard_enabled:
        return None

    lock = scan_lock or threading.Lock()
    scan_state: dict[str, Any] = {
        "running": False,
        "started_at": "",
        "last_finished_at": "",
        "last_msg": "",
    }

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:  # noqa: A002
            return

        def _is_authenticated(self) -> bool:
            pwd = (config.dashboard_password or "").strip()
            if not pwd:
                return True
            expected = _compute_auth_token(pwd)
            raw_cookie = self.headers.get("Cookie", "")
            if raw_cookie:
                c = SimpleCookie()
                try:
                    c.load(raw_cookie)
                    if "ft_admin" in c and hmac.compare_digest(c["ft_admin"].value, expected):
                        return True
                except Exception:
                    pass
            hdr_pwd = (self.headers.get("X-Admin-Password") or "").strip()
            if hdr_pwd and hmac.compare_digest(hdr_pwd, pwd):
                return True
            auth_hdr = self.headers.get("Authorization", "")
            if auth_hdr.startswith("Basic "):
                try:
                    decoded = base64.b64decode(auth_hdr[6:].strip()).decode("utf-8", errors="ignore")
                    _, given_pwd = decoded.split(":", 1)
                    if hmac.compare_digest(given_pwd, pwd):
                        return True
                except Exception:
                    pass
            return False

        def _send_json(
            self,
            code: int,
            payload: dict,
            extra_headers: Optional[list[tuple[str, str]]] = None,
        ) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            for hk, hv in (extra_headers or []):
                self.send_header(hk, hv)
            self.end_headers()
            self.wfile.write(raw)

        def _read_json_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except Exception:
                return {}

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)

            if not self._is_authenticated():
                login_bytes = _render_login_html(config).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(login_bytes)))
                self.end_headers()
                self.wfile.write(login_bytes)
                return

            if parsed.path == "/amazon-depo-fast.user.js":
                js_bytes = _build_amazon_depo_userscript().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript; charset=utf-8")
                self.send_header("Content-Disposition", 'inline; filename="amazon-depo-fast.user.js"')
                self.send_header("Content-Length", str(len(js_bytes)))
                self.end_headers()
                self.wfile.write(js_bytes)
                return

            if parsed.path == "/export.csv":
                data = generate_csv_report_bytes(storage)
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="amazon_depo_raporu.csv"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

            if parsed.path == "/backup.db":
                db_bytes = build_sqlite_backup_bytes(config.database_path)
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", 'attachment; filename="depo_backup.db"')
                self.send_header("Content-Length", str(len(db_bytes)))
                self.end_headers()
                self.wfile.write(db_bytes)
                return

            if parsed.path == "/api/oracle/bundle.zip":
                pwd_q = (qs.get("pwd") or [""])[0].strip()
                zbytes = build_oracle_bundle_zip(config, panel_password=pwd_q, include_db=True)
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", 'attachment; filename="oracle_7x24_yayin_paketi.zip"')
                self.send_header("Content-Length", str(len(zbytes)))
                self.end_headers()
                self.wfile.write(zbytes)
                return

            if parsed.path == "/api/scan-status":
                last_c = getattr(runner, "last_cycle_ts", "") if runner else ""
                self._send_json(200, {**scan_state, "daemon_paused": config.daemon_paused, "last_cycle_ts": last_c})
                return

            if parsed.path == "/api/logs":
                lines_n = max(20, min(400, int((qs.get("lines") or ["120"])[0] or 120)))
                log_file = config.base_dir / "logs" / "tracker.log"
                txt = ""
                if log_file.exists():
                    all_lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
                    txt = "\n".join(all_lines[-lines_n:])
                self._send_json(200, {"ok": True, "logs": txt or "Henüz log kaydı yok."})
                return

            body = _render_dashboard_html(config, storage, notifier=notifier).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            data = self._read_json_body()
            path = parsed.path

            if path == "/api/auth/login":
                given = str(data.get("password", "")).strip()
                real_pwd = (config.dashboard_password or "").strip()
                if not real_pwd or hmac.compare_digest(given, real_pwd):
                    tok = _compute_auth_token(real_pwd) if real_pwd else "open"
                    cookie = f"ft_admin={tok}; Path=/; Max-Age=2592000; HttpOnly; SameSite=Lax"
                    self._send_json(200, {"ok": True, "msg": "Giriş başarılı!"}, [("Set-Cookie", cookie)])
                else:
                    self._send_json(401, {"ok": False, "msg": "Hatalı panel şifresi!"})
                return

            if path == "/api/auth/logout":
                cookie = "ft_admin=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"
                self._send_json(200, {"ok": True, "msg": "Çıkış yapıldı."}, [("Set-Cookie", cookie)])
                return

            if not self._is_authenticated():
                self._send_json(401, {"ok": False, "msg": "Yetkisiz erişim."})
                return

            try:
                if path == "/api/daemon/toggle":
                    new_paused = bool(data.get("paused", not config.daemon_paused))
                    config.daemon_paused = new_paused
                    config.raw_yaml.setdefault("schedule", {})["daemon_paused"] = new_paused
                    config.persist_yaml()
                    msg = "⏸ Otomatik Depo taraması duraklatıldı." if new_paused else "▶️ Otomatik Depo taraması başlatıldı!"
                    self._send_json(200, {"ok": True, "paused": new_paused, "msg": msg})
                    return

                if path in ("/api/depo/scan", "/api/scan-now"):
                    if runner is None:
                        self._send_json(400, {"ok": False, "msg": "Depo motoru bağlı değil."})
                        return
                    if scan_state["running"]:
                        self._send_json(200, {"ok": True, "running": True, "msg": "Depo taraması zaten çalışıyor..."})
                        return
                    scan_state["running"] = True
                    scan_state["started_at"] = datetime.now().strftime("%H:%M:%S")
                    scan_state["last_msg"] = "Amazon Depo taranıyor..."

                    def _bg_depo() -> None:
                        try:
                            with lock:
                                if hasattr(runner, "run_cycle"):
                                    res = runner.run_cycle(force=True)
                                    scan_state["last_msg"] = (
                                        f"Depo tarandı: {res['found']} aktif fırsat, {res['removed']} tükenen silindi (En yüksek: -%{res.get('top_discount', 0)})."
                                    )
                                else:
                                    deals = runner.scan_amazon_depo(force=True)
                                    scan_state["last_msg"] = f"Depo tarandı: {len(deals)} fırsat bulundu."
                        except Exception as exc:
                            scan_state["last_msg"] = f"Tarama hatası: {exc}"
                        finally:
                            scan_state["running"] = False
                            scan_state["last_finished_at"] = datetime.now().strftime("%H:%M:%S")

                    threading.Thread(target=_bg_depo, daemon=True).start()
                    self._send_json(200, {"ok": True, "running": True, "msg": "Amazon Depo taraması başlatıldı! Bitince sayfa yenilenecek..."})
                    return

                if path == "/api/depo/verify-stock":
                    if runner is None or not hasattr(runner, "verify_stock_now"):
                        self._send_json(400, {"ok": False, "msg": "Stok doğrulayıcı bağlı değil."})
                        return
                    res = runner.verify_stock_now()
                    self._send_json(
                        200,
                        {"ok": True, "msg": f"Canlı AOD stok kontrolü bitti: {res['alive']} ürün stokta, {res['removed']} tükenen ürün silindi!"},
                    )
                    return

                if path == "/api/depo/inspect-asin":
                    raw_in = str(data.get("input", "")).strip()
                    asin = extract_asin(raw_in)
                    if not asin:
                        self._send_json(400, {"ok": False, "msg": "Geçerli bir 10 haneli ASIN veya Amazon ürün linki girin."})
                        return
                    hunter = runner.hunter if (runner and hasattr(runner, "hunter")) else AmazonDepoHunter(runner.fetcher)
                    try:
                        deal = hunter.inspect_asin_aod(
                            asin,
                            category_hint="🔍 Anlık Sorgu",
                            super_discount_percent=config.depo_super_discount_percent,
                            is_watchlist=True,
                        )
                    except Exception as exc:
                        self._send_json(200, {"ok": True, "in_depo": False, "asin": asin, "msg": f"⚠️ Amazon anlık yanıt vermedi (503), tekrar deneyin: {exc}"})
                        return
                    if deal is None:
                        storage.remove_depo_asins({asin})
                        self._send_json(
                            200,
                            {
                                "ok": True,
                                "in_depo": False,
                                "asin": asin,
                                "msg": f"❌ {asin} için şu an Amazon Depo'da aktif ikinci el stok bulunmuyor.",
                            },
                        )
                        return
                    storage.upsert_depo_deals([deal])
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "in_depo": True,
                            "deal": {
                                "asin": deal.asin,
                                "title": deal.title,
                                "depo_price_fmt": format_tl(deal.depo_price_tl),
                                "market_price_fmt": format_tl(deal.market_price_tl),
                                "discount_percent": deal.discount_percent,
                                "condition": deal.condition_label,
                                "note": deal.condition_note,
                                "has_oid": bool(deal.offer_listing_id),
                                "cart_url": deal.cart_url(config.amazon_associate_tag),
                                "url": deal.url,
                            },
                            "msg": f"✅ YAKALANDI! {deal.title[:45]} — {format_tl(deal.depo_price_tl)} (-%{deal.discount_percent})",
                        },
                    )
                    return

                if path == "/api/depo/watchlist/add":
                    raw_in = str(data.get("input", "")).strip()
                    asin = extract_asin(raw_in)
                    if not asin:
                        self._send_json(400, {"ok": False, "msg": "Geçerli bir ASIN (örn. B0D92CF1P5) veya Amazon linki girin."})
                        return
                    note = str(data.get("note", "")).strip()
                    t_price = float(data.get("target_price_tl") or 0.0)
                    min_pct = int(data.get("min_discount_percent") or 20)
                    storage.add_depo_watchlist(asin, note=note, target_price_tl=t_price, min_discount_percent=min_pct)
                    self._send_json(200, {"ok": True, "msg": f"🎯 {asin} özel nöbet listesine eklendi! Her döngüde canlı AOD ile izlenecek."})
                    return

                if path == "/api/depo/watchlist/delete":
                    asin = extract_asin(str(data.get("asin", "")))
                    ok = storage.remove_depo_watchlist(asin)
                    self._send_json(200, {"ok": ok, "msg": f"{asin} nöbet listesinden çıkarıldı."})
                    return

                if path == "/api/depo/publish":
                    if runner is None:
                        self._send_json(400, {"ok": False, "msg": "Runner yok."})
                        return
                    asin = extract_asin(str(data.get("asin", "")))
                    row = storage.get_depo_deal(asin)
                    if not row:
                        self._send_json(400, {"ok": False, "msg": "Depo ürünü bulunamadı."})
                        return
                    deal_obj = AmazonDepoDeal(
                        asin=asin,
                        title=str(row["title"]),
                        depo_price_tl=float(row["depo_price_tl"]),
                        market_price_tl=float(row["market_price_tl"]),
                        discount_percent=int(row["discount_percent"]),
                        savings_tl=float(row["savings_tl"]),
                        condition_label=str(row.get("condition_label") or "İkinci El"),
                        condition_note=str(row.get("condition_note") or ""),
                        offer_listing_id=str(row.get("offer_listing_id") or ""),
                        category=str(row.get("category") or "Amazon Depo"),
                        url=str(row["url"]),
                        image_url=str(row.get("image_url") or ""),
                        price_source=str(row.get("price_source") or "Amazon Sıfır"),
                    )
                    if hasattr(runner, "publish_depo_to_channel"):
                        ok_p = runner.publish_depo_to_channel(deal_obj, force_repost=True)
                    else:
                        ok_p = False
                    self._send_json(
                        200 if ok_p else 400,
                        {"ok": ok_p, "msg": f"🎉 {asin} kanala yayınlandı!" if ok_p else "Yayınlanamadı (kanal ayarlarını kontrol edin)."},
                    )
                    return

                if path == "/api/depo/delete":
                    asin = extract_asin(str(data.get("asin", "")))
                    ok = storage.delete_depo_deal(asin)
                    self._send_json(200, {"ok": ok, "msg": "Ürün panelden kaldırıldı." if ok else "Bulunamadı."})
                    return

                if path == "/api/depo/clear":
                    cnt = storage.clear_depo_deals()
                    self._send_json(200, {"ok": True, "msg": f"{cnt} Depo kaydı temizlendi."})
                    return

                if path == "/api/depo/settings":
                    config.depo_enabled = bool(data.get("depo_enabled", config.depo_enabled))
                    config.depo_interval_seconds = max(15, int(data.get("depo_interval_seconds", config.depo_interval_seconds)))
                    config.depo_min_discount_percent = max(5, int(data.get("depo_min_discount_percent", config.depo_min_discount_percent)))
                    config.depo_super_discount_percent = max(20, int(data.get("depo_super_discount_percent", config.depo_super_discount_percent)))
                    config.depo_min_price_tl = max(0.0, float(data.get("depo_min_price_tl", config.depo_min_price_tl)))
                    config.depo_max_price_tl = max(100.0, float(data.get("depo_max_price_tl", config.depo_max_price_tl)))
                    config.depo_auto_notify_admin = bool(data.get("depo_auto_notify_admin", config.depo_auto_notify_admin))
                    config.depo_auto_publish_channel = bool(data.get("depo_auto_publish_channel", config.depo_auto_publish_channel))
                    if isinstance(data.get("depo_categories"), list):
                        cats = [str(c) for c in data["depo_categories"] if str(c) in DEPO_CATEGORY_FEEDS]
                        if cats:
                            config.depo_categories = cats
                    if "depo_keywords_csv" in data:
                        raw_k = str(data["depo_keywords_csv"])
                        config.depo_keywords = [x.strip() for x in raw_k.replace("\n", ",").split(",") if len(x.strip()) >= 2]
                    if "callmebot_telegram_user" in data:
                        config.callmebot_telegram_user = str(data["callmebot_telegram_user"]).strip()
                        config.save_env_vars({"CALLMEBOT_TELEGRAM_USER": config.callmebot_telegram_user})

                    dp_sec = config.raw_yaml.setdefault("amazon_depo", {})
                    dp_sec["enabled"] = config.depo_enabled
                    dp_sec["interval_seconds"] = config.depo_interval_seconds
                    dp_sec["min_discount_percent"] = config.depo_min_discount_percent
                    dp_sec["super_discount_percent"] = config.depo_super_discount_percent
                    dp_sec["min_price_tl"] = int(config.depo_min_price_tl)
                    dp_sec["max_price_tl"] = int(config.depo_max_price_tl)
                    dp_sec["auto_notify_admin"] = config.depo_auto_notify_admin
                    dp_sec["auto_publish_channel"] = config.depo_auto_publish_channel
                    dp_sec["categories"] = list(config.depo_categories)
                    dp_sec["keywords"] = list(config.depo_keywords)
                    config.persist_yaml()
                    self._send_json(200, {"ok": True, "msg": "Amazon Depo Sniper ayarları kaydedildi ve anında devreye alındı!"})
                    return

                # Geriye dönük uyumluluk (mevcut birim testler ve API entegrasyonları için)
                if path == "/api/channel/settings":
                    config.only_photo_deals = bool(data.get("only_photo_deals", config.only_photo_deals))
                    config.prevent_duplicate_deals = bool(data.get("prevent_duplicate_deals", config.prevent_duplicate_deals))
                    if "min_discount_percent" in data:
                        config.min_popular_discount_percent = max(1, int(data["min_discount_percent"]))
                    if "telegram_channel_id" in data and str(data["telegram_channel_id"]).strip():
                        config.telegram_channel_id = str(data["telegram_channel_id"]).strip()
                        config.save_env_vars({"TELEGRAM_CHANNEL_ID": config.telegram_channel_id})
                    config.persist_yaml()
                    self._send_json(200, {"ok": True, "msg": "Kanal ayarları kaydedildi."})
                    return

                if path == "/api/sources/settings":
                    srcs_in = data.get("sources") or {}
                    for k, v in srcs_in.items():
                        config.sources[str(k)] = bool(v)
                    config.timeout_seconds = max(5, int(data.get("timeout_seconds", config.timeout_seconds)))
                    config.max_workers = max(1, int(data.get("max_workers", config.max_workers)))
                    config.persist_yaml()
                    self._send_json(200, {"ok": True, "msg": "Ağ motoru ayarları kaydedildi."})
                    return

                if path == "/api/integrations/save":
                    tok = str(data.get("telegram_bot_token", "")).strip()
                    cid = str(data.get("telegram_chat_id", "")).strip()
                    ch_id = str(data.get("telegram_channel_id", "")).strip()
                    amz_tag = str(data.get("amazon_tag", "")).strip()
                    new_pwd = str(data.get("dashboard_password", "")).strip()

                    env_up: dict[str, str] = {"AMAZON_ASSOCIATE_TAG": amz_tag}
                    config.amazon_associate_tag = amz_tag
                    if tok and "***" not in tok:
                        config.telegram_bot_token = tok
                        env_up["TELEGRAM_BOT_TOKEN"] = tok
                        if notifier is not None:
                            notifier.bot_token = tok
                    if cid:
                        config.telegram_chat_id = cid
                        env_up["TELEGRAM_CHAT_ID"] = cid
                        if notifier is not None:
                            notifier.chat_id = cid
                    if ch_id:
                        config.telegram_channel_id = ch_id
                        env_up["TELEGRAM_CHANNEL_ID"] = ch_id
                        config.raw_yaml.setdefault("channel", {})["channel_id"] = ch_id

                    extra_headers: list[tuple[str, str]] = []
                    if new_pwd and "***" not in new_pwd:
                        config.dashboard_password = new_pwd
                        env_up["DASHBOARD_PASSWORD"] = new_pwd
                        tok_h = _compute_auth_token(new_pwd)
                        extra_headers.append(("Set-Cookie", f"ft_admin={tok_h}; Path=/; Max-Age=2592000; HttpOnly; SameSite=Lax"))

                    config.save_env_vars(env_up)
                    config.persist_yaml()
                    self._send_json(200, {"ok": True, "msg": "Bağlantı, Affiliate ve Güvenlik ayarları kaydedildi!"}, extra_headers=extra_headers)
                    return

                if path == "/api/alarm/test":
                    if wakeup:
                        wakeup.trigger(
                            summary_html="🔔 <b>Amazon Depo Sniper Test Alarmı!</b>",
                            voice_text="Amazon Depo keskin nişancı test alarmı başarılı.",
                            force=True,
                        )
                    self._send_json(200, {"ok": True, "msg": "Test alarmı gönderildi!"})
                    return

                if path == "/api/logs/clear":
                    log_file = config.base_dir / "logs" / "tracker.log"
                    if log_file.exists():
                        log_file.write_text("", encoding="utf-8")
                    self._send_json(200, {"ok": True, "msg": "Loglar temizlendi."})
                    return

                self._send_json(404, {"ok": False, "msg": "Bilinmeyen API endpoint."})
            except Exception as exc:
                logger.exception("Dashboard API hatası (%s):", path)
                self._send_json(500, {"ok": False, "msg": f"Sunucu hatası: {exc}"})

    try:
        server = ThreadingHTTPServer((config.dashboard_host, config.dashboard_port), _Handler)
        t = threading.Thread(target=server.serve_forever, daemon=True, name="web-dashboard")
        t.start()
        logger.info("Amazon Depo Web Kontrol Merkezi aktif: http://%s:%d", config.dashboard_host, config.dashboard_port)
        return server
    except Exception as exc:
        logger.warning("Web Dashboard başlatılamadı (%s:%d): %s", config.dashboard_host, config.dashboard_port, exc)
        return None


def _build_amazon_depo_userscript() -> str:
    return """// ==UserScript==
// @name         Amazon.com.tr Depo Hizli Odeme Gecisi
// @namespace    amazon-depo-sniper
// @version      1.0
// @description  #ft_buy=1 ile acilan Amazon Depo urununu sepete ekler ve Alisverisi Tamamla'ya tiklayip odeme ekraninda durur.
// @match        https://www.amazon.com.tr/*
// @grant        none
// ==/UserScript==

(function () {
  'use strict';

  if (location.hash.includes('ft_buy=1')) {
    sessionStorage.setItem('ft_buy_active', '1');
    sessionStorage.setItem('ft_buy_ts', String(Date.now()));
  }

  const isActive = sessionStorage.getItem('ft_buy_active') === '1';
  const startedAt = Number(sessionStorage.getItem('ft_buy_ts') || '0');
  if (!isActive || (Date.now() - startedAt > 45000)) {
    sessionStorage.removeItem('ft_buy_active');
    return;
  }

  // Odeme ekranina veya giris/guvenlik ekranina ulasinca hemen dur (kontrol kullanicida)
  if (location.pathname.includes('/gp/buy/') || location.pathname.includes('/checkout') || location.pathname.includes('/ap/signin')) {
    sessionStorage.removeItem('ft_buy_active');
    return;
  }

  let stepDone = false;
  const timer = setInterval(() => {
    if (stepDone) return;

    // 1) Sepet / SmartWagon ekranindaysak -> Alisverisi Tamamla butonuna bas
    const ptc = document.querySelector(
      'input[name="proceedToRetailCheckout"], #sc-buy-box-ptc-button input, #attach-sidesheet-checkout-button input, [data-feature-id="proceed-to-checkout-action"]'
    );
    if (ptc && ptc.offsetParent !== null) {
      stepDone = true;
      clearInterval(timer);
      ptc.click();
      return;
    }

    // 2) AOD cekmecesi aciksa -> "Amazon Depo" saticisinin Sepete Ekle butonuna bas
    const offers = document.querySelectorAll('#aod-pinned-offer, #aod-offer');
    for (const off of offers) {
      const soldEl = off.querySelector('#aod-offer-soldBy');
      const soldTxt = (soldEl ? soldEl.textContent : '').toLowerCase();
      if (soldTxt.includes('amazon depo')) {
        const atc = off.querySelector('[data-aod-atc-action] input[type="submit"], [data-aod-atc-action] button, input[name="submit.addToCart"], .aod-atc-btn input');
        if (atc) {
          stepDone = true;
          setTimeout(() => { stepDone = false; }, 2200);
          atc.click();
          return;
        }
      }
    }

    // 3) Ana sayfa BuyBox zaten Amazon Depo ise -> direkt Sepete Ekle
    const merchEl = document.querySelector('#merchant-info, #sellerProfileTriggerId');
    const merchTxt = (merchEl ? merchEl.textContent : '').toLowerCase();
    if (merchTxt.includes('amazon depo')) {
      const mainAtc = document.querySelector('#add-to-cart-button');
      if (mainAtc) {
        stepDone = true;
        setTimeout(() => { stepDone = false; }, 2200);
        mainAtc.click();
      }
    }
  }, 350);
})();
"""


def _mask_secret(val: str) -> str:
    if not val:
        return ""
    if len(val) <= 8:
        return "***"
    return f"{val[:4]}***{val[-4:]}"


def _format_uptime() -> str:
    sec = max(0, int(time.time() - _BOOT_TS))
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d > 0:
        return f"{d}g {h}s {m}dk"
    if h > 0:
        return f"{h}s {m}dk"
    return f"{m}dk"


def _render_login_html(config: AppConfig) -> str:
    return f"""<!DOCTYPE html>
<html lang="tr">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Yönetim Merkezi Girişi — Amazon Depo Sniper</title>
  <style>
    body {{ background:#090d16; color:#f8fafc; font-family:system-ui,-apple-system,sans-serif; display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0; padding:16px; }}
    .box {{ background:#151e2e; border:1px solid #2d3b55; border-radius:16px; padding:26px; width:100%; max-width:380px; box-shadow:0 16px 40px rgba(0,0,0,0.6); }}
    h1 {{ margin:0 0 6px 0; font-size:20px; color:#fb923c; }}
    p {{ color:#94a3b8; font-size:13px; margin:0 0 18px 0; }}
    input {{ width:100%; box-sizing:border-box; background:#0b1120; color:#fff; border:1px solid #475569; border-radius:10px; padding:12px; font-size:16px; margin-bottom:14px; }}
    button {{ width:100%; background:#ea580c; color:#fff; border:none; border-radius:10px; padding:12px; font-size:15px; font-weight:700; cursor:pointer; }}
    #err {{ color:#f87171; font-size:13px; margin-top:10px; display:none; }}
  </style>
</head>
<body>
  <div class="box">
    <h1>📦 Yönetim Merkezi Girişi</h1>
    <p>Amazon.com.tr Depo (<code>{AMAZON_DEPO_MERCHANT_ID}</code>) 7/24 Keskin Nişancı Paneli</p>
    <input id="pwd" type="password" placeholder="Panel Şifresi" autofocus onkeydown="if(event.key==='Enter')login()" />
    <button onclick="login()">Giriş Yap</button>
    <div id="err"></div>
  </div>
  <script>
    async function login() {{
      const res = await fetch('/api/auth/login', {{
        method:'POST',
        headers:{{'Content-Type':'application/json'}},
        body:JSON.stringify({{password: document.getElementById('pwd').value}})
      }});
      const j = await res.json();
      if (j.ok) location.reload();
      else {{
        const e = document.getElementById('err');
        e.textContent = j.msg || 'Hatalı şifre';
        e.style.display = 'block';
      }}
    }}
  </script>
</body>
</html>"""


def _render_dashboard_html(
    config: AppConfig,
    storage: PriceStorage,
    notifier: Optional[TelegramNotifier] = None,
) -> str:
    depo_deals = storage.list_depo_deals(min_discount=0, limit=80) if hasattr(storage, "list_depo_deals") else []
    watchlist = storage.list_depo_watchlist() if hasattr(storage, "list_depo_watchlist") else []
    watched_kws = storage.list_watched_keywords() if hasattr(storage, "list_watched_keywords") else []
    super_cnt = sum(1 for x in depo_deals if int(x.get("discount_percent") or 0) >= config.depo_super_discount_percent)
    oid_cnt = sum(1 for x in depo_deals if str(x.get("offer_listing_id") or "").strip())

    plat_lbl = f"☁️ Oracle ({platform.machine()})" if sys.platform != "win32" else "💻 Yerel"
    status_pill = (
        "<span class='pill pill-amber'>⏸ Duraklatıldı</span>"
        if config.daemon_paused
        else f"<span class='pill pill-green'>🟢 Aktif ({config.depo_interval_seconds} sn)</span>"
    )

    depo_cards_html: list[str] = []
    for dd in depo_deals:
        dpct = int(dd.get("discount_percent") or 0)
        dprice = float(dd.get("depo_price_tl") or 0)
        mprice = float(dd.get("market_price_tl") or 0)
        sav = float(dd.get("savings_tl") or 0)
        asin_s = str(dd.get("asin") or "")
        oid_s = str(dd.get("offer_listing_id") or "").strip()
        img_s = str(dd.get("image_url") or "")
        note_s = str(dd.get("condition_note") or "")
        title_s = str(dd.get("title") or "")

        badge_bg = "#dc2626" if dpct >= 70 else ("#ea580c" if dpct >= 50 else "#16a34a")
        cart_url = build_depo_cart_url(asin_s, oid_s, config.amazon_associate_tag)
        oid_tag = "<span class='tag oid'>⚡ OID Hazır</span>" if oid_s else "<span class='tag'>Sayfa Linki</span>"
        img_tag = (
            f"<img class='prod-thumb' src='{html.escape(img_s, quote=True)}' alt='' loading='lazy' />"
            if img_s.startswith("http")
            else "<div class='prod-thumb-empty'>📦</div>"
        )
        note_html = f"<div class='cond-note'>📝 {html.escape(note_s)}</div>" if note_s else ""
        asin_js = json.dumps(asin_s)

        depo_cards_html.append(
            f"<div class='depo-card' data-pct='{dpct}' data-oid='{1 if oid_s else 0}' data-q='{html.escape(title_s.lower() + ' ' + asin_s.lower(), quote=True)}'>"
            f"<div class='depo-top'>"
            f"{img_tag}"
            f"<div class='depo-info'>"
            f"<div class='badges'>"
            f"<span class='pct-badge' style='background:{badge_bg}'>-%{dpct}</span>"
            f"<span class='tag'>{html.escape(str(dd.get('condition_label') or 'İkinci El'))}</span>"
            f"{oid_tag}"
            f"<span class='tag'>{html.escape(asin_s)}</span>"
            f"</div>"
            f"<div class='prod-title'>{html.escape(title_s)}</div>"
            f"<div class='price-line'>"
            f"<span class='depo-p'>{format_tl(dprice)}</span> "
            f"<s class='mkt-p'>{format_tl(mprice)}</s> "
            f"<span class='sav-p'>({format_tl(sav)} kazanç • {html.escape(str(dd.get('price_source') or 'Amazon Sıfır'))})</span>"
            f"</div>"
            f"{note_html}"
            f"</div></div>"
            f"<div class='depo-btns'>"
            f"<a class='btn btn-cart' href='{html.escape(cart_url, quote=True)}' target='_blank'>⚡ Depo Teklifini Aç &amp; Al</a>"
            f"<a class='btn btn-dark' href='{html.escape(str(dd.get('url') or '#'), quote=True)}' target='_blank'>🛒 Sayfa</a>"
            f"<button class='btn btn-purple' onclick='publishDepo({asin_js})'>📢 Kanal</button>"
            f"<button class='btn btn-red-sm' onclick='deleteDepo({asin_js})'>🗑</button>"
            f"</div></div>"
        )

    wl_rows_html: list[str] = []
    for w in watchlist:
        wasin = str(w.get("asin") or "")
        in_st = bool(w.get("in_depo_stock"))
        lp = float(w.get("last_depo_price_tl") or 0)
        tp = float(w.get("target_price_tl") or 0)
        st_badge = (
            f"<span class='tag oid'>🟢 DEPO STOKTA: {format_tl(lp)}</span>"
            if in_st and lp > 0
            else "<span class='tag'>⏳ Nöbette (Depo Bekleniyor)</span>"
        )
        wasin_js = json.dumps(wasin)
        w_min_pct = int(w.get("min_discount_percent") or 20)
        target_lbl = format_tl(tp) if tp > 0 else f"-%{w_min_pct}+"
        wl_rows_html.append(
            f"<div class='wl-row'>"
            f"<div><b>{html.escape(wasin)}</b> — {html.escape(str(w.get('note') or 'Ürün'))[:70]} {st_badge} "
            f"<span class='muted'>(Hedef: {target_lbl})</span></div>"
            f"<div class='actions'>"
            f"<button class='btn btn-blue btn-sm' onclick='inspectAsinNow({wasin_js})'>🔍 Kontrol Et</button>"
            f"<button class='btn btn-red-sm' onclick='removeWatchlist({wasin_js})'>🗑</button>"
            f"</div></div>"
        )

    cat_boxes = []
    for ckey, (clbl, _) in DEPO_CATEGORY_FEEDS.items():
        ch = "checked" if ckey in config.depo_categories else ""
        cat_boxes.append(
            f"<label class='chk cat-box'><input type='checkbox' class='depo-cat-chk' data-cat='{ckey}' {ch} /> <span>{html.escape(clbl)}</span></label>"
        )

    kw_hidden_compat = "".join(html.escape(str(k.get("query") or "")) for k in watched_kws)
    daemon_btn = (
        "<button class='btn btn-green' onclick=\"api('/api/daemon/toggle', {paused:false})\">▶️ Başlat</button>"
        if config.daemon_paused
        else "<button class='btn btn-amber' onclick=\"api('/api/daemon/toggle', {paused:true})\">⏸ Duraklat</button>"
    )
    logout_btn = (
        "<button class='btn btn-dark btn-sm' onclick=\"api('/api/auth/logout', {})\">🚪</button>"
        if config.dashboard_password
        else ""
    )

    return f"""<!DOCTYPE html>
<html lang="tr">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1" />
  <title>Amazon Depo Sniper — Telegram Bot &amp; Kanal Yönetim Merkezi</title>
  <style>
    * {{ box-sizing: border-box; }}
    html, body {{ margin: 0; padding: 0; background: #090d16; color: #f8fafc; font-family: system-ui, -apple-system, 'Segoe UI', sans-serif; overflow-x: hidden; }}
    .wrap {{ max-width: 1120px; margin: 0 auto; padding: 10px 12px 44px 12px; }}
    .header {{ background: linear-gradient(135deg, #162033 0%, #1e293b 100%); border: 1px solid #334155; border-radius: 14px; padding: 14px; margin-bottom: 10px; display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 10px; }}
    h1 {{ margin: 0 0 4px 0; font-size: 18px; color: #fb923c; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
    h2 {{ margin: 0; font-size: 15px; color: #f1f5f9; }}
    h3 {{ margin: 14px 0 8px 0; font-size: 13px; color: #fdba74; }}
    .sub {{ font-size: 12px; color: #94a3b8; }}
    .pill {{ font-size: 11px; padding: 3px 9px; border-radius: 999px; font-weight: 700; }}
    .pill-green {{ background: #064e3b; color: #6ee7b7; border: 1px solid #10b981; }}
    .pill-amber {{ background: #78350f; color: #fcd34d; border: 1px solid #f59e0b; }}
    .pill-blue {{ background: #0c4a6e; color: #7dd3fc; border: 1px solid #0284c7; }}
    .actions {{ display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }}
    .btn, button {{ border: none; cursor: pointer; border-radius: 9px; font-weight: 700; font-size: 13px; padding: 9px 12px; color: #fff; text-decoration: none; display: inline-flex; align-items: center; justify-content: center; gap: 5px; min-height: 38px; }}
    .btn-cart {{ background: #16a34a; flex: 1.4; }} .btn-cart:hover {{ background: #15803d; }}
    .btn-orange {{ background: #ea580c; }} .btn-blue {{ background: #0284c7; }}
    .btn-green {{ background: #16a34a; }} .btn-amber {{ background: #d97706; }}
    .btn-purple {{ background: #7c3aed; }} .btn-dark {{ background: #334155; }}
    .btn-red-sm {{ background: #dc2626; padding: 8px 10px; min-height: 38px; }}
    .btn-sm {{ padding: 6px 10px; font-size: 12px; min-height: 34px; }}
    .tabs {{ display: flex; overflow-x: auto; scrollbar-width: none; gap: 6px; background: #111827; padding: 6px; border-radius: 12px; border: 1px solid #1f2937; margin-bottom: 12px; position: sticky; top: 6px; z-index: 40; }}
    .tabs::-webkit-scrollbar {{ display: none; }}
    .tab-btn {{ background: transparent; color: #94a3b8; padding: 8px 13px; border-radius: 8px; font-size: 13px; white-space: nowrap; flex: 0 0 auto; min-height: 36px; }}
    .tab-btn.active {{ background: #ea580c; color: #fff; }}
    .tab-pane {{ display: none; }} .tab-pane.active {{ display: block; }}
    .card {{ background: #151e2e; border: 1px solid #293548; border-radius: 14px; padding: 14px; margin-bottom: 12px; }}
    .card-head {{ display: flex; justify-content: space-between; align-items: center; gap: 8px; flex-wrap: wrap; border-bottom: 1px solid #293548; padding-bottom: 10px; margin-bottom: 12px; }}
    .filter-bar {{ display: flex; gap: 6px; overflow-x: auto; scrollbar-width: none; padding-bottom: 4px; margin-bottom: 10px; }}
    .filter-bar::-webkit-scrollbar {{ display: none; }}
    .depo-card {{ background: #0f172a; border: 1px solid #293548; border-radius: 12px; padding: 12px; margin-bottom: 10px; display: flex; flex-direction: column; gap: 10px; }}
    .depo-top {{ display: flex; gap: 12px; align-items: flex-start; }}
    .prod-thumb {{ width: 78px; height: 78px; object-fit: contain; background: #fff; border-radius: 10px; padding: 4px; flex-shrink: 0; }}
    .prod-thumb-empty {{ width: 78px; height: 78px; background: #1e293b; border-radius: 10px; display: flex; align-items: center; justify-content: center; font-size: 28px; flex-shrink: 0; }}
    .depo-info {{ flex: 1; min-width: 0; }}
    .badges {{ display: flex; flex-wrap: wrap; gap: 5px; margin-bottom: 5px; align-items: center; }}
    .pct-badge {{ color: #fff; font-weight: 900; font-size: 13px; padding: 3px 8px; border-radius: 6px; }}
    .tag {{ background: #263349; color: #cbd5e1; font-size: 11px; padding: 2px 7px; border-radius: 5px; }}
    .tag.oid {{ background: #064e3b; color: #6ee7b7; border: 1px solid #059669; font-weight: 700; }}
    .prod-title {{ font-weight: 700; font-size: 14px; line-height: 1.35; margin-bottom: 5px; word-break: break-word; }}
    .price-line {{ font-size: 13px; display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px; }}
    .depo-p {{ font-size: 18px; font-weight: 900; color: #4ade80; }}
    .mkt-p {{ color: #94a3b8; font-size: 13px; }}
    .sav-p {{ color: #fbbf24; font-size: 12px; font-weight: 600; }}
    .cond-note {{ margin-top: 5px; font-size: 12px; color: #cbd5e1; background: #162033; padding: 5px 8px; border-radius: 6px; }}
    .depo-btns {{ display: flex; flex-wrap: wrap; gap: 6px; }}
    .wl-row {{ display: flex; justify-content: space-between; align-items: center; gap: 8px; flex-wrap: wrap; padding: 10px 0; border-bottom: 1px solid #293548; font-size: 13px; }}
    input, select, textarea {{ width: 100%; background: #090d16; color: #f8fafc; border: 1px solid #3b4c66; border-radius: 9px; padding: 10px 12px; font-size: 16px; margin-top: 4px; margin-bottom: 10px; }}
    label {{ font-size: 12px; color: #cbd5e1; display: block; }}
    .row2 {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 180px), 1fr)); gap: 10px; }}
    .cats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 190px), 1fr)); gap: 8px; margin-bottom: 12px; }}
    .chk {{ display: flex; align-items: center; gap: 8px; font-size: 13px; margin: 8px 0; cursor: pointer; }}
    .chk input {{ width: 18px; height: 18px; margin: 0; flex-shrink: 0; }}
    .cat-box {{ background: #0f172a; border: 1px solid #293548; padding: 9px 11px; border-radius: 9px; margin: 0; }}
    .muted {{ color: #94a3b8; font-size: 12px; }}
    pre.codebox {{ background: #060911; color: #a7f3d0; border: 1px solid #293548; border-radius: 10px; padding: 11px; font-size: 12px; overflow-x: auto; white-space: pre-wrap; word-break: break-word; max-height: 360px; }}
    #toast {{ position: fixed; bottom: 14px; left: 12px; right: 12px; max-width: 430px; margin: 0 auto; background: #0f172a; border: 2px solid #fb923c; color: #fff; padding: 12px 15px; border-radius: 11px; display: none; z-index: 999; font-weight: 700; font-size: 13px; box-shadow: 0 8px 28px rgba(0,0,0,0.7); }}
    #status-bar {{ display: none; background: #0f172a; border: 1px solid #fb923c; color: #fde68a; padding: 10px 14px; border-radius: 10px; margin-bottom: 10px; font-size: 13px; font-weight: 700; }}
    @media (max-width: 640px) {{
      .header .actions {{ width: 100%; display: grid; grid-template-columns: 1fr 1fr; }}
      .depo-btns .btn-cart {{ flex: 1 1 100%; }}
    }}
  </style>
</head>
<body>
  <div id="toast"></div>
  <div class="wrap">
    <div id="status-bar"></div>
    <!-- ÜST BAŞLIK -->
    <div class="header">
      <div>
        <h1>
          📦 Amazon Depo Sniper
          {status_pill}
          <span class="pill pill-blue">{html.escape(plat_lbl)} • {_format_uptime()}</span>
        </h1>
        <div class="sub">
          Telegram Bot &amp; Kanal Yönetim Merkezi • Aktif Stok: <b>{len(depo_deals)}</b> •
          🚨 Süper (-%{config.depo_super_discount_percent}+): <b>{super_cnt}</b> •
          ⚡ OID Sepet Hazır: <b>{oid_cnt}</b> • Nöbet: <b>{len(watchlist)}</b>
        </div>
      </div>
      <div class="actions">
        <button class="btn btn-orange" onclick="api('/api/depo/scan', {{}})">🔎 Şimdi Tara</button>
        <button class="btn btn-blue" onclick="api('/api/depo/verify-stock', {{}})">🔄 Bitenleri Sil</button>
        {daemon_btn}
        {logout_btn}
      </div>
    </div>

    <!-- 4 NET SEKME (Kanal Stüdyosu, Mağaza Kaynakları, Oracle Cloud 7/24 Yayın uyumlu) -->
    <div class="tabs">
      <button class="tab-btn active" data-tab="tab-deals" onclick="switchTab('tab-deals')">📦 Amazon Depo Özel Radarı ({len(depo_deals)})</button>
      <button class="tab-btn" data-tab="tab-watch" onclick="switchTab('tab-watch')">🎯 Özel ASIN Nöbetçisi ({len(watchlist)})</button>
      <button class="tab-btn" data-tab="tab-settings" onclick="switchTab('tab-settings')">⚙️ Radar &amp; Kanal Stüdyosu</button>
      <button class="tab-btn" data-tab="tab-system" onclick="switchTab('tab-system')">☁️ Oracle Cloud 7/24 Yayın &amp; Loglar</button>
    </div>

    <!-- SEKME 1: CANLI AMAZON DEPO FIRSATLARI -->
    <div id="tab-deals" class="tab-pane active">
      <div class="card">
        <div class="filter-bar">
          <button class="btn btn-dark btn-sm" onclick="filterDepo(0, false)">Tümü ({len(depo_deals)})</button>
          <button class="btn btn-green btn-sm" onclick="filterDepo(40, false)">🔥 %40+</button>
          <button class="btn btn-amber btn-sm" onclick="filterDepo(60, false)">⚡ %60+</button>
          <button class="btn btn-orange btn-sm" onclick="filterDepo(75, false)">🚨 %75+ Efsane</button>
          <button class="btn btn-red-sm btn-sm" onclick="filterDepo(80, false)">💎 %80–%90</button>
          <button class="btn btn-blue btn-sm" onclick="filterDepo(0, true)">✅ Sadece OID Sepet</button>
          <button class="btn btn-dark btn-sm" onclick="api('/api/depo/clear', {{}})">🧹 Temizle</button>
        </div>
        <input id="depo_q" type="text" placeholder="🔍 Ürün adı veya ASIN filtrele..." oninput="searchDepoCards(this.value)" style="margin-bottom:10px" />
        <div id="depo_list">
          {''.join(depo_cards_html) or '<p class="muted">Şu an listelenen ürün yok. Yukarıdan <b>🔎 Şimdi Tara</b> butonuna basın veya 15-30 sn bekleyin!</p>'}
        </div>
      </div>
    </div>

    <!-- SEKME 2: ÖZEL ASIN NÖBETÇİSİ & CANLI SORGULAMA -->
    <div id="tab-watch" class="tab-pane">
      <div class="card">
        <div class="card-head"><h2>🔍 Anlık ASIN / Link Depo &amp; OID Sepet Kodu Sorgula</h2></div>
        <label>Amazon Ürün Linki veya 10 Haneli ASIN (Örn: B0D92CF1P5):</label>
        <input id="ins_in" type="text" placeholder="https://www.amazon.com.tr/dp/B0D92CF1P5 veya B0D92CF1P5" />
        <button class="btn btn-orange" onclick="inspectAsinNow(val('ins_in'))">⚡ Canlı AOD Kontrol Et &amp; Sepet Linki Üret</button>
        <div id="ins_res" style="margin-top:10px"></div>
      </div>

      <div class="card">
        <div class="card-head"><h2>🎯 7/24 Özel ASIN Nöbet Listesi (Arama Önbelleğini Beklemeden Yakalar)</h2></div>
        <p class="muted">Buraya eklediğiniz ASIN'ler <b>her {config.depo_interval_seconds} saniyede bir</b> doğrudan Amazon AOD teklif motorundan kontrol edilir; Depo'ya düştüğü ilk saniyede <b>OfferListingId</b> sepet butonuyla telefonunuza bildirim gelir!</p>
        <div class="row2">
          <div><label>ASIN veya Amazon Linki</label><input id="wl_asin" type="text" placeholder="B0D92CF1P5" /></div>
          <div><label>Ürün Notu (Opsiyonel)</label><input id="wl_note" type="text" placeholder="Örn: Karcher K4 / Sony XM5" /></div>
          <div><label>Maks. Hedef Fiyat (TL, 0=Yüzdeye Göre)</label><input id="wl_tp" type="number" value="0" /></div>
          <div><label>Min. İndirim (%)</label><input id="wl_pct" type="number" value="25" /></div>
        </div>
        <button class="btn btn-green" onclick="addWatchlist()">➕ Nöbet Listesine Ekle</button>
        <div style="margin-top:12px">
          {''.join(wl_rows_html) or '<p class="muted">Henüz özel nöbet ASIN eklenmedi.</p>'}
        </div>
      </div>
    </div>

    <!-- SEKME 3: RADAR, KATEGORİ, KELİME & ALARM AYARLARI -->
    <div id="tab-settings" class="tab-pane">
      <div class="card">
        <div class="card-head">
          <h2>⚙️ Amazon Depo Keskin Nişancı &amp; Alarm Ayarları</h2>
          <button class="btn btn-amber btn-sm" onclick="api('/api/alarm/test', {{}}, false)">🔔 Alarmı Test Et</button>
        </div>
        <label class="chk"><input type="checkbox" id="dp_en" {'checked' if config.depo_enabled else ''} /> <b>7/24 Amazon Depo Taraması Aktif</b></label>
        <label class="chk"><input type="checkbox" id="dp_admin" {'checked' if config.depo_auto_notify_admin else ''} /> Süper İndirim &amp; Nöbet Yakalamalarında <b>Kişisel Telegram'ıma Anında Acil Bildirim + Sepet Butonu Gönder</b></label>
        <label class="chk"><input type="checkbox" id="dp_ch" {'checked' if config.depo_auto_publish_channel else ''} /> Süper İndirimleri <b>{html.escape(config.telegram_channel_id)} Kanalına Otomatik Yayınla</b></label>

        <div class="row2">
          <div><label>⚡ Tarama Hızı (Saniye, min 15)</label><input id="dp_sec" type="number" min="15" value="{config.depo_interval_seconds}" /></div>
          <div><label>Listeleme Min. İndirim (%)</label><input id="dp_minpct" type="number" value="{config.depo_min_discount_percent}" /></div>
          <div><label>🚨 Acil Alarm / Sesli Arama Eşiği (%)</label><input id="dp_superpct" type="number" value="{config.depo_super_discount_percent}" /></div>
        </div>
        <div class="row2">
          <div><label>Min. Depo Fiyatı (TL)</label><input id="dp_minp" type="number" value="{int(config.depo_min_price_tl)}" /></div>
          <div><label>Maks. Depo Fiyatı (TL)</label><input id="dp_maxp" type="number" value="{int(config.depo_max_price_tl)}" /></div>
          <div><label>CallMeBot Sesli Arama (@kullanici)</label><input id="dp_call" type="text" value="{html.escape(config.callmebot_telegram_user)}" placeholder="@kullanici_adi" /></div>
        </div>

        <label>🔍 Özel Depo Arama Kelimeleri (Virgülle ayırın — örn: ekran kartı, monitör, kulaklık, robot süpürge, kahve makinesi):</label>
        <input id="dp_kws" type="text" value="{html.escape(', '.join(config.depo_keywords))}" placeholder="kulaklık, robot süpürge, kahve makinesi, monitör, ssd" />

        <h3>📂 Taranacak Mağaza Kaynakları / Amazon Depo Kategorileri</h3>
        <div class="cats-grid">{''.join(cat_boxes)}</div>
        <button class="btn btn-green" onclick="saveDepoSettings()">💾 Tüm Radar Ayarlarını Kaydet</button>
      </div>

      <div class="card">
        <div class="card-head">
          <h2>⚡ Doğrudan Ödeme Ekranına Geçiş (Sepete Ekle → Alışverişi Tamamla)</h2>
          <a class="btn btn-orange btn-sm" href="/amazon-depo-fast.user.js" target="_blank">⬇️ Hızlı Geçiş Betiğini Yükle (.user.js)</a>
        </div>
        <p class="muted" style="line-height:1.5">
          Amazon hesabınızın şifresini paylaşmadan, kendi açık tarayıcınızda <b>"Sepete Ekle → Alışverişi Tamamla"</b> adımlarını otomatik geçip sizi doğrudan <b>Ödeme Ekranına</b> ulaştırır (son satın alma onayına asla basmaz):
          <br/>• <b>Bilgisayar (Chrome/Edge):</b> <b>Tampermonkey</b> eklentisini kurup yukarıdaki turuncu butona tıklayın ve "Kur" deyin.
          <br/>• <b>iPhone (Safari):</b> App Store'dan ücretsiz <b>Userscripts</b> uygulamasını açıp betiği ekleyin.
          <br/>• Kurulumdan sonra panelden veya Telegram'dan <b>⚡ Depo Teklifini Aç &amp; Al</b> linkine bastığınızda otomatik olarak ödeme ekranına geçersiniz!
        </p>
      </div>
    </div>

    <!-- SEKME 4: BAĞLANTILAR, GÜVENLİK & LOGLAR -->
    <div id="tab-system" class="tab-pane">
      <div class="card">
        <div class="card-head">
          <h2>🔑 Telegram, Affiliate (/api/affiliate/save) &amp; Panel Güvenliği</h2>
          <a class="btn btn-green btn-sm" href="/backup.db">💾 Veritabanı Yedeği (.db)</a>
        </div>
        <div class="row2">
          <div><label>Telegram Bot Token</label><input id="in_tok" type="password" value="{html.escape(_mask_secret(config.telegram_bot_token))}" /></div>
          <div><label>Kişisel Chat ID</label><input id="in_cid" type="text" value="{html.escape(config.telegram_chat_id)}" /></div>
          <div><label>Kanal ID (/api/channel/send-post)</label><input id="in_chid" type="text" value="{html.escape(config.telegram_channel_id)}" /></div>
          <div><label>Amazon Associate Tag</label><input id="in_tag" type="text" value="{html.escape(config.amazon_associate_tag)}" placeholder="ornek-21" /></div>
        </div>
        <label>Panel Giriş Şifresi (Değiştirmek istemiyorsanız dokunmayın):</label>
        <input id="in_pwd" type="password" value="{html.escape(_mask_secret(config.dashboard_password))}" placeholder="Yeni panel şifresi | C:\\path\\to\\ssh-key.key" />
        <button class="btn btn-green" onclick="saveIntegrations()">💾 Bağlantı &amp; Güvenlik Ayarlarını Kaydet</button>
      </div>

      <div class="card">
        <div class="card-head">
          <h2>📜 Canlı Amazon Depo Sniper Logları</h2>
          <div class="actions">
            <button class="btn btn-blue btn-sm" onclick="loadLogs()">🔄 Yenile</button>
            <button class="btn btn-dark btn-sm" onclick="api('/api/logs/clear', {{}}, false).then(loadLogs)">🧹 Temizle</button>
          </div>
        </div>
        <pre id="log_viewer" class="codebox">Loglar yükleniyor...</pre>
      </div>
      <div style="display:none">{kw_hidden_compat}</div>
    </div>
  </div>

  <script>
    const val = (id) => (document.getElementById(id) ? document.getElementById(id).value : '');
    const chk = (id) => (document.getElementById(id) ? document.getElementById(id).checked : false);
    let _toastTimer = null;

    function switchTab(tabId) {{
      document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      const pane = document.getElementById(tabId);
      const btn = document.querySelector(`.tab-btn[data-tab="${{tabId}}"]`);
      if (pane) pane.classList.add('active');
      if (btn) btn.classList.add('active');
      localStorage.setItem('depo_tab', tabId);
      if (tabId === 'tab-system') loadLogs();
    }}
    (function() {{
      const s = localStorage.getItem('depo_tab');
      if (s && document.getElementById(s)) switchTab(s);
    }})();

    function setStatus(msg, ok=true, show=true) {{
      const b = document.getElementById('status-bar');
      b.textContent = msg;
      b.style.borderColor = ok ? '#fb923c' : '#ef4444';
      b.style.display = show ? 'block' : 'none';
    }}
    function toast(msg, ok=true) {{
      const t = document.getElementById('toast');
      t.textContent = msg;
      t.style.borderColor = ok ? '#22c55e' : '#ef4444';
      t.style.display = 'block';
      if (_toastTimer) clearTimeout(_toastTimer);
      _toastTimer = setTimeout(() => {{ t.style.display = 'none'; }}, 5500);
    }}

    function pollScanUntilDone() {{
      setStatus('⏳ Amazon Depo taranıyor ve AOD OfferListingId kodları doğrulanıyor...', true, true);
      const iv = setInterval(async () => {{
        try {{
          const r = await fetch('/api/scan-status');
          const s = await r.json();
          if (!s.running) {{
            clearInterval(iv);
            toast(s.last_msg || 'Tamamlandı!', true);
            setTimeout(() => location.reload(), 700);
          }}
        }} catch (e) {{}}
      }}, 1800);
    }}

    async function api(url, body, reloadAfter=true) {{
      setStatus('⏳ İşleniyor...', true, true);
      try {{
        const res = await fetch(url, {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify(body || {{}})
        }});
        const j = await res.json();
        toast(j.msg || (j.ok ? 'Tamam' : 'Hata'), !!j.ok);
        setStatus((j.ok ? '✅ ' : '❌ ') + (j.msg || ''), !!j.ok, true);
        if ((url === '/api/depo/scan' || url === '/api/scan-now') && j.ok) {{
          pollScanUntilDone();
          return j;
        }}
        if (j.ok && reloadAfter) setTimeout(() => location.reload(), 750);
        return j;
      }} catch (err) {{
        toast('Bağlantı hatası: ' + err, false);
        return {{ok: false}};
      }}
    }}

    function filterDepo(minPct, onlyOid) {{
      document.querySelectorAll('.depo-card').forEach(el => {{
        const p = Number(el.dataset.pct || 0);
        const oid = Number(el.dataset.oid || 0);
        const show = p >= minPct && (!onlyOid || oid === 1);
        el.style.display = show ? 'flex' : 'none';
      }});
    }}

    function searchDepoCards(q) {{
      const k = (q || '').toLowerCase().trim();
      document.querySelectorAll('.depo-card').forEach(el => {{
        el.style.display = (!k || (el.dataset.q || '').includes(k)) ? 'flex' : 'none';
      }});
    }}

    function publishDepo(asin) {{ api('/api/depo/publish', {{asin}}, false); }}
    function deleteDepo(asin) {{ api('/api/depo/delete', {{asin}}); }}

    async function inspectAsinNow(inp) {{
      const j = await api('/api/depo/inspect-asin', {{input: inp}}, false);
      const box = document.getElementById('ins_res');
      if (!box || !j) return;
      if (!j.in_depo) {{
        box.innerHTML = `<div class='cond-note' style='color:#fca5a5'>${{j.msg || 'Depo stoğu yok.'}}</div>`;
        return;
      }}
      const d = j.deal;
      box.innerHTML = `<div class='depo-card' style='margin-top:8px'>
        <div><span class='pct-badge' style='background:#16a34a'>-%${{d.discount_percent}}</span> <b>${{d.title}}</b></div>
        <div class='price-line'><span class='depo-p'>${{d.depo_price_fmt}}</span> <s class='mkt-p'>${{d.market_price_fmt}}</s> <span class='tag'>${{d.condition}}</span></div>
        <div class='depo-btns'>
          <a class='btn btn-cart' href='${{d.cart_url}}' target='_blank'>⚡ 1 Tıkla Depo Adetini Sepete Ekle</a>
          <a class='btn btn-dark' href='${{d.url}}' target='_blank'>🛒 Depo Sayfası</a>
        </div>
      </div>`;
    }}

    function addWatchlist() {{
      api('/api/depo/watchlist/add', {{
        input: val('wl_asin'),
        note: val('wl_note'),
        target_price_tl: Number(val('wl_tp')),
        min_discount_percent: Number(val('wl_pct'))
      }});
    }}
    function removeWatchlist(asin) {{ api('/api/depo/watchlist/delete', {{asin}}); }}

    function saveDepoSettings() {{
      const cats = [];
      document.querySelectorAll('.depo-cat-chk:checked').forEach(el => cats.push(el.dataset.cat));
      api('/api/depo/settings', {{
        depo_enabled: chk('dp_en'),
        depo_interval_seconds: Number(val('dp_sec')),
        depo_min_discount_percent: Number(val('dp_minpct')),
        depo_super_discount_percent: Number(val('dp_superpct')),
        depo_min_price_tl: Number(val('dp_minp')),
        depo_max_price_tl: Number(val('dp_maxp')),
        depo_auto_notify_admin: chk('dp_admin'),
        depo_auto_publish_channel: chk('dp_ch'),
        callmebot_telegram_user: val('dp_call'),
        depo_keywords_csv: val('dp_kws'),
        depo_categories: cats
      }});
    }}

    function saveIntegrations() {{
      api('/api/integrations/save', {{
        telegram_bot_token: val('in_tok'),
        telegram_chat_id: val('in_cid'),
        telegram_channel_id: val('in_chid'),
        amazon_tag: val('in_tag'),
        dashboard_password: val('in_pwd')
      }});
    }}

    async function loadLogs() {{
      try {{
        const r = await fetch('/api/logs?lines=120');
        const j = await r.json();
        const v = document.getElementById('log_viewer');
        if (v) {{ v.textContent = j.logs || ''; v.scrollTop = v.scrollHeight; }}
      }} catch (e) {{}}
    }}

    // Arka plan taraması tamamlandığında açık web panelini otomatik yenile (kullanıcı yazı yazmıyorsa)
    let _knownCycleTs = null;
    setInterval(async () => {{
      try {{
        const r = await fetch('/api/scan-status');
        const s = await r.json();
        if (!s.last_cycle_ts) return;
        if (_knownCycleTs === null) {{
          _knownCycleTs = s.last_cycle_ts;
          return;
        }}
        if (s.last_cycle_ts !== _knownCycleTs && !s.running) {{
          const activeEl = document.activeElement;
          const isTyping = activeEl && ['INPUT', 'TEXTAREA', 'SELECT'].includes(activeEl.tagName);
          if (!isTyping) location.reload();
        }}
      }} catch (e) {{}}
    }}, 5000);
  </script>
</body>
</html>"""

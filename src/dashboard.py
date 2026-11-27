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

import requests

from src.amazon_depo import DEPO_CATEGORY_FEEDS
from src.charts import render_price_chart_png
from src.community_deals import decode_onual_coupon_param
from src.config import AppConfig
from src.exporter import generate_csv_report_bytes
from src.habit_analyzer import analyze_shopping_habits, apply_habit_recommendations
from src.keyword_search import search_product_across_sites
from src.market_intel import MarketIntelligenceHunter, predict_upcoming_campaigns
from src.notifier import TelegramNotifier, format_tl
from src.oracle_deployer import (
    build_oracle_bundle_zip,
    build_sqlite_backup_bytes,
    deploy_to_oracle_ssh,
    install_detached_windows_task,
)
from src.popular_deals import (
    PopularDeal,
    PopularDealsHunter,
    _enrich_deal_insights,
    canonicalize_deal_key,
    fetch_realtime_market_avg_price,
    verify_coupon_live,
)
from src.storage import PriceStorage
from src.url_tracker import fetch_watched_product
from src.wakeup import get_dynamic_interval_minutes

logger = logging.getLogger(__name__)

_BOOT_TS = time.time()
_ALL_KNOWN_SOURCES = (
    ("epey", "Epey"),
    ("akakce", "Akakçe"),
    ("cimri", "Cimri"),
    ("amazon_tr", "Amazon.com.tr"),
    ("hepsiburada", "Hepsiburada"),
    ("trendyol", "Trendyol"),
    ("vatan", "Vatan Bilgisayar"),
    ("teknosa", "Teknosa"),
    ("mediamarkt", "MediaMarkt"),
    ("samsung_tr", "Samsung Shop TR"),
    ("n11", "n11"),
    ("pazarama", "Pazarama"),
)


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

            # 1) Cookie kontrolü
            raw_cookie = self.headers.get("Cookie", "")
            if raw_cookie:
                c = SimpleCookie()
                try:
                    c.load(raw_cookie)
                    if "ft_admin" in c and hmac.compare_digest(c["ft_admin"].value, expected):
                        return True
                except Exception:
                    pass

            # 2) X-Admin-Password başlığı
            hdr_pwd = (self.headers.get("X-Admin-Password") or "").strip()
            if hdr_pwd and hmac.compare_digest(hdr_pwd, pwd):
                return True

            # 3) HTTP Basic Auth
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

            if parsed.path == "/export.csv":
                data = generate_csv_report_bytes(storage)
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="firsat_takip_raporu.csv"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

            if parsed.path == "/backup.db":
                db_bytes = build_sqlite_backup_bytes(config.database_path)
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", 'attachment; filename="prices_backup.db"')
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

            if parsed.path == "/chart":
                variant = (qs.get("variant") or ["256 GB"])[0]
                points = storage.get_variant_price_series(variant, days=config.six_month_days)
                target = config.target_prices_tl.get(variant)
                six_m = storage.get_six_month_low(variant, days=config.six_month_days)
                png = render_price_chart_png(
                    title=f"{config.product_name} ({variant}) Fiyat Geçmişi",
                    points=points,
                    target_price_tl=target,
                    six_month_low_tl=six_m,
                )
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(png)))
                self.end_headers()
                self.wfile.write(png)
                return

            if parsed.path == "/chart-url":
                wid = int((qs.get("id") or ["0"])[0] or 0)
                witem = storage.get_watched_url(wid) or {"title": f"Ürün #{wid}", "target_price_tl": None}
                points = storage.get_watched_url_price_series(wid, days=config.six_month_days)
                six_m = storage.get_watched_url_six_month_low(wid, days=config.six_month_days)
                t_val = float(witem["target_price_tl"]) if witem.get("target_price_tl") else None
                png = render_price_chart_png(
                    title=f"#{wid} {str(witem.get('title') or '')[:42]}",
                    points=points,
                    target_price_tl=t_val,
                    six_month_low_tl=six_m,
                )
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(png)))
                self.end_headers()
                self.wfile.write(png)
                return

            if parsed.path == "/api/channel/stats":
                self._send_json(200, _fetch_channel_stats(config, notifier))
                return

            if parsed.path == "/api/scan-status":
                self._send_json(200, {**scan_state, "daemon_paused": config.daemon_paused})
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
                self._send_json(401, {"ok": False, "msg": "Oturum süresi doldu veya yetkisiz erişim."})
                return

            try:
                # --- 1. DAEMON & TARAMA KONTROLLERİ ---
                if path == "/api/daemon/toggle":
                    new_paused = bool(data.get("paused", not config.daemon_paused))
                    config.daemon_paused = new_paused
                    config.raw_yaml.setdefault("schedule", {})["daemon_paused"] = new_paused
                    config.persist_yaml()
                    msg = (
                        "⏸ Otomatik arka plan taraması duraklatıldı (panel açık kalmaya devam eder)."
                        if new_paused
                        else "▶️ Otomatik arka plan taraması yeniden başlatıldı!"
                    )
                    self._send_json(200, {"ok": True, "paused": new_paused, "msg": msg})
                    return

                if path == "/api/scan-now":
                    if runner is None:
                        self._send_json(400, {"ok": False, "msg": "Tarayıcı bağlı değil."})
                        return
                    if scan_state["running"]:
                        self._send_json(200, {"ok": True, "running": True, "msg": "Tarama zaten devam ediyor..."})
                        return
                    force_sum = bool(data.get("force_summary", False))
                    scan_state["running"] = True
                    scan_state["started_at"] = datetime.now().strftime("%H:%M:%S")
                    scan_state["last_msg"] = "Tüm kaynaklar taranıyor..."

                    def _bg_scan() -> None:
                        try:
                            with lock:
                                res = runner.run_once(force_summary=force_sum)
                            scan_state["last_msg"] = (
                                f"Tarama tamamlandı ({res.sources_ok} kaynak OK, {len(res.offers)} teklif, {len(res.alerts)} alarm)."
                            )
                        except Exception as exc:
                            scan_state["last_msg"] = f"Tarama hatası: {exc}"
                        finally:
                            scan_state["running"] = False
                            scan_state["last_finished_at"] = datetime.now().strftime("%H:%M:%S")

                    threading.Thread(target=_bg_scan, daemon=True).start()
                    self._send_json(200, {"ok": True, "running": True, "msg": "Tam tarama başlatıldı! Bitince sayfa yenilenecek..."})
                    return

                if path == "/api/scan/community-now":
                    if runner is None:
                        self._send_json(400, {"ok": False, "msg": "Runner yok."})
                        return
                    runner._check_community_deals()
                    self._send_json(200, {"ok": True, "msg": "Topluluk & OnuAl / Sıcak Fırsat taraması tamamlandı!"})
                    return

                if path == "/api/scan/bestsellers-now":
                    if runner is None:
                        self._send_json(400, {"ok": False, "msg": "Runner yok."})
                        return
                    runner._check_bestseller_drops()
                    self._send_json(200, {"ok": True, "msg": "Çok satanlar & %60+ fiyat hatası taraması tamamlandı!"})
                    return

                if path == "/api/depo/scan":
                    if runner is None:
                        self._send_json(400, {"ok": False, "msg": "Runner yok."})
                        return
                    deals = runner.scan_amazon_depo(force=True)
                    top_txt = f" En yüksek: -%{deals[0].discount_percent} ({deals[0].title[:35]})" if deals else ""
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "count": len(deals),
                            "msg": f"Amazon Depo taraması bitti: {len(deals)} fırsat bulundu!{top_txt}",
                        },
                    )
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
                    self._send_json(200, {"ok": True, "msg": "Amazon Depo özel radar ayarları kaydedildi!"})
                    return

                if path == "/api/depo/publish":
                    if runner is None:
                        self._send_json(400, {"ok": False, "msg": "Runner yok."})
                        return
                    asin = str(data.get("asin", "")).strip().upper()
                    row = storage.get_depo_deal(asin)
                    if not row:
                        self._send_json(400, {"ok": False, "msg": "Depo ürünü bulunamadı."})
                        return
                    d_url = str(row["url"])
                    canon = canonicalize_deal_key(d_url)
                    storage.delete_published_channel_deal(canon)
                    pop = PopularDeal(
                        deal_key=canon,
                        title=f"[Amazon Depo] {row['title']}",
                        seller="Amazon.com.tr Depo",
                        price_tl=float(row["depo_price_tl"]),
                        previous_or_second_price_tl=float(row["market_price_tl"]),
                        discount_percent=int(row["discount_percent"]),
                        direct_url=d_url,
                        image_url=str(row.get("image_url") or ""),
                        category=f"Amazon Depo • {row.get('category') or ''}",
                        is_official_seller=True,
                        badge_label=f"🚨📦 AMAZON DEPO -%{int(row['discount_percent'])} FIRSATI!",
                        market_avg_price_tl=float(row["market_price_tl"]),
                        is_glitch=(int(row["discount_percent"]) >= 65),
                    )
                    _enrich_deal_insights(pop)
                    ok_p = runner.channel_publisher.publish_popular_deal(pop)
                    self._send_json(
                        200 if ok_p else 400,
                        {"ok": ok_p, "msg": f"🎉 Amazon Depo ürünü kanala yayınlandı!" if ok_p else "Yayınlanamadı (fotoğraf veya kanal yetkisini kontrol edin)."},
                    )
                    return

                if path == "/api/depo/clear":
                    cnt = storage.clear_depo_deals()
                    self._send_json(200, {"ok": True, "msg": f"{cnt} Amazon Depo kaydı temizlendi."})
                    return

                if path == "/api/depo/delete":
                    asin = str(data.get("asin", "")).strip().upper()
                    ok = storage.delete_depo_deal(asin)
                    self._send_json(200, {"ok": ok, "msg": "Ürün panelden kaldırıldı." if ok else "Bulunamadı."})
                    return

                if path == "/api/summary/send-now":
                    if notifier is None or not notifier.is_configured:
                        self._send_json(400, {"ok": False, "msg": "Telegram ayarlı değil."})
                        return
                    grouped = storage.get_latest_offers_by_variant(config.top_offers_per_variant)
                    if not grouped:
                        self._send_json(400, {"ok": False, "msg": "Henüz kayıtlı teklif yok, önce tarama başlatın."})
                        return
                    msg_html = notifier.build_summary_message(
                        grouped,
                        sources_ok=sum(1 for v in config.sources.values() if v),
                        sources_total=len(config.sources),
                        paused_variants=storage.get_paused_variants(),
                    )
                    sent = notifier.send_html(
                        msg_html,
                        reply_markup=notifier.build_main_menu_keyboard(only_official=storage.is_only_official_sellers()),
                    )
                    self._send_json(200 if sent else 400, {"ok": sent, "msg": "Fiyat özeti Telegram'a gönderildi!" if sent else "Gönderilemedi."})
                    return

                # --- 2. ALARM KONTROLLERİ ---
                if path == "/api/alarm/test":
                    if wakeup is None:
                        self._send_json(400, {"ok": False, "msg": "Alarm yöneticisi bağlı değil."})
                        return
                    wakeup.trigger(
                        summary_html="🔔 <b>Yönetim Panelinden Test Alarmı!</b>",
                        voice_text="Yönetim panelinden test alarmı başlatıldı.",
                        force=True,
                    )
                    self._send_json(200, {"ok": True, "msg": "Test alarmı ve sesli arama tetiklendi!"})
                    return

                if path == "/api/alarm/stop":
                    if wakeup:
                        wakeup.stop_alarm()
                    self._send_json(200, {"ok": True, "msg": "Alarm susturuldu."})
                    return

                # --- 3. ÜRÜN, VARYANT, HEDEF & ALARM EŞİKLERİ ---
                if path == "/api/targets":
                    for v in config.variants:
                        if v in data and str(data[v]).strip():
                            config.save_target_price(v, float(data[v]))
                    self._send_json(200, {"ok": True, "msg": "Hedef fiyatlar kaydedildi."})
                    return

                if path == "/api/product-alerts/settings":
                    p_name = str(data.get("product_name", config.product_name)).strip()
                    raw_vars = str(data.get("variants_csv", "")).strip()
                    if raw_vars:
                        parsed_vars = [v.strip() for v in raw_vars.split(",") if v.strip()]
                        if parsed_vars:
                            config.variants = parsed_vars
                    if p_name:
                        config.product_name = p_name
                    config.min_valid_price_tl = max(0.0, float(data.get("min_valid_price_tl", config.min_valid_price_tl)))
                    config.max_valid_price_tl = max(100.0, float(data.get("max_valid_price_tl", config.max_valid_price_tl)))
                    config.exclude_refurbished = bool(data.get("exclude_refurbished", config.exclude_refurbished))
                    config.min_drop_tl = max(0.0, float(data.get("min_drop_tl", config.min_drop_tl)))
                    config.min_drop_percent = max(0.1, float(data.get("min_drop_percent", config.min_drop_percent)))
                    config.below_market_percent = max(0.5, float(data.get("below_market_percent", config.below_market_percent)))
                    config.six_month_days = max(7, int(data.get("six_month_days", config.six_month_days)))
                    config.cooldown_hours = max(1, int(data.get("cooldown_hours", config.cooldown_hours)))

                    targets_in = data.get("targets") or {}
                    baselines_in = data.get("baselines") or {}
                    for v in config.variants:
                        if v in targets_in and str(targets_in[v]).strip() != "":
                            config.target_prices_tl[v] = float(targets_in[v])
                        if v in baselines_in and str(baselines_in[v]).strip() != "":
                            bval = float(baselines_in[v])
                            if bval > 0:
                                config.six_month_baseline_tl[v] = bval
                            else:
                                config.six_month_baseline_tl.pop(v, None)

                    config.bestseller_tracker_enabled = bool(data.get("bestseller_enabled", config.bestseller_tracker_enabled))
                    config.bestseller_compare_days = max(7, int(data.get("bestseller_compare_days", config.bestseller_compare_days)))
                    config.bestseller_min_drop_percent = max(1.0, float(data.get("bestseller_min_drop_percent", config.bestseller_min_drop_percent)))
                    config.bestseller_min_drop_tl = max(0.0, float(data.get("bestseller_min_drop_tl", config.bestseller_min_drop_tl)))

                    prod_sec = config.raw_yaml.setdefault("product", {})
                    prod_sec["name"] = config.product_name
                    prod_sec["variants"] = list(config.variants)
                    prod_sec["min_valid_price_tl"] = int(config.min_valid_price_tl)
                    prod_sec["max_valid_price_tl"] = int(config.max_valid_price_tl)
                    prod_sec["exclude_refurbished"] = config.exclude_refurbished

                    al_sec = config.raw_yaml.setdefault("alerts", {})
                    al_sec["target_prices_tl"] = {k: int(v) if float(v).is_integer() else float(v) for k, v in config.target_prices_tl.items()}
                    al_sec["six_month_baseline_tl"] = {v: int(config.six_month_baseline_tl.get(v, 0)) for v in config.variants}
                    al_sec["min_drop_tl"] = float(config.min_drop_tl)
                    al_sec["min_drop_percent"] = float(config.min_drop_percent)
                    al_sec["below_market_percent"] = float(config.below_market_percent)
                    al_sec["six_month_days"] = int(config.six_month_days)
                    al_sec["cooldown_hours"] = int(config.cooldown_hours)

                    bs_sec = config.raw_yaml.setdefault("bestseller_tracker", {})
                    bs_sec["enabled"] = config.bestseller_tracker_enabled
                    bs_sec["compare_window_days"] = config.bestseller_compare_days
                    bs_sec["min_drop_percent"] = config.bestseller_min_drop_percent
                    bs_sec["min_drop_tl"] = config.bestseller_min_drop_tl

                    if "only_official" in data:
                        storage.set_only_official_sellers(bool(data["only_official"]))

                    config.persist_yaml()
                    self._send_json(200, {"ok": True, "msg": "Ürün, hedef fiyatlar, alarm eşikleri ve çok satan ayarları kaydedildi!"})
                    return

                if path == "/api/tracking/toggle":
                    variant = str(data.get("variant", ""))
                    action = str(data.get("action", ""))
                    if variant == "ALL":
                        if action == "pause":
                            storage.pause_variant("ALL")
                        else:
                            storage.resume_variant("ALL")
                            for v in config.variants:
                                storage.resume_variant(v)
                    elif variant in config.variants:
                        if action == "pause":
                            storage.pause_variant(variant)
                        else:
                            storage.resume_variant("ALL")
                            storage.resume_variant(variant)
                    self._send_json(200, {"ok": True, "msg": "Takip durumu güncellendi."})
                    return

                if path == "/api/tracking/official":
                    enabled = bool(data.get("enabled", False))
                    storage.set_only_official_sellers(enabled)
                    self._send_json(200, {"ok": True, "msg": f"Resmi satıcı filtresi: {'AÇIK' if enabled else 'KAPALI'}"})
                    return

                # --- 4. ÖZEL LİNK & KELİME TAKİBİ ---
                if path == "/api/urls/add":
                    url = str(data.get("url", "")).strip()
                    target_raw = data.get("target_price_tl")
                    target_p = float(target_raw) if target_raw not in (None, "", 0, "0") else None
                    if not url.startswith("http") or runner is None:
                        self._send_json(400, {"ok": False, "msg": "Geçerli bir http/https linki girin."})
                        return
                    snap = fetch_watched_product(url, runner.fetcher, storage=storage, allow_fallback=True)
                    wid, _ = storage.add_or_update_watched_url(
                        url=snap.best_url,
                        title=snap.title,
                        seller=snap.seller,
                        price_tl=snap.effective_price_tl,
                        target_price_tl=target_p,
                        in_stock=snap.in_stock,
                    )
                    p_info = format_tl(snap.effective_price_tl) if snap.effective_price_tl > 0 else "İlk taramada fiyat çekilecek"
                    self._send_json(200, {"ok": True, "msg": f"#{wid} takibe eklendi: {snap.title[:60]} ({p_info})"})
                    return

                if path == "/api/urls/update-target":
                    wid = int(data.get("id", 0))
                    t_val = float(data.get("target_price_tl") or 0)
                    if wid <= 0 or t_val <= 0:
                        self._send_json(400, {"ok": False, "msg": "Geçerli bir hedef fiyat girin."})
                        return
                    ok = storage.set_watched_url_target(wid, t_val)
                    self._send_json(200 if ok else 400, {"ok": ok, "msg": f"#{wid} hedef fiyatı {format_tl(t_val)} yapıldı!" if ok else "Kayıt bulunamadı."})
                    return

                if path == "/api/urls/refresh":
                    wid = int(data.get("id", 0))
                    item = storage.get_watched_url(wid)
                    if not item or runner is None:
                        self._send_json(400, {"ok": False, "msg": "Ürün bulunamadı."})
                        return
                    snap = fetch_watched_product(str(item["url"]), runner.fetcher, storage=storage)
                    storage.update_watched_url_price(
                        watch_id=wid,
                        title=snap.title,
                        price_tl=snap.price_tl,
                        in_stock=snap.in_stock,
                        effective_price_tl=snap.effective_price_tl,
                        coupon_info=snap.coupon_info,
                        coupon_code=snap.coupon_code,
                    )
                    self._send_json(200, {"ok": True, "msg": f"#{wid} güncellendi: {format_tl(snap.effective_price_tl)}"})
                    return

                if path == "/api/urls/delete":
                    wid = int(data.get("id", 0))
                    ok = storage.remove_watched_url(wid)
                    self._send_json(200, {"ok": ok, "msg": f"#{wid} silindi." if ok else "Bulunamadı."})
                    return

                if path == "/api/keywords/add":
                    q = str(data.get("query", "")).strip()
                    t_raw = data.get("target_price_tl")
                    t_p = float(t_raw) if t_raw not in (None, "", 0, "0") else None
                    if len(q) < 2 or runner is None:
                        self._send_json(400, {"ok": False, "msg": "Ürün adı en az 2 karakter olmalı."})
                        return
                    offers = search_product_across_sites(q, runner.fetcher, max_results=3)
                    best = offers[0] if offers else None
                    kid, _ = storage.add_or_update_watched_keyword(
                        q,
                        best.effective_price_tl if best else 0.0,
                        best.seller if best else "-",
                        best.url if best else "",
                        target_price_tl=t_p,
                    )
                    self._send_json(200, {"ok": True, "msg": f"Kelime takibi #{kid} eklendi: {q}"})
                    return

                if path == "/api/keywords/update-target":
                    kid = int(data.get("id", 0))
                    t_raw = data.get("target_price_tl")
                    t_p = float(t_raw) if t_raw not in (None, "", 0, "0") else None
                    ok = storage.set_watched_keyword_target(kid, t_p)
                    self._send_json(200 if ok else 400, {"ok": ok, "msg": f"Kelime #{kid} hedef fiyatı güncellendi!"})
                    return

                if path == "/api/keywords/search-now":
                    q = str(data.get("query", "")).strip()
                    if len(q) < 2 or runner is None:
                        self._send_json(400, {"ok": False, "msg": "Arama kelimesi girin."})
                        return
                    offers = search_product_across_sites(q, runner.fetcher, max_results=8)
                    out = [
                        {
                            "title": o.title,
                            "seller": o.seller,
                            "price_tl": o.effective_price_tl,
                            "price_fmt": format_tl(o.effective_price_tl),
                            "url": o.url,
                            "coupon": o.coupon_code or o.coupon_info,
                        }
                        for o in offers
                    ]
                    self._send_json(200, {"ok": True, "results": out, "msg": f"'{q}' için {len(out)} mağaza teklifi bulundu."})
                    return

                if path == "/api/keywords/delete":
                    kid = int(data.get("id", 0))
                    ok = storage.remove_watched_keyword(kid)
                    self._send_json(200, {"ok": ok, "msg": f"Kelime #{kid} silindi."})
                    return

                # --- 5. KANAL STÜDYOSU, KUPON DOĞRULAMA & ÖZEL ÜRÜN PAYLAŞIMI ---
                if path == "/api/channel/verify-coupon":
                    raw_code = str(data.get("coupon_code", "")).strip()
                    url = str(data.get("url", "")).strip()
                    if not raw_code or runner is None:
                        self._send_json(400, {"ok": False, "msg": "Test edilecek kupon kodunu girin."})
                        return
                    decoded_code, howto = decode_onual_coupon_param(raw_code)
                    if howto and not decoded_code:
                        self._send_json(
                            200,
                            {
                                "ok": True,
                                "valid": True,
                                "coupon_code": "",
                                "coupon_info": howto,
                                "msg": f"ℹ️ '{raw_code}' yazılı bir kod değil, mağaza içi kupon talimatıdır: {howto}",
                            },
                        )
                        return
                    ok_c, v_code, v_info = verify_coupon_live(raw_code, "", url, runner.fetcher)
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "valid": ok_c,
                            "coupon_code": v_code,
                            "coupon_info": v_info,
                            "msg": (
                                f"✅ KUPON DOĞRULANDI: '{v_code}' aktif görünüyor ve gönderiye güvenle eklenebilir!"
                                if ok_c
                                else f"❌ KUPON DOĞRULANAMADI: '{raw_code}' ürün/kampanya sayfasında veya aktif kod listelerinde bulunamadı."
                            ),
                        },
                    )
                    return

                if path == "/api/channel/preview-url":
                    url = str(data.get("url", "")).strip()
                    raw_coupon = str(data.get("coupon_code", "")).strip()
                    if not url.startswith("http") or runner is None:
                        self._send_json(400, {"ok": False, "msg": "Geçerli bir mağaza ürün linki girin."})
                        return
                    snap = fetch_watched_product(url, runner.fetcher, storage=storage)
                    hunter = PopularDealsHunter(runner.fetcher)
                    img_url, old_p = hunter._fetch_product_image_and_old_price(snap.best_url, snap.effective_price_tl)
                    avg_p = fetch_realtime_market_avg_price(
                        title=snap.title,
                        current_price_tl=snap.effective_price_tl,
                        fetcher=runner.fetcher,
                        fallback_prev_tl=old_p,
                    )
                    c_code = snap.coupon_code
                    c_info = snap.coupon_info
                    coupon_note = ""
                    if raw_coupon:
                        dec_c, howto = decode_onual_coupon_param(raw_coupon)
                        if howto and not dec_c:
                            c_code, c_info = "", howto
                            coupon_note = f"Mağaza kupon talimatı: {howto}"
                        else:
                            ok_c, vc, vi = verify_coupon_live(raw_coupon, c_info, snap.best_url, runner.fetcher)
                            if ok_c:
                                c_code, c_info = vc, vi
                                coupon_note = f"✅ '{vc}' kuponu canlı doğrulandı."
                            else:
                                coupon_note = f"⚠️ '{raw_coupon}' canlı doğrulanamadığı için otomatik çıkarıldı."
                    ref_old = old_p or avg_p or round(snap.effective_price_tl * 1.25, 2)
                    disc_pct = max(0, int(round(((ref_old - snap.effective_price_tl) / ref_old) * 100.0))) if ref_old > snap.effective_price_tl else 0
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "preview": {
                                "title": snap.title,
                                "seller": snap.seller,
                                "url": snap.best_url,
                                "price_tl": snap.effective_price_tl,
                                "old_price_tl": ref_old,
                                "market_avg_tl": avg_p or ref_old,
                                "discount_percent": disc_pct,
                                "image_url": img_url,
                                "coupon_code": c_code,
                                "coupon_info": c_info,
                                "coupon_note": coupon_note,
                            },
                            "msg": "Ürün bilgileri, görsel ve anlık piyasa ortalaması çekildi!",
                        },
                    )
                    return

                if path == "/api/channel/publish-custom-deal":
                    if runner is None:
                        self._send_json(400, {"ok": False, "msg": "Runner yok."})
                        return
                    d_url = str(data.get("url", "")).strip()
                    title = str(data.get("title", "")).strip()
                    seller = str(data.get("seller", "Mağaza")).strip() or "Mağaza"
                    price_tl = float(data.get("price_tl") or 0)
                    old_p = float(data.get("old_price_tl") or 0) or None
                    avg_p = float(data.get("market_avg_tl") or 0) or old_p
                    img_url = str(data.get("image_url", "")).strip()
                    c_code = str(data.get("coupon_code", "")).strip()
                    c_info = str(data.get("coupon_info", "")).strip()
                    badge = str(data.get("badge_label", "SON 6 AYIN EN DÜŞÜK FİYATI!")).strip()
                    force_repost = bool(data.get("force_repost", False))

                    if not d_url.startswith("http") or not title or price_tl <= 0:
                        self._send_json(400, {"ok": False, "msg": "Ürün linki, başlık ve geçerli fiyat zorunludur."})
                        return
                    if config.only_photo_deals and not img_url.startswith("http"):
                        self._send_json(400, {"ok": False, "msg": "Fotoğrafsız ürün paylaşımı kapalı! Geçerli bir görsel URL'si gerekli."})
                        return
                    canon = canonicalize_deal_key(d_url)
                    if force_repost:
                        storage.delete_published_channel_deal(canon)

                    disc_pct = int(round(((old_p - price_tl) / old_p) * 100.0)) if (old_p and old_p > price_tl) else 25
                    deal = PopularDeal(
                        deal_key=canon,
                        title=title,
                        seller=seller,
                        price_tl=price_tl,
                        previous_or_second_price_tl=old_p,
                        discount_percent=max(1, disc_pct),
                        direct_url=d_url,
                        image_url=img_url,
                        coupon_info=c_info,
                        coupon_code=c_code,
                        badge_label=badge,
                        market_avg_price_tl=avg_p,
                    )
                    _enrich_deal_insights(deal)
                    ok_pub = runner.channel_publisher.publish_popular_deal(deal)
                    self._send_json(
                        200 if ok_pub else 400,
                        {
                            "ok": ok_pub,
                            "msg": (
                                f"🎉 '{title[:50]}' fotoğraflı olarak {config.telegram_channel_id} kanalına yayınlandı!"
                                if ok_pub
                                else "Yayınlanamadı (ürün daha önce paylaşılmış, görsel yüklenemedi veya kanal yetkisi kapalı)."
                            ),
                        },
                    )
                    return

                if path == "/api/channel/promo-pack":
                    if runner is None:
                        self._send_json(400, {"ok": False, "msg": "Runner yok."})
                        return
                    deals = PopularDealsHunter(runner.fetcher).hunt(min_discount_percent=15, max_items=2)
                    promo_html = runner.channel_publisher.build_social_media_promo_pack(deals)
                    self._send_json(200, {"ok": True, "promo_html": promo_html, "msg": "Üye çekme metinleri hazırlandı!"})
                    return

                if path == "/api/channel/delete-published":
                    dkey = str(data.get("deal_key", "")).strip()
                    ok = storage.delete_published_channel_deal(dkey)
                    self._send_json(200, {"ok": ok, "msg": "Kayıt silindi, ürün tekrar paylaşılabilir." if ok else "Kayıt bulunamadı."})
                    return

                if path == "/api/channel/send-post":
                    text = str(data.get("text", "")).strip()
                    pin = bool(data.get("pin", False))
                    if not text or notifier is None or not config.telegram_channel_id:
                        self._send_json(400, {"ok": False, "msg": "Mesaj boş olamaz ve kanal ayarlı olmalı."})
                        return
                    resp = requests.post(
                        f"{notifier.api_base}/sendMessage",
                        json={
                            "chat_id": config.telegram_channel_id,
                            "text": text,
                            "parse_mode": "HTML",
                            "disable_web_page_preview": False,
                            "protect_content": False,
                        },
                        timeout=15,
                    ).json()
                    if not resp.get("ok"):
                        self._send_json(400, {"ok": False, "msg": f"Telegram hatası: {resp}"})
                        return
                    if pin:
                        msg_id = resp["result"]["message_id"]
                        requests.post(
                            f"{notifier.api_base}/pinChatMessage",
                            json={"chat_id": config.telegram_channel_id, "message_id": msg_id},
                            timeout=10,
                        )
                    self._send_json(200, {"ok": True, "msg": "Mesaj kanala gönderildi!" + (" (Sabitlendi)" if pin else "")})
                    return

                if path == "/api/channel/set-description":
                    desc = str(data.get("description", "")).strip()
                    if notifier is None:
                        self._send_json(400, {"ok": False, "msg": "Notifier yok."})
                        return
                    resp = requests.post(
                        f"{notifier.api_base}/setChatDescription",
                        json={"chat_id": config.telegram_channel_id, "description": desc},
                        timeout=10,
                    ).json()
                    self._send_json(200, {"ok": bool(resp.get("ok")), "msg": "Kanal açıklaması güncellendi!" if resp.get("ok") else str(resp)})
                    return

                if path == "/api/channel/publish-popular":
                    if runner is None:
                        self._send_json(400, {"ok": False, "msg": "Runner yok."})
                        return
                    cnt = runner.publish_next_popular_deals(max_posts=max(1, int(data.get("max_posts", 2))))
                    msg = (
                        f"{cnt} yeni fotoğraflı popüler fırsat kanala yayınlandı!"
                        if cnt > 0
                        else "Şu an kriterlere uygun yeni fırsat bulunamadı (mevcutlar zaten paylaşıldı)."
                    )
                    self._send_json(200, {"ok": True, "msg": msg})
                    return

                if path == "/api/channel/settings":
                    config.channel_enabled = bool(data.get("channel_enabled", config.channel_enabled))
                    if "telegram_channel_id" in data and str(data["telegram_channel_id"]).strip():
                        config.telegram_channel_id = str(data["telegram_channel_id"]).strip()
                    if "telegram_channel_url" in data and str(data["telegram_channel_url"]).strip():
                        config.telegram_channel_url = str(data["telegram_channel_url"]).strip()
                    config.auto_publish_s25_deals = bool(data.get("auto_publish_s25_deals", config.auto_publish_s25_deals))
                    config.auto_publish_popular_deals = bool(data.get("auto_publish_popular_deals", config.auto_publish_popular_deals))
                    config.auto_publish_bestseller_drops = bool(data.get("auto_publish_bestseller_drops", config.auto_publish_bestseller_drops))
                    config.auto_publish_community_deals = bool(data.get("auto_publish_community_deals", config.auto_publish_community_deals))
                    config.only_photo_deals = bool(data.get("only_photo_deals", config.only_photo_deals))
                    config.prevent_duplicate_deals = bool(data.get("prevent_duplicate_deals", config.prevent_duplicate_deals))
                    config.duplicate_repost_discount_pct = max(
                        1.0, float(data.get("duplicate_repost_discount_pct", config.duplicate_repost_discount_pct))
                    )
                    config.max_popular_posts_per_scan = max(1, int(data.get("max_popular_posts_per_scan", config.max_popular_posts_per_scan)))
                    config.min_popular_discount_percent = max(1, int(data.get("min_discount_percent", config.min_popular_discount_percent)))
                    config.min_popular_savings_tl = max(0.0, float(data.get("min_savings_tl", config.min_popular_savings_tl)))
                    config.always_show_ad_disclosure = bool(data.get("always_show_ad_disclosure", config.always_show_ad_disclosure))
                    config.show_disclosure_when_affiliate_active = bool(
                        data.get("show_disclosure_when_affiliate_active", config.show_disclosure_when_affiliate_active)
                    )
                    ch_sec = config.raw_yaml.setdefault("channel", {})
                    ch_sec["enabled"] = config.channel_enabled
                    ch_sec["channel_id"] = config.telegram_channel_id
                    ch_sec["channel_url"] = config.telegram_channel_url
                    ch_sec["auto_publish_s25_deals"] = config.auto_publish_s25_deals
                    ch_sec["auto_publish_popular_deals"] = config.auto_publish_popular_deals
                    ch_sec["auto_publish_bestseller_drops"] = config.auto_publish_bestseller_drops
                    ch_sec["auto_publish_community_deals"] = config.auto_publish_community_deals
                    ch_sec["only_photo_deals"] = config.only_photo_deals
                    ch_sec["prevent_duplicate_deals"] = config.prevent_duplicate_deals
                    ch_sec["duplicate_repost_discount_pct"] = config.duplicate_repost_discount_pct
                    ch_sec["max_popular_posts_per_scan"] = config.max_popular_posts_per_scan
                    ch_sec["min_discount_percent"] = config.min_popular_discount_percent
                    ch_sec["min_savings_tl"] = int(config.min_popular_savings_tl)
                    ch_sec["always_show_ad_disclosure"] = config.always_show_ad_disclosure
                    ch_sec["show_disclosure_when_affiliate_active"] = config.show_disclosure_when_affiliate_active
                    config.persist_yaml()
                    config.save_env_vars({"TELEGRAM_CHANNEL_ID": config.telegram_channel_id})
                    self._send_json(200, {"ok": True, "msg": "Tüm kanal kuralları ve yayın filtreleri kaydedildi!"})
                    return

                # --- 6. ONAY KUYRUĞU ---
                if path == "/api/approvals/resolve":
                    aid = int(data.get("id", 0))
                    approved = bool(data.get("approved", False))
                    row = storage.resolve_pending_approval(aid, approved=approved)
                    if not row:
                        self._send_json(400, {"ok": False, "msg": f"#{aid} bulunamadı veya zaten işlendi."})
                        return
                    if approved and runner is not None:
                        runner.publish_approved_item(row)
                    self._send_json(
                        200,
                        {"ok": True, "msg": f"#{aid} onaylandı ve kanala yayınlandı!" if approved else f"#{aid} reddedildi."},
                    )
                    return

                if path == "/api/approvals/resolve-all":
                    approved = bool(data.get("approved", False))
                    pending = storage.list_pending_approvals(limit=50)
                    cnt = 0
                    for p in pending:
                        row = storage.resolve_pending_approval(int(p["id"]), approved=approved)
                        if row:
                            cnt += 1
                            if approved and runner is not None:
                                runner.publish_approved_item(row)
                    self._send_json(200, {"ok": True, "msg": f"{cnt} bekleyen gönderi toplu olarak {'onaylandı' if approved else 'reddedildi'}."})
                    return

                # --- 7. MAĞAZA KAYNAKLARI & SCRAPER MOTORU ---
                if path == "/api/sources/settings":
                    srcs_in = data.get("sources") or {}
                    for skey, _ in _ALL_KNOWN_SOURCES:
                        if skey in srcs_in:
                            config.sources[skey] = bool(srcs_in[skey])
                    config.timeout_seconds = max(5, int(data.get("timeout_seconds", config.timeout_seconds)))
                    config.max_workers = max(1, min(16, int(data.get("max_workers", config.max_workers))))
                    config.use_curl_cffi = bool(data.get("use_curl_cffi", config.use_curl_cffi))
                    config.use_playwright_fallback = bool(data.get("use_playwright_fallback", config.use_playwright_fallback))

                    if runner is not None and hasattr(runner, "fetcher"):
                        runner.fetcher.timeout = config.timeout_seconds
                        runner.fetcher.use_curl_cffi = config.use_curl_cffi
                        runner.fetcher.use_playwright_fallback = config.use_playwright_fallback

                    sc_sec = config.raw_yaml.setdefault("scraping", {})
                    sc_sec["timeout_seconds"] = config.timeout_seconds
                    sc_sec["max_workers"] = config.max_workers
                    sc_sec["use_curl_cffi"] = config.use_curl_cffi
                    sc_sec["use_playwright_fallback"] = config.use_playwright_fallback
                    sc_sec["sources"] = dict(config.sources)
                    config.persist_yaml()
                    self._send_json(200, {"ok": True, "msg": "Aktif mağaza kaynakları ve kazıma motoru ayarları kaydedildi!"})
                    return

                if path == "/api/system/smoke-test":
                    if runner is None:
                        self._send_json(400, {"ok": False, "msg": "Runner yok."})
                        return
                    send_tg = bool(data.get("send_telegram", False))
                    res = runner.run_live_smoke_test(send_telegram=send_tg)
                    summary = (
                        f"Duman testi tamamlandı: {res['healthy']} kaynak sağlıklı, "
                        f"{len(res['zero_sources'])} kaynak 0 ürün ({', '.join(res['zero_sources']) or '-'}), "
                        f"{len(res['failed_sources'])} hata ({', '.join(res['failed_sources']) or '-'})."
                    )
                    self._send_json(200, {"ok": True, "report_html": res["report_html"], "msg": summary})
                    return

                # --- 8. ZAMANLAYICI, TURBO SAATLER & UYANDIRMA ALARMI ---
                if path == "/api/system/settings":
                    config.interval_minutes = max(1, int(data.get("interval_minutes", config.interval_minutes)))
                    config.turbo_interval_minutes = max(1, int(data.get("turbo_interval_minutes", config.turbo_interval_minutes)))
                    if "turbo_windows" in data:
                        tw_raw = str(data["turbo_windows"])
                        tw_list = [x.strip() for x in tw_raw.replace("\n", ",").split(",") if "-" in x]
                        if tw_list:
                            config.turbo_windows = tw_list
                    if "daily_summary_times" in data:
                        ds_raw = str(data["daily_summary_times"])
                        config.daily_summary_times = [x.strip() for x in ds_raw.replace("\n", ",").split(",") if ":" in x]
                    config.notify_every_check = bool(data.get("notify_every_check", config.notify_every_check))
                    config.wakeup_enabled = bool(data.get("wakeup_enabled", config.wakeup_enabled))
                    config.wakeup_window = str(data.get("wakeup_window", config.wakeup_window)).strip()
                    config.wakeup_repeat_count = max(1, int(data.get("wakeup_repeat_count", config.wakeup_repeat_count)))
                    config.wakeup_repeat_interval_seconds = max(
                        5, int(data.get("wakeup_repeat_interval_seconds", config.wakeup_repeat_interval_seconds))
                    )
                    config.wakeup_auto_open_browser = bool(data.get("wakeup_auto_open_browser", config.wakeup_auto_open_browser))
                    config.wakeup_pc_beep = bool(data.get("wakeup_pc_beep", config.wakeup_pc_beep))
                    if "callmebot_telegram_user" in data:
                        config.callmebot_telegram_user = str(data["callmebot_telegram_user"]).strip()
                        config.save_env_vars({"CALLMEBOT_TELEGRAM_USER": config.callmebot_telegram_user})

                    sch = config.raw_yaml.setdefault("schedule", {})
                    sch["interval_minutes"] = config.interval_minutes
                    sch["turbo_interval_minutes"] = config.turbo_interval_minutes
                    sch["turbo_windows"] = list(config.turbo_windows)
                    sch["daily_summary_times"] = list(config.daily_summary_times)
                    sch["notify_every_check"] = config.notify_every_check

                    wu = config.raw_yaml.setdefault("alerts", {}).setdefault("wakeup_alarm", {})
                    wu["enabled"] = config.wakeup_enabled
                    wu["active_window"] = config.wakeup_window
                    wu["repeat_count"] = config.wakeup_repeat_count
                    wu["repeat_interval_seconds"] = config.wakeup_repeat_interval_seconds
                    wu["auto_open_browser"] = config.wakeup_auto_open_browser
                    wu["pc_beep"] = config.wakeup_pc_beep
                    config.persist_yaml()
                    self._send_json(200, {"ok": True, "msg": "Zamanlayıcı, Turbo pencereler, özet saatleri ve uyandırma alarmı kaydedildi!"})
                    return

                if path == "/api/habits/apply":
                    report = analyze_shopping_habits(storage, config)
                    added = apply_habit_recommendations(config, report)
                    msg = (
                        f"Öğrenilen yoğun saatler Turbo takvime eklendi: {', '.join(added)}"
                        if added
                        else "Takvim zaten en yoğun alışveriş saatlerine göre optimize edilmiş durumda."
                    )
                    self._send_json(200, {"ok": True, "msg": msg})
                    return

                # --- 9. PİYASA İSTİHBARATI, ÇARK & INFLUENCER RADARI ---
                if path == "/api/intel/scan":
                    if runner is None:
                        self._send_json(400, {"ok": False, "msg": "Runner yok."})
                        return
                    from src.market_intel import format_market_intel_html

                    rep = MarketIntelligenceHunter(runner.fetcher).collect_all()
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "intel_html": format_market_intel_html(rep),
                            "msg": (
                                f"İstihbarat tamamlandı: {len(rep.bank_campaigns)} banka, "
                                f"{len(rep.brand_deals)} resmi marka, {len(rep.hashtag_signals)} kupon sinyali!"
                            ),
                        },
                    )
                    return

                if path == "/api/intel/gamified":
                    if runner is None:
                        self._send_json(400, {"ok": False, "msg": "Runner yok."})
                        return
                    publish = bool(data.get("publish", False))
                    items = MarketIntelligenceHunter(runner.fetcher).scan_gamified_campaigns(limit=8)
                    pub_cnt = 0
                    rows_out = []
                    for it in items:
                        rows_out.append({"title": it.title, "source": it.source_name, "url": it.url, "benefit": it.benefit_summary})
                        if publish and pub_cnt < 2:
                            key = f"GAME:{it.url.split('?')[0]}"
                            if not storage.is_channel_deal_published(key, title=it.title):
                                if runner.channel_publisher.publish_gamified_campaign(it):
                                    pub_cnt += 1
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "items": rows_out,
                            "msg": f"{len(items)} çark/tıkla-kazan kampanyası bulundu"
                            + (f", {pub_cnt} tanesi kanala yayınlandı!" if publish else "!"),
                        },
                    )
                    return

                if path == "/api/intel/influencer":
                    if runner is None:
                        self._send_json(400, {"ok": False, "msg": "Runner yok."})
                        return
                    publish = bool(data.get("publish", False))
                    items = MarketIntelligenceHunter(runner.fetcher).scan_influencer_hashtag_codes(limit=8)
                    pub_cnt = 0
                    rows_out = []
                    for it in items:
                        c_ok, vc, _ = verify_coupon_live(it.coupon_code, "", it.url, runner.fetcher)
                        rows_out.append({"title": it.title, "coupon_code": vc or it.coupon_code, "verified": c_ok, "url": it.url})
                        if publish and c_ok and vc and pub_cnt < 2:
                            it.coupon_code = vc
                            key = f"INF:{vc}"
                            if not storage.is_channel_deal_published(key, title=it.title):
                                if runner.channel_publisher.publish_influencer_code(it):
                                    pub_cnt += 1
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "items": rows_out,
                            "msg": f"{len(items)} influencer kodu tarandı"
                            + (f", doğrulanmış {pub_cnt} tanesi kanala yayınlandı!" if publish else "!"),
                        },
                    )
                    return

                # --- 10. ENTEGRASYONLAR: TELEGRAM, X, AFFILIATE & GÜVENLİK ---
                if path == "/api/integrations/telegram":
                    tok = str(data.get("telegram_bot_token", "")).strip()
                    cid = str(data.get("telegram_chat_id", "")).strip()
                    config.enable_bot_commands = bool(data.get("enable_bot_commands", config.enable_bot_commands))
                    config.top_offers_per_variant = max(1, min(15, int(data.get("top_offers_per_variant", config.top_offers_per_variant))))

                    env_up: dict[str, str] = {}
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
                    if env_up:
                        config.save_env_vars(env_up)

                    tg_sec = config.raw_yaml.setdefault("telegram", {})
                    tg_sec["enable_bot_commands"] = config.enable_bot_commands
                    tg_sec["top_offers_per_variant"] = config.top_offers_per_variant
                    config.persist_yaml()
                    self._send_json(200, {"ok": True, "msg": "Telegram Bot ayarları kaydedildi ve canlı uygulandı!"})
                    return

                if path == "/api/integrations/test-telegram":
                    if notifier is None:
                        self._send_json(400, {"ok": False, "msg": "Notifier yok."})
                        return
                    ok, msg = notifier.test_connection()
                    self._send_json(200 if ok else 400, {"ok": ok, "msg": msg})
                    return

                if path == "/api/x/settings":
                    config.x_auto_publish_deals = bool(data.get("x_auto_publish_deals", config.x_auto_publish_deals))
                    config.x_max_tweets_per_day = max(1, int(data.get("x_max_tweets_per_day", config.x_max_tweets_per_day)))
                    config.x_min_discount_percent = max(1, int(data.get("x_min_discount_percent", config.x_min_discount_percent)))
                    config.x_min_interval_minutes = max(5, int(data.get("x_min_interval_minutes", config.x_min_interval_minutes)))
                    x_sec = config.raw_yaml.setdefault("x_twitter", {})
                    x_sec["auto_publish_deals"] = config.x_auto_publish_deals
                    x_sec["max_tweets_per_day"] = config.x_max_tweets_per_day
                    x_sec["min_discount_percent"] = config.x_min_discount_percent
                    x_sec["min_interval_minutes"] = config.x_min_interval_minutes
                    config.persist_yaml()

                    x_env: dict[str, str] = {}
                    for field_attr, env_key in (
                        ("x_api_key", "X_API_KEY"),
                        ("x_api_secret", "X_API_SECRET"),
                        ("x_access_token", "X_ACCESS_TOKEN"),
                        ("x_access_token_secret", "X_ACCESS_TOKEN_SECRET"),
                    ):
                        val_in = str(data.get(field_attr, "")).strip()
                        if val_in and "***" not in val_in:
                            setattr(config, field_attr, val_in)
                            x_env[env_key] = val_in
                    if x_env:
                        config.save_env_vars(x_env)
                    self._send_json(200, {"ok": True, "msg": "X (Twitter) ayarları ve anahtarları kaydedildi!"})
                    return

                if path == "/api/integrations/test-x":
                    if runner is None:
                        self._send_json(400, {"ok": False, "msg": "Runner yok."})
                        return
                    ok, msg = runner.x_publisher.test_connection()
                    self._send_json(200 if ok else 400, {"ok": ok, "msg": msg})
                    return

                if path == "/api/x/post-now":
                    if runner is None or not runner.x_publisher.is_configured:
                        self._send_json(400, {"ok": False, "msg": "X hesabı bağlı değil."})
                        return
                    deals = PopularDealsHunter(runner.fetcher).hunt(min_discount_percent=15, max_items=2)
                    if not deals:
                        self._send_json(400, {"ok": False, "msg": "Uygun fırsat bulunamadı."})
                        return
                    d = deals[0]
                    ok_x, res_x = runner.x_publisher.post_deal_tweet(
                        title=d.title,
                        price_tl=d.effective_price_tl,
                        seller=d.seller,
                        direct_url=d.direct_url,
                        discount_pct=d.discount_percent,
                        coupon_code=d.coupon_code,
                    )
                    if ok_x:
                        storage.mark_x_deal_posted(d.deal_key, res_x)
                    elif "402" in str(res_x) or "credits depleted" in str(res_x).lower():
                        res_x = "X API kredisi tükenmiş (402 Payment Required)."
                    self._send_json(200 if ok_x else 400, {"ok": ok_x, "msg": f"Tweet atıldı: {res_x}" if ok_x else res_x})
                    return

                if path == "/api/affiliate/save":
                    config.amazon_associate_tag = str(data.get("amazon_tag", "")).strip()
                    config.hepsiburada_affiliate_tag = str(data.get("hepsiburada_tag", "")).strip()
                    config.trendyol_affiliate_tag = str(data.get("trendyol_tag", "")).strip()
                    config.admitad_deeplink_base = str(data.get("admitad_base", "")).strip()
                    config.affiliate_utm_source = str(data.get("utm_source", config.affiliate_utm_source)).strip() or "telegram"
                    config.affiliate_utm_medium = str(data.get("utm_medium", config.affiliate_utm_medium)).strip() or "firsat_takibi"
                    config.save_env_vars({
                        "AMAZON_ASSOCIATE_TAG": config.amazon_associate_tag,
                        "HEPSIBURADA_AFFILIATE_TAG": config.hepsiburada_affiliate_tag,
                        "TRENDYOL_AFFILIATE_TAG": config.trendyol_affiliate_tag,
                        "ADMITAD_DEEPLINK_BASE": config.admitad_deeplink_base,
                    })
                    aff_sec = config.raw_yaml.setdefault("affiliate", {})
                    aff_sec["utm_source"] = config.affiliate_utm_source
                    aff_sec["utm_medium"] = config.affiliate_utm_medium
                    config.persist_yaml()
                    self._send_json(200, {"ok": True, "msg": "Affiliate (Satış Ortaklığı) kodları ve UTM etiketleri kaydedildi!"})
                    return

                if path == "/api/security/settings":
                    new_host = str(data.get("dashboard_host", config.dashboard_host)).strip() or "127.0.0.1"
                    new_port = max(1024, min(65535, int(data.get("dashboard_port", config.dashboard_port))))
                    new_pwd = str(data.get("dashboard_password", "")).strip()
                    clear_pwd = bool(data.get("clear_password", False))

                    config.dashboard_host = new_host
                    config.dashboard_port = new_port
                    dash_sec = config.raw_yaml.setdefault("dashboard", {})
                    dash_sec["host"] = new_host
                    dash_sec["port"] = new_port
                    config.persist_yaml()

                    env_updates = {"DASHBOARD_HOST": new_host, "DASHBOARD_PORT": str(new_port)}
                    extra_headers: list[tuple[str, str]] = []
                    if clear_pwd:
                        config.dashboard_password = ""
                        env_updates["DASHBOARD_PASSWORD"] = ""
                    elif new_pwd and "***" not in new_pwd:
                        config.dashboard_password = new_pwd
                        env_updates["DASHBOARD_PASSWORD"] = new_pwd
                        tok = _compute_auth_token(new_pwd)
                        extra_headers.append(("Set-Cookie", f"ft_admin={tok}; Path=/; Max-Age=2592000; HttpOnly; SameSite=Lax"))
                    config.save_env_vars(env_updates)
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "msg": "Panel ağ ve şifre güvenliği ayarları kaydedildi! (Host/Port değişikliği servis yeniden başladığında geçerli olur.)",
                        },
                        extra_headers=extra_headers,
                    )
                    return

                # --- 11. ORACLE CLOUD 7/24 YAYIN & BAKIM ---
                if path == "/api/oracle/deploy-ssh":
                    srv_ip = str(data.get("server_ip", "")).strip()
                    ssh_u = str(data.get("ssh_user", "ubuntu")).strip() or "ubuntu"
                    ssh_k = str(data.get("ssh_key_path", "")).strip()
                    p_pwd = str(data.get("panel_password", "")).strip()
                    ok_dep, msg_dep = deploy_to_oracle_ssh(
                        config,
                        server_ip=srv_ip,
                        ssh_user=ssh_u,
                        ssh_key_path=ssh_k,
                        panel_password=p_pwd,
                    )
                    self._send_json(200 if ok_dep else 400, {"ok": ok_dep, "msg": msg_dep})
                    return

                if path == "/api/system/install-windows-task":
                    ok_w, msg_w = install_detached_windows_task(config)
                    self._send_json(200 if ok_w else 400, {"ok": ok_w, "msg": msg_w})
                    return

                if path == "/api/logs/clear":
                    log_file = config.base_dir / "logs" / "tracker.log"
                    if log_file.exists():
                        log_file.write_text("", encoding="utf-8")
                    self._send_json(200, {"ok": True, "msg": "Log dosyası temizlendi."})
                    return

                if path == "/api/db/clear-cooldowns":
                    cnt = storage.clear_sent_alerts()
                    self._send_json(200, {"ok": True, "msg": f"{cnt} adet bildirim bekleme kilidi (cooldown) sıfırlandı."})
                    return

                self._send_json(404, {"ok": False, "msg": "Bilinmeyen API endpoint."})
            except Exception as exc:
                logger.exception("Dashboard API hatası (%s):", path)
                self._send_json(500, {"ok": False, "msg": f"Sunucu hatası: {exc}"})

    try:
        server = ThreadingHTTPServer((config.dashboard_host, config.dashboard_port), _Handler)
        t = threading.Thread(target=server.serve_forever, daemon=True, name="web-dashboard")
        t.start()
        logger.info("Canlı Web Yönetim Paneli aktif: http://%s:%d", config.dashboard_host, config.dashboard_port)
        return server
    except Exception as exc:
        logger.warning("Web Dashboard başlatılamadı (%s:%d): %s", config.dashboard_host, config.dashboard_port, exc)
        return None


def _fetch_channel_stats(config: AppConfig, notifier: Optional[TelegramNotifier]) -> dict:
    if not notifier or not notifier.is_configured or not config.telegram_channel_id:
        return {"ok": False, "members": "-", "title": config.telegram_channel_id, "description": ""}
    try:
        c_info = requests.get(
            f"{notifier.api_base}/getChat",
            params={"chat_id": config.telegram_channel_id},
            timeout=6,
        ).json()
        m_cnt = requests.get(
            f"{notifier.api_base}/getChatMemberCount",
            params={"chat_id": config.telegram_channel_id},
            timeout=6,
        ).json()
        res = c_info.get("result") or {}
        return {
            "ok": True,
            "members": m_cnt.get("result", "-"),
            "title": res.get("title", config.telegram_channel_id),
            "description": res.get("description", ""),
        }
    except Exception:
        return {"ok": False, "members": "-", "title": config.telegram_channel_id, "description": ""}


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
  <title>Giriş — Fırsat Takip Yönetim Merkezi</title>
  <style>
    body {{ background:#0b1120; color:#f8fafc; font-family:'Segoe UI',system-ui,sans-serif; display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0; }}
    .box {{ background:#1e293b; border:1px solid #334155; border-radius:14px; padding:28px; width:100%; max-width:390px; box-shadow:0 12px 32px rgba(0,0,0,0.5); }}
    h1 {{ margin:0 0 8px 0; font-size:20px; color:#38bdf8; }}
    p {{ color:#94a3b8; font-size:13px; margin:0 0 18px 0; }}
    input {{ width:100%; box-sizing:border-box; background:#0f172a; color:#fff; border:1px solid #475569; border-radius:8px; padding:10px 12px; font-size:14px; margin-bottom:14px; }}
    button {{ width:100%; background:#0284c7; color:#fff; border:none; border-radius:8px; padding:11px; font-size:14px; font-weight:700; cursor:pointer; }}
    button:hover {{ background:#0369a1; }}
    #err {{ color:#f87171; font-size:13px; margin-top:10px; display:none; }}
  </style>
</head>
<body>
  <div class="box">
    <h1>🔐 Yönetim Merkezi Girişi</h1>
    <p>Kanal: <b>{html.escape(config.telegram_channel_id)}</b> • 7/24 Bulut Kontrol Paneli</p>
    <input id="pwd" type="password" placeholder="Panel Şifresi (DASHBOARD_PASSWORD)" autofocus onkeydown="if(event.key==='Enter')login()" />
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
        e.textContent = j.msg || 'Giriş başarısız';
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
    grouped = storage.get_latest_offers_by_variant(limit_per_variant=config.top_offers_per_variant)
    watched_urls = storage.list_watched_urls(active_only=True)
    watched_kws = storage.list_watched_keywords() if hasattr(storage, "list_watched_keywords") else []
    paused = storage.get_paused_variants()
    only_official = "ONLY_OFFICIAL" in paused
    all_paused = "ALL" in paused
    x_today = storage.count_x_tweets_today() if hasattr(storage, "count_x_tweets_today") else 0
    bs_count = storage.count_tracked_bestsellers() if hasattr(storage, "count_tracked_bestsellers") else 0
    recent_deals = storage.get_recent_channel_deals(limit=20) if hasattr(storage, "get_recent_channel_deals") else []
    pending_apps = storage.list_pending_approvals(limit=25) if hasattr(storage, "list_pending_approvals") else []
    depo_deals = storage.list_depo_deals(min_discount=0, limit=60) if hasattr(storage, "list_depo_deals") else []
    depo_super_cnt = sum(1 for x in depo_deals if int(x.get("discount_percent") or 0) >= config.depo_super_discount_percent)
    db_stats = storage.get_db_stats() if hasattr(storage, "get_db_stats") else {}

    cur_wait, is_turbo = get_dynamic_interval_minutes(config)
    plat_lbl = f"☁️ Linux ({platform.machine()})" if sys.platform != "win32" else "💻 Windows Yerel"
    daemon_badge = (
        "<span class='pill pill-amber'>⏸ Otomatik Tarama Duraklatıldı</span>"
        if config.daemon_paused
        else (
            f"<span class='pill pill-green'>🟢 7/24 Aktif • ⚡ Turbo ({cur_wait} dk)</span>"
            if is_turbo
            else f"<span class='pill pill-green'>🟢 7/24 Aktif • Normal ({cur_wait} dk)</span>"
        )
    )
    security_warn = ""
    if config.dashboard_host == "0.0.0.0" and not config.dashboard_password:
        security_warn = (
            "<div class='warn-banner'>⚠️ Panel dış ağa açık (<code>0.0.0.0</code>) ancak şifre belirlenmemiş! "
            "Lütfen <b>🔑 API &amp; Güvenlik</b> sekmesinden hemen bir Panel Şifresi kaydedin.</div>"
        )

    pending_rows = []
    for pa in pending_apps:
        c_tag = f"<span class='tag coupon'>🎟 {html.escape(str(pa.get('coupon_code') or ''))}</span>" if pa.get("coupon_code") else ""
        pending_rows.append(
            f"<div class='offer-row'>"
            f"<div><b>#{pa['id']}</b> <span class='tag'>{html.escape(str(pa.get('post_type') or ''))}</span> "
            f"<span class='tag'>Güven: {pa['confidence']}/100</span> "
            f"<b>{html.escape(str(pa.get('title') or '')[:85])}</b> {c_tag}</div>"
            f"<div class='actions'><a class='btn-go' href='{html.escape(str(pa.get('url') or '#'), quote=True)}' target='_blank'>🔗</a> "
            f"<button class='btn-sm btn-green' onclick=\"api('/api/approvals/resolve', {{id:{pa['id']}, approved:true}})\">✅ Yayınla</button> "
            f"<button class='btn-sm btn-red' onclick=\"api('/api/approvals/resolve', {{id:{pa['id']}, approved:false}})\">❌ Reddet</button></div>"
            f"</div>"
        )

    habit_report = analyze_shopping_habits(storage, config)
    habit_recs_html = "".join(f"<div class='offer-row'><div>💡 {html.escape(r)}</div></div>" for r in habit_report.recommendations)
    habit_stores = ", ".join(f"{html.escape(s)} ({c})" for s, c in habit_report.top_stores) or "-"
    habit_cats = ", ".join(f"{html.escape(k)} ({c})" for k, c in habit_report.top_categories) or "-"
    habit_hours = ", ".join(f"{h:02d}:00 ({c})" for h, c in habit_report.peak_hours) or "-"
    upcoming_preds = predict_upcoming_campaigns(datetime.now())
    preds_html = "".join(
        f"<div class='offer-row'><div>📅 <b>{html.escape(p.window_label)}:</b> {html.escape(p.campaign_name)} "
        f"<span class='tag'>{html.escape(p.focus_categories)}</span><br/><span class='muted'>💡 {html.escape(p.action_advice)}</span></div></div>"
        for p in upcoming_preds
    )

    last_run = storage.get_last_run_info() if hasattr(storage, "get_last_run_info") else None
    last_run_str = (
        f"Son tarama: {str(last_run.get('started_at') or '-')[11:16]} "
        f"({last_run.get('sources_ok', 0)} kaynak OK, {last_run.get('total_offers', 0)} teklif)"
        if last_run
        else "Son tarama: -"
    )

    cards_html: list[str] = []
    target_inputs_html: list[str] = []
    baseline_inputs_html: list[str] = []
    for idx_v, variant in enumerate(config.variants):
        offers = grouped.get(variant, [])
        target = config.target_prices_tl.get(variant, 0)
        base_6m = config.six_month_baseline_tl.get(variant, 0)
        target_inputs_html.append(
            f"<div><label>{html.escape(variant)} Hedef (TL)</label>"
            f"<input class='var-target-inp' data-var='{html.escape(variant, quote=True)}' type='number' value='{int(target) if target else 0}' /></div>"
        )
        baseline_inputs_html.append(
            f"<div><label>{html.escape(variant)} 6 Ay Baz (TL, 0=Oto)</label>"
            f"<input class='var-base-inp' data-var='{html.escape(variant, quote=True)}' type='number' value='{int(base_6m) if base_6m else 0}' /></div>"
        )
        is_var_paused = all_paused or (variant in paused)
        rows = []
        for idx, off in enumerate(offers, 1):
            badge = "✅ Resmi" if off.is_official_seller else "🏪 Mağaza"
            color_str = f"<span class='tag'>{html.escape(off.color)}</span>" if off.color else ""
            coupon_str = (
                f"<span class='tag coupon'>🎟 {html.escape(off.coupon_info or off.coupon_code)}</span>"
                if (off.coupon_info or off.coupon_code)
                else ""
            )
            trade_str = f"<span class='tag trade'>🔄 {html.escape(off.trade_in_info)}</span>" if off.trade_in_info else ""
            js_url = json.dumps(off.url)
            rows.append(
                f"<div class='offer-row'>"
                f"<div><b>{idx}. {format_tl(off.effective_price_tl)}</b> — "
                f"<a href='{html.escape(off.url, quote=True)}' target='_blank'>{html.escape(off.seller)}</a> "
                f"<span class='tag'>{badge}</span> {color_str} {coupon_str} {trade_str}</div>"
                f"<div class='actions'>"
                f"<button class='btn-sm btn-purple' title='Kanal Stüdyosuna Aktar' onclick='sendUrlToStudio({js_url})'>📢</button>"
                f"<a class='btn-go' href='{html.escape(off.url, quote=True)}' target='_blank'>🛒 Aç</a>"
                f"</div></div>"
            )
        rows_joined = "".join(rows) or "<p class='muted'>Henüz veri yok.</p>"
        v_q = variant.replace(" ", "%20")
        v_js = json.dumps(variant)
        toggle_btn = (
            f"<button class='btn-sm btn-green' onclick='toggleVariant({v_js}, \"resume\")'>▶️ Başlat</button>"
            if is_var_paused
            else f"<button class='btn-sm btn-amber' onclick='toggleVariant({v_js}, \"pause\")'>⏸ Durdur</button>"
        )
        cards_html.append(
            f"<div class='card'>"
            f"<div class='card-head'><h2>📱 {html.escape(config.product_name)} — {html.escape(variant)}</h2>{toggle_btn}</div>"
            f"<div class='muted' style='margin-bottom:8px'>Hedef: <b>{format_tl(target) if target else '-'}</b> • Durum: {'⏸ Durduruldu' if is_var_paused else '✅ Aktif'}</div>"
            f"{rows_joined}"
            f"<img class='chart-img' src='/chart?variant={v_q}' alt='{html.escape(variant)} Grafik' />"
            f"</div>"
        )

    url_rows = []
    for w in watched_urls:
        wid = int(w["id"])
        st_txt = format_tl(float(w["last_price_tl"])) if w.get("in_stock", 1) and float(w["last_price_tl"]) > 0 else "📦 Stok Bekleniyor"
        cur_t = int(float(w["target_price_tl"])) if w.get("target_price_tl") else ""
        w_url_js = json.dumps(str(w["url"]))
        url_rows.append(
            f"<div class='offer-row'>"
            f"<div style='flex:1'><b>#{wid}</b> {html.escape(str(w['title'])[:72])} — <b>{st_txt}</b> "
            f"<span class='tag'>{html.escape(str(w['seller']))}</span> "
            f"<span class='muted'>(En Düşük: {format_tl(float(w.get('min_price_tl') or 0))})</span></div>"
            f"<div class='actions'>"
            f"<input id='ut_{wid}' type='number' placeholder='Hedef TL' value='{cur_t}' style='width:105px;margin:0;padding:4px 7px' />"
            f"<button class='btn-sm btn-green' onclick=\"api('/api/urls/update-target', {{id:{wid}, target_price_tl: val('ut_{wid}')}})\">💾</button>"
            f"<button class='btn-sm btn-blue' title='Şimdi Fiyat Çek' onclick=\"api('/api/urls/refresh', {{id:{wid}}})\">🔄</button>"
            f"<a class='btn-sm btn-dark' href='/chart-url?id={wid}' target='_blank' title='Fiyat Grafiği'>📈</a>"
            f"<button class='btn-sm btn-purple' title='Kanala Yayınla' onclick='sendUrlToStudio({w_url_js})'>📢</button>"
            f"<a class='btn-go' href='{html.escape(str(w['url']), quote=True)}' target='_blank'>🛒</a>"
            f"<button class='btn-sm btn-red' onclick=\"api('/api/urls/delete', {{id:{wid}}})\">🗑</button>"
            f"</div></div>"
        )

    kw_rows = []
    for kw in watched_kws:
        kid = int(kw["id"])
        p_txt = format_tl(float(kw["last_min_price_tl"])) if float(kw.get("last_min_price_tl") or 0) > 0 else "-"
        kw_t = int(float(kw["target_price_tl"])) if kw.get("target_price_tl") else ""
        q_js = json.dumps(str(kw["query"]))
        kw_rows.append(
            f"<div class='offer-row'>"
            f"<div style='flex:1'><b>🔍 #{kid} {html.escape(str(kw['query']))}</b> — En Ucuz: <b>{p_txt}</b> "
            f"<span class='tag'>{html.escape(str(kw.get('last_seller') or '-'))}</span></div>"
            f"<div class='actions'>"
            f"<input id='kwt_{kid}' type='number' placeholder='Hedef TL' value='{kw_t}' style='width:105px;margin:0;padding:4px 7px' />"
            f"<button class='btn-sm btn-green' onclick=\"api('/api/keywords/update-target', {{id:{kid}, target_price_tl: val('kwt_{kid}')}})\">💾</button>"
            f"<button class='btn-sm btn-blue' onclick='searchKwLive({q_js})'>🔎 Ara</button>"
            f"<a class='btn-go' href='{html.escape(str(kw.get('last_url') or '#'), quote=True)}' target='_blank'>🛒</a>"
            f"<button class='btn-sm btn-red' onclick=\"api('/api/keywords/delete', {{id:{kid}}})\">🗑</button>"
            f"</div></div>"
        )

    recent_deal_rows = []
    for rd in recent_deals:
        ts_short = str(rd.get("published_at") or "")[5:16].replace("T", " ")
        p_val = float(rd.get("price_tl") or 0)
        p_txt = format_tl(p_val) if p_val > 0 else "-"
        dk_js = json.dumps(str(rd.get("deal_key") or ""))
        recent_deal_rows.append(
            f"<div class='offer-row'>"
            f"<div><span class='tag'>{html.escape(ts_short)}</span> <b>{html.escape(str(rd.get('title') or '')[:85])}</b> — <b>{p_txt}</b></div>"
            f"<div class='actions'>"
            f"<a class='btn-go' href='{html.escape(str(rd.get('url') or '#'), quote=True)}' target='_blank'>🛒 Mağaza</a>"
            f"<button class='btn-sm btn-dark' title='Tekrar paylaşılabilmesi için geçmiş kaydını sil' onclick='deletePublished({dk_js})'>♻️ Kilidi Aç</button>"
            f"</div></div>"
        )

    source_toggles_html = []
    for skey, slabel in _ALL_KNOWN_SOURCES:
        is_on = config.sources.get(skey, True)
        source_toggles_html.append(
            f"<label class='chk source-box'><input type='checkbox' class='src-chk' data-src='{skey}' {'checked' if is_on else ''} /> "
            f"<b>{html.escape(slabel)}</b> <span class='muted'>({skey})</span></label>"
        )

    depo_cat_boxes_html = []
    for ckey, (clbl, _) in DEPO_CATEGORY_FEEDS.items():
        checked = "checked" if ckey in config.depo_categories else ""
        depo_cat_boxes_html.append(
            f"<label class='chk source-box'><input type='checkbox' class='depo-cat-chk' data-cat='{ckey}' {checked} /> "
            f"<b>{html.escape(clbl)}</b></label>"
        )

    depo_cards_html = []
    for dd in depo_deals:
        dpct = int(dd.get("discount_percent") or 0)
        dprice = float(dd.get("depo_price_tl") or 0)
        mprice = float(dd.get("market_price_tl") or 0)
        sav = float(dd.get("savings_tl") or 0)
        asin_s = str(dd.get("asin") or "")
        img_s = str(dd.get("image_url") or "")
        badge_cls = "background:#dc2626;color:#fff" if dpct >= 70 else ("background:#d97706;color:#fff" if dpct >= 50 else "background:#16a34a;color:#fff")
        img_tag = (
            f"<img src='{html.escape(img_s, quote=True)}' alt='' style='width:68px;height:68px;object-fit:contain;background:#fff;border-radius:8px;padding:3px;flex-shrink:0' />"
            if img_s.startswith("http")
            else ""
        )
        asin_js = json.dumps(asin_s)
        cart_url = f"https://www.amazon.com.tr/gp/aws/cart/add.html?ASIN.1={asin_s}&Quantity.1=1"
        depo_cards_html.append(
            f"<div class='offer-row depo-item' data-pct='{dpct}' style='align-items:flex-start'>"
            f"<div style='display:flex;gap:10px;align-items:flex-start;flex:1;min-width:0'>"
            f"{img_tag}"
            f"<div style='min-width:0'>"
            f"<span class='tag' style='{badge_cls};font-weight:800;font-size:12px'>-%{dpct}</span> "
            f"<span class='tag'>{html.escape(str(dd.get('category') or 'Depo'))}</span> "
            f"<span class='tag'>{html.escape(str(dd.get('condition_label') or 'İkinci El'))}</span><br/>"
            f"<b>{html.escape(str(dd.get('title') or '')[:115])}</b><br/>"
            f"<span style='font-size:15px;font-weight:800;color:#4ade80'>{format_tl(dprice)}</span> "
            f"<s class='muted'>{format_tl(mprice)}</s> "
            f"<span class='muted'>({format_tl(sav)} kazanç • {html.escape(str(dd.get('price_source') or 'Piyasa'))})</span>"
            f"</div></div>"
            f"<div class='actions'>"
            f"<a class='btn-sm btn-green' href='{html.escape(cart_url, quote=True)}' target='_blank'>⚡ Sepete Ekle</a>"
            f"<a class='btn-go' href='{html.escape(str(dd.get('url') or '#'), quote=True)}' target='_blank'>🛒 Depo</a>"
            f"<button class='btn-sm btn-purple' onclick='publishDepo({asin_js})'>📢 Kanala At</button>"
            f"<button class='btn-sm btn-red' title='Tükendiyse kaldır' onclick='deleteDepo({asin_js})'>🗑</button>"
            f"</div></div>"
        )

    logout_btn = (
        "<button class='btn-dark btn-sm' onclick=\"api('/api/auth/logout', {})\">🚪 Çıkış</button>"
        if config.dashboard_password
        else ""
    )
    daemon_btn = (
        "<button class='btn-green' onclick=\"api('/api/daemon/toggle', {paused:false})\">▶️ Otomatik Taramayı Başlat</button>"
        if config.daemon_paused
        else "<button class='btn-amber' onclick=\"api('/api/daemon/toggle', {paused:true})\">⏸ Otomatik Taramayı Duraklat</button>"
    )

    return f"""<!DOCTYPE html>
<html lang="tr">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>7/24 Tam Kapsamlı Fırsat &amp; Kanal Yönetim Merkezi</title>
  <style>
    * {{ box-sizing: border-box; }}
    html, body {{ overflow-x: hidden; max-width: 100%; }}
    body {{ background: #0b1120; color: #f8fafc; font-family: 'Segoe UI', system-ui, -apple-system, sans-serif; margin: 0; padding: 14px 16px 40px 16px; }}
    .topbar {{ display: flex; flex-wrap: wrap; gap: 12px; justify-content: space-between; align-items: center; background: #1e293b; padding: 14px 18px; border-radius: 14px; border: 1px solid #334155; margin-bottom: 12px; }}
    h1 {{ margin: 0 0 6px 0; font-size: 19px; color: #38bdf8; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
    h2 {{ margin: 0; font-size: 15px; color: #e2e8f0; }}
    h3 {{ margin: 12px 0 8px 0; font-size: 14px; color: #7dd3fc; }}
    .pill {{ font-size: 11px; padding: 3px 9px; border-radius: 999px; font-weight: 700; white-space: nowrap; }}
    .pill-green {{ background: #064e3b; color: #6ee7b7; border: 1px solid #10b981; }}
    .pill-amber {{ background: #78350f; color: #fcd34d; border: 1px solid #f59e0b; }}
    .pill-blue {{ background: #0c4a6e; color: #7dd3fc; border: 1px solid #0284c7; }}
    .actions {{ display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }}
    button, .btn-export, .btn-go {{ border: none; cursor: pointer; border-radius: 8px; font-weight: 600; font-size: 13px; padding: 8px 12px; color: #fff; text-decoration: none; display: inline-flex; align-items: center; justify-content: center; gap: 5px; transition: opacity 0.15s; min-height: 36px; }}
    .btn-blue {{ background: #0284c7; }} .btn-blue:hover {{ background: #0369a1; }}
    .btn-green {{ background: #16a34a; }} .btn-green:hover {{ background: #15803d; }}
    .btn-amber {{ background: #d97706; }} .btn-red {{ background: #dc2626; }}
    .btn-purple {{ background: #7c3aed; }} .btn-dark {{ background: #334155; }}
    .btn-sm {{ padding: 6px 10px; font-size: 12px; min-height: 32px; }}
    .btn-go {{ background: #0284c7; padding: 6px 10px; font-size: 12px; min-height: 32px; }}
    .tabs {{ display: flex; flex-wrap: nowrap; overflow-x: auto; -webkit-overflow-scrolling: touch; scrollbar-width: none; gap: 6px; margin-bottom: 14px; background: #111827; padding: 8px; border-radius: 12px; border: 1px solid #1f2937; position: sticky; top: 6px; z-index: 50; }}
    .tabs::-webkit-scrollbar {{ display: none; }}
    .tab-btn {{ background: transparent; color: #94a3b8; border: 1px solid transparent; padding: 8px 13px; border-radius: 8px; font-size: 13px; font-weight: 600; white-space: nowrap; flex: 0 0 auto; }}
    .tab-btn:hover {{ color: #f8fafc; background: #1e293b; }}
    .tab-btn.active {{ background: #0284c7; color: #fff; border-color: #38bdf8; }}
    .tab-pane {{ display: none; }}
    .tab-pane.active {{ display: block; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 360px), 1fr)); gap: 14px; margin-bottom: 16px; }}
    .grid3 {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 210px), 1fr)); gap: 8px; }}
    .card {{ background: #1e293b; border-radius: 12px; padding: 14px; border: 1px solid #334155; margin-bottom: 14px; overflow: hidden; }}
    .card-head {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #334155; padding-bottom: 10px; margin-bottom: 12px; gap: 8px; flex-wrap: wrap; }}
    .offer-row {{ display: flex; justify-content: space-between; align-items: center; padding: 9px 0; border-bottom: 1px solid #293548; font-size: 13px; gap: 8px; flex-wrap: wrap; word-break: break-word; }}
    a {{ color: #38bdf8; text-decoration: none; }}
    .muted {{ color: #94a3b8; font-size: 12px; }}
    .tag {{ background: #334155; color: #cbd5e1; padding: 2px 7px; border-radius: 5px; font-size: 11px; display: inline-block; }}
    .tag.coupon {{ background: #065f46; color: #a7f3d0; }}
    .tag.trade {{ background: #7c2d12; color: #fed7aa; }}
    .chart-img {{ width: 100%; height: auto; border-radius: 8px; margin-top: 10px; }}
    input, textarea, select {{ width: 100%; background: #0f172a; color: #f8fafc; border: 1px solid #475569; border-radius: 8px; padding: 9px 11px; font-size: 14px; margin-top: 4px; margin-bottom: 10px; }}
    label {{ font-size: 12px; color: #cbd5e1; display: block; }}
    .row2 {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 160px), 1fr)); gap: 10px; }}
    .chk {{ display: flex; align-items: center; gap: 8px; margin: 8px 0; font-size: 13px; cursor: pointer; }}
    .chk input {{ width: 18px; height: 18px; margin: 0; flex-shrink: 0; }}
    .source-box {{ background: #0f172a; border: 1px solid #334155; padding: 10px 12px; border-radius: 8px; margin: 0; }}
    pre.codebox {{ background: #090d16; color: #a7f3d0; border: 1px solid #334155; border-radius: 8px; padding: 11px; font-size: 12px; overflow-x: auto; white-space: pre-wrap; word-break: break-word; max-height: 360px; }}
    .warn-banner {{ background: #7f1d1d; border: 1px solid #ef4444; color: #fecaca; padding: 10px 14px; border-radius: 10px; margin-bottom: 12px; font-size: 13px; }}
    #toast {{ position: fixed; bottom: 16px; right: 16px; left: 16px; margin-left: auto; background: #0f172a; border: 2px solid #38bdf8; color: #fff; padding: 13px 16px; border-radius: 10px; display: none; z-index: 999; box-shadow: 0 6px 24px rgba(0,0,0,0.6); font-weight: 600; max-width: 440px; font-size: 13px; }}
    #status-bar {{ display: none; background: #0f172a; border: 1px solid #38bdf8; color: #e2e8f0; padding: 10px 14px; border-radius: 10px; margin-bottom: 12px; font-size: 13px; font-weight: 600; }}
    button:disabled {{ opacity: 0.6; cursor: wait; }}
    @media (max-width: 768px) {{
      body {{ padding: 8px 10px 36px 10px; }}
      .topbar {{ padding: 12px; gap: 10px; }}
      .topbar h1 {{ font-size: 16px; }}
      .topbar .actions {{ width: 100%; display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }}
      .topbar .actions button, .topbar .actions a {{ width: 100%; font-size: 12px; padding: 8px 6px; }}
      .grid, .row2 {{ grid-template-columns: 1fr !important; }}
      .grid3 {{ grid-template-columns: 1fr 1fr !important; }}
      .card {{ padding: 12px; }}
      .offer-row {{ flex-direction: column; align-items: stretch; gap: 8px; }}
      .offer-row .actions {{ width: 100%; justify-content: flex-start; }}
      input, textarea, select {{ font-size: 16px; padding: 10px 12px; }}
      .tab-btn {{ font-size: 12px; padding: 8px 11px; }}
      #toast {{ left: 10px; right: 10px; bottom: 12px; max-width: none; }}
    }}
  </style>
</head>
<body>
  <div id="toast"></div>
  <div id="status-bar"></div>
  {security_warn}

  <!-- ÜST KOMUTA ÇUBUĞU -->
  <div class="topbar">
    <div>
      <h1>
        🎛️ Telegram Bot &amp; Kanal Yönetim Merkezi
        {daemon_badge}
        <span class="pill pill-blue">{html.escape(plat_lbl)} • Süre: {_format_uptime()}</span>
      </h1>
      <div class="muted">
        Kanal: <b>{html.escape(config.telegram_channel_id)}</b> (<span id="ch_members">...</span> üye) •
        Onay Bekleyen: <b>{len(pending_rows)}</b> •
        Çok Satan Takip: <b>{bs_count}</b> •
        X: <b>{x_today}/{config.x_max_tweets_per_day}</b> •
        {html.escape(last_run_str)}
      </div>
    </div>
    <div class="actions">
      {daemon_btn}
      <button class="btn-blue" onclick="api('/api/scan-now', {{}})">🔎 Tam Tarama</button>
      <button class="btn-purple" onclick="api('/api/channel/publish-popular', {{max_posts:{config.max_popular_posts_per_scan}}})">📢 Popüler Yayınla</button>
      <button class="btn-green" onclick="api('/api/scan/community-now', {{}})">⚡ Topluluk Tara</button>
      <button class="btn-dark" onclick="api('/api/scan/bestsellers-now', {{}})">📦 Çok Satan Tara</button>
      <button class="btn-dark" onclick="api('/api/summary/send-now', {{}}, false)">📊 Özeti Gönder</button>
      <a class="btn-green btn-export" href="/export.csv">📥 CSV</a>
      <button class="btn-dark btn-sm" onclick="location.reload()">🔄</button>
      {logout_btn}
    </div>
  </div>

  <!-- 9 ANA SEKME MENÜSÜ -->
  <div class="tabs">
    <button class="tab-btn active" data-tab="tab-overview" onclick="switchTab('tab-overview')">📊 Genel Bakış ({len(pending_rows)})</button>
    <button class="tab-btn" data-tab="tab-depo" onclick="switchTab('tab-depo')">📦 Amazon Depo ({len(depo_deals)}{f' • 🔥{depo_super_cnt}' if depo_super_cnt else ''})</button>
    <button class="tab-btn" data-tab="tab-channel" onclick="switchTab('tab-channel')">📢 Kanal Stüdyosu &amp; Kupon Test</button>
    <button class="tab-btn" data-tab="tab-tracking" onclick="switchTab('tab-tracking')">🎯 Ürün, Hedef, Link &amp; Kelime</button>
    <button class="tab-btn" data-tab="tab-sources" onclick="switchTab('tab-sources')">🌐 Mağaza Kaynakları (12 Site)</button>
    <button class="tab-btn" data-tab="tab-schedule" onclick="switchTab('tab-schedule')">⏰ Zamanlayıcı &amp; Alarm</button>
    <button class="tab-btn" data-tab="tab-intel" onclick="switchTab('tab-intel')">🔮 Piyasa, Çark &amp; Influencer</button>
    <button class="tab-btn" data-tab="tab-integrations" onclick="switchTab('tab-integrations')">🔑 API, X, Affiliate &amp; Güvenlik</button>
    <button class="tab-btn" data-tab="tab-oracle" onclick="switchTab('tab-oracle')">☁️ Oracle Cloud 7/24 Yayın &amp; Loglar</button>
  </div>

  <!-- ==================== SEKME 1: GENEL BAKIŞ ==================== -->
  <div id="tab-overview" class="tab-pane active">
    <div class="card">
      <div class="card-head">
        <h2>🛡 Yayın Onayı Bekleyen Gönderiler ({len(pending_rows)})</h2>
        <div class="actions">
          <button class="btn-green btn-sm" onclick="api('/api/approvals/resolve-all', {{approved:true}})">✅ Tümünü Onayla &amp; Yayınla</button>
          <button class="btn-red btn-sm" onclick="api('/api/approvals/resolve-all', {{approved:false}})">❌ Tümünü Reddet</button>
        </div>
      </div>
      <div>{''.join(pending_rows) or '<p class="muted">Onay bekleyen gönderi yok.</p>'}</div>
    </div>

    <div class="grid">
      {''.join(cards_html)}
    </div>

    <div class="card">
      <div class="card-head">
        <h2>🔥 Son Yayınlanan Kanal Fırsatları ({len(recent_deal_rows)})</h2>
        <div class="actions">
          <input id="quick_pop_n" type="number" value="2" min="1" max="10" style="width:70px;margin:0;padding:5px 8px" />
          <button class="btn-purple btn-sm" onclick="api('/api/channel/publish-popular', {{max_posts:Number(val('quick_pop_n'))}})">📢 Adet Yeni Fırsat Bul &amp; Yayınla</button>
        </div>
      </div>
      <div>{''.join(recent_deal_rows) or '<p class="muted">Henüz kanalda yayınlanan fırsat yok.</p>'}</div>
    </div>
  </div>

  <!-- ==================== SEKME 2: AMAZON DEPO (%40 - %90 FIRSAT RADARI) ==================== -->
  <div id="tab-depo" class="tab-pane">
    <div class="card">
      <div class="card-head">
        <h2>📦 Amazon Depo Özel Radarı — Piyasanın %40–%90 Altındaki Ürünler ({len(depo_deals)})</h2>
        <div class="actions">
          <button class="btn-blue btn-sm" onclick="api('/api/depo/scan', {{}})">🔎 Şimdi Amazon Depo'yu Tara</button>
          <button class="btn-dark btn-sm" onclick="filterDepo(0)">Tümü</button>
          <button class="btn-green btn-sm" onclick="filterDepo(50)">🔥 %50+</button>
          <button class="btn-amber btn-sm" onclick="filterDepo(70)">🚨 %70+ Efsane</button>
          <button class="btn-red btn-sm" onclick="filterDepo(80)">⚡ %80+ Çılgın</button>
          <button class="btn-dark btn-sm" onclick="api('/api/depo/clear', {{}})">🧹 Temizle</button>
        </div>
      </div>
      <div id="depo_list_box">
        {''.join(depo_cards_html) or '<p class="muted">Henüz Amazon Depo taraması yapılmadı veya kriterlere uygun ürün yok. Yukarıdaki <b>🔎 Şimdi Amazon Depo\'yu Tara</b> butonuna basın!</p>'}
      </div>
    </div>

    <div class="card">
      <div class="card-head"><h2>⚙️ Amazon Depo Odak, Kategori, Kelime &amp; %80–%90 Acil Alarm Ayarları</h2></div>
      <label class="chk"><input type="checkbox" id="dp_en" {'checked' if config.depo_enabled else ''} /> <b>7/24 Bağımsız Yüksek Hızlı Amazon Depo Radarı Aktif</b> (Her {config.depo_interval_seconds} saniyede bir yeni düşenleri yakalar)</label>
      <label class="chk"><input type="checkbox" id="dp_admin" {'checked' if config.depo_auto_notify_admin else ''} /> Süper Düşüş Eşiğinde (örn. %75–%90) <b>Kişisel Telegram'ıma Anında Acil Alarm Gönder</b></label>
      <label class="chk"><input type="checkbox" id="dp_ch" {'checked' if config.depo_auto_publish_channel else ''} /> Süper Düşüşleri Fotoğraflı Olarak <b>{html.escape(config.telegram_channel_id)} Kanalına Otomatik Yayınla</b></label>
      <div class="row2">
        <div><label>⚡ Tarama Sıklığı (Saniye, min 15 sn)</label><input id="dp_sec" type="number" min="15" value="{config.depo_interval_seconds}" /></div>
        <div><label>Listeleme Min. İndirim (%)</label><input id="dp_minpct" type="number" value="{config.depo_min_discount_percent}" /></div>
        <div><label>🚨 Acil Alarm Süper İndirim Eşiği (%)</label><input id="dp_superpct" type="number" value="{config.depo_super_discount_percent}" /></div>
        <div><label>Min. Ürün Fiyatı (TL)</label><input id="dp_minp" type="number" value="{int(config.depo_min_price_tl)}" /></div>
        <div><label>Maks. Ürün Fiyatı (TL)</label><input id="dp_maxp" type="number" value="{int(config.depo_max_price_tl)}" /></div>
      </div>
      <label>🔍 Özel Amazon Depo Odak Kelimeleri (Virgülle ayırın — örn: kulaklık, robot süpürge, kahve makinesi, monitör, ssd):</label>
      <input id="dp_kws" type="text" value="{html.escape(', '.join(config.depo_keywords))}" placeholder="Örn: kulaklık, kahve makinesi, monitör, ekran kartı, gamepad" />
      <h3>📂 Taranacak Amazon Depo Kategorileri</h3>
      <div class="grid3" style="margin-bottom:14px">
        {''.join(depo_cat_boxes_html)}
      </div>
      <button class="btn-green" onclick="saveDepoSettings()">💾 Amazon Depo Ayarlarını Kaydet</button>
    </div>
  </div>

  <!-- ==================== SEKME 3: KANAL STÜDYOSU & KUPON TEST ==================== -->
  <div id="tab-channel" class="tab-pane">
    <div class="grid">
      <!-- ÖZEL ÜRÜN LİNKİNDEN FOTOĞRAFLI FIRSAT OLUŞTURUCU -->
      <div class="card">
        <div class="card-head"><h2>🚀 Linkten Otomatik Ürün Fırsatı Oluştur &amp; Kanala Yayınla</h2></div>
        <label>Mağaza Ürün Linki (Amazon, Hepsiburada, Trendyol, Teknosa vb.):</label>
        <div class="row2">
          <div><input id="st_url" type="text" placeholder="https://www.hepsiburada.com/..." /></div>
          <div><input id="st_coupon_raw" type="text" placeholder="Varsa Kupon Kodu (Örn: MEGA200)" /></div>
        </div>
        <button class="btn-blue" onclick="previewStudioDeal()">🔍 Ürün Bilgisi, Fotoğraf, Ortalama Fiyat &amp; Kuponu Çek / Doğrula</button>
        <div id="st_note" class="muted" style="margin:8px 0;color:#6ee7b7"></div>
        <hr style="border-color:#334155;margin:12px 0" />
        <label>Ürün Başlığı:</label>
        <input id="st_title" type="text" placeholder="Önizleme sonrası otomatik dolar veya elle düzenleyin" />
        <div class="row2">
          <div><label>Mağaza Adı</label><input id="st_seller" type="text" placeholder="Hepsiburada" /></div>
          <div><label>Üst Başlık Rozeti</label>
            <select id="st_badge">
              <option value="SON 6 AYIN EN DÜŞÜK FİYATI!">🔥 SON 6 AYIN EN DÜŞÜK FİYATI!</option>
              <option value="🚨⚡ FİYAT HATASI / %60+ ANİ DÜŞÜŞ!">🚨⚡ FİYAT HATASI / %60+ ANİ DÜŞÜŞ!</option>
              <option value="🔥 ANLIK FIRSAT RADARI!">🔥 ANLIK FIRSAT RADARI!</option>
              <option value="ÇOK SATANLARDA FİYAT DÜŞTÜ!">📦 ÇOK SATANLARDA FİYAT DÜŞTÜ!</option>
            </select>
          </div>
        </div>
        <div class="row2">
          <div><label>Şimdiki Fiyat (TL)</label><input id="st_price" type="number" step="0.01" /></div>
          <div><label>Önceki Fiyat (TL)</label><input id="st_old" type="number" step="0.01" /></div>
          <div><label>Piyasa Ortalaması (TL)</label><input id="st_avg" type="number" step="0.01" /></div>
        </div>
        <div class="row2">
          <div><label>Doğrulanmış Kupon Kodu</label><input id="st_code" type="text" placeholder="Boş bırakılabilir" /></div>
          <div><label>Kupon Açıklaması / Talimatı</label><input id="st_cinfo" type="text" placeholder="Örn: Altındaki 200 TL kuponu işaretleyin" /></div>
        </div>
        <label>Ürün Fotoğraf URL'si (Zorunlu):</label>
        <input id="st_img" type="text" placeholder="https://..." oninput="document.getElementById('st_img_prev').src=this.value" />
        <img id="st_img_prev" src="" alt="" style="max-height:130px;border-radius:8px;display:block;margin-bottom:10px" onerror="this.style.display='none'" onload="this.style.display='block'" />
        <label class="chk"><input type="checkbox" id="st_force" /> Daha önce paylaşıldıysa bile tekrar paylaşmaya zorla</label>
        <button class="btn-purple" onclick="publishStudioDeal()">📢 Fotoğraflı Olarak {html.escape(config.telegram_channel_id)} Kanalına Yayınla</button>
      </div>

      <!-- KANAL KURALLARI, DUYURU & KUPON TEST ARACI -->
      <div>
        <div class="card">
          <div class="card-head"><h2>🎟 Canlı Kupon Kodu Doğrulama Testi</h2></div>
          <div class="row2">
            <div><label>Kupon Kodu</label><input id="chk_c_code" type="text" placeholder="Örn: ECOWELL200 veya ALT500" /></div>
            <div><label>Ürün/Kampanya Linki (Opsiyonel)</label><input id="chk_c_url" type="text" placeholder="https://..." /></div>
          </div>
          <button class="btn-green btn-sm" onclick="testCouponOnly()">🧪 Kuponun Geçerliliğini Canlı Test Et</button>
          <div id="chk_c_res" style="margin-top:10px;font-size:13px"></div>
        </div>

        <div class="card">
          <div class="card-head"><h2>⚙️ Tüm Kanal Yayın Kuralları &amp; Filtreleri</h2></div>
          <div class="row2">
            <div><label>Kanal Kullanıcı Adı / ID</label><input id="ch_id" type="text" value="{html.escape(config.telegram_channel_id)}" /></div>
            <div><label>Kanal Paylaşım Linki</label><input id="ch_url" type="text" value="{html.escape(config.telegram_channel_url)}" /></div>
          </div>
          <label class="chk"><input type="checkbox" id="ch_en" {'checked' if config.channel_enabled else ''} /> Kanal Yayını Aktif</label>
          <label class="chk"><input type="checkbox" id="ch_photo_only" {'checked' if config.only_photo_deals else ''} /> <b>Sadece Fotoğraflı Ürünleri Paylaş</b> (Fotoğrafsız ürünleri kesinlikle atla)</label>
          <label class="chk"><input type="checkbox" id="ch_dedup" {'checked' if config.prevent_duplicate_deals else ''} /> <b>Tekrar Eden Ürün Paylaşımını Engelle</b> (SKU + Başlık parmak izi kilidi)</label>
          <label class="chk"><input type="checkbox" id="ch_s25" {'checked' if config.auto_publish_s25_deals else ''} /> Ana Ürün ({html.escape(config.product_name)}) Fırsatlarını Otomatik Yayınla</label>
          <label class="chk"><input type="checkbox" id="ch_pop" {'checked' if config.auto_publish_popular_deals else ''} /> Popüler Fırsatları Otomatik Yayınla</label>
          <label class="chk"><input type="checkbox" id="ch_bs" {'checked' if config.auto_publish_bestseller_drops else ''} /> Çok Satan Düşüşlerini &amp; Fiyat Hatalarını Yayınla</label>
          <label class="chk"><input type="checkbox" id="ch_comm" {'checked' if config.auto_publish_community_deals else ''} /> Topluluk / OnuAl Fırsatlarını Otomatik Yayınla</label>
          <label class="chk"><input type="checkbox" id="ch_disc_always" {'checked' if config.always_show_ad_disclosure else ''} /> Gönderilerde her zaman <code>#reklam #işbirliği</code> etiketi ekle (Yasal Uyum)</label>
          <label class="chk"><input type="checkbox" id="ch_disc_aff" {'checked' if config.show_disclosure_when_affiliate_active else ''} /> Affiliate kodu aktifken yasal etiketi göster</label>
          <div class="row2">
            <div><label>Tarama Başına Maks. Gönderi</label><input id="ch_max" type="number" value="{config.max_popular_posts_per_scan}" /></div>
            <div><label>Min. Gerçek İndirim (%)</label><input id="ch_pct" type="number" value="{config.min_popular_discount_percent}" /></div>
            <div><label>Min. Kazanç (TL)</label><input id="ch_sav" type="number" value="{int(config.min_popular_savings_tl)}" /></div>
            <div><label>Tekrar Paylaşım Ek İndirim (%)</label><input id="ch_repost_pct" type="number" step="0.5" value="{config.duplicate_repost_discount_pct}" /></div>
          </div>
          <button class="btn-green" onclick="saveChannelSettings()">💾 Tüm Kanal Kurallarını Kaydet</button>
        </div>

        <div class="card">
          <div class="card-head">
            <h2>📝 Serbest Duyuru, Bio &amp; Üye Çekme Paketi</h2>
            <button class="btn-blue btn-sm" onclick="loadPromoPack()">📣 Viral Üye Çekme Metinleri Üret</button>
          </div>
          <textarea id="ch_post_txt" rows="3" placeholder="Kanala HTML destekli serbest duyuru mesajı gönderin..."></textarea>
          <label class="chk"><input type="checkbox" id="ch_post_pin" /> Mesajı kanalın en üstüne sabitle (Pin)</label>
          <button class="btn-purple btn-sm" onclick="sendChannelPost()">📤 Duyuruyu Gönder</button>
          <div id="promo_out" style="margin-top:10px"></div>
          <hr style="border-color:#334155;margin:12px 0" />
          <label>Kanal Profil Açıklaması (Bio):</label>
          <input id="ch_desc" type="text" placeholder="Kanal açıklaması..." />
          <button class="btn-dark btn-sm" onclick="api('/api/channel/set-description', {{description: val('ch_desc')}}, false)">💾 Kanal Açıklamasını Güncelle</button>
        </div>
      </div>
    </div>
  </div>

  <!-- ==================== SEKME 3: ÜRÜN, HEDEF, LİNK & KELİME ==================== -->
  <div id="tab-tracking" class="tab-pane">
    <div class="card">
      <div class="card-head">
        <h2>📱 Ana Takip Ürünü, Varyant Hedefleri, Alarm Eşikleri &amp; Çok Satanlar</h2>
        <div class="actions">
          <button class="btn-dark btn-sm" onclick="api('/api/tracking/official', {{enabled: {str(not only_official).lower()}}})">🛡 Resmi Satıcı Filtresi: {'AÇIK ✅' if only_official else 'KAPALI (Tümü)'}</button>
          <button class="btn-red btn-sm" onclick="api('/api/tracking/toggle', {{variant:'ALL', action:'{'resume' if all_paused else 'pause'}'}})">{'▶️ Ana Ürün Takibini Başlat' if all_paused else '🛑 Ana Ürün Takibini Durdur'}</button>
        </div>
      </div>
      <div class="row2">
        <div><label>Ana Ürün Adı</label><input id="pr_name" type="text" value="{html.escape(config.product_name)}" /></div>
        <div><label>Varyantlar (Virgülle ayırın)</label><input id="pr_vars" type="text" value="{html.escape(', '.join(config.variants))}" /></div>
        <div><label>Min. Geçerli Fiyat (TL)</label><input id="pr_minp" type="number" value="{int(config.min_valid_price_tl)}" /></div>
        <div><label>Maks. Geçerli Fiyat (TL)</label><input id="pr_maxp" type="number" value="{int(config.max_valid_price_tl)}" /></div>
      </div>
      <label class="chk"><input type="checkbox" id="pr_norefurb" {'checked' if config.exclude_refurbished else ''} /> Yenilenmiş / İkinci El / Teşhir ürünlerini hariç tut</label>
      <label class="chk"><input type="checkbox" id="pr_official" {'checked' if only_official else ''} /> Yalnızca Resmi / Yetkili Satıcı tekliflerini kabul et</label>

      <h3>🎯 Varyant Hedef Fiyatları &amp; 6 Aylık Baz Fiyatlar</h3>
      <div class="row2">{''.join(target_inputs_html)}</div>
      <div class="row2">{''.join(baseline_inputs_html)}</div>

      <h3>📉 Alarm Tetikleme Eşikleri &amp; Cooldown</h3>
      <div class="row2">
        <div><label>Min. Fiyat Düşüşü (TL)</label><input id="al_droptl" type="number" value="{int(config.min_drop_tl)}" /></div>
        <div><label>Min. Fiyat Düşüşü (%)</label><input id="al_droppct" type="number" step="0.1" value="{config.min_drop_percent}" /></div>
        <div><label>Piyasa Altı Fırsat Eşiğİ (%)</label><input id="al_below" type="number" step="0.5" value="{config.below_market_percent}" /></div>
        <div><label>Dip Fiyat Penceresi (Gün)</label><input id="al_6mdays" type="number" value="{config.six_month_days}" /></div>
        <div><label>Aynı Satıcı Bildirim Bekleme (Saat)</label><input id="al_cool" type="number" value="{config.cooldown_hours}" /></div>
      </div>

      <h3>📦 Çok Satanlar &amp; Fiyat Hatası Takipçisi</h3>
      <label class="chk"><input type="checkbox" id="bs_en" {'checked' if config.bestseller_tracker_enabled else ''} /> Çok Satanlar Fiyat Geçmişi &amp; %60+ Fiyat Hatası Avcısı Aktif</label>
      <div class="row2">
        <div><label>Karşılaştırma Penceresi (Gün)</label><input id="bs_days" type="number" value="{config.bestseller_compare_days}" /></div>
        <div><label>Min. Düşüş Oranı (%)</label><input id="bs_pct" type="number" step="0.5" value="{config.bestseller_min_drop_percent}" /></div>
        <div><label>Min. Düşüş Tutarı (TL)</label><input id="bs_tl" type="number" value="{int(config.bestseller_min_drop_tl)}" /></div>
      </div>
      <button class="btn-green" onclick="saveProductAndAlerts()">💾 Ürün, Hedef ve Alarm Ayarlarının Tümünü Kaydet</button>
    </div>

    <div class="grid">
      <div class="card">
        <div class="card-head"><h2>📋 Özel Ürün Linki Takibi ({len(watched_urls)})</h2></div>
        <div class="row2">
          <div><input id="new_url" type="text" placeholder="https://... mağaza/akakçe linki" /></div>
          <div><input id="new_url_t" type="number" placeholder="Hedef fiyat TL (opsiyonel)" /></div>
        </div>
        <button class="btn-blue btn-sm" onclick="addUrl()">➕ Linki Takibe Ekle</button>
        <div style="margin-top:12px">{''.join(url_rows) or '<p class="muted">Henüz özel link eklenmedi.</p>'}</div>
      </div>

      <div class="card">
        <div class="card-head"><h2>🔍 Kelime Bazlı Çoklu Mağaza Takibi ({len(watched_kws)})</h2></div>
        <div class="row2">
          <div><input id="new_kw" type="text" placeholder="Örn: AirPods Pro 2, Dyson V15" /></div>
          <div><input id="new_kw_t" type="number" placeholder="Hedef fiyat TL (opsiyonel)" /></div>
        </div>
        <div class="actions">
          <button class="btn-blue btn-sm" onclick="addKw()">➕ Tüm Sitelerden Takibe Al</button>
          <button class="btn-dark btn-sm" onclick="searchKwLive(val('new_kw'))">🔎 Takibe Almadan Anlık Ara</button>
        </div>
        <div id="kw_live_results" style="margin-top:10px"></div>
        <div style="margin-top:12px">{''.join(kw_rows) or '<p class="muted">Henüz kelime araması eklenmedi.</p>'}</div>
      </div>
    </div>
  </div>

  <!-- ==================== SEKME 4: MAĞAZA KAYNAKLARI & SCRAPER ==================== -->
  <div id="tab-sources" class="tab-pane">
    <div class="card">
      <div class="card-head">
        <h2>🌐 Taranan E-Ticaret &amp; Fiyat Karşılaştırma Kaynakları (12 Mağaza)</h2>
        <div class="actions">
          <button class="btn-dark btn-sm" onclick="document.querySelectorAll('.src-chk').forEach(c=>c.checked=true)">Tümünü Seç</button>
          <button class="btn-dark btn-sm" onclick="document.querySelectorAll('.src-chk').forEach(c=>c.checked=false)">Tümünü Kaldır</button>
          <button class="btn-green btn-sm" onclick="runSmokeTestPanel()">🧪 Canlı Kaynak Duman Testi Çalıştır</button>
        </div>
      </div>
      <div class="grid3" style="margin-bottom:16px">
        {''.join(source_toggles_html)}
      </div>
      <h3>⚙️ Kazıma (Scraper) Motoru Performans &amp; Bot Koruması Ayarları</h3>
      <div class="row2">
        <div><label>İstek Zaman Aşımı (Saniye)</label><input id="sc_timeout" type="number" value="{config.timeout_seconds}" /></div>
        <div><label>Paralel İş Parçacığı (Max Workers)</label><input id="sc_workers" type="number" value="{config.max_workers}" /></div>
      </div>
      <label class="chk"><input type="checkbox" id="sc_cffi" {'checked' if config.use_curl_cffi else ''} /> <b>curl_cffi TLS Parmak İzi Taklidi</b> (Mağaza bot engellerini aşmak için önerilir)</label>
      <label class="chk"><input type="checkbox" id="sc_pw" {'checked' if config.use_playwright_fallback else ''} /> Playwright Tarayıcı Yedeği (Sunucuda yüksek RAM tüketir, sadece gerektiğinde açın)</label>
      <button class="btn-green" onclick="saveSourcesSettings()">💾 Kaynakları ve Motor Ayarlarını Kaydet</button>
      <div id="smoke_out" style="margin-top:14px"></div>
    </div>
  </div>

  <!-- ==================== SEKME 5: ZAMANLAYICI & UYANDIRMA ALARMI ==================== -->
  <div id="tab-schedule" class="tab-pane">
    <div class="grid">
      <div class="card">
        <div class="card-head"><h2>⏰ Otomatik Tarama Sıklığı, Turbo Kampanya Saatleri &amp; Günlük Özet</h2></div>
        <div class="row2">
          <div><label>Normal Tarama Aralığı (Dakika)</label><input id="sys_int" type="number" value="{config.interval_minutes}" /></div>
          <div><label>Turbo Tarama Aralığı (Dakika)</label><input id="sys_turbo" type="number" value="{config.turbo_interval_minutes}" /></div>
        </div>
        <label>⚡ Turbo Tarama Saat Pencereleri (Virgülle ayırın, örn: 09:00-10:30, 23:45-01:30):</label>
        <input id="sys_tw" type="text" value="{html.escape(', '.join(config.turbo_windows))}" />
        <label>📊 Günlük Telegram Özet Rapor Saatleri (Virgülle ayırın, örn: 09:00, 18:00):</label>
        <input id="sys_sum" type="text" value="{html.escape(', '.join(config.daily_summary_times))}" />
        <label class="chk"><input type="checkbox" id="sys_every" {'checked' if config.notify_every_check else ''} /> Her tarama sonunda değişiklik olmasa da Telegram özet mesajı gönder</label>
        <button class="btn-green" onclick="saveSystem()">💾 Zamanlayıcı Ayarlarını Kaydet</button>
      </div>

      <div class="card">
        <div class="card-head">
          <h2>🚨 Gece Uyandırma Alarmı &amp; CallMeBot Sesli Arama</h2>
          <div class="actions">
            <button class="btn-amber btn-sm" onclick="api('/api/alarm/test', {{}}, false)">⏰ Alarmı Şimdi Test Et</button>
            <button class="btn-red btn-sm" onclick="api('/api/alarm/stop', {{}}, false)">🔕 Alarmı Sustur</button>
          </div>
        </div>
        <label class="chk"><input type="checkbox" id="wu_en" {'checked' if config.wakeup_enabled else ''} /> 6 Aylık Dip Fiyatta Uyandırma Alarmı Aktif</label>
        <div class="row2">
          <div><label>Aktif Saat Penceresi</label><input id="sys_win" type="text" value="{html.escape(config.wakeup_window)}" /></div>
          <div><label>CallMeBot Telegram Kullanıcı Adı</label><input id="wu_call" type="text" value="{html.escape(config.callmebot_telegram_user)}" placeholder="@kullanici_adi" /></div>
        </div>
        <div class="row2">
          <div><label>Tekrar Sayısı</label><input id="wu_rep" type="number" value="{config.wakeup_repeat_count}" /></div>
          <div><label>Tekrar Aralığı (Saniye)</label><input id="wu_rep_sec" type="number" value="{config.wakeup_repeat_interval_seconds}" /></div>
        </div>
        <label class="chk"><input type="checkbox" id="sys_open" {'checked' if config.wakeup_auto_open_browser else ''} /> Masaüstü bilgisayarda dip fırsatı tarayıcıda otomatik aç</label>
        <label class="chk"><input type="checkbox" id="sys_beep" {'checked' if config.wakeup_pc_beep else ''} /> Bilgisayar hoparlöründen sesli okuma + uyarı çal (Windows)</label>
        <button class="btn-green" onclick="saveSystem()">💾 Alarm &amp; Arama Ayarlarını Kaydet</button>
      </div>
    </div>
  </div>

  <!-- ==================== SEKME 6: PİYASA İSTİHBARATI & ALIŞKANLIK ==================== -->
  <div id="tab-intel" class="tab-pane">
    <div class="card">
      <div class="card-head">
        <h2>🔮 Canlı Banka, Resmi Marka, Çark/Tıkla-Kazan &amp; Influencer Kod Radarı</h2>
        <div class="actions">
          <button class="btn-blue btn-sm" onclick="runIntelScan()">🏦 Banka &amp; Marka Tara (Görüntüle)</button>
          <button class="btn-amber btn-sm" onclick="runGamified(false)">🎡 Çark Kampanyalarını Listele</button>
          <button class="btn-amber btn-sm" onclick="runGamified(true)">🎡 Çark Bul &amp; Kanala Yayınla</button>
          <button class="btn-purple btn-sm" onclick="runInfluencer(false)">🏷 Influencer Kodlarını Listele</button>
          <button class="btn-purple btn-sm" onclick="runInfluencer(true)">🏷 Doğrulanmış Kodu Yayınla</button>
        </div>
      </div>
      <div id="intel_live_box" style="margin-bottom:12px"></div>
      <h3>📅 Yaklaşan Kampanya Dönemleri &amp; Strateji</h3>
      <div>{preds_html}</div>
    </div>

    <div class="card">
      <div class="card-head">
        <h2>🧠 Alışveriş Alışkanlıkları &amp; Akıllı Takvim Optimizasyonu</h2>
        <button class="btn-green btn-sm" onclick="api('/api/habits/apply', {{}})">✨ Önerilen Yoğun Saatleri Otomatik Uygula</button>
      </div>
      <div class="muted" style="margin-bottom:8px">
        🏪 <b>Öne Çıkan Mağazalar:</b> {habit_stores} • 📂 <b>Kategoriler:</b> {habit_cats} • ⏰ <b>En Verimli Saatler:</b> {habit_hours}
      </div>
      <div>{habit_recs_html}</div>
    </div>
  </div>

  <!-- ==================== SEKME 7: API, X, AFFILIATE & GÜVENLİK ==================== -->
  <div id="tab-integrations" class="tab-pane">
    <div class="grid">
      <div class="card">
        <div class="card-head">
          <h2>🤖 Telegram Bot &amp; Komut Dinleyici Ayarları</h2>
          <button class="btn-blue btn-sm" onclick="api('/api/integrations/test-telegram', {{}}, false)">🔌 Bağlantıyı Test Et</button>
        </div>
        <label>Telegram Bot Token (Değiştirmek istemiyorsanız dokunmayın):</label>
        <input id="tg_tok" type="password" value="{html.escape(_mask_secret(config.telegram_bot_token))}" placeholder="123456:ABC-DEF..." />
        <div class="row2">
          <div><label>Yönetici Chat ID</label><input id="tg_cid" type="text" value="{html.escape(config.telegram_chat_id)}" /></div>
          <div><label>Varyant Başına Gösterilecek Teklif</label><input id="tg_topn" type="number" value="{config.top_offers_per_variant}" /></div>
        </div>
        <label class="chk"><input type="checkbox" id="tg_cmds" {'checked' if config.enable_bot_commands else ''} /> Telegram Sohbet Komutlarını ve Buton Dinleyiciyi Aktif Tut</label>
        <button class="btn-green" onclick="saveTelegramSettings()">💾 Telegram Ayarlarını Kaydet</button>
      </div>

      <div class="card">
        <div class="card-head"><h2>💰 Affiliate (Satış Ortaklığı) &amp; UTM Etiketleri</h2></div>
        <div class="row2">
          <div><label>Amazon Associate Tag</label><input id="aff_amz" type="text" value="{html.escape(config.amazon_associate_tag)}" placeholder="ornek-21" /></div>
          <div><label>Hepsiburada Affiliate Tag</label><input id="aff_hb" type="text" value="{html.escape(config.hepsiburada_affiliate_tag)}" /></div>
        </div>
        <div class="row2">
          <div><label>Trendyol Affiliate Tag</label><input id="aff_ty" type="text" value="{html.escape(config.trendyol_affiliate_tag)}" /></div>
          <div><label>Admitad / Deeplink Base URL</label><input id="aff_adm" type="text" value="{html.escape(config.admitad_deeplink_base)}" /></div>
        </div>
        <div class="row2">
          <div><label>UTM Source</label><input id="aff_utms" type="text" value="{html.escape(config.affiliate_utm_source)}" /></div>
          <div><label>UTM Medium</label><input id="aff_utmm" type="text" value="{html.escape(config.affiliate_utm_medium)}" /></div>
        </div>
        <button class="btn-green" onclick="saveAffiliate()">💰 Affiliate &amp; UTM Ayarlarını Kaydet</button>
      </div>

      <div class="card">
        <div class="card-head">
          <h2>🐦 X (Twitter) API &amp; Otomatik Paylaşım</h2>
          <div class="actions">
            <button class="btn-blue btn-sm" onclick="api('/api/integrations/test-x', {{}}, false)">🔌 X Test</button>
            <button class="btn-dark btn-sm" onclick="api('/api/x/post-now', {{}}, false)">🐦 Şimdi Tweet At</button>
          </div>
        </div>
        <label class="chk"><input type="checkbox" id="x_auto" {'checked' if config.x_auto_publish_deals else ''} /> En İyi Fırsatları Otomatik Tweetle</label>
        <div class="row2">
          <div><label>Günlük Maks. Tweet</label><input id="x_max" type="number" value="{config.x_max_tweets_per_day}" /></div>
          <div><label>Min. İndirim (%)</label><input id="x_pct" type="number" value="{config.x_min_discount_percent}" /></div>
          <div><label>Tweetler Arası Bekleme (dk)</label><input id="x_wait" type="number" value="{config.x_min_interval_minutes}" /></div>
        </div>
        <div class="row2">
          <div><label>X API Key</label><input id="x_k1" type="password" value="{html.escape(_mask_secret(config.x_api_key))}" /></div>
          <div><label>X API Secret</label><input id="x_k2" type="password" value="{html.escape(_mask_secret(config.x_api_secret))}" /></div>
          <div><label>X Access Token</label><input id="x_k3" type="password" value="{html.escape(_mask_secret(config.x_access_token))}" /></div>
          <div><label>X Access Secret</label><input id="x_k4" type="password" value="{html.escape(_mask_secret(config.x_access_token_secret))}" /></div>
        </div>
        <button class="btn-green" onclick="saveX()">💾 X Ayarlarını Kaydet</button>
      </div>

      <div class="card">
        <div class="card-head"><h2>🔐 Panel Ağ Erişimi &amp; Şifre Koruması (Oracle Cloud Güvenliği)</h2></div>
        <div class="row2">
          <div>
            <label>Dinlenen Ağ Adresi (Host)</label>
            <select id="sec_host">
              <option value="127.0.0.1" {'selected' if config.dashboard_host == '127.0.0.1' else ''}>127.0.0.1 (Sadece Bu Bilgisayar)</option>
              <option value="0.0.0.0" {'selected' if config.dashboard_host == '0.0.0.0' else ''}>0.0.0.0 (Oracle Cloud / Her Yerden Erişim)</option>
            </select>
          </div>
          <div><label>Panel Portu</label><input id="sec_port" type="number" value="{config.dashboard_port}" /></div>
        </div>
        <label>Panel Giriş Şifresi (Oracle sunucuda dış erişime açarken mutlaka belirleyin):</label>
        <input id="sec_pwd" type="password" value="{html.escape(_mask_secret(config.dashboard_password))}" placeholder="Yeni güçlü panel şifresi girin..." />
        <label class="chk"><input type="checkbox" id="sec_clear" /> Şifre korumasını kaldır (Yalnızca 127.0.0.1 yerel kullanımda önerilir)</label>
        <button class="btn-green" onclick="saveSecurity()">🔐 Güvenlik &amp; Ağ Ayarlarını Kaydet</button>
      </div>
    </div>
  </div>

  <!-- ==================== SEKME 8: ORACLE CLOUD 7/24 YAYIN & LOGLAR ==================== -->
  <div id="tab-oracle" class="tab-pane">
    <div class="grid">
      <div class="card">
        <div class="card-head"><h2>☁️ Oracle Cloud Always Free ile 7/24 Kesintisiz Yayın Kurulumu</h2></div>
        <p class="muted" style="line-height:1.5">
          Asistan kotası bitse veya kendi bilgisayarınızı tamamen kapatsanız bile <b>@firsat_takibi</b> kanalının 7/24 yayın yapması ve bu yönetim paneline telefon/bilgisayardan her an erişebilmeniz için sistemi <b>Oracle Cloud Always Free (Ubuntu 22.04 / 24.04)</b> sunucusuna tek adımda taşıyabilirsiniz.
        </p>

        <h3>Seçenek A: Panelden Doğrudan SSH ile Oracle Sunucuya Otomatik Kur / Güncelle</h3>
        <div class="row2">
          <div><label>Oracle Sunucu Public IP</label><input id="ora_ip" type="text" placeholder="Örn: 130.61.123.45" /></div>
          <div><label>SSH Kullanıcı Adı</label><input id="ora_user" type="text" value="ubuntu" placeholder="ubuntu veya opc" /></div>
        </div>
        <div class="row2">
          <div><label>SSH Private Key (.key / .pem) Dosya Yolu</label><input id="ora_key" type="text" placeholder="C:\\path\\to\\ssh-key.key" /></div>
          <div><label>Oracle Panel Giriş Şifresi Belirleyin</label><input id="ora_pwd" type="text" placeholder="Örn: GucluSifre2026!" /></div>
        </div>
        <div class="actions" style="margin-bottom:14px">
          <button class="btn-blue" onclick="deployOracleSSH()">🚀 SSH ile Oracle Sunucuya Bağlan ve 7/24 Servisi Kur</button>
          <button class="btn-green" onclick="downloadOracleZip()">📦 Oracle Yayın Paketini İndir (.zip)</button>
        </div>

        <h3>Seçenek B: İndirilen .zip Paketiyle Manuel (3 Komutla) Kurulum</h3>
        <pre class="codebox"># 1. Yukarıdaki yeşil butonla indirdiğiniz zip dosyasını Oracle sunucuya yükleyin:
scp -i ssh-key.key oracle_7x24_yayin_paketi.zip ubuntu@SUNUCU_IP:~/

# 2. Sunucuya bağlanıp tek komutluk kurucuyu çalıştırın (systemd + güvenlik duvarı otomatik kurulur):
ssh -i ssh-key.key ubuntu@SUNUCU_IP
sudo apt-get update && sudo apt-get install -y unzip && mkdir -p ~/firsat-takip && unzip -o ~/oracle_7x24_yayin_paketi.zip -d ~/firsat-takip && sudo bash ~/firsat-takip/deploy/oracle_setup.sh

# 3. Oracle Cloud Console -> Networking -> Virtual Cloud Networks -> Security Lists -> Ingress Rules:
# Source CIDR: 0.0.0.0/0  |  IP Protocol: TCP  |  Destination Port: {config.dashboard_port}
# Ardından tarayıcıdan girin: http://SUNUCU_IP:{config.dashboard_port}</pre>

        <h3>Seçenek C: Bu Bilgisayarda Asistan Kotasından Bağımsız Çalıştır (Windows Görevi)</h3>
        <p class="muted">Oracle kurana kadar bilgisayarınız açıkken asistan dursa bile yayının kesilmemesi için bağımsız Windows arka plan görevini aktifleştirin:</p>
        <button class="btn-amber btn-sm" onclick="api('/api/system/install-windows-task', {{}}, false)">💻 Bağımsız Windows Arka Plan Görevini Kur &amp; Başlat</button>
      </div>

      <div class="card">
        <div class="card-head">
          <h2>📜 Canlı Sistem Logları &amp; Veritabanı Bakımı</h2>
          <div class="actions">
            <button class="btn-blue btn-sm" onclick="loadLogs()">🔄 Logları Yenile</button>
            <button class="btn-dark btn-sm" onclick="api('/api/logs/clear', {{}}, false).then(loadLogs)">🧹 Logu Temizle</button>
            <a class="btn-green btn-sm btn-export" href="/backup.db">💾 Veritabanı Yedeği (.db)</a>
            <button class="btn-amber btn-sm" onclick="api('/api/db/clear-cooldowns', {{}}, false)">🔓 Bildirim Kilitlerini Sıfırla</button>
          </div>
        </div>
        <div class="muted" style="margin-bottom:8px">
          📊 Toplam Tarama: <b>{db_stats.get('runs', 0)}</b> •
          Fiyat Kaydı: <b>{db_stats.get('snapshots', 0)}</b> •
          Kanal Paylaşımı: <b>{db_stats.get('channel_posts', 0)}</b> •
          Çok Satan Ürünü: <b>{db_stats.get('bestsellers', 0)}</b>
        </div>
        <pre id="log_viewer" class="codebox">Loglar yükleniyor...</pre>
      </div>
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
      localStorage.setItem('ft_active_tab', tabId);
      if (tabId === 'tab-oracle') loadLogs();
    }}

    (function restoreTab() {{
      const saved = localStorage.getItem('ft_active_tab');
      if (saved && document.getElementById(saved)) switchTab(saved);
    }})();

    function setStatus(msg, ok=true, show=true) {{
      const b = document.getElementById('status-bar');
      b.textContent = msg;
      b.style.borderColor = ok ? '#38bdf8' : '#ef4444';
      b.style.display = show ? 'block' : 'none';
    }}

    function toast(msg, ok=true) {{
      const t = document.getElementById('toast');
      t.textContent = msg;
      t.style.borderColor = ok ? '#22c55e' : '#ef4444';
      t.style.display = 'block';
      if (_toastTimer) clearTimeout(_toastTimer);
      _toastTimer = setTimeout(() => {{ t.style.display = 'none'; }}, 6000);
    }}

    function pollScanUntilDone() {{
      setStatus('⏳ Tüm siteler taranıyor, tamamlandığında sayfa otomatik güncellenecek...', true, true);
      const iv = setInterval(async () => {{
        try {{
          const r = await fetch('/api/scan-status');
          const s = await r.json();
          if (!s.running) {{
            clearInterval(iv);
            toast(s.last_msg || 'Tarama tamamlandı!', true);
            setStatus('✅ ' + (s.last_msg || 'Tarama tamamlandı!'), true, true);
            setTimeout(() => location.reload(), 900);
          }}
        }} catch (e) {{}}
      }}, 2000);
    }}

    async function api(url, body, reloadAfter=true) {{
      const btn = (window.event && window.event.target && window.event.target.closest) ? window.event.target.closest('button') : null;
      const origHtml = btn ? btn.innerHTML : '';
      if (btn) {{
        btn.disabled = true;
        btn.innerHTML = '⏳ İşleniyor...';
      }}
      setStatus('⏳ İşlem yürütülüyor: ' + url, true, true);
      try {{
        const res = await fetch(url, {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify(body || {{}})
        }});
        const raw = await res.text();
        let j = null;
        try {{ j = JSON.parse(raw); }} catch (_) {{ j = {{ok: res.ok, msg: raw || ('HTTP ' + res.status)}}; }}
        toast(j.msg || (j.ok ? 'Tamam' : 'Hata'), !!j.ok);
        setStatus((j.ok ? '✅ ' : '❌ ') + (j.msg || ''), !!j.ok, true);
        if (url === '/api/scan-now' && j.ok) {{
          pollScanUntilDone();
          return j;
        }}
        if (j.ok && reloadAfter) setTimeout(() => location.reload(), 850);
        return j;
      }} catch (err) {{
        const emsg = 'Bağlantı hatası: ' + (err && err.message ? err.message : err);
        toast(emsg, false);
        setStatus('❌ ' + emsg, false, true);
        return {{ok: false, msg: emsg}};
      }} finally {{
        if (btn) {{
          btn.disabled = false;
          btn.innerHTML = origHtml;
        }}
      }}
    }}

    function toggleVariant(variant, action) {{
      api('/api/tracking/toggle', {{variant, action}});
    }}

    function deletePublished(deal_key) {{
      api('/api/channel/delete-published', {{deal_key}});
    }}

    function publishDepo(asin) {{
      api('/api/depo/publish', {{asin}}, false);
    }}

    function deleteDepo(asin) {{
      api('/api/depo/delete', {{asin}});
    }}

    function filterDepo(minPct) {{
      document.querySelectorAll('.depo-item').forEach(el => {{
        const p = Number(el.dataset.pct || 0);
        el.style.display = p >= minPct ? 'flex' : 'none';
      }});
    }}

    function saveDepoSettings() {{
      const cats = [];
      document.querySelectorAll('.depo-cat-chk:checked').forEach(el => cats.push(el.dataset.cat));
      api('/api/depo/settings', {{
        depo_enabled: chk('dp_en'),
        depo_interval_seconds: Number(val('dp_sec')),
        depo_auto_notify_admin: chk('dp_admin'),
        depo_auto_publish_channel: chk('dp_ch'),
        depo_min_discount_percent: Number(val('dp_minpct')),
        depo_super_discount_percent: Number(val('dp_superpct')),
        depo_min_price_tl: Number(val('dp_minp')),
        depo_max_price_tl: Number(val('dp_maxp')),
        depo_keywords_csv: val('dp_kws'),
        depo_categories: cats
      }});
    }}

    function sendUrlToStudio(u) {{
      switchTab('tab-channel');
      document.getElementById('st_url').value = u;
      previewStudioDeal();
    }}

    async function previewStudioDeal() {{
      const j = await api('/api/channel/preview-url', {{url: val('st_url'), coupon_code: val('st_coupon_raw')}}, false);
      if (j && j.ok && j.preview) {{
        const p = j.preview;
        document.getElementById('st_title').value = p.title || '';
        document.getElementById('st_seller').value = p.seller || '';
        document.getElementById('st_url').value = p.url || val('st_url');
        document.getElementById('st_price').value = p.price_tl || '';
        document.getElementById('st_old').value = p.old_price_tl || '';
        document.getElementById('st_avg').value = p.market_avg_tl || '';
        document.getElementById('st_code').value = p.coupon_code || '';
        document.getElementById('st_cinfo').value = p.coupon_info || '';
        document.getElementById('st_img').value = p.image_url || '';
        document.getElementById('st_img_prev').src = p.image_url || '';
        document.getElementById('st_note').textContent = p.coupon_note || ('Tahmini gerçek indirim: -%' + (p.discount_percent || 0));
      }}
    }}

    function publishStudioDeal() {{
      api('/api/channel/publish-custom-deal', {{
        url: val('st_url'),
        title: val('st_title'),
        seller: val('st_seller'),
        badge_label: val('st_badge'),
        price_tl: Number(val('st_price')),
        old_price_tl: Number(val('st_old')),
        market_avg_tl: Number(val('st_avg')),
        coupon_code: val('st_code'),
        coupon_info: val('st_cinfo'),
        image_url: val('st_img'),
        force_repost: chk('st_force')
      }});
    }}

    async function testCouponOnly() {{
      const j = await api('/api/channel/verify-coupon', {{coupon_code: val('chk_c_code'), url: val('chk_c_url')}}, false);
      if (j) {{
        const el = document.getElementById('chk_c_res');
        el.style.color = j.valid ? '#4ade80' : '#f87171';
        el.textContent = j.msg || '';
      }}
    }}

    async function loadPromoPack() {{
      const j = await api('/api/channel/promo-pack', {{}}, false);
      if (j && j.promo_html) {{
        document.getElementById('promo_out').innerHTML = `<div class="card" style="background:#0f172a;margin-top:8px">${{j.promo_html.replace(/\\n/g, '<br/>')}}</div>`;
      }}
    }}

    function sendChannelPost() {{
      api('/api/channel/send-post', {{text: val('ch_post_txt'), pin: chk('ch_post_pin')}}, false);
      document.getElementById('ch_post_txt').value = '';
    }}

    function saveChannelSettings() {{
      api('/api/channel/settings', {{
        telegram_channel_id: val('ch_id'),
        telegram_channel_url: val('ch_url'),
        channel_enabled: chk('ch_en'),
        only_photo_deals: chk('ch_photo_only'),
        prevent_duplicate_deals: chk('ch_dedup'),
        auto_publish_s25_deals: chk('ch_s25'),
        auto_publish_popular_deals: chk('ch_pop'),
        auto_publish_bestseller_drops: chk('ch_bs'),
        auto_publish_community_deals: chk('ch_comm'),
        always_show_ad_disclosure: chk('ch_disc_always'),
        show_disclosure_when_affiliate_active: chk('ch_disc_aff'),
        max_popular_posts_per_scan: Number(val('ch_max')),
        min_discount_percent: Number(val('ch_pct')),
        min_savings_tl: Number(val('ch_sav')),
        duplicate_repost_discount_pct: Number(val('ch_repost_pct'))
      }});
    }}

    function saveProductAndAlerts() {{
      const targets = {{}};
      document.querySelectorAll('.var-target-inp').forEach(el => {{ targets[el.dataset.var] = Number(el.value); }});
      const baselines = {{}};
      document.querySelectorAll('.var-base-inp').forEach(el => {{ baselines[el.dataset.var] = Number(el.value); }});
      api('/api/product-alerts/settings', {{
        product_name: val('pr_name'),
        variants_csv: val('pr_vars'),
        min_valid_price_tl: Number(val('pr_minp')),
        max_valid_price_tl: Number(val('pr_maxp')),
        exclude_refurbished: chk('pr_norefurb'),
        only_official: chk('pr_official'),
        targets,
        baselines,
        min_drop_tl: Number(val('al_droptl')),
        min_drop_percent: Number(val('al_droppct')),
        below_market_percent: Number(val('al_below')),
        six_month_days: Number(val('al_6mdays')),
        cooldown_hours: Number(val('al_cool')),
        bestseller_enabled: chk('bs_en'),
        bestseller_compare_days: Number(val('bs_days')),
        bestseller_min_drop_percent: Number(val('bs_pct')),
        bestseller_min_drop_tl: Number(val('bs_tl'))
      }});
    }}

    function saveSourcesSettings() {{
      const sources = {{}};
      document.querySelectorAll('.src-chk').forEach(el => {{ sources[el.dataset.src] = el.checked; }});
      api('/api/sources/settings', {{
        sources,
        timeout_seconds: Number(val('sc_timeout')),
        max_workers: Number(val('sc_workers')),
        use_curl_cffi: chk('sc_cffi'),
        use_playwright_fallback: chk('sc_pw')
      }});
    }}

    async function runSmokeTestPanel() {{
      const j = await api('/api/system/smoke-test', {{send_telegram: false}}, false);
      if (j && j.report_html) {{
        document.getElementById('smoke_out').innerHTML = `<div class="card" style="background:#0f172a">${{j.report_html.replace(/\\n/g, '<br/>')}}</div>`;
      }}
    }}

    function saveSystem() {{
      api('/api/system/settings', {{
        interval_minutes: Number(val('sys_int')),
        turbo_interval_minutes: Number(val('sys_turbo')),
        turbo_windows: val('sys_tw'),
        daily_summary_times: val('sys_sum'),
        notify_every_check: chk('sys_every'),
        wakeup_enabled: chk('wu_en'),
        wakeup_window: val('sys_win'),
        callmebot_telegram_user: val('wu_call'),
        wakeup_repeat_count: Number(val('wu_rep')),
        wakeup_repeat_interval_seconds: Number(val('wu_rep_sec')),
        wakeup_auto_open_browser: chk('sys_open'),
        wakeup_pc_beep: chk('sys_beep')
      }});
    }}

    async function runIntelScan() {{
      const j = await api('/api/intel/scan', {{}}, false);
      if (j && j.intel_html) {{
        document.getElementById('intel_live_box').innerHTML = `<div class="card" style="background:#0f172a">${{j.intel_html.replace(/\\n/g, '<br/>')}}</div>`;
      }}
    }}

    async function runGamified(publish) {{
      const j = await api('/api/intel/gamified', {{publish}}, false);
      if (j && j.items) {{
        const rows = j.items.map(x => `<div class='offer-row'><div>🎡 <b>${{x.source}}:</b> ${{x.title}} <span class='tag'>${{x.benefit}}</span></div><a class='btn-go' href='${{x.url}}' target='_blank'>Aç</a></div>`).join('');
        document.getElementById('intel_live_box').innerHTML = rows || '<p class="muted">Aktif çark kampanyası bulunamadı.</p>';
      }}
    }}

    async function runInfluencer(publish) {{
      const j = await api('/api/intel/influencer', {{publish}}, false);
      if (j && j.items) {{
        const rows = j.items.map(x => `<div class='offer-row'><div>🏷 Kod: <b>${{x.coupon_code}}</b> ${{x.verified ? '✅ Doğrulandı' : '⚠️ Doğrulanamadı'}} — ${{x.title}}</div><a class='btn-go' href='${{x.url}}' target='_blank'>Aç</a></div>`).join('');
        document.getElementById('intel_live_box').innerHTML = rows || '<p class="muted">Yeni influencer kodu bulunamadı.</p>';
      }}
    }}

    function saveTelegramSettings() {{
      api('/api/integrations/telegram', {{
        telegram_bot_token: val('tg_tok'),
        telegram_chat_id: val('tg_cid'),
        enable_bot_commands: chk('tg_cmds'),
        top_offers_per_variant: Number(val('tg_topn'))
      }});
    }}

    function saveX() {{
      api('/api/x/settings', {{
        x_auto_publish_deals: chk('x_auto'),
        x_max_tweets_per_day: Number(val('x_max')),
        x_min_discount_percent: Number(val('x_pct')),
        x_min_interval_minutes: Number(val('x_wait')),
        x_api_key: val('x_k1'),
        x_api_secret: val('x_k2'),
        x_access_token: val('x_k3'),
        x_access_token_secret: val('x_k4')
      }});
    }}

    function saveAffiliate() {{
      api('/api/affiliate/save', {{
        amazon_tag: val('aff_amz'),
        hepsiburada_tag: val('aff_hb'),
        trendyol_tag: val('aff_ty'),
        admitad_base: val('aff_adm'),
        utm_source: val('aff_utms'),
        utm_medium: val('aff_utmm')
      }});
    }}

    function saveSecurity() {{
      api('/api/security/settings', {{
        dashboard_host: val('sec_host'),
        dashboard_port: Number(val('sec_port')),
        dashboard_password: val('sec_pwd'),
        clear_password: chk('sec_clear')
      }});
    }}

    function addUrl() {{
      api('/api/urls/add', {{url: val('new_url'), target_price_tl: val('new_url_t')}});
    }}

    function addKw() {{
      api('/api/keywords/add', {{query: val('new_kw'), target_price_tl: val('new_kw_t')}});
    }}

    async function searchKwLive(q) {{
      if (!q) return;
      const j = await api('/api/keywords/search-now', {{query: q}}, false);
      if (j && j.results) {{
        const htmlRows = j.results.map(r => `<div class='offer-row'><div><b>${{r.price_fmt}}</b> — ${{r.title}} <span class='tag'>${{r.seller}}</span></div><div class='actions'><button class='btn-sm btn-purple' onclick='sendUrlToStudio(${{JSON.stringify(r.url)}})'>📢</button><a class='btn-go' href='${{r.url}}' target='_blank'>🛒</a></div></div>`).join('');
        document.getElementById('kw_live_results').innerHTML = htmlRows || '<p class="muted">Sonuç bulunamadı.</p>';
      }}
    }}

    function downloadOracleZip() {{
      const p = encodeURIComponent(val('ora_pwd') || '');
      window.location.href = '/api/oracle/bundle.zip?pwd=' + p;
    }}

    function deployOracleSSH() {{
      api('/api/oracle/deploy-ssh', {{
        server_ip: val('ora_ip'),
        ssh_user: val('ora_user'),
        ssh_key_path: val('ora_key'),
        panel_password: val('ora_pwd')
      }}, false);
    }}

    async function loadLogs() {{
      try {{
        const r = await fetch('/api/logs?lines=120');
        const j = await r.json();
        const v = document.getElementById('log_viewer');
        if (v) {{
          v.textContent = j.logs || '';
          v.scrollTop = v.scrollHeight;
        }}
      }} catch (e) {{}}
    }}

    fetch('/api/channel/stats').then(r => r.json()).then(d => {{
      const m = document.getElementById('ch_members');
      if (m) m.textContent = d.members ?? '-';
      const desc = document.getElementById('ch_desc');
      if (d.description && desc && !desc.value) desc.value = d.description;
    }}).catch(() => {{}});
  </script>
</body>
</html>"""

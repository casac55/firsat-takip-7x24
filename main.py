from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from pathlib import Path

from src.bot_commands import TelegramCommandListener
from src.config import load_config
from src.dashboard import start_dashboard_server
from src.notifier import TelegramNotifier, format_tl
from src.runner import PriceTrackerRunner, RunResult
from src.storage import PriceStorage
from src.wakeup import WakeupAlarmManager, get_dynamic_interval_minutes


def setup_logging(log_dir: Path, verbose: bool = False) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_dir / "tracker.log", encoding="utf-8"),
    ]
    logging.basicConfig(level=level, format=fmt, handlers=handlers)


def print_run_result(res: RunResult) -> None:
    print(f"\n=== Tarama #{res.run_id} Tamamlandı ({res.sources_ok} kaynak OK, {res.sources_failed} hata) ===")
    for outcome in res.outcomes:
        status = f"{len(outcome.offers)} teklif" if outcome.error is None else f"HATA: {outcome.error}"
        print(f"  - {outcome.source:<12} ({outcome.elapsed_seconds:.2f}s): {status}")

    if not res.grouped_offers:
        print("\nGeçerli Galaxy S25 Ultra teklifi bulunamadı.")
        return

    for variant in ("256 GB", "512 GB", "1 TB"):
        offers = res.grouped_offers.get(variant, [])
        if not offers:
            continue
        print(f"\n[{variant}]")
        for i, off in enumerate(offers, 1):
            c_txt = f' [Kupon: "{off.coupon_code}" / {off.coupon_info}]' if (off.coupon_code or off.coupon_info) else ""
            print(f"  {i}. {format_tl(off.effective_price_tl):>12} | {off.seller:<20} | {off.url}{c_txt}")

    if res.alerts:
        print(f"\n>>> {len(res.alerts)} ADET FIRSAT TESPİT EDİLDİ! <<<")
        for a in res.alerts:
            reasons = ", ".join(r.value for r in a.alert_types)
            print(f"  * {a.offer.variant} - {format_tl(a.offer.price_tl)} ({a.offer.seller}) [{reasons}]")


def main() -> int:
    parser = argparse.ArgumentParser(description="Samsung Galaxy S25 Ultra Fiyat Takip Sistemi")
    parser.add_argument("--config", type=str, default=None, help="Özel config.yaml yolu")
    parser.add_argument("--once", action="store_true", help="Tek seferlik tarama yap ve çık")
    parser.add_argument("--summary", action="store_true", help="Tarama sonrası özet raporu da Telegram'a at")
    parser.add_argument("--daemon", action="store_true", help="Sürekli arka plan modunda çalış")
    parser.add_argument("--report", action="store_true", help="Veritabanındaki son fiyatları terminalde göster")
    parser.add_argument("--test-telegram", action="store_true", help="Telegram bağlantısını test et")
    parser.add_argument("--test-x", action="store_true", help="X (Twitter) API bağlantısını test et")
    parser.add_argument("--verbose", "-v", action="store_true", help="Detaylı log çıktısı")
    args = parser.parse_args()

    config = load_config(args.config)
    setup_logging(config.base_dir / "logs", verbose=args.verbose)
    logger = logging.getLogger("main")

    storage = PriceStorage(config.database_path)
    notifier = TelegramNotifier(config.telegram_bot_token, config.telegram_chat_id)
    wakeup = WakeupAlarmManager(config, notifier)
    runner = PriceTrackerRunner(config, storage, notifier, wakeup=wakeup)

    if args.test_telegram:
        ok, msg = notifier.test_connection()
        print(msg)
        return 0 if ok else 1

    if args.test_x:
        ok, msg = runner.x_publisher.test_connection()
        print(msg)
        return 0 if ok else 1

    if args.report:
        grouped = storage.get_latest_offers_by_variant(config.top_offers_per_variant)
        if not grouped:
            print("Henüz kayıtlı veri yok. Önce 'python main.py --once' çalıştırın.")
            return 0
        for variant in ("256 GB", "512 GB", "1 TB"):
            offers = grouped.get(variant, [])
            if not offers:
                continue
            print(f"\n[{variant}]")
            for i, off in enumerate(offers, 1):
                print(f"  {i}. {format_tl(off.price_tl):>12} | {off.seller:<20} | {off.url}")
        return 0

    if args.once or not args.daemon:
        res = runner.run_once(force_summary=args.summary)
        print_run_result(res)
        return 0

    # Daemon modu
    lock_file = _acquire_daemon_lock(config.base_dir / "data" / "daemon.lock")
    if lock_file is None:
        logger.warning("Başka bir daemon örneği zaten çalışıyor. Çıkılıyor.")
        return 0

    scan_lock = threading.Lock()
    dashboard_server = start_dashboard_server(
        config,
        storage,
        notifier=notifier,
        runner=runner,
        wakeup=wakeup,
        scan_lock=scan_lock,
    )
    listener = None
    if config.enable_bot_commands and notifier.is_configured:
        listener = TelegramCommandListener(config, storage, notifier, runner, scan_lock, wakeup=wakeup)
        listener.start_background()
    elif not notifier.is_configured:
        logger.warning("Telegram ayarlanmamış! Sadece konsol ve SQLite veritabanına kayıt yapılacak.")

    logger.info(
        "Daemon başlatıldı: normal %d dk, kampanya saatlerinde (%s) Turbo %d dk aralıkla kontrol edilecek. "
        "Amazon Depo bağımsız hızlı tarayıcı: %d sn.",
        config.interval_minutes,
        ", ".join(config.turbo_windows),
        config.turbo_interval_minutes,
        config.depo_interval_seconds,
    )

    # Amazon Depo için ana taramadan bağımsız yüksek hızlı arka plan döngüsü (varsayılan 30 sn)
    depo_stop = threading.Event()

    def _depo_sniper_loop() -> None:
        while not depo_stop.is_set():
            try:
                if config.depo_enabled and not config.daemon_paused:
                    runner.scan_amazon_depo(force=False)
            except Exception as exc:
                logger.debug("Amazon Depo hızlı döngü hatası: %s", exc)
            wait_s = max(15, int(getattr(config, "depo_interval_seconds", 30)))
            for _ in range(wait_s):
                if depo_stop.is_set():
                    break
                time.sleep(1)

    threading.Thread(target=_depo_sniper_loop, daemon=True, name="amazon-depo-sniper").start()

    try:
        while True:
            if not config.daemon_paused:
                with scan_lock:
                    res = runner.run_once(force_summary=False)
                print_run_result(res)
            else:
                logger.info("⏸ Otomatik tarama panel üzerinden duraklatılmış durumda.")
            wait_min, is_turbo = get_dynamic_interval_minutes(config)
            if is_turbo and not config.daemon_paused:
                logger.info("⚡ Turbo Tarama Modu aktif: sonraki tarama %d dakika sonra.", wait_min)
            # Panelden süre veya duraklatma değiştirildiğinde anında tepki verebilmek için 2 sn'lik dilimlerle bekle
            elapsed = 0
            while elapsed < max(1, config.turbo_interval_minutes if is_turbo else config.interval_minutes) * 60:
                time.sleep(2)
                elapsed += 2
    except KeyboardInterrupt:
        logger.info("Kullanıcı tarafından durduruldu.")
        if listener:
            listener.stop()
        return 0
    finally:
        try:
            lock_file.close()
        except Exception:
            pass


def _acquire_daemon_lock(lock_path: Path):
    """Aynı anda birden fazla daemon çalışmasını önler (Windows + Linux/Oracle uyumlu)."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(lock_path, "a+", encoding="utf-8")
    try:
        if sys.platform == "win32":
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except OSError:
        f.close()
        return None


if __name__ == "__main__":
    raise SystemExit(main())

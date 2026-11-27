from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from pathlib import Path

from src.config import load_config
from src.dashboard import start_dashboard_server
from src.depo_runner import AmazonDepoRunner
from src.notifier import TelegramNotifier
from src.storage import PriceStorage
from src.tg_radar import TelegramLiveRadar
from src.wakeup import WakeupAlarmManager


def setup_logging(log_dir: Path, verbose: bool = False) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_dir / "tracker.log", encoding="utf-8"),
    ]
    logging.basicConfig(level=level, format=fmt, handlers=handlers)


def _acquire_daemon_lock(lock_path: Path):
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Amazon.com.tr Depo 7/24 Keskin Nişancı Botu")
    parser.add_argument("--config", type=str, default=None, help="Özel config.yaml yolu")
    parser.add_argument("--once", action="store_true", help="Tek seferlik Amazon Depo taraması yap ve çık")
    parser.add_argument("--daemon", action="store_true", help="7/24 arka plan modunda çalış")
    parser.add_argument("--test-telegram", action="store_true", help="Telegram bağlantısını test et")
    parser.add_argument("--verbose", "-v", action="store_true", help="Detaylı log çıktısı")
    args = parser.parse_args()

    config = load_config(args.config)
    setup_logging(config.base_dir / "logs", verbose=args.verbose)
    logger = logging.getLogger("main")

    storage = PriceStorage(config.database_path)
    notifier = TelegramNotifier(config.telegram_bot_token, config.telegram_chat_id)
    wakeup = WakeupAlarmManager(config, notifier)
    depo_runner = AmazonDepoRunner(config, storage, notifier, wakeup=wakeup)

    if args.test_telegram:
        ok, msg = notifier.test_connection()
        print(msg)
        return 0 if ok else 1

    if args.once or not args.daemon:
        stats = depo_runner.run_cycle(force=True)
        print(f"Amazon Depo Taraması Tamamlandı: {stats}")
        return 0

    lock_file = _acquire_daemon_lock(config.base_dir / "data" / "daemon.lock")
    if lock_file is None:
        logger.warning("Başka bir Amazon Depo daemon örneği zaten çalışıyor. Çıkılıyor.")
        return 0

    scan_lock = threading.Lock()
    start_dashboard_server(
        config,
        storage,
        notifier=notifier,
        runner=depo_runner,
        wakeup=wakeup,
        scan_lock=scan_lock,
    )

    logger.info(
        "📦 %%100 Amazon Depo Sniper Daemon başlatıldı! Tarama aralığı: %d sn | Acil alarm eşiği: -%%%d",
        config.depo_interval_seconds,
        config.depo_super_discount_percent,
    )
    TelegramLiveRadar(config, depo_runner).start_background()

    try:
        while True:
            if not config.daemon_paused and config.depo_enabled:
                with scan_lock:
                    depo_runner.run_cycle(force=False)
            else:
                logger.info("⏸ Amazon Depo otomatik taraması panelden duraklatılmış durumda.")

            wait_s = max(15, int(config.depo_interval_seconds))
            elapsed = 0
            while elapsed < wait_s:
                time.sleep(1)
                elapsed += 1
                # Panelden süre kısaltılırsa anında uyum sağla
                if elapsed >= max(15, int(config.depo_interval_seconds)):
                    break
    except KeyboardInterrupt:
        logger.info("Kullanıcı tarafından durduruldu.")
        return 0
    finally:
        try:
            lock_file.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())

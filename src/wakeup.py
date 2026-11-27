from __future__ import annotations

import io
import logging
import math
import re
import struct
import subprocess
import sys
import threading
import wave
import webbrowser
from datetime import datetime
from typing import Optional
from urllib.parse import quote

import requests

from src.config import AppConfig
from src.notifier import TelegramNotifier

logger = logging.getLogger(__name__)


def is_within_wakeup_window(window_str: str, now: Optional[datetime] = None) -> bool:
    """'00:00-23:59' veya '23:45-01:30' biçimindeki saat aralığını kontrol eder."""
    cur = now or datetime.now()
    try:
        start_s, end_s = [p.strip() for p in window_str.split("-", 1)]
        sh, sm = map(int, start_s.split(":"))
        eh, em = map(int, end_s.split(":"))
    except Exception:
        return True

    now_m = cur.hour * 60 + cur.minute
    start_m = sh * 60 + sm
    end_m = eh * 60 + em
    if start_m <= end_m:
        return start_m <= now_m <= end_m
    return now_m >= start_m or now_m <= end_m


def get_dynamic_interval_minutes(
    config: AppConfig, now: Optional[datetime] = None
) -> tuple[int, bool]:
    """
    Gece 00:00 ve akşam kampanya saatlerinde Turbo Tarama aralığını (örn. 4 dk),
    diğer saatlerde normal tarama aralığını (örn. 20 dk) döndürür.
    """
    cur = now or datetime.now()
    for win in config.turbo_windows:
        if is_within_wakeup_window(win, cur):
            return config.turbo_interval_minutes, True
    return config.interval_minutes, False


def generate_alarm_wav_bytes(duration_sec: float = 3.2, sample_rate: int = 16000) -> bytes:
    """Hiçbir dış pakete ihtiyaç duymadan çift tonlu yüksek sesli alarm WAV dosyası üretir."""
    buf = io.BytesIO()
    n_samples = int(duration_sec * sample_rate)
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        frames = bytearray()
        for i in range(n_samples):
            t = i / sample_rate
            freq = 1320.0 if int(t * 4) % 2 == 0 else 880.0
            val = int(26000 * math.sin(2.0 * math.pi * freq * t))
            frames.extend(struct.pack("<h", val))
        wf.writeframes(bytes(frames))
    return buf.getvalue()


class WakeupAlarmManager:
    """
    Son 6 aylık fiyatın altına inen veya stoğa giren fırsatlar için:
    - Tek bir yüksek öncelikli Telegram bildirimi gönderir (5'li mesaj tekrarı kaldırıldı!)
    - CallMeBot üzerinden gerçek Telegram sesli araması başlatır
    - Bilgisayar açıksa mağaza sayfasını tarayıcıda otomatik açar ve Türkçe sesli okur
    """

    def __init__(self, config: AppConfig, notifier: TelegramNotifier) -> None:
        self.config = config
        self.notifier = notifier
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._active_thread: Optional[threading.Thread] = None

    def stop_alarm(self) -> bool:
        was_running = self._active_thread is not None and self._active_thread.is_alive()
        self._stop_event.set()
        return was_running

    def trigger(
        self,
        *,
        summary_html: str,
        voice_text: str,
        reply_markup: Optional[dict] = None,
        direct_url: str = "",
        force: bool = False,
    ) -> None:
        if not force:
            if not self.config.wakeup_enabled:
                return
            if not is_within_wakeup_window(self.config.wakeup_window):
                logger.info(
                    "Uyandırma alarmı pencere dışında (%s), normal bildirim atılıyor.",
                    self.config.wakeup_window,
                )
                self.notifier.send_html(summary_html, reply_markup=reply_markup)
                return

        with self._lock:
            self._stop_event.set()
            self._stop_event = threading.Event()
            stop_ev = self._stop_event
            t = threading.Thread(
                target=self._run_alarm_sequence,
                args=(summary_html, voice_text, reply_markup, direct_url, stop_ev, force),
                daemon=True,
                name="wakeup-alarm",
            )
            self._active_thread = t
            t.start()

    def _run_alarm_sequence(
        self,
        summary_html: str,
        voice_text: str,
        reply_markup: Optional[dict],
        direct_url: str,
        stop_ev: threading.Event,
        is_test: bool,
    ) -> None:
        # 1) CallMeBot gerçek Telegram sesli araması
        if self.config.callmebot_telegram_user:
            threading.Thread(
                target=self._place_callmebot_call_with_retry,
                args=(voice_text, stop_ev),
                daemon=True,
                name="callmebot-call",
            ).start()

        # 2) TEK BİLDİRİM gönder (5'li mesaj tekrarı kaldırıldı)
        ring_header = "⏰🚨 <b>6 AYLIK DİP / FIRSAT UYANDIRMA ALARMI!</b> 🚨⏰\n\n"
        self.notifier.send_html(
            ring_header + summary_html,
            reply_markup=reply_markup,
            disable_notification=False,
        )

        # 3) Masaüstü ortamı varsa gerçek fırsatta doğrudan mağaza sayfasını yeni sekmede otomatik aç (Oracle sunucuda atla)
        import os

        has_gui = sys.platform == "win32" or sys.platform == "darwin" or bool(os.environ.get("DISPLAY"))
        if (
            has_gui
            and not is_test
            and self.config.wakeup_auto_open_browser
            and direct_url
            and direct_url.startswith("http")
        ):
            try:
                webbrowser.open(direct_url, new=2)
                logger.info("Fırsat sayfası tarayıcıda otomatik açıldı: %s", direct_url)
            except Exception as exc:
                logger.debug("Tarayıcı otomatik açılamadı: %s", exc)

        # 4) Bilgisayar hoparlöründen tek seferlik sesli okuma + bip
        if self.config.wakeup_pc_beep:
            self._pc_beep_and_speak(voice_text)

    def _place_callmebot_call_with_retry(self, text: str, stop_ev: threading.Event) -> None:
        user = self.config.callmebot_telegram_user.strip()
        if not user:
            return
        if not user.startswith("@"):
            user = "@" + user

        url = (
            "https://api.callmebot.com/start.php"
            f"?user={quote(user)}&text={quote(text[:220])}&lang=tr-TR-Standard-A&rpt=2"
        )

        for attempt in range(1, 3):
            if stop_ev.is_set():
                return
            try:
                resp = requests.get(url, timeout=25)
                body = resp.text or ""
                bot_match = re.search(r"Using bot:\s*(@\w+)", body)
                calling_bot = bot_match.group(1) if bot_match else "@CallMeBot_API16"

                if "FLOOD" in body or ("line is busy" in body.lower() and "Error:" in body):
                    logger.warning(
                        "CallMeBot hattı yoğun (deneme %d/2, bot: %s). 30sn sonra tekrar denenecek.",
                        attempt,
                        calling_bot,
                    )
                    if attempt == 1 and not stop_ev.wait(timeout=30):
                        continue
                elif "Starting Telegram Audio Call" in body:
                    logger.info("CallMeBot sesli arama başlatıldı (%s -> %s).", calling_bot, user)
                    return
                else:
                    logger.warning("CallMeBot beklenmeyen yanıt: %s", body[:300])
                    return
            except Exception as exc:
                logger.warning("CallMeBot sesli arama hatası (deneme %d): %s", attempt, exc)
                if attempt == 1 and not stop_ev.wait(timeout=15):
                    continue

    def _pc_beep_and_speak(self, speak_text: str = "") -> None:
        if sys.platform != "win32":
            return
        try:
            import winsound

            for freq, dur in ((1200, 220), (1600, 220), (1200, 280)):
                winsound.Beep(freq, dur)
        except Exception:
            pass

        if speak_text:
            try:
                safe_txt = speak_text.replace("'", " ").replace('"', " ")[:180]
                subprocess.Popen(
                    [
                        "powershell",
                        "-NoProfile",
                        "-WindowStyle",
                        "Hidden",
                        "-Command",
                        f"(New-Object -ComObject SAPI.SpVoice).Speak('{safe_txt}') | Out-Null",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass

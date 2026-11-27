from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
from pathlib import Path
from typing import Optional

from src.amazon_depo import AmazonDepoHunter, extract_asin
from src.config import AppConfig

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s<>\"'\)\]]+", re.IGNORECASE)
DEFAULT_RADAR_CHATS = ("PiyasaRADARiViP", "PiyasaRADARiViPchat")


def extract_amazon_asins_from_text(text: str, fetcher=None) -> list[str]:
    """Mesaj metnindeki doğrudan Amazon linklerinden veya amzn.to kısa linklerinden ASIN listesi çıkarır."""
    asins: list[str] = []
    seen: set[str] = set()

    for m in _URL_RE.finditer(text or ""):
        u = m.group(0).rstrip(".,;:")
        a = extract_asin(u)
        if not a and fetcher is not None and any(s in u.lower() for s in ("amzn.to", "amzn.eu", "a.co")):
            try:
                html = fetcher.get_text(u)
                m_dp = re.search(r"/dp/([A-Z0-9]{10})", html)
                if m_dp:
                    a = m_dp.group(1).upper()
            except Exception:
                pass
        if a and a not in seen:
            seen.add(a)
            asins.append(a)
    return asins


class TelegramLiveRadar:
    """
    Telethon (MTProto Client API) kullanarak t.me/s/ web önizlemesi kapalı olan
    kanalları (@PiyasaRADARiViP) ve sohbet gruplarını (@PiyasaRADARiViPchat)
    0.2 saniye gecikmeyle anlık dinler; paylaşılan Amazon ASIN'lerini anında
    Amazon Depo AOD motorundan doğrular ve panele/alarma düşürür.
    """

    def __init__(
        self,
        config: AppConfig,
        depo_runner,
        chats: tuple[str, ...] = DEFAULT_RADAR_CHATS,
    ) -> None:
        self.config = config
        self.depo_runner = depo_runner
        self.chats = chats
        self.api_id = int(os.getenv("TELEGRAM_API_ID", "0") or "0")
        self.api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
        self.session_path = config.base_dir / "data" / "tg_radar"
        self.running = False

    @property
    def is_ready(self) -> bool:
        return bool(
            self.api_id > 0
            and self.api_hash
            and Path(f"{self.session_path}.session").exists()
        )

    def start_background(self) -> Optional[threading.Thread]:
        if not self.is_ready:
            logger.info(
                "Telegram Canlı Kanal/Grup Radarı (@PiyasaRADARiViP) beklemede: "
                "TELEGRAM_API_ID, TELEGRAM_API_HASH ve oturum dosyası (data/tg_radar.session) tanımlandığında aktif olur."
            )
            return None
        t = threading.Thread(target=self._run_thread, daemon=True, name="tg-live-radar")
        t.start()
        return t

    def _run_thread(self) -> None:
        try:
            asyncio.run(self._listen_async())
        except Exception as exc:
            logger.warning("Telegram Canlı Radar durdu: %s", exc)

    async def _listen_async(self) -> None:
        from telethon import TelegramClient, events

        client = TelegramClient(str(self.session_path), self.api_id, self.api_hash)
        await client.start()
        self.running = True
        logger.info("📡 Telegram Canlı Radar aktif! Dinlenen kaynaklar: %s", ", ".join(self.chats))

        @client.on(events.NewMessage(chats=list(self.chats)))
        async def _on_msg(event) -> None:
            txt = event.raw_text or ""
            if not txt:
                return
            asins = await asyncio.to_thread(extract_amazon_asins_from_text, txt, self.depo_runner.fetcher)
            for asin in asins:
                try:
                    deal = await asyncio.to_thread(
                        self.depo_runner.hunter.inspect_asin_aod,
                        asin,
                        category_hint="📡 PiyasaRADARiViP",
                        super_discount_percent=self.config.depo_super_discount_percent,
                        is_watchlist=True,
                    )
                    if deal is None:
                        continue
                    self.depo_runner.storage.upsert_depo_deals([deal])
                    self.depo_runner.last_cycle_ts = __import__("datetime").datetime.now().isoformat(timespec="seconds")
                    if not self.depo_runner.storage.was_depo_asin_notified(deal.asin, deal.depo_price_tl):
                        if self.config.depo_auto_notify_admin and self.depo_runner.notifier.is_configured:
                            self.depo_runner._send_admin_super_alert(deal)
                            self.depo_runner.storage.mark_depo_asin_notified(deal.asin, deal.depo_price_tl)
                except Exception as exc:
                    logger.debug("Radar ASIN kontrol hatası (%s): %s", asin, exc)

        await client.run_until_disconnected()

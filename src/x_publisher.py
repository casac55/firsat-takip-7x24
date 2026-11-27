from __future__ import annotations

import io
import logging
import re
from typing import Optional

import requests

from src.affiliate import convert_to_affiliate_url
from src.config import AppConfig
from src.notifier import format_tl

logger = logging.getLogger(__name__)


class XPublisher:
    """
    X (Twitter) API v2 üzerinden otomatik fırsat tweet'i paylaşır.
    Hem OAuth 2.0 (2 saatte bir otomatik refresh_token yenilemeli)
    hem OAuth 1.0a destekler.
    """

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    @property
    def has_oauth2(self) -> bool:
        return bool(
            self.config.x_oauth2_access_token
            or (
                self.config.x_client_id
                and self.config.x_client_secret
                and self.config.x_oauth2_refresh_token
            )
        )

    @property
    def has_oauth1(self) -> bool:
        return bool(
            self.config.x_api_key
            and self.config.x_api_secret
            and self.config.x_access_token
            and self.config.x_access_token_secret
        )

    @property
    def is_configured(self) -> bool:
        return self.has_oauth2 or self.has_oauth1

    def _build_client(self):
        import tweepy  # type: ignore

        if self.has_oauth2:
            return tweepy.Client(bearer_token=self.config.x_oauth2_access_token)
        return tweepy.Client(
            consumer_key=self.config.x_api_key,
            consumer_secret=self.config.x_api_secret,
            access_token=self.config.x_access_token,
            access_token_secret=self.config.x_access_token_secret,
        )

    def _refresh_oauth2_token(self) -> bool:
        if not (
            self.config.x_client_id
            and self.config.x_client_secret
            and self.config.x_oauth2_refresh_token
        ):
            return False
        try:
            resp = requests.post(
                "https://api.x.com/2/oauth2/token",
                auth=(self.config.x_client_id, self.config.x_client_secret),
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self.config.x_oauth2_refresh_token,
                },
                timeout=15,
            )
            if resp.status_code != 200:
                logger.warning("X OAuth2 token yenileme hatası (%s): %s", resp.status_code, resp.text)
                return False
            data = resp.json()
            new_access = str(data.get("access_token", "")).strip()
            new_refresh = str(data.get("refresh_token", "")).strip()
            if not new_access:
                return False

            self.config.x_oauth2_access_token = new_access
            if new_refresh:
                self.config.x_oauth2_refresh_token = new_refresh
            self._persist_env_tokens(new_access, new_refresh or self.config.x_oauth2_refresh_token)
            logger.info("X OAuth2 access token başarıyla yenilendi.")
            return True
        except Exception as exc:
            logger.warning("X token yenileme istisnası: %s", exc)
            return False

    def _persist_env_tokens(self, access_token: str, refresh_token: str) -> None:
        env_path = self.config.base_dir / ".env"
        if not env_path.exists():
            return
        try:
            content = env_path.read_text(encoding="utf-8")
            content = _replace_env_key(content, "X_OAUTH2_ACCESS_TOKEN", access_token)
            content = _replace_env_key(content, "X_OAUTH2_REFRESH_TOKEN", refresh_token)
            env_path.write_text(content, encoding="utf-8")
        except Exception as exc:
            logger.debug(".env token güncellenemedi: %s", exc)

    def test_connection(self) -> tuple[bool, str]:
        if not self.is_configured:
            return False, ".env dosyasında X API anahtarları tanımlanmalı."
        use_user_auth = not self.has_oauth2
        for attempt in range(2):
            try:
                client = self._build_client()
                me = client.get_me(user_auth=use_user_auth)
                if me and me.data:
                    return True, f"X hesabı bağlantısı başarılı (@{me.data.username})."
                return False, "X hesabı doğrulanamadı."
            except Exception as exc:
                if attempt == 0 and self.has_oauth2 and "401" in str(exc):
                    if self._refresh_oauth2_token():
                        continue
                return False, f"X bağlantı hatası: {exc}"
        return False, "X bağlantısı doğrulanamadı."

    def format_deal_tweet(
        self,
        *,
        title: str,
        price_tl: float,
        seller: str,
        direct_url: str,
        discount_pct: int = 0,
        coupon_code: str = "",
        include_channel_link: Optional[bool] = None,
    ) -> str:
        import random

        aff_url = convert_to_affiliate_url(direct_url, self.config)
        pct_str = f" (%{discount_pct} İndirim)" if discount_pct > 0 else ""
        coupon_str = f' | Kupon: "{coupon_code}"' if coupon_code else ""
        ch_url = self.config.telegram_channel_url

        # Shadowban riskini azaltmak için her tweet'te aynı Telegram linkini tekrarlamak yerine dönüşümlü kullan
        if include_channel_link is None:
            include_channel_link = random.choice([True, False, False])

        headers = [
            f"🔥 Son 6 Ayın En Düşüğü{pct_str}\n",
            f"📉 Güzel Fiyat Yakalandı{pct_str}\n",
            f"⚡ Anlık Fırsat{pct_str}\n",
        ]
        header = random.choice(headers)
        price_line = f"💰 {format_tl(price_tl)} ({seller}){coupon_str}\n"

        if include_channel_link:
            links_block = f"🛒 {aff_url}\n📲 Telegram: {ch_url}"
        else:
            links_block = f"🛒 {aff_url}\n📲 Tüm anlık düşüşler profildeki Telegram kanalında."

        disclosure = (
            "\n#reklam #işbirliği"
            if (self.config.always_show_ad_disclosure or self.config.has_any_affiliate_tag)
            else "\n#indirim"
        )

        fixed_len = len(header) + len(price_line) + len(links_block) + len(disclosure) + 5
        avail_for_title = 280 - fixed_len
        if avail_for_title < 18:
            header = "🔥 Fırsat!\n"
            fixed_len = len(header) + len(price_line) + len(links_block) + len(disclosure) + 5
            avail_for_title = max(10, 280 - fixed_len)

        short_title = title[:avail_for_title].rstrip()
        if len(title) > avail_for_title and len(short_title) > 3:
            short_title = short_title[:-3].rstrip() + "..."

        return f"{header}📦 {short_title}\n{price_line}\n{links_block}{disclosure}"[:280]

    def post_deal_tweet(
        self,
        *,
        title: str,
        price_tl: float,
        seller: str,
        direct_url: str,
        discount_pct: int = 0,
        coupon_code: str = "",
        image_bytes: Optional[bytes] = None,
    ) -> tuple[bool, str]:
        if not self.is_configured:
            return False, "X API anahtarları .env dosyasında tanımlı değil."

        tweet_text = self.format_deal_tweet(
            title=title,
            price_tl=price_tl,
            seller=seller,
            direct_url=direct_url,
            discount_pct=discount_pct,
            coupon_code=coupon_code,
        )
        use_user_auth = not self.has_oauth2
        for attempt in range(2):
            try:
                client = self._build_client()
                resp = client.create_tweet(text=tweet_text, user_auth=use_user_auth)
                tweet_id = resp.data.get("id") if resp and resp.data else ""
                return True, f"https://x.com/i/web/status/{tweet_id}" if tweet_id else "Tweet paylaşıldı."
            except Exception as exc:
                if attempt == 0 and self.has_oauth2 and "401" in str(exc):
                    if self._refresh_oauth2_token():
                        continue
                logger.warning("X tweet gönderme hatası: %s", exc)
                return False, str(exc)
        return False, "Tweet gönderilemedi."


def _replace_env_key(content: str, key: str, value: str) -> str:
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    if pattern.search(content):
        return pattern.sub(f"{key}={value}", content)
    return content.rstrip() + f"\n{key}={value}\n"

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Optional
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

try:
    from curl_cffi import requests as cffi_requests  # type: ignore

    HAS_CURL_CFFI = True
except ImportError:
    cffi_requests = None
    HAS_CURL_CFFI = False


_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
]

# Aynı domain'e eşzamanlı aşırı yüklenip TLS reset (curl 35) yememek için domain bazlı kilit
_DOMAIN_LOCKS_GUARD = threading.Lock()
_DOMAIN_LOCKS: dict[str, threading.Lock] = {}
_DOMAIN_LAST_HIT: dict[str, float] = {}


def _get_domain_lock(url: str) -> tuple[str, threading.Lock]:
    host = (urlparse(url).hostname or "default").lower()
    with _DOMAIN_LOCKS_GUARD:
        lock = _DOMAIN_LOCKS.get(host)
        if lock is None:
            lock = threading.Lock()
            _DOMAIN_LOCKS[host] = lock
        return host, lock


class HttpFetcher:
    """
    Çok katmanlı TLS / HTTP istemcisi:
    1) curl_cffi safari15_5 (Türk e-ticaret CDN'lerinde Post-Quantum TLS takılmasını önler)
    2) Standart requests (OpenSSL / Windows TLS yığını)
    3) curl_cffi chrome120
    4) Opsiyonel Playwright tarayıcı fallback
    """

    def __init__(
        self,
        timeout_seconds: int = 15,
        use_curl_cffi: bool = True,
        use_playwright_fallback: bool = False,
        min_domain_interval: float = 0.35,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.use_curl_cffi = use_curl_cffi and HAS_CURL_CFFI
        self.use_playwright_fallback = use_playwright_fallback
        self.min_domain_interval = min_domain_interval

    def _headers(self, extra: Optional[dict[str, str]] = None) -> dict[str, str]:
        headers = {
            "User-Agent": random.choice(_USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
            "Cache-Control": "no-cache",
            "Upgrade-Insecure-Requests": "1",
        }
        if extra:
            headers.update(extra)
        return headers

    def _pace_domain(self, url: str) -> None:
        if self.min_domain_interval <= 0:
            return
        host, lock = _get_domain_lock(url)
        with lock:
            now = time.monotonic()
            last = _DOMAIN_LAST_HIT.get(host, 0.0)
            wait = self.min_domain_interval - (now - last)
            if wait > 0:
                time.sleep(wait)
            _DOMAIN_LAST_HIT[host] = time.monotonic()

    def get_text(
        self,
        url: str,
        extra_headers: Optional[dict[str, str]] = None,
        retries: int = 1,
    ) -> str:
        strategies: list[tuple[str, object]] = []
        if self.use_curl_cffi and cffi_requests is not None:
            strategies.append(("cffi:safari15_5", "safari15_5"))
            strategies.append(("requests:openssl", None))
            strategies.append(("cffi:chrome120", "chrome120"))
        else:
            strategies.append(("requests:openssl", None))

        errors: list[str] = []
        per_try_timeout = max(6, min(self.timeout_seconds, 12))

        for strat_name, impersonate in strategies:
            try:
                self._pace_domain(url)
                if impersonate and cffi_requests is not None:
                    resp = cffi_requests.get(
                        url,
                        headers=self._headers(extra_headers),
                        impersonate=str(impersonate),
                        timeout=per_try_timeout,
                        allow_redirects=True,
                    )
                else:
                    resp = requests.get(
                        url,
                        headers=self._headers(extra_headers),
                        timeout=per_try_timeout,
                        allow_redirects=True,
                    )

                if resp.status_code == 200 and resp.text:
                    low = resp.text[:2500].lower()
                    if "cf-browser-verification" in low or "just a moment..." in low:
                        errors.append(f"{strat_name}: Bot doğrulaması")
                        continue
                    return resp.text

                errors.append(f"{strat_name}: HTTP {resp.status_code}")
            except Exception as exc:
                errors.append(f"{strat_name}: {type(exc).__name__} ({str(exc)[:80]})")

        if self.use_playwright_fallback:
            html = self._fetch_playwright(url)
            if html:
                return html

        raise RuntimeError(f"Sayfa alınamadı ({url}) [{' | '.join(errors)}]")

    def get_json(
        self,
        url: str,
        extra_headers: Optional[dict[str, str]] = None,
        retries: int = 1,
    ) -> dict | list:
        headers = {"Accept": "application/json, text/plain, */*"}
        if extra_headers:
            headers.update(extra_headers)

        strategies: list[Optional[str]] = (
            ["safari15_5", None, "chrome120"]
            if (self.use_curl_cffi and cffi_requests is not None)
            else [None]
        )
        last_err: Optional[Exception] = None
        per_try_timeout = max(6, min(self.timeout_seconds, 12))

        for imp in strategies:
            try:
                self._pace_domain(url)
                if imp and cffi_requests is not None:
                    resp = cffi_requests.get(
                        url,
                        headers=self._headers(headers),
                        impersonate=imp,
                        timeout=per_try_timeout,
                    )
                else:
                    resp = requests.get(
                        url,
                        headers=self._headers(headers),
                        timeout=per_try_timeout,
                    )
                if resp.status_code == 200:
                    return resp.json()
                last_err = RuntimeError(f"HTTP {resp.status_code}")
            except Exception as exc:
                last_err = exc

        raise RuntimeError(f"JSON alınamadı ({url}): {last_err}")

    def _fetch_playwright(self, url: str) -> Optional[str]:
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except ImportError:
            return None

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(
                    locale="tr-TR",
                    user_agent=random.choice(_USER_AGENTS),
                )
                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_seconds * 1000)
                page.wait_for_timeout(2000)
                content = page.content()
                browser.close()
                return content
        except Exception as exc:
            logger.warning("Playwright fallback başarısız (%s): %s", url, exc)
            return None

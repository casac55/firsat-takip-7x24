from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup

from src.analyzer import is_direct_store_url
from src.fetchers import HttpFetcher
from src.filter import (
    extract_coupon_info,
    extract_trade_in_info,
    is_official_or_trusted_seller,
    normalize_seller_name,
    normalize_tr,
    parse_price_tl,
)
from src.scrapers.structured import extract_json_ld_products

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class KeywordOffer:
    query: str
    title: str
    seller: str
    price_tl: float
    url: str
    is_official_seller: bool = False
    coupon_discount_tl: float = 0.0
    coupon_info: str = ""
    coupon_code: str = ""
    trade_in_info: str = ""

    @property
    def effective_price_tl(self) -> float:
        if self.coupon_discount_tl > 0:
            return round(max(1.0, self.price_tl - self.coupon_discount_tl), 2)
        return self.price_tl


_GENERIC_ACCESSORY_WORDS = ("kilif", "kılıf", "ekran koruyucu", "temperli cam", "hayalet cam", "kordon", "sticker")


def search_product_across_sites(
    query: str, fetcher: HttpFetcher, max_results: int = 6
) -> list[KeywordOffer]:
    """
    Herhangi bir ürün adını (örn. 'AirPods Pro 2', 'PS5 Pro', 'Dyson V15')
    Akakçe (JSON-LD doğrudan mağaza linkleriyle), Trendyol ve Teknosa üzerinde arar.
    """
    clean_q = " ".join(query.strip().split())
    if not clean_q:
        return []

    norm_q = normalize_tr(clean_q)
    query_tokens = [t for t in norm_q.split() if len(t) >= 2]
    user_wants_accessory = any(w in norm_q for w in _GENERIC_ACCESSORY_WORDS)

    offers: list[KeywordOffer] = []
    enc_q = quote_plus(clean_q)

    # 1) Akakçe arama -> En uygun ürün detay sayfasından doğrudan mağaza JSON-LD linkleri
    try:
        search_url = f"https://www.akakce.com/arama/?q={enc_q}"
        s_html = fetcher.get_text(search_url)
        s_soup = BeautifulSoup(s_html, "lxml")

        detail_hrefs: list[tuple[str, str]] = []
        for card in s_soup.select("li.p, ul#APL li, li[data-pr]"):
            t_el = card.select_one("h3.pn_v8, span.pn_v8, a[title]")
            a_el = card.select_one("a[href]")
            if not t_el or not a_el:
                continue
            title = t_el.get("title") or t_el.get_text(" ", strip=True)
            if not _matches_query(title, query_tokens, user_wants_accessory):
                continue
            href = a_el.get("href", "").strip()
            if href and href not in [h for _, h in detail_hrefs]:
                detail_hrefs.append((title, href))
            if len(detail_hrefs) >= 2:
                break

        for prod_title, href in detail_hrefs:
            full_u = href if href.startswith("http") else urljoin("https://www.akakce.com", href)
            try:
                p_html = fetcher.get_text(full_u)
                for item in extract_json_ld_products(p_html):
                    p_val = item.get("price_tl")
                    t_str = str(item.get("title") or prod_title)
                    u_str = str(item.get("url") or full_u)
                    s_str = normalize_seller_name(str(item.get("seller") or "Mağaza"), "Akakçe")
                    if not p_val or p_val < 50:
                        continue
                    if not _matches_query(t_str, query_tokens, user_wants_accessory):
                        continue
                    disc, c_info, c_code = extract_coupon_info(t_str, float(p_val))
                    offers.append(
                        KeywordOffer(
                            query=clean_q,
                            title=t_str,
                            seller=s_str,
                            price_tl=float(p_val),
                            url=u_str,
                            is_official_seller=is_official_or_trusted_seller(s_str, u_str),
                            coupon_discount_tl=disc,
                            coupon_info=c_info,
                            coupon_code=c_code,
                            trade_in_info=extract_trade_in_info(t_str),
                        )
                    )
            except Exception as exc:
                logger.debug("Kelime arama Akakçe detay hatası: %s", exc)
    except Exception as exc:
        logger.debug("Kelime arama Akakçe hatası: %s", exc)

    # 2) Trendyol doğrudan arama
    try:
        ty_url = f"https://www.trendyol.com/sr?q={enc_q}"
        ty_html = fetcher.get_text(ty_url)
        ty_soup = BeautifulSoup(ty_html, "lxml")
        for card in ty_soup.select("a.product-card")[:8]:
            txt = card.get_text(" ", strip=True)
            if not _matches_query(txt, query_tokens, user_wants_accessory):
                continue
            import re

            m_prices = re.findall(r"(\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?)\s*TL", txt, re.IGNORECASE)
            if not m_prices:
                continue
            p_val = parse_price_tl(m_prices[-1])
            if not p_val or p_val < 50:
                continue
            u_str = urljoin("https://www.trendyol.com", card.get("href", ""))
            disc, c_info, c_code = extract_coupon_info(txt, p_val)
            offers.append(
                KeywordOffer(
                    query=clean_q,
                    title=txt[:130],
                    seller="Trendyol",
                    price_tl=p_val,
                    url=u_str,
                    is_official_seller=is_official_or_trusted_seller("Trendyol", u_str),
                    coupon_discount_tl=disc,
                    coupon_info=c_info,
                    coupon_code=c_code,
                    trade_in_info=extract_trade_in_info(txt),
                )
            )
    except Exception as exc:
        logger.debug("Kelime arama Trendyol hatası: %s", exc)

    # Satıcı bazlı tekilleştir (doğrudan mağaza linkini tercih et)
    best_by_seller: dict[str, KeywordOffer] = {}
    for off in offers:
        key = off.seller.lower()
        ex = best_by_seller.get(key)
        if ex is None:
            best_by_seller[key] = off
            continue
        if is_direct_store_url(off.url) and not is_direct_store_url(ex.url):
            best_by_seller[key] = off
        elif off.effective_price_tl < ex.effective_price_tl:
            best_by_seller[key] = off

    sorted_offers = sorted(best_by_seller.values(), key=lambda o: o.effective_price_tl)
    return sorted_offers[:max_results]


def _matches_query(title: str, tokens: list[str], allow_accessory: bool) -> bool:
    norm = normalize_tr(title)
    if not allow_accessory:
        for acc in _GENERIC_ACCESSORY_WORDS:
            if acc in norm:
                return False
    return all(tok in norm for tok in tokens)

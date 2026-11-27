from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from src.fetchers import HttpFetcher
from src.filter import (
    extract_coupon_info,
    extract_trade_in_info,
    normalize_seller_name,
    parse_price_tl,
)
from src.scrapers.structured import extract_json_ld_products

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class WatchedProductSnapshot:
    url: str
    title: str
    price_tl: float
    seller: str
    in_stock: bool = True
    coupon_discount_tl: float = 0.0
    coupon_info: str = ""
    coupon_code: str = ""
    trade_in_info: str = ""
    direct_url: str = ""

    @property
    def effective_price_tl(self) -> float:
        if self.coupon_discount_tl > 0:
            return round(max(1.0, self.price_tl - self.coupon_discount_tl), 2)
        return self.price_tl

    @property
    def best_url(self) -> str:
        return self.direct_url or self.url


_DOMAIN_SELLERS = {
    "amazon.com.tr": "Amazon.com.tr",
    "hepsiburada.com": "Hepsiburada",
    "trendyol.com": "Trendyol",
    "teknosa.com": "Teknosa",
    "mediamarkt.com.tr": "MediaMarkt",
    "vatanbilgisayar.com": "Vatan Bilgisayar",
    "n11.com": "N11",
    "pazarama.com": "Pazarama",
    "idefix.com": "İdefix",
    "akakce.com": "Akakçe",
    "cimri.com": "Cimri",
    "samsung.com": "Samsung Türkiye",
    "migros.com.tr": "Migros",
    "ciceksepeti.com": "ÇiçekSepeti",
    "pttavm.com": "PttAVM",
}


def infer_seller_from_url(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    for domain, name in _DOMAIN_SELLERS.items():
        if host == domain or host.endswith("." + domain):
            return name
    return host or "Web"


def parse_product_page(html: str, url: str) -> Optional[WatchedProductSnapshot]:
    """Herhangi bir e-ticaret ürün sayfasından başlık, fiyat ve stok bilgisini çıkarır."""
    soup = BeautifulSoup(html, "lxml")
    default_seller = infer_seller_from_url(url)

    title = _extract_title(soup)
    price_tl: Optional[float] = None
    seller = default_seller
    in_stock = True
    direct_url = ""

    # 1) JSON-LD Product / Offer (en güvenilir yapısal veri)
    ld_items = extract_json_ld_products(html)
    valid_ld = [x for x in ld_items if x.get("price_tl") and x["price_tl"] > 0]
    if valid_ld:
        # Stokta olan en ucuz teklifi seç
        in_stock_ld = [x for x in valid_ld if x.get("in_stock", True)]
        chosen = min(in_stock_ld or valid_ld, key=lambda x: float(x["price_tl"]))
        price_tl = float(chosen["price_tl"])
        if chosen.get("title"):
            title = str(chosen["title"]).strip()
        if chosen.get("seller"):
            seller = normalize_seller_name(str(chosen["seller"]), default_seller)
        if chosen.get("url") and str(chosen["url"]).startswith("http"):
            direct_url = str(chosen["url"]).strip()
        in_stock = bool(chosen.get("in_stock", True))

    # 2) OpenGraph / Meta fiyat etiketleri
    if price_tl is None:
        meta_selectors = (
            ("meta[property='product:price:amount']", "content"),
            ("meta[property='og:price:amount']", "content"),
            ("meta[itemprop='price']", "content"),
            ("meta[name='twitter:data1']", "content"),
        )
        for sel, attr in meta_selectors:
            el = soup.select_one(sel)
            if el and el.get(attr):
                parsed = parse_price_tl(el.get(attr))
                if parsed and parsed >= 10:
                    price_tl = parsed
                    break

    # 3) Siteye özel ve genel CSS seçiciler
    if price_tl is None:
        price_selectors = (
            # Amazon
            "#corePrice_feature_div span.a-price span.a-offscreen",
            "#corePriceDisplay_desktop_feature_div span.a-price span.a-offscreen",
            "span.a-price span.a-offscreen",
            "#priceblock_ourprice",
            "#priceblock_dealprice",
            # Trendyol
            "span.prc-dsc",
            "div.pr-bx-nm span.prc-slg",
            # Hepsiburada
            "span[data-bind*='currentPriceBeforePoint']",
            "div[data-test-id='price-current-price']",
            "#offering-price",
            # Vatan / Teknosa / MediaMarkt / N11 / Pazarama / Akakçe
            "span.product-list__price",
            "span.prc-last",
            "div[data-test='mms-price']",
            "div.newPrice ins",
            "span.pt_v8",
            # Genel schema.org / fiyat sınıfları
            "[itemprop='price']",
            "[class*='salePrice']",
            "[class*='currentPrice']",
            "[class*='product-price']",
        )
        for sel in price_selectors:
            el = soup.select_one(sel)
            if el:
                raw_txt = el.get("content") or el.get_text(" ", strip=True)
                parsed = parse_price_tl(raw_txt)
                if parsed and parsed >= 10:
                    price_tl = parsed
                    break

    # 4) Gömülü JSON fallback (Next.js / SPA state içinde sellingPrice / discountedPrice / nested value)
    if price_tl is None:
        for pat in (
            r'"discountedPrice"\s*:\s*\{[^{}]{0,80}"value"\s*:\s*(\d+(?:\.\d+)?)',
            r'"sellingPrice"\s*:\s*\{[^{}]{0,80}"value"\s*:\s*(\d+(?:\.\d+)?)',
            r'"discountedPrice"\s*:\s*(\d+(?:\.\d+)?)',
            r'"sellingPrice"\s*:\s*(\d+(?:\.\d+)?)',
            r'"currentPrice"\s*:\s*(\d+(?:\.\d+)?)',
            r'"formattedPrice"\s*:\s*"([^"]{2,25})"',
            r'"price"\s*:\s*"?(\d+(?:[\.,]\d+)?)"?',
        ):
            m = re.search(pat, html)
            if m:
                parsed = parse_price_tl(m.group(1))
                if parsed and parsed >= 10:
                    price_tl = parsed
                    break

    # 5) Amazon parça fiyat (a-price-whole + a-price-fraction) veya genel TL fiyat yakalama
    if price_tl is None:
        whole_el = soup.select_one("span.a-price-whole")
        if whole_el:
            frac_el = soup.select_one("span.a-price-fraction")
            raw_amz = whole_el.get_text("", strip=True).rstrip(",.")
            if frac_el:
                raw_amz = f"{raw_amz},{frac_el.get_text('', strip=True)}"
            parsed = parse_price_tl(raw_amz)
            if parsed and parsed >= 10:
                price_tl = parsed

    if not title:
        title = _extract_title_from_url_slug(url)
    if not title:
        return None

    low_html = html.lower()
    oos_markers = (
        "stokta yok",
        "tükendi",
        "tukendi",
        "gelince haber ver",
        "temin edilemiyor",
        "şu anda mevcut değil",
        "satışta değil",
    )
    if any(m in low_html for m in oos_markers) and price_tl is None:
        in_stock = False
        price_tl = 0.0

    if price_tl is None:
        return None

    # Kupon / sepette ek indirim alanlarını tara
    coupon_chunks: list[str] = [title]
    for c_sel in (
        "#couponText",
        "span.couponBadge",
        "[class*='coupon']",
        "[class*='Coupon']",
        "[class*='basket']",
        "[class*='sepette']",
        "[data-test-id*='campaign']",
        "div.pr-in-w",
    ):
        for el in soup.select(c_sel)[:4]:
            txt = el.get_text(" ", strip=True)
            if txt:
                coupon_chunks.append(txt)

    joined_chunks = " | ".join(coupon_chunks)
    discount_tl, coupon_info, coupon_code = (
        extract_coupon_info(joined_chunks, float(price_tl))
        if price_tl > 0
        else (0.0, "", "")
    )
    trade_in = extract_trade_in_info(joined_chunks)

    return WatchedProductSnapshot(
        url=url,
        title=" ".join(title.split())[:180],
        price_tl=float(price_tl),
        seller=seller,
        in_stock=in_stock,
        coupon_discount_tl=discount_tl,
        coupon_info=coupon_info,
        coupon_code=coupon_code,
        trade_in_info=trade_in,
        direct_url=direct_url or url,
    )


def _extract_title(soup: BeautifulSoup) -> str:
    for sel in ("#productTitle", "h1[data-test-id='title']", "h1.pr-new-br", "h1"):
        el = soup.select_one(sel)
        if el:
            txt = el.get_text(" ", strip=True)
            if txt:
                return txt
    og = soup.select_one("meta[property='og:title']")
    if og and og.get("content"):
        return str(og["content"]).strip()
    if soup.title:
        return soup.title.get_text(" ", strip=True)
    return ""


def _extract_title_from_url_slug(url: str) -> str:
    """Mağaza 403/503 engeli verdiğinde URL yolundan okunabilir ürün adını çıkarır."""
    path = (urlparse(url).path or "").strip("/")
    if not path:
        return ""
    segments = [s for s in path.split("/") if s and s.lower() not in ("tr", "product", "urun", "dp", "p", "sr")]
    if not segments:
        return ""
    # En uzun anlamlı segmenti seç
    slug = max(segments, key=len)
    slug = re.sub(r"\.html?$", "", slug, flags=re.IGNORECASE)
    slug = re.sub(r"-(?:p|pm)-[A-Z0-9]{5,20}$", "", slug, flags=re.IGNORECASE)
    slug = re.sub(r"-p-\d{4,15}$", "", slug, flags=re.IGNORECASE)
    slug = re.sub(r"-\d{5,12}$", "", slug)
    slug = slug.lstrip("_-")
    words = [w for w in re.split(r"[-_+]+", slug) if w and not (w.isdigit() and len(w) >= 5)]
    if not words:
        return ""
    return " ".join(w.capitalize() if w.islower() else w for w in words[:12])


def _fallback_via_akakce_search(
    url: str, title_hint: str, fetcher: HttpFetcher
) -> Optional[WatchedProductSnapshot]:
    """Mağaza doğrudan 403/503 verirse ürün adını Akakçe üzerinden arayıp canlı piyasa fiyatını kurtarır."""
    from urllib.parse import quote_plus

    tokens = re.findall(r"[A-Za-zÇĞİÖŞÜçğıöşü0-9]+", title_hint)
    if not tokens:
        return None
    # Önce kısa marka+model sorgusu (2-3 kelime) ASCII Türkçe sluglarda çok daha yüksek eşleşme sağlar
    candidate_queries: list[str] = []
    model_tokens = [t for t in tokens[:6] if any(ch.isdigit() for ch in t)]
    if model_tokens and len(tokens) >= 2:
        q_model = f"{tokens[0]} {model_tokens[0]}".strip()
        if len(q_model) >= 4:
            candidate_queries.append(q_model)
    for n in (3, 4, 2):
        q = " ".join(tokens[:n]).strip()
        if len(q) >= 4 and q not in candidate_queries:
            candidate_queries.append(q)

    default_seller = infer_seller_from_url(url)
    for clean_q in candidate_queries:
        try:
            from src.keyword_search import search_product_across_sites

            offers = search_product_across_sites(clean_q, fetcher, max_results=6)
            if offers:
                same_store = [o for o in offers if default_seller.lower() in o.seller.lower()]
                best = same_store[0] if same_store else offers[0]
                return WatchedProductSnapshot(
                    url=url,
                    title=(best.title or title_hint)[:180],
                    price_tl=float(best.price_tl),
                    seller=best.seller or default_seller,
                    in_stock=True,
                    coupon_discount_tl=float(getattr(best, "coupon_discount_tl", 0.0) or 0.0),
                    coupon_info=str(getattr(best, "coupon_info", "") or ""),
                    coupon_code=str(getattr(best, "coupon_code", "") or ""),
                    direct_url=url,
                )
        except Exception as exc:
            logger.debug("search_product_across_sites fallback hatası (%s): %s", clean_q, exc)

        # Doğrudan Akakçe arama kartından (span.pt_v8) hızlı kurtarma
        try:
            s_html = fetcher.get_text(f"https://www.akakce.com/arama/?q={quote_plus(clean_q)}")
            s_soup = BeautifulSoup(s_html, "lxml")
            for card in s_soup.select("li.p, ul#APL li, li[data-pr], li.p_v8")[:5]:
                p_el = card.select_one("span.pt_v8, span.pt_v9")
                if not p_el:
                    continue
                p_val = parse_price_tl(p_el.get_text(" ", strip=True))
                if p_val and p_val >= 10:
                    t_el = card.select_one("h3.pn_v8, span.pn_v8, a[title]")
                    c_title = (
                        (t_el.get("title") or t_el.get_text(" ", strip=True))
                        if t_el
                        else title_hint
                    )
                    return WatchedProductSnapshot(
                        url=url,
                        title=" ".join(str(c_title).split())[:180],
                        price_tl=float(p_val),
                        seller=f"{default_seller} (Akakçe)",
                        in_stock=True,
                        direct_url=url,
                    )
        except Exception as exc:
            logger.debug("Akakçe kart kurtarma hatası (%s): %s", clean_q, exc)

        # 3) Oracle Cloud IP'lerinde %100 erişilebilen Hepsiburada ve N11 arama kurtarması
        for store_lbl, search_u, item_sel, title_sel, price_sel in (
            (
                "Hepsiburada",
                f"https://www.hepsiburada.com/ara?q={quote_plus(clean_q)}",
                "li[id^='i'], [data-test-id='product-card-item'], article",
                "h3, [data-test-id='product-card-name'], a[title]",
                "[data-test-id='price-current-price'], [data-test-id='final-price-1'], [class*='price']",
            ),
            (
                "N11",
                f"https://www.n11.com/arama?q={quote_plus(clean_q)}",
                "li.column, div.pro",
                "h3.productName, a[title]",
                "span.newPrice ins, ins, span[class*='price']",
            ),
        ):
            try:
                st_html = fetcher.get_text(search_u)
                st_soup = BeautifulSoup(st_html, "lxml")
                # Önce JSON-LD içinde fiyat varsa al
                ld_prods = [x for x in extract_json_ld_products(st_html) if x.get("price_tl") and float(x["price_tl"]) >= 10]
                if ld_prods:
                    best_ld = min(ld_prods, key=lambda x: float(x["price_tl"]))
                    return WatchedProductSnapshot(
                        url=url,
                        title=" ".join(str(best_ld.get("title") or title_hint).split())[:180],
                        price_tl=float(best_ld["price_tl"]),
                        seller=f"{default_seller} ({store_lbl} Ref)",
                        in_stock=True,
                        direct_url=url,
                    )
                for card in st_soup.select(item_sel)[:8]:
                    p_el = card.select_one(price_sel)
                    if not p_el:
                        continue
                    p_val = parse_price_tl(p_el.get_text(" ", strip=True))
                    if p_val and p_val >= 10:
                        t_el = card.select_one(title_sel)
                        c_title = (
                            (t_el.get("title") or t_el.get_text(" ", strip=True))
                            if t_el
                            else title_hint
                        )
                        return WatchedProductSnapshot(
                            url=url,
                            title=" ".join(str(c_title).split())[:180],
                            price_tl=float(p_val),
                            seller=f"{default_seller} ({store_lbl} Ref)",
                            in_stock=True,
                            direct_url=url,
                        )
            except Exception as exc:
                logger.debug("%s slug kurtarma hatası (%s): %s", store_lbl, clean_q, exc)
    return None


def fetch_watched_product(
    url: str,
    fetcher: HttpFetcher,
    storage: Optional[object] = None,
    allow_fallback: bool = False,
) -> WatchedProductSnapshot:
    page_title_hint = ""
    try:
        html = fetcher.get_text(url)
        snap = parse_product_page(html, url)
        if snap is not None:
            return snap
        page_title_hint = _extract_title(BeautifulSoup(html, "lxml"))
        raise ValueError("Sayfadan ürün adı veya fiyatı ayrıştırılamadı.")
    except Exception as exc:
        # 1) Eğer veritabanında bu URL için yakın zamanda çekilmiş önbellek varsa oradan kurtar
        if storage is not None and hasattr(storage, "get_cached_snapshot_by_url"):
            cached = storage.get_cached_snapshot_by_url(url)  # type: ignore[attr-defined]
            if cached:
                logger.info("Canlı sayfa hatası (%s), önbellekten kurtarıldı: %s", exc, url)
                price = float(cached["price_tl"])
                eff = float(cached["eff_price"])
                return WatchedProductSnapshot(
                    url=url,
                    title=str(cached["title"]),
                    price_tl=price,
                    seller=str(cached["seller"]),
                    in_stock=True,
                    coupon_discount_tl=max(0.0, round(price - eff, 2)),
                    coupon_info=str(cached.get("coupon_info") or ""),
                )

        # 2) Mağaza 403/503 engeli verdiyse URL slug/başlıktan Akakçe canlı fiyat kurtarması dene
        title_hint = page_title_hint or _extract_title_from_url_slug(url)
        if title_hint:
            akakce_snap = _fallback_via_akakce_search(url, title_hint, fetcher)
            if akakce_snap is not None:
                logger.info(
                    "Doğrudan mağaza engeli (%s), Akakçe üzerinden fiyat kurtarıldı: %s -> %.2f TL",
                    type(exc).__name__,
                    akakce_snap.title[:50],
                    akakce_snap.effective_price_tl,
                )
                return akakce_snap

        # 3) Panelden yeni ekleme yapılıyorsa hata fırlatmak yerine linki yine de takibe al
        if allow_fallback:
            seller = infer_seller_from_url(url)
            fallback_title = title_hint or f"{seller} Ürünü"
            logger.warning("Link ilk eklemede canlı okunamadı (%s), bekleyen kayıt olarak eklendi: %s", exc, url)
            return WatchedProductSnapshot(
                url=url,
                title=fallback_title[:180],
                price_tl=0.0,
                seller=seller,
                in_stock=False,
                direct_url=url,
            )
        raise

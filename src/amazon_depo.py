from __future__ import annotations

import logging
import re
import statistics
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote_plus

from bs4 import BeautifulSoup

from src.fetchers import HttpFetcher
from src.filter import parse_price_tl
from src.scrapers.structured import extract_json_ld_products

logger = logging.getLogger(__name__)

AMAZON_DEPO_MERCHANT_ID = "A215JX4S9CANSO"

DEPO_CATEGORY_FEEDS: dict[str, tuple[str, str]] = {
    "tumu": ("Tüm Amazon Depo (En Yeni)", "https://www.amazon.com.tr/s?i=warehouse-deals&s=date-desc-rank"),
    "populer": ("Amazon Depo (Öne Çıkanlar)", "https://www.amazon.com.tr/s?i=warehouse-deals&s=exact-aware-popularity-rank"),
    "yildizli": ("Amazon Depo (Çok Değerlendirilen)", "https://www.amazon.com.tr/s?i=warehouse-deals&s=review-rank"),
    "elektronik": ("Elektronik", "https://www.amazon.com.tr/s?i=warehouse-deals&rh=n%3A12466496031&s=date-desc-rank"),
    "bilgisayar": ("Bilgisayar & Bileşen", "https://www.amazon.com.tr/s?i=warehouse-deals&rh=n%3A12466439031&s=date-desc-rank"),
    "ev_mutfak": ("Ev & Mutfak", "https://www.amazon.com.tr/s?i=warehouse-deals&rh=n%3A12466553031&s=date-desc-rank"),
    "kisisel_bakim": ("Kişisel Bakım & Kozmetik", "https://www.amazon.com.tr/s?i=warehouse-deals&rh=n%3A12466702031&s=date-desc-rank"),
    "oyuncak": ("Oyuncak & Oyun", "https://www.amazon.com.tr/s?i=warehouse-deals&rh=n%3A12466209031&s=date-desc-rank"),
    "spor": ("Spor & Outdoor", "https://www.amazon.com.tr/s?i=warehouse-deals&rh=n%3A12466528031&s=date-desc-rank"),
}

_DIGITAL_SKIP_RE = re.compile(
    r"(?:prime\s*video|bölüm\s*\d|sezon\s*\d|kindle\s*edition|e-kitap|dijital\s*kod)",
    re.IGNORECASE,
)
_SECONDARY_PRICE_RE = re.compile(r"(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?)\s*TL", re.IGNORECASE)
_CONDITION_RE = re.compile(
    r"(İkinci El[^<)\n]{0,30}|Yeni Gibi|Çok İyi|Kabul Edilebilir|Hasarlı Kutu)",
    re.IGNORECASE,
)


@dataclass(slots=True)
class AmazonDepoDeal:
    asin: str
    title: str
    depo_price_tl: float
    market_price_tl: float
    discount_percent: int
    savings_tl: float
    condition_label: str
    category: str
    url: str
    image_url: str
    price_source: str = "Piyasa Ref"
    is_super_steal: bool = False


class AmazonDepoHunter:
    """
    Amazon.com.tr Depo (Warehouse Deals / İkinci El) ürünlerini tarar,
    her ürünün gerçek piyasa (sıfır) fiyatını karşılaştırır ve piyasa fiyatının
    %40 - %90 altında olan efsane fırsatları öne çıkarır.
    """

    def __init__(self, fetcher: HttpFetcher) -> None:
        self.fetcher = fetcher

    def hunt(
        self,
        *,
        categories: list[str] | None = None,
        keywords: list[str] | None = None,
        min_discount_percent: int = 35,
        super_discount_percent: int = 75,
        min_price_tl: float = 100.0,
        max_price_tl: float = 150000.0,
        max_items: int = 35,
    ) -> list[AmazonDepoDeal]:
        cats = [c for c in (categories or ["tumu", "elektronik", "bilgisayar", "ev_mutfak"]) if c in DEPO_CATEGORY_FEEDS]
        if not cats:
            cats = ["tumu"]

        feeds: list[tuple[str, str]] = [DEPO_CATEGORY_FEEDS[c] for c in cats]
        for kw in (keywords or []):
            clean_kw = kw.strip()
            if len(clean_kw) >= 2:
                feeds.append(
                    (
                        f"Arama: {clean_kw}",
                        f"https://www.amazon.com.tr/s?i=warehouse-deals&k={quote_plus(clean_kw)}&s=price-asc-rank",
                    )
                )

        raw_candidates: dict[str, dict] = {}
        for idx_f, (cat_label, feed_url) in enumerate(feeds):
            if idx_f > 0:
                time.sleep(1.8)
            html = ""
            for attempt in range(2):
                try:
                    html = self.fetcher.get_text(feed_url)
                    break
                except Exception as exc:
                    if attempt == 0 and "503" in str(exc):
                        time.sleep(3.0)
                        continue
                    logger.debug("Amazon Depo beslemesi okunamadı (%s): %s", cat_label, exc)
            if not html:
                continue

            for item in self._parse_depo_search_page(html, cat_label):
                asin = item["asin"]
                p = item["depo_price_tl"]
                if p < min_price_tl or p > max_price_tl:
                    continue
                if asin not in raw_candidates or p < raw_candidates[asin]["depo_price_tl"]:
                    raw_candidates[asin] = item

        found: list[AmazonDepoDeal] = []
        # Önce kendi üzerinde sıfır fiyatı olanları + en fazla 28 adayı canlı piyasa karşılaştırmasına sok
        ordered = sorted(
            raw_candidates.values(),
            key=lambda x: (x["card_new_price_tl"] is not None, -x["depo_price_tl"]),
            reverse=True,
        )[:28]

        for item in ordered:
            depo_p = float(item["depo_price_tl"])
            market_p: Optional[float] = item["card_new_price_tl"]
            p_src = "Amazon Yeni Fiyatı"

            if not market_p or market_p <= depo_p * 1.08:
                market_p, p_src = self._fetch_market_reference_price(
                    asin=item["asin"],
                    title=item["title"],
                    depo_price_tl=depo_p,
                )

            if not market_p or market_p <= depo_p:
                continue

            # Aşırı saçma birim fiyat / yanlış eşleşme filtresi (15x üstü anormallikleri ele)
            if market_p > depo_p * 15.0:
                continue

            savings = round(market_p - depo_p, 2)
            disc_pct = int(round((savings / market_p) * 100.0))
            if disc_pct < min_discount_percent or savings < 50.0:
                continue

            found.append(
                AmazonDepoDeal(
                    asin=item["asin"],
                    title=item["title"],
                    depo_price_tl=depo_p,
                    market_price_tl=round(market_p, 2),
                    discount_percent=disc_pct,
                    savings_tl=savings,
                    condition_label=item["condition_label"],
                    category=item["category"],
                    url=f"https://www.amazon.com.tr/dp/{item['asin']}?smid={AMAZON_DEPO_MERCHANT_ID}&th=1",
                    image_url=item["image_url"],
                    price_source=p_src,
                    is_super_steal=(disc_pct >= super_discount_percent),
                )
            )

        found.sort(key=lambda d: (d.is_super_steal, d.discount_percent, d.savings_tl), reverse=True)
        return found[:max_items]

    def _parse_depo_search_page(self, html: str, category_label: str) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        items: list[dict] = []
        for card in soup.select("div[data-component-type='s-search-result']")[:24]:
            asin = str(card.get("data-asin") or "").strip().upper()
            if not re.fullmatch(r"[A-Z0-9]{10}", asin):
                continue

            h2 = card.select_one("h2")
            title = " ".join(h2.get_text(" ", strip=True).split()) if h2 else ""
            if len(title) < 8 or _DIGITAL_SKIP_RE.search(title):
                continue
            card_txt = card.get_text(" ", strip=True)
            if "Prime Video" in card_txt or "Bölüm" in card_txt and "TL" not in card_txt:
                continue

            # 1) İkinci el / Depo teklif fiyatı (secondary-offer-recipe) ve gerçek stok kontrolü
            sec_el = card.select_one("div[data-cy='secondary-offer-recipe']")
            sec_txt = sec_el.get_text(" ", strip=True) if sec_el else ""
            sec_price: Optional[float] = None
            if sec_txt:
                m_sec = _SECONDARY_PRICE_RE.search(sec_txt)
                if m_sec:
                    sec_price = parse_price_tl(m_sec.group(1))

            card_hrefs = " ".join(str(a.get("href") or "") for a in card.select("a[href]"))
            has_live_depo_stock = bool(
                sec_price
                or "ikinci el" in sec_txt.lower()
                or "ikinci el" in card_txt.lower()
                or "amazon depo" in card_txt.lower()
                or AMAZON_DEPO_MERCHANT_ID in card_hrefs
            )
            # Depo stoğu tükenmiş önbellek kartlarını (yalnızca sıfır satıcı kalanları) ele!
            if not has_live_depo_stock:
                continue

            # 2) Karttaki ana fiyat ve üstü çizili / yeni fiyat
            main_price: Optional[float] = None
            strike_price: Optional[float] = None
            for p_span in card.select("span.a-price"):
                off = p_span.select_one("span.a-offscreen")
                if not off:
                    continue
                val = parse_price_tl(off.get_text(" ", strip=True))
                if not val or val < 15.0:
                    continue
                classes = " ".join(p_span.get("class") or [])
                if "a-text-price" in classes or p_span.get("data-a-strike") == "true":
                    if strike_price is None or val > strike_price:
                        strike_price = val
                elif main_price is None:
                    main_price = val

            # Depo fiyatı belirleme: secondary varsa ve ana fiyattan ucuzsa depo fiyatı odur;
            # ana fiyat yoksa secondary depo fiyatıdır; sadece ana fiyat varsa depo kartının kendisidir.
            depo_price: Optional[float] = None
            card_new_price: Optional[float] = strike_price
            if sec_price and main_price:
                if sec_price < main_price:
                    depo_price = sec_price
                    card_new_price = max(main_price, strike_price or 0.0)
                else:
                    depo_price = main_price
            elif sec_price:
                depo_price = sec_price
            elif main_price:
                depo_price = main_price

            if not depo_price or depo_price < 20.0:
                continue

            cond_m = _CONDITION_RE.search(sec_txt or card_txt)
            condition_label = cond_m.group(1).strip() if cond_m else "Amazon Depo (İkinci El)"

            img_el = card.select_one("img.s-image") or card.select_one("img[src]")
            img_url = str(img_el.get("src") or "").strip() if img_el else ""

            items.append(
                {
                    "asin": asin,
                    "title": title[:160],
                    "depo_price_tl": float(depo_price),
                    "card_new_price_tl": float(card_new_price) if (card_new_price and card_new_price > depo_price) else None,
                    "condition_label": condition_label[:45],
                    "category": category_label,
                    "image_url": img_url,
                }
            )
        return items

    def _fetch_market_reference_price(
        self, *, asin: str, title: str, depo_price_tl: float
    ) -> tuple[Optional[float], str]:
        """
        Ürün için gerçek sıfır piyasa fiyatını bulur:
        1) Hepsiburada arama (Oracle Cloud'da %100 erişilebilir)
        2) N11 arama (Oracle Cloud'da %100 erişilebilir)
        """
        tokens = re.findall(r"[A-Za-zÇĞİÖŞÜçğıöşü0-9]+", title)
        if len(tokens) < 2:
            return None, ""
        # Marka + model kodu ağırlıklı en temiz 3-4 kelimelik sorgu
        model_toks = [t for t in tokens[:7] if any(c.isdigit() for c in t)]
        if model_toks and tokens[0] != model_toks[0]:
            q = f"{tokens[0]} {model_toks[0]}"
            if len(tokens) >= 3 and tokens[1] != model_toks[0]:
                q = f"{tokens[0]} {tokens[1]} {model_toks[0]}"
        else:
            q = " ".join(tokens[:4])

        # 1) Hepsiburada canlı piyasa medyanı
        for store_lbl, search_u in (
            ("Hepsiburada Piyasa", f"https://www.hepsiburada.com/ara?q={quote_plus(q)}"),
            ("N11 Piyasa", f"https://www.n11.com/arama?q={quote_plus(q)}"),
        ):
            try:
                html = self.fetcher.get_text(search_u)
                prices: list[float] = []
                # JSON-LD ürünleri en temiz fiyat kaynağıdır
                for prod in extract_json_ld_products(html):
                    p_val = float(prod.get("price_tl") or 0.0)
                    p_title = str(prod.get("title") or "").lower()
                    if p_val <= depo_price_tl * 1.15 or p_val > depo_price_tl * 14.0:
                        continue
                    # Marka adı eşleşmesi (yanlış alakasız ürünleri elemek için)
                    if tokens[0].lower() in p_title:
                        prices.append(p_val)
                if not prices:
                    soup = BeautifulSoup(html, "lxml")
                    sel = "[data-test-id='price-current-price'], [data-test-id='final-price-1']" if "hepsiburada" in search_u else "span.newPrice ins"
                    for el in soup.select(sel)[:8]:
                        pv = parse_price_tl(el.get_text(" ", strip=True))
                        if pv and depo_price_tl * 1.15 < pv <= depo_price_tl * 14.0:
                            prices.append(pv)
                if prices:
                    # En ucuz 3 sıfır teklifin medyanı (şişirilmiş fiyatları dışlar, gerçek sıfır fiyatı verir)
                    prices.sort()
                    ref_p = float(statistics.median(prices[:4]))
                    return round(ref_p, 2), store_lbl
            except Exception:
                continue

        return None, ""

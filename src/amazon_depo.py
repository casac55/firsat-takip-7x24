from __future__ import annotations

import json
import logging
import re
import statistics
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote, quote_plus

from bs4 import BeautifulSoup

from src.fetchers import HttpFetcher
from src.filter import parse_price_tl
from src.scrapers.structured import extract_json_ld_products

logger = logging.getLogger(__name__)

AMAZON_DEPO_MERCHANT_ID = "A215JX4S9CANSO"
ASIN_RE = re.compile(r"(?:dp/|gp/product/|^)([A-Z0-9]{10})(?:[/?&#]|$)", re.IGNORECASE)

DEPO_CATEGORY_FEEDS: dict[str, tuple[str, str]] = {
    "tumu": ("Tüm Depo (En Yeni)", "https://www.amazon.com.tr/s?i=warehouse-deals&s=date-desc-rank"),
    "populer": ("Tüm Depo (Öne Çıkanlar)", "https://www.amazon.com.tr/s?i=warehouse-deals&s=exact-aware-popularity-rank"),
    "yildizli": ("Tüm Depo (Çok Değerlendirilen)", "https://www.amazon.com.tr/s?i=warehouse-deals&s=review-rank"),
    "elektronik": ("Elektronik", "https://www.amazon.com.tr/s?i=warehouse-deals&rh=n%3A12466496031&s=exact-aware-popularity-rank"),
    "bilgisayar": ("Bilgisayar & Bileşen", "https://www.amazon.com.tr/s?i=warehouse-deals&rh=n%3A12466439031&s=exact-aware-popularity-rank"),
    "telefon": ("Telefon & Aksesuar", "https://www.amazon.com.tr/s?i=warehouse-deals&rh=n%3A13709880031&s=exact-aware-popularity-rank"),
    "ev_mutfak": ("Ev & Mutfak", "https://www.amazon.com.tr/s?i=warehouse-deals&rh=n%3A12466553031&s=exact-aware-popularity-rank"),
    "kisisel_bakim": ("Kişisel Bakım", "https://www.amazon.com.tr/s?i=warehouse-deals&rh=n%3A12466702031&s=exact-aware-popularity-rank"),
    "oyuncak": ("Oyuncak & Hobi", "https://www.amazon.com.tr/s?i=warehouse-deals&rh=n%3A12466209031&s=exact-aware-popularity-rank"),
    "spor": ("Spor & Outdoor", "https://www.amazon.com.tr/s?i=warehouse-deals&rh=n%3A12466528031&s=exact-aware-popularity-rank"),
}

POPULAR_SEED_URLS: tuple[tuple[str, str], ...] = (
    ("Elektronik Çok Satan", "https://www.amazon.com.tr/gp/bestsellers/electronics"),
    ("Bilgisayar Çok Satan", "https://www.amazon.com.tr/gp/bestsellers/computers"),
    ("Ev & Mutfak Çok Satan", "https://www.amazon.com.tr/gp/bestsellers/kitchen"),
)

_DIGITAL_SKIP_RE = re.compile(
    r"(?:prime\s*video|bölüm\s*\d|sezon\s*\d|kindle\s*edition|e-kitap|dijital\s*kod)",
    re.IGNORECASE,
)
_SECONDARY_PRICE_RE = re.compile(r"(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?)\s*TL", re.IGNORECASE)
_CONDITION_RE = re.compile(
    r"(İkinci El[^<)\n]{0,38}|Yeni Gibi|Çok İyi|Kabul Edilebilir|Hasarlı Kutu)",
    re.IGNORECASE,
)


def extract_asin(text_or_url: str) -> str:
    raw = (text_or_url or "").strip()
    if re.fullmatch(r"[A-Za-z0-9]{10}", raw):
        return raw.upper()
    m = ASIN_RE.search(raw)
    return m.group(1).upper() if m else ""


def build_depo_cart_url(asin: str, offer_listing_id: str = "", associate_tag: str = "") -> str:
    """
    Şifreli OfferListingId varsa DOĞRUDAN o ucuz Amazon Depo (İkinci El) adetini sepete ekleyen
    resmi Amazon linkini üretir; yoksa doğrudan Amazon Depo satıcılı ürün sayfasına yönlendirir.
    """
    tag_q = f"&AssociateTag={quote(associate_tag)}" if associate_tag else ""
    if offer_listing_id:
        return (
            f"https://www.amazon.com.tr/gp/aws/cart/add.html"
            f"?OfferListingId.1={quote(offer_listing_id)}&Quantity.1=1{tag_q}"
        )
    tag_dp = f"&tag={quote(associate_tag)}" if associate_tag else ""
    return f"https://www.amazon.com.tr/dp/{asin}?smid={AMAZON_DEPO_MERCHANT_ID}&th=1{tag_dp}"


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
    offer_listing_id: str = ""
    condition_note: str = ""
    price_source: str = "Amazon Sıfır"
    is_super_steal: bool = False
    is_watchlist: bool = False

    def cart_url(self, associate_tag: str = "") -> str:
        return build_depo_cart_url(self.asin, self.offer_listing_id, associate_tag)


class AmazonDepoHunter:
    """
    %100 Amazon.com.tr Depo (A215JX4S9CANSO) odaklı 3 katmanlı keskin nişancı motoru:
    1) Özel ASIN Nöbetçisi (Watchlist) -> aodAjaxMain ile saliselik gerçek stok + OfferListingId
    2) Çok Satanlar Popüler Havuz Tarayıcısı -> arama önbelleğine düşmeyen büyük ürünleri yakalar
    3) 10 Kategori + Özel Kelime Warehouse-Deals Akışı -> sürpriz %50-%90 düşüşleri yakalar
    """

    def __init__(self, fetcher: HttpFetcher) -> None:
        self.fetcher = fetcher
        self._popular_pool: list[tuple[str, str]] = []
        self._pool_cursor: int = 0
        self._last_pool_refresh: float = 0.0

    def inspect_asin_aod(
        self,
        asin: str,
        *,
        category_hint: str = "Amazon Depo",
        fallback_title: str = "",
        fallback_image: str = "",
        super_discount_percent: int = 75,
        is_watchlist: bool = False,
    ) -> Optional[AmazonDepoDeal]:
        """
        Amazon'un canlı AOD (/gp/product/ajax/aodAjaxMain) uç noktasından:
        - Gerçek 'Amazon Depo' stok varlığını
        - Gerçek Depo fiyatını
        - Şifreli OfferListingId (oid) sepet kodunu
        - Kondisyon ve kusur açıklamasını
        - Aynı ASIN'in sıfır (Yeni) fiyatını (yoksa Hepsiburada/N11 piyasa fiyatını)
        tek istekte çeker. Depo stoğu yoksa None döner.
        """
        clean_asin = extract_asin(asin)
        if not clean_asin:
            return None

        aod_url = f"https://www.amazon.com.tr/gp/product/ajax/aodAjaxMain?asin={clean_asin}&pc=dp&experienceId=aodAjaxMain"
        try:
            html = self.fetcher.get_text(aod_url)
        except Exception as exc:
            logger.debug("AOD okunamadı (%s): %s", clean_asin, exc)
            return None

        soup = BeautifulSoup(html, "lxml")
        t_el = soup.select_one("#aod-asin-title-text")
        img_el = soup.select_one("#aod-asin-image-id")
        title = " ".join(t_el.get_text(" ", strip=True).split()) if t_el else fallback_title
        image_url = str(img_el.get("src") or "").strip() if img_el else fallback_image

        best_depo: Optional[dict] = None
        new_prices: list[float] = []

        for offer_el in soup.select("#aod-pinned-offer, #aod-offer"):
            price_val = self._extract_aod_offer_price(offer_el)
            if not price_val or price_val < 15.0:
                continue

            cond_el = offer_el.select_one("#aod-offer-heading")
            cond_txt = " ".join(cond_el.get_text(" ", strip=True).split()) if cond_el else ""
            sold_el = offer_el.select_one("#aod-offer-soldBy")
            sold_txt = " ".join(sold_el.get_text(" ", strip=True).split()) if sold_el else ""
            sold_hrefs = " ".join(str(a.get("href") or "") for a in offer_el.select("a[href]"))

            is_depo = (
                "amazon depo" in sold_txt.lower()
                or AMAZON_DEPO_MERCHANT_ID in sold_hrefs
                or ("ikinci el" in cond_txt.lower() and "amazon" in sold_txt.lower())
            )
            is_new = "yeni" in cond_txt.lower() and "ikinci el" not in cond_txt.lower() and not is_depo

            if is_new:
                new_prices.append(price_val)
                continue

            if is_depo:
                oid = self._extract_oid_from_offer(offer_el)
                note_el = offer_el.select_one(".a-expander-content, #aod-offer-condition-note")
                note_txt = " ".join(note_el.get_text(" ", strip=True).split())[:140] if note_el else ""
                if best_depo is None or price_val < best_depo["price"]:
                    best_depo = {
                        "price": price_val,
                        "condition": cond_txt or "İkinci El - Amazon Depo",
                        "note": note_txt,
                        "oid": oid,
                    }

        if best_depo is None:
            return None

        depo_p = float(best_depo["price"])
        market_p: Optional[float] = min(new_prices) if new_prices else None
        p_src = "Amazon Sıfır"

        if (not market_p or market_p <= depo_p * 1.05) and title:
            ext_p, ext_src = self._fetch_market_reference_price(
                asin=clean_asin, title=title, depo_price_tl=depo_p
            )
            if ext_p and ext_p > depo_p:
                market_p = ext_p
                p_src = ext_src

        if not market_p or market_p <= depo_p:
            # Watchlist ürünlerinde sıfır referansı bulunamasa bile Depo stoğa girdiyse kaçırma!
            if is_watchlist:
                market_p = depo_p
                p_src = "Depo Stokta"
            else:
                return None

        if market_p > depo_p * 15.0:
            return None

        savings = max(0.0, round(market_p - depo_p, 2))
        disc_pct = int(round((savings / market_p) * 100.0)) if market_p > depo_p else 0

        return AmazonDepoDeal(
            asin=clean_asin,
            title=(title or f"Amazon Depo Ürünü ({clean_asin})")[:165],
            depo_price_tl=depo_p,
            market_price_tl=round(market_p, 2),
            discount_percent=disc_pct,
            savings_tl=savings,
            condition_label=best_depo["condition"][:48],
            condition_note=best_depo["note"],
            category=category_hint,
            url=f"https://www.amazon.com.tr/dp/{clean_asin}?smid={AMAZON_DEPO_MERCHANT_ID}&th=1",
            image_url=image_url,
            offer_listing_id=best_depo["oid"],
            price_source=p_src,
            is_super_steal=(disc_pct >= super_discount_percent),
            is_watchlist=is_watchlist,
        )

    @staticmethod
    def _extract_aod_offer_price(offer_el) -> Optional[float]:
        off = offer_el.select_one("span.a-price span.a-offscreen")
        if off:
            pv = parse_price_tl(off.get_text(" ", strip=True))
            if pv and pv > 0:
                return pv
        whole = offer_el.select_one("span.a-price-whole")
        if whole:
            frac = offer_el.select_one("span.a-price-fraction")
            w_txt = whole.get_text("", strip=True).rstrip(",.")
            f_txt = frac.get_text("", strip=True) if frac else "00"
            return parse_price_tl(f"{w_txt},{f_txt}")
        return None

    @staticmethod
    def _extract_oid_from_offer(offer_el) -> str:
        atc = offer_el.select_one("[data-aod-atc-action]")
        if atc and atc.get("data-aod-atc-action"):
            raw = str(atc.get("data-aod-atc-action"))
            try:
                parsed = json.loads(raw)
                oid = str(parsed.get("oid") or "").strip()
                if oid:
                    return oid
            except Exception:
                m = re.search(r'"oid"\s*:\s*"([^"]+)"', raw)
                if m:
                    return m.group(1).strip()
        inp = offer_el.select_one("input[name='offeringID.1'], input[name='offerListingID']")
        if inp and inp.get("value"):
            return str(inp.get("value")).strip()
        return ""

    def refresh_popular_pool_if_needed(self) -> None:
        now = time.time()
        if self._popular_pool and (now - self._last_pool_refresh) < 1800.0:
            return
        collected: list[tuple[str, str]] = []
        seen: set[str] = set()
        for cat_lbl, url in POPULAR_SEED_URLS:
            try:
                html = self.fetcher.get_text(url)
                for m in re.finditer(r"/dp/([A-Z0-9]{10})", html):
                    asin = m.group(1).upper()
                    if asin not in seen:
                        seen.add(asin)
                        collected.append((asin, cat_lbl))
                    if len(collected) >= 90:
                        break
                time.sleep(0.8)
            except Exception:
                continue
        if collected:
            self._popular_pool = collected
            self._last_pool_refresh = now

    def hunt(
        self,
        *,
        categories: list[str] | None = None,
        keywords: list[str] | None = None,
        watchlist: list[dict] | None = None,
        scan_popular_pool: bool = True,
        min_discount_percent: int = 30,
        super_discount_percent: int = 75,
        min_price_tl: float = 100.0,
        max_price_tl: float = 150000.0,
        max_items: int = 45,
    ) -> tuple[list[AmazonDepoDeal], set[str]]:
        """
        Tam kapsamlı Amazon Depo taraması yapar.
        Dönüş: (bulunan_fırsatlar, stoğu_bittiği_kesinleşen_asinler)
        """
        found_by_asin: dict[str, AmazonDepoDeal] = {}
        sold_out_asins: set[str] = set()

        # KATMAN 1: Kullanıcının Özel ASIN Nöbet Listesi (Watchlist) — her döngüde anında AOD kontrolü!
        for w in (watchlist or []):
            w_asin = extract_asin(str(w.get("asin") or ""))
            if not w_asin:
                continue
            deal = self.inspect_asin_aod(
                w_asin,
                category_hint="🎯 Özel Nöbet",
                fallback_title=str(w.get("note") or ""),
                super_discount_percent=super_discount_percent,
                is_watchlist=True,
            )
            if deal is None:
                sold_out_asins.add(w_asin)
                continue
            target_p = float(w.get("target_price_tl") or 0.0)
            min_w_pct = int(w.get("min_discount_percent") or 0)
            if (target_p > 0 and deal.depo_price_tl <= target_p) or deal.discount_percent >= max(5, min_w_pct):
                if target_p > 0 and deal.depo_price_tl <= target_p:
                    deal.is_super_steal = True
                found_by_asin[deal.asin] = deal
            time.sleep(0.35)

        # KATMAN 2: Çok Satan Popüler Ürünler Havuzundan Dönüşümlü AOD Yoklaması (B0D92CF1P5 tarzı ürünleri yakalar)
        if scan_popular_pool:
            self.refresh_popular_pool_if_needed()
            if self._popular_pool:
                batch_size = 8
                start = self._pool_cursor % len(self._popular_pool)
                slice_pool = (self._popular_pool[start : start + batch_size]) or self._popular_pool[:batch_size]
                self._pool_cursor = (start + batch_size) % max(1, len(self._popular_pool))
                for p_asin, p_cat in slice_pool:
                    if p_asin in found_by_asin:
                        continue
                    deal = self.inspect_asin_aod(
                        p_asin,
                        category_hint=p_cat,
                        super_discount_percent=super_discount_percent,
                    )
                    if deal and min_price_tl <= deal.depo_price_tl <= max_price_tl and deal.discount_percent >= min_discount_percent:
                        found_by_asin[deal.asin] = deal
                    time.sleep(0.35)

        # KATMAN 3: Warehouse-Deals Kategori + Özel Kelime Akışları + AOD Doğrulaması (oid & kesin stok!)
        cats = [c for c in (categories or ["tumu", "populer", "elektronik", "bilgisayar", "ev_mutfak"]) if c in DEPO_CATEGORY_FEEDS]
        if not cats:
            cats = ["tumu", "populer"]

        feeds: list[tuple[str, str]] = [DEPO_CATEGORY_FEEDS[c] for c in cats]
        for kw in (keywords or []):
            clean_kw = kw.strip()
            if len(clean_kw) >= 2:
                feeds.append(
                    (
                        f"🔍 {clean_kw}",
                        f"https://www.amazon.com.tr/s?i=warehouse-deals&k={quote_plus(clean_kw)}&s=exact-aware-popularity-rank",
                    )
                )

        raw_candidates: dict[str, dict] = {}
        for idx_f, (cat_label, feed_url) in enumerate(feeds):
            if idx_f > 0:
                time.sleep(1.5)
            html = ""
            for attempt in range(2):
                try:
                    html = self.fetcher.get_text(feed_url)
                    break
                except Exception as exc:
                    if attempt == 0 and "503" in str(exc):
                        time.sleep(2.5)
                        continue
                    logger.debug("Depo beslemesi okunamadı (%s): %s", cat_label, exc)
            if not html:
                continue

            for item in self._parse_depo_search_page(html, cat_label):
                asin = item["asin"]
                p = item["depo_price_tl"]
                if p < min_price_tl or p > max_price_tl:
                    continue
                if asin not in raw_candidates or p < raw_candidates[asin]["depo_price_tl"]:
                    raw_candidates[asin] = item

        ordered = sorted(
            raw_candidates.values(),
            key=lambda x: (x["card_new_price_tl"] is not None, -x["depo_price_tl"]),
            reverse=True,
        )[:22]

        for item in ordered:
            asin = item["asin"]
            if asin in found_by_asin:
                continue
            # Her adayı AOD üzerinden doğrula: hem sahte/bitmiş önbelleği eler hem de gerçek OfferListingId (oid) alır!
            aod_deal = self.inspect_asin_aod(
                asin,
                category_hint=item["category"],
                fallback_title=item["title"],
                fallback_image=item["image_url"],
                super_discount_percent=super_discount_percent,
            )
            time.sleep(0.35)
            if aod_deal is None:
                sold_out_asins.add(asin)
                continue
            if aod_deal.discount_percent < min_discount_percent or aod_deal.savings_tl < 40.0:
                continue
            if not (min_price_tl <= aod_deal.depo_price_tl <= max_price_tl):
                continue
            found_by_asin[asin] = aod_deal

        results = sorted(
            found_by_asin.values(),
            key=lambda d: (d.is_watchlist, d.is_super_steal, d.discount_percent, d.savings_tl),
            reverse=True,
        )
        return results[:max_items], sold_out_asins

    def verify_existing_deals_stock(
        self, existing_rows: list[dict], super_discount_percent: int = 75, max_check: int = 12
    ) -> tuple[list[AmazonDepoDeal], set[str]]:
        """
        Panelde listelenen mevcut Depo ürünlerinin hâlâ stokta olup olmadığını AOD ile kontrol eder;
        biten ASIN'leri anında döndürür ki panelden silinsin.
        """
        still_alive: list[AmazonDepoDeal] = []
        gone_asins: set[str] = set()
        for row in existing_rows[:max_check]:
            asin = str(row.get("asin") or "").strip().upper()
            if not asin:
                continue
            deal = self.inspect_asin_aod(
                asin,
                category_hint=str(row.get("category") or "Amazon Depo"),
                fallback_title=str(row.get("title") or ""),
                fallback_image=str(row.get("image_url") or ""),
                super_discount_percent=super_discount_percent,
            )
            if deal is None:
                gone_asins.add(asin)
            else:
                still_alive.append(deal)
            time.sleep(0.35)
        return still_alive, gone_asins

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
            if "Prime Video" in card_txt:
                continue

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
            if not has_live_depo_stock:
                continue

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
            condition_label = cond_m.group(1).strip() if cond_m else "İkinci El - Amazon Depo"
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
        tokens = re.findall(r"[A-Za-zÇĞİÖŞÜçğıöşü0-9]+", title)
        if len(tokens) < 2:
            return None, ""
        model_toks = [t for t in tokens[:7] if any(c.isdigit() for c in t)]
        if model_toks and tokens[0] != model_toks[0]:
            q = f"{tokens[0]} {model_toks[0]}"
            if len(tokens) >= 3 and tokens[1] != model_toks[0]:
                q = f"{tokens[0]} {tokens[1]} {model_toks[0]}"
        else:
            q = " ".join(tokens[:4])

        for store_lbl, search_u in (
            ("Hepsiburada Sıfır", f"https://www.hepsiburada.com/ara?q={quote_plus(q)}"),
            ("N11 Sıfır", f"https://www.n11.com/arama?q={quote_plus(q)}"),
        ):
            try:
                html = self.fetcher.get_text(search_u)
                prices: list[float] = []
                for prod in extract_json_ld_products(html):
                    p_val = float(prod.get("price_tl") or 0.0)
                    p_title = str(prod.get("title") or "").lower()
                    if p_val <= depo_price_tl * 1.15 or p_val > depo_price_tl * 14.0:
                        continue
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
                    prices.sort()
                    ref_p = float(statistics.median(prices[:4]))
                    return round(ref_p, 2), store_lbl
            except Exception:
                continue
        return None, ""

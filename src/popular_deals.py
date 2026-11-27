from __future__ import annotations

import json
import logging
import random
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from src.analyzer import is_direct_store_url
from src.fetchers import HttpFetcher
from src.filter import (
    extract_coupon_info,
    extract_trade_in_info,
    is_official_or_trusted_seller,
    normalize_seller_name,
    parse_price_tl,
)
from src.scrapers.structured import extract_json_ld_products

logger = logging.getLogger(__name__)

_AMAZON_ASIN_RE = re.compile(r"(?:https?://[^/]+)?(?:/[^?#]*)?/dp/([A-Z0-9]{10})", re.IGNORECASE)
_HB_SKU_RE = re.compile(r"-(?:p|pm)-(HBC?V?[A-Z0-9]{7,16})\b", re.IGNORECASE)
_TY_PID_RE = re.compile(r"-p-(\d{5,15})\b", re.IGNORECASE)


def canonicalize_deal_key(url: str) -> str:
    """URL'deki sorgu parametrelerini, slug farklarını ve oturum eklerini temizleyerek mağaza ürün kodu bazlı tekil anahtar üretir."""
    raw = (url or "").strip().split("#")[0].split("?")[0]
    low = raw.lower()
    m_amz = _AMAZON_ASIN_RE.search(raw)
    if m_amz and "amazon." in low:
        return f"https://www.amazon.com.tr/dp/{m_amz.group(1).upper()}"
    m_hb = _HB_SKU_RE.search(raw)
    if m_hb and "hepsiburada." in low:
        return f"https://www.hepsiburada.com/urun-p-{m_hb.group(1).upper()}"
    m_ty = _TY_PID_RE.search(raw)
    if m_ty and "trendyol." in low:
        return f"https://www.trendyol.com/urun-p-{m_ty.group(1)}"
    raw = re.sub(r"/ref=[^/]+.*$", "", raw, flags=re.IGNORECASE)
    return raw.rstrip("/")


@dataclass(slots=True)
class PopularDeal:
    deal_key: str
    title: str
    seller: str
    price_tl: float
    previous_or_second_price_tl: Optional[float]
    discount_percent: int
    direct_url: str
    image_url: str = ""
    is_official_seller: bool = False
    coupon_discount_tl: float = 0.0
    coupon_info: str = ""
    coupon_code: str = ""
    trade_in_info: str = ""
    category: str = ""
    badge_label: str = "SON 6 AYIN EN DÜŞÜK FİYATI!"
    stack_summary: str = ""
    multibuy_summary: str = ""
    is_glitch: bool = False
    market_avg_price_tl: Optional[float] = None

    @property
    def effective_price_tl(self) -> float:
        if self.coupon_discount_tl > 0:
            return round(max(1.0, self.price_tl - self.coupon_discount_tl), 2)
        return self.price_tl


class PopularDealsHunter:
    """
    Türkiye'de tüm kategorilerde (kulaklık, bilgisayar, ev aletleri, giyim, telefon, market vb.):
    - Son 6 ayın en ucuz fiyatlı ürünleri
    - Fiyatı düşen ürünler
    - Piyasaya göre ucuz ürünler
    - Sepette indirimle en ucuz ürünler
    listelerini tarayıp doğrudan mağaza linkleriyle çözer.
    """

    DEAL_FEEDS = (
        ("https://www.akakce.com/son-alti-ayin-en-ucuz-fiyatli-urunleri/", "SON 6 AYIN EN DÜŞÜK FİYATI!"),
        ("https://www.akakce.com/fiyati-dusen-urunler/", "FİYATI DÜŞEN POPÜLER ÜRÜN!"),
        ("https://www.akakce.com/piyasaya-gore-ucuz-urunler/", "PİYASAYA GÖRE UCUZ FIRSAT!"),
        ("https://www.akakce.com/sepette-indirimle-en-ucuz/", "SEPETTE EK İNDİRİM FIRSATI!"),
    )
    AMAZON_BESTSELLER_FEEDS = (
        ("https://www.amazon.com.tr/gp/bestsellers/electronics", "AMAZON ÇOK SATANLAR (ELEKTRONİK)"),
        ("https://www.amazon.com.tr/gp/bestsellers/kitchen", "AMAZON ÇOK SATANLAR (EV & MUTFAK)"),
        ("https://www.amazon.com.tr/gp/bestsellers/computers", "AMAZON ÇOK SATANLAR (BİLGİSAYAR)"),
        ("https://www.amazon.com.tr/gp/bestsellers/hpc", "AMAZON ÇOK SATANLAR (KİŞİSEL BAKIM)"),
        ("https://www.amazon.com.tr/gp/bestsellers/beauty", "AMAZON ÇOK SATANLAR (KOZMETİK)"),
        ("https://www.amazon.com.tr/gp/bestsellers/sports", "AMAZON ÇOK SATANLAR (SPOR)"),
        ("https://www.amazon.com.tr/gp/bestsellers/apparel", "AMAZON ÇOK SATANLAR (GİYİM)"),
        ("https://www.amazon.com.tr/gp/bestsellers/shoes", "AMAZON ÇOK SATANLAR (AYAKKABI)"),
        ("https://www.amazon.com.tr/gp/bestsellers/grocery", "AMAZON ÇOK SATANLAR (SÜPERMARKET)"),
        ("https://www.amazon.com.tr/gp/bestsellers/toys", "AMAZON ÇOK SATANLAR (OYUNCAK & HOBİ)"),
        ("https://www.amazon.com.tr/gp/bestsellers/diy", "AMAZON ÇOK SATANLAR (YAPI MARKET)"),
        ("https://www.amazon.com.tr/gp/bestsellers/automotive", "AMAZON ÇOK SATANLAR (OTO AKSESUAR)"),
    )
    TRENDYOL_BESTSELLER_FEEDS = (
        ("https://www.trendyol.com/sr?sst=BEST_SELLER&q=elektronik", "TRENDYOL ÇOK SATANLAR (ELEKTRONİK)"),
        ("https://www.trendyol.com/sr?sst=BEST_SELLER&q=bilgisayar", "TRENDYOL ÇOK SATANLAR (BİLGİSAYAR)"),
        ("https://www.trendyol.com/sr?sst=BEST_SELLER&q=ev+yasam", "TRENDYOL ÇOK SATANLAR (EV & YAŞAM)"),
        ("https://www.trendyol.com/sr?sst=BEST_SELLER&q=kucuk+ev+aletleri", "TRENDYOL ÇOK SATANLAR (KÜÇÜK EV ALETLERİ)"),
        ("https://www.trendyol.com/sr?sst=BEST_SELLER&q=kozmetik", "TRENDYOL ÇOK SATANLAR (KOZMETİK)"),
        ("https://www.trendyol.com/sr?sst=BEST_SELLER&q=giyim", "TRENDYOL ÇOK SATANLAR (GİYİM)"),
        ("https://www.trendyol.com/sr?sst=BEST_SELLER&q=ayakkabi", "TRENDYOL ÇOK SATANLAR (AYAKKABI)"),
        ("https://www.trendyol.com/sr?sst=BEST_SELLER&q=spor+outdoor", "TRENDYOL ÇOK SATANLAR (SPOR)"),
        ("https://www.trendyol.com/sr?sst=BEST_SELLER&q=supermarket", "TRENDYOL ÇOK SATANLAR (SÜPERMARKET)"),
        ("https://www.trendyol.com/sr?sst=BEST_SELLER&q=anne+bebek", "TRENDYOL ÇOK SATANLAR (ANNE & BEBEK)"),
    )
    OTHER_RETAILER_FEEDS = (
        ("https://www.n11.com/kampanyalar", "N11", "https://www.n11.com", "N11 KAMPANYA FIRSATI!"),
        ("https://www.teknosa.com/arama/?s=indirim", "Teknosa", "https://www.teknosa.com", "TEKNOSA İNDİRİM FIRSATI!"),
        ("https://www.mediamarkt.com.tr/tr/search.html?query=kampanya", "MediaMarkt", "https://www.mediamarkt.com.tr", "MEDIAMARKT KAMPANYA!"),
        ("https://www.pazarama.com/arama?q=indirim", "Pazarama", "https://www.pazarama.com", "PAZARAMA İNDİRİM FIRSATI!"),
        ("https://www.idefix.com/arama?q=indirim", "İdefix", "https://www.idefix.com", "İDEFİX İNDİRİM FIRSATI!"),
        ("https://www.vatanbilgisayar.com/arama/indirim/", "Vatan Bilgisayar", "https://www.vatanbilgisayar.com", "VATAN İNDİRİM FIRSATI!"),
    )
    BASE_URL = "https://www.akakce.com"

    def __init__(self, fetcher: HttpFetcher) -> None:
        self.fetcher = fetcher

    def hunt(
        self,
        min_discount_percent: int = 25,
        min_savings_tl: float = 100.0,
        max_items: int = 10,
        storage=None,
    ) -> list[PopularDeal]:
        candidates: list[tuple[str, str, int, Optional[float], str]] = []
        seen_hrefs: set[str] = set()

        for feed_url, badge in self.DEAL_FEEDS:
            try:
                html = self.fetcher.get_text(feed_url)
            except Exception as exc:
                logger.debug("Fırsat akışı okunamadı (%s): %s", feed_url, exc)
                continue
            soup = BeautifulSoup(html, "lxml")

            for card in soup.select("li.p, ul#APL li, ul.pl_v8 li, li[data-pr]"):
                a_el = card.select_one("a[href]")
                if not a_el:
                    continue
                href = (a_el.get("href") or "").strip()
                if not href or "fiyati," not in href or href in seen_hrefs:
                    continue
                seen_hrefs.add(href)

                card_txt = card.get_text(" ", strip=True)
                title_el = card.select_one("h3.pn_v8, span.pn_v8, a[title]")
                title = (
                    (title_el.get("title") or title_el.get_text(" ", strip=True))
                    if title_el
                    else a_el.get_text(" ", strip=True)
                )
                title = " ".join(title.split())
                if not title:
                    continue

                pct_match = re.search(r"%\s*(\d{1,2})\b", card_txt)
                pct = int(pct_match.group(1)) if pct_match else 5
                if pct < min_discount_percent:
                    continue

                price_el = card.select_one("span.pt_v8, .pt_v8")
                card_price = parse_price_tl(price_el.get_text(" ", strip=True) if price_el else card_txt)
                candidates.append((title, href, pct, card_price, badge))

        # İndirim oranı en yüksek olanları öne al
        candidates.sort(key=lambda x: x[2], reverse=True)

        # Tüm büyük mağaza kaynaklarından dengeli ve gerçek indirimli fırsat havuzu topla
        akakce_deals: list[PopularDeal] = []
        akakce_cap = max(3, max_items // 2)
        for title, href, pct, card_price, badge in candidates[: max_items * 2]:
            if len(akakce_deals) >= akakce_cap:
                break
            full_url = href if href.startswith("http") else urljoin(self.BASE_URL, href)
            deal = self._resolve_detail_page(title, full_url, pct, card_price, badge)
            if deal is not None and deal.discount_percent >= min_discount_percent:
                akakce_deals.append(deal)

        amazon_deals = (
            self._hunt_amazon_todays_deals(limit=8, min_discount_percent=min_discount_percent)
            + self._hunt_amazon_bestsellers(limit=4, min_discount_percent=min_discount_percent)
        )
        hepsiburada_deals = self._hunt_hepsiburada_deals(limit=4, min_discount_percent=min_discount_percent)
        trendyol_deals = self._hunt_trendyol_bestsellers(limit=6, min_discount_percent=min_discount_percent)
        retailer_deals = self._hunt_other_retailers(limit=6, min_discount_percent=min_discount_percent)
        price_asc_deals = self._hunt_price_asc_below_market(limit=6, min_discount_percent=min_discount_percent)

        for bucket in (akakce_deals, amazon_deals, hepsiburada_deals, trendyol_deals, retailer_deals, price_asc_deals):
            if storage is not None:
                bucket[:] = _filter_by_historical_prices(bucket, storage)
            bucket[:] = [
                d for d in bucket
                if is_meaningful_deal(d, min_discount_percent=min_discount_percent, min_savings_tl=min_savings_tl)
            ]
            bucket.sort(key=lambda d: d.discount_percent, reverse=True)

        # Mağazaları sırayla karıştır (round-robin) ki kanalda tek mağaza/kategori birikmesin
        mixed: list[PopularDeal] = []
        seen_keys: set[str] = set()
        buckets = [akakce_deals, price_asc_deals, amazon_deals, hepsiburada_deals, trendyol_deals, retailer_deals]
        while len(mixed) < max_items and any(buckets):
            for bucket in buckets:
                if not bucket:
                    continue
                d = bucket.pop(0)
                if d.deal_key not in seen_keys:
                    seen_keys.add(d.deal_key)
                    _enrich_deal_insights(d)
                    mixed.append(d)
                if len(mixed) >= max_items:
                    break

        return mixed

    def _hunt_amazon_todays_deals(self, limit: int = 6, min_discount_percent: int = 5) -> list[PopularDeal]:
        """Amazon.com.tr 'Bugünün Fırsatları' filtresinden gerçek indirimli ürünleri çeker."""
        found: list[PopularDeal] = []
        deal_urls = list(
            (
                ("https://www.amazon.com.tr/s?i=electronics&rh=p_n_deal_type%3A26904970031", "AMAZON BUGÜNÜN FIRSATI (ELEKTRONİK)"),
                ("https://www.amazon.com.tr/s?i=kitchen&rh=p_n_deal_type%3A26904970031", "AMAZON BUGÜNÜN FIRSATI (EV & MUTFAK)"),
                ("https://www.amazon.com.tr/s?i=computers&rh=p_n_deal_type%3A26904970031", "AMAZON BUGÜNÜN FIRSATI (BİLGİSAYAR)"),
                ("https://www.amazon.com.tr/s?i=hpc&rh=p_n_deal_type%3A26904970031", "AMAZON BUGÜNÜN FIRSATI (KİŞİSEL BAKIM)"),
                ("https://www.amazon.com.tr/s?i=beauty&rh=p_n_deal_type%3A26904970031", "AMAZON BUGÜNÜN FIRSATI (KOZMETİK)"),
                ("https://www.amazon.com.tr/s?i=sports&rh=p_n_deal_type%3A26904970031", "AMAZON BUGÜNÜN FIRSATI (SPOR)"),
                ("https://www.amazon.com.tr/s?i=apparel&rh=p_n_deal_type%3A26904970031", "AMAZON BUGÜNÜN FIRSATI (GİYİM)"),
                ("https://www.amazon.com.tr/s?i=shoes&rh=p_n_deal_type%3A26904970031", "AMAZON BUGÜNÜN FIRSATI (AYAKKABI)"),
                ("https://www.amazon.com.tr/s?i=toys&rh=p_n_deal_type%3A26904970031", "AMAZON BUGÜNÜN FIRSATI (OYUNCAK)"),
                ("https://www.amazon.com.tr/s?i=diy&rh=p_n_deal_type%3A26904970031", "AMAZON BUGÜNÜN FIRSATI (YAPI MARKET)"),
                ("https://www.amazon.com.tr/s?i=office-products&rh=p_n_deal_type%3A26904970031", "AMAZON BUGÜNÜN FIRSATI (OFİS)"),
                ("https://www.amazon.com.tr/s?i=automotive&rh=p_n_deal_type%3A26904970031", "AMAZON BUGÜNÜN FIRSATI (OTO AKSESUAR)"),
            )
        )
        random.shuffle(deal_urls)
        for feed_url, badge in deal_urls:
            if len(found) >= limit:
                break
            try:
                html = self.fetcher.get_text(feed_url)
            except Exception as exc:
                logger.debug("Amazon Bugünün Fırsatları okunamadı (%s): %s", feed_url, exc)
                continue
            soup = BeautifulSoup(html, "lxml")
            per_feed = 0
            for card in soup.select("div.s-result-item[data-asin]"):
                if len(found) >= limit or per_feed >= 2:
                    break
                asin = str(card.get("data-asin", "")).strip()
                if not asin:
                    continue
                card_txt = card.get_text(" ", strip=True)
                if "Sponsorlu" in card_txt[:40]:
                    continue
                title_el = card.select_one("h2 a span, h2 span")
                price_el = card.select_one("span.a-price span.a-offscreen")
                if not title_el or not price_el:
                    continue
                price_val = parse_price_tl(price_el.get_text(" ", strip=True))
                if not price_val or price_val < 100:
                    continue
                title = " ".join(title_el.get_text(" ", strip=True).split())
                dp_url = canonicalize_deal_key(f"https://www.amazon.com.tr/dp/{asin}")
                img_el = card.select_one("img.s-image")
                img_url = str(img_el.get("src", "")) if img_el else ""
                old_el = card.select_one(
                    "span.a-price.a-text-price[data-a-strike='true'] span.a-offscreen, "
                    "span.basisPrice span.a-offscreen"
                )
                old_price = None
                if old_el:
                    parent_txt = (old_el.parent.parent.get_text(" ", strip=True) if old_el.parent and old_el.parent.parent else "")
                    if not re.search(r"/\s*(?:100\s*)?(?:g|gr|kg|ml|l|lt|adet|m)\b", parent_txt, re.I):
                        old_price = parse_price_tl(old_el.get_text(" ", strip=True))
                if old_price and (old_price <= price_val or old_price > price_val * 2.8):
                    old_price = None
                pct_m = re.search(r"(?:-\s*%\s*(\d{1,2})|%\s*(\d{1,2})\s*(?:indirim|tasarruf|düşüş))\b", card_txt, re.I)
                if old_price and old_price > price_val:
                    pct = max(1, int(round(((old_price - price_val) / old_price) * 100.0)))
                elif pct_m:
                    pct = int(pct_m.group(1) or pct_m.group(2))
                else:
                    continue
                if pct < min_discount_percent or (not old_price and pct > 75):
                    continue
                found.append(
                    PopularDeal(
                        deal_key=dp_url,
                        title=title[:150],
                        seller="Amazon.com.tr",
                        price_tl=price_val,
                        previous_or_second_price_tl=old_price,
                        discount_percent=pct,
                        direct_url=dp_url,
                        image_url=img_url,
                        is_official_seller=True,
                        badge_label=badge,
                    )
                )
                per_feed += 1
        return found

    def _hunt_amazon_bestsellers(self, limit: int = 4, min_discount_percent: int = 5) -> list[PopularDeal]:
        """Amazon Çok Satanlar'dan yalnızca ürün sayfasında gerçek indirimi doğrulanmış ürünleri alır."""
        found: list[PopularDeal] = []
        feeds = list(self.AMAZON_BESTSELLER_FEEDS)
        random.shuffle(feeds)
        checked_pages = 0
        for feed_url, badge in feeds:
            if len(found) >= limit or checked_pages >= 6:
                break
            try:
                html = self.fetcher.get_text(feed_url)
            except Exception as exc:
                logger.debug("Amazon çok satanlar okunamadı (%s): %s", feed_url, exc)
                continue
            soup = BeautifulSoup(html, "lxml")
            for card in soup.select("div.zg-grid-general-faceout")[:12]:
                if len(found) >= limit or checked_pages >= 6:
                    break
                link_el = card.select_one("a[href*='/dp/']")
                price_el = card.select_one("span._cDEzb_p13n-sc-price_3mJ9Z, span.p13n-sc-price, span.a-color-price")
                if not link_el or not price_el:
                    continue
                raw_href = urljoin("https://www.amazon.com.tr", str(link_el.get("href", "")))
                dp_url = canonicalize_deal_key(raw_href)
                price_val = parse_price_tl(price_el.get_text(" ", strip=True))
                if not price_val or price_val < 50:
                    continue
                img_el = card.select_one("img[src]")
                title = (
                    (img_el.get("alt") if img_el and img_el.get("alt") else "")
                    or link_el.get_text(" ", strip=True)
                )
                title = " ".join(str(title).split())
                if len(title) < 6:
                    continue

                checked_pages += 1
                old_price, real_pct = self._fetch_amazon_discount(dp_url, price_val)
                if real_pct < min_discount_percent:
                    continue

                img_url = str(img_el.get("src", "")) if img_el else ""
                found.append(
                    PopularDeal(
                        deal_key=dp_url,
                        title=title[:150],
                        seller="Amazon.com.tr",
                        price_tl=price_val,
                        previous_or_second_price_tl=old_price,
                        discount_percent=real_pct,
                        direct_url=dp_url,
                        image_url=img_url,
                        is_official_seller=True,
                        badge_label=badge,
                    )
                )
        return found

    def _fetch_amazon_discount(self, dp_url: str, current_price: float) -> tuple[Optional[float], int]:
        try:
            html = self.fetcher.get_text(dp_url)
            soup = BeautifulSoup(html, "lxml")
            # DİKKAT: Amazon'da 'a-text-price' bazen '(9.163,33 TL / 100 g)' birim fiyatını taşır!
            # Yalnızca üstü çizili (data-a-strike='true' veya basisPrice) gerçek liste fiyatını al.
            old_el = soup.select_one(
                "#corePriceDisplay_desktop_feature_div span.basisPrice span.a-offscreen, "
                "#corePriceDisplay_desktop_feature_div span.a-price.a-text-price[data-a-strike='true'] span.a-offscreen, "
                "span.basisPrice span.a-offscreen"
            )
            old_p = None
            if old_el:
                p_ctx = old_el.parent.parent.get_text(" ", strip=True) if (old_el.parent and old_el.parent.parent) else ""
                if not re.search(r"/\s*(?:100\s*)?(?:g|gr|kg|ml|l|lt|adet|m)\b", p_ctx, re.I):
                    old_p = parse_price_tl(old_el.get_text(" ", strip=True))
            if old_p and current_price * 1.04 < old_p <= current_price * 2.8:
                pct = max(1, int(round(((old_p - current_price) / old_p) * 100.0)))
                return old_p, pct
            sav_el = soup.select_one("#corePriceDisplay_desktop_feature_div span.savingsPercentage")
            if sav_el:
                m = re.search(r"(\d{1,2})\s*%", sav_el.get_text(" ", strip=True))
                if m:
                    pct = int(m.group(1))
                    if 0 < pct <= 70:
                        est_old = round(current_price / max(0.30, 1.0 - pct / 100.0), 2)
                        return est_old, pct
        except Exception:
            pass
        return None, 0

    def _hunt_hepsiburada_deals(self, limit: int = 4, min_discount_percent: int = 25) -> list[PopularDeal]:
        """Hepsiburada kampanya ve indirim sayfalarından gerçek yüksek indirimli ürünleri tarar."""
        found: list[PopularDeal] = []
        feeds = (
            ("https://www.hepsiburada.com/kampanyalar", "HEPSİBURADA KAMPANYA FIRSATI!"),
            ("https://www.hepsiburada.com/ara?q=indirim&siralama=indirim", "HEPSİBURADA YÜKSEK İNDİRİM!"),
        )
        for feed_url, badge in feeds:
            if len(found) >= limit:
                break
            try:
                html = self.fetcher.get_text(feed_url)
            except Exception as exc:
                logger.debug("Hepsiburada fırsat akışı okunamadı (%s): %s", feed_url, exc)
                continue
            for item in extract_json_ld_products(html):
                if len(found) >= limit:
                    break
                p_val = item.get("price_tl")
                old_p = item.get("high_price_tl")
                t_str = str(item.get("title") or "").strip()
                u_str = urljoin("https://www.hepsiburada.com", str(item.get("url") or ""))
                if not p_val or not old_p or float(p_val) < 100 or float(old_p) <= float(p_val) or not t_str:
                    continue
                pct = int(round(((float(old_p) - float(p_val)) / float(old_p)) * 100.0))
                if pct < min_discount_percent:
                    continue
                found.append(
                    PopularDeal(
                        deal_key=canonicalize_deal_key(u_str),
                        title=t_str[:150],
                        seller="Hepsiburada",
                        price_tl=float(p_val),
                        previous_or_second_price_tl=float(old_p),
                        discount_percent=pct,
                        direct_url=u_str,
                        image_url=str(item.get("image_url") or ""),
                        is_official_seller=is_official_or_trusted_seller("Hepsiburada", u_str),
                        badge_label=badge,
                    )
                )
        return found

    def _hunt_price_asc_below_market(self, limit: int = 6, min_discount_percent: int = 25) -> list[PopularDeal]:
        """Sitelerdeki ürünleri küçükten büyüğe (artan fiyat) sıralayıp piyasa fiyatı altında olanları yakalar."""
        found: list[PopularDeal] = []
        asc_feeds = [
            ("Trendyol", "https://www.trendyol.com/sr?sst=PRICE_BY_ASC&q=kulakl%C4%B1k&prc=300-*", "https://www.trendyol.com"),
            ("Trendyol", "https://www.trendyol.com/sr?sst=PRICE_BY_ASC&q=k%C3%BC%C3%A7%C3%BCk+ev+aletleri&prc=500-*", "https://www.trendyol.com"),
            ("Hepsiburada", "https://www.hepsiburada.com/ara?q=elektronik&siralama=artanfiyat", "https://www.hepsiburada.com"),
            ("Amazon.com.tr", "https://www.amazon.com.tr/s?i=electronics&s=price-asc-rank&rh=p_n_deal_type%3A26904970031", "https://www.amazon.com.tr"),
            ("Amazon.com.tr", "https://www.amazon.com.tr/s?i=kitchen&s=price-asc-rank&rh=p_n_deal_type%3A26904970031", "https://www.amazon.com.tr"),
        ]
        random.shuffle(asc_feeds)
        seen_keys: set[str] = set()

        for seller_name, feed_url, base_url in asc_feeds:
            if len(found) >= limit:
                break
            try:
                html = self.fetcher.get_text(feed_url)
            except Exception as exc:
                logger.debug("Artan fiyat taraması okunamadı (%s): %s", feed_url, exc)
                continue
            soup = BeautifulSoup(html, "lxml")

            if seller_name == "Trendyol":
                for card in soup.select("a.product-card")[:12]:
                    if len(found) >= limit:
                        break
                    txt = card.get_text(" ", strip=True)
                    m_prices = re.findall(r"(\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?)\s*TL", txt, re.IGNORECASE)
                    if len(m_prices) < 2:
                        continue
                    old_p = parse_price_tl(m_prices[0])
                    new_p = parse_price_tl(m_prices[-1])
                    if not old_p or not new_p or new_p < 200 or old_p <= new_p:
                        continue
                    pct = int(round(((old_p - new_p) / old_p) * 100.0))
                    if pct < min_discount_percent:
                        continue
                    href = canonicalize_deal_key(urljoin(base_url, str(card.get("href", ""))))
                    img_el = card.select_one("img[src]")
                    title = " ".join(str((img_el.get("alt") if img_el and img_el.get("alt") else txt[:120])).split())
                    found.append(
                        PopularDeal(
                            deal_key=href,
                            title=title[:150],
                            seller="Trendyol",
                            price_tl=new_p,
                            previous_or_second_price_tl=old_p,
                            discount_percent=pct,
                            direct_url=href,
                            image_url=str(img_el.get("src", "")) if img_el else "",
                            badge_label="📉 PİYASA FİYATININ ALTINDA FIRSAT!",
                        )
                    )
            elif seller_name == "Amazon.com.tr":
                for card in soup.select("div.s-result-item[data-asin]")[:12]:
                    if len(found) >= limit:
                        break
                    asin = str(card.get("data-asin", "")).strip()
                    if not asin:
                        continue
                    title_el = card.select_one("h2 a span, h2 span")
                    price_el = card.select_one("span.a-price span.a-offscreen")
                    old_el = card.select_one("span.a-price.a-text-price span.a-offscreen")
                    if not title_el or not price_el or not old_el:
                        continue
                    new_p = parse_price_tl(price_el.get_text(" ", strip=True))
                    old_p = parse_price_tl(old_el.get_text(" ", strip=True))
                    if not new_p or not old_p or new_p < 200 or old_p <= new_p:
                        continue
                    pct = int(round(((old_p - new_p) / old_p) * 100.0))
                    if pct < min_discount_percent:
                        continue
                    dp_url = canonicalize_deal_key(f"https://www.amazon.com.tr/dp/{asin}")
                    img_el = card.select_one("img.s-image")
                    found.append(
                        PopularDeal(
                            deal_key=dp_url,
                            title=" ".join(title_el.get_text(" ", strip=True).split())[:150],
                            seller="Amazon.com.tr",
                            price_tl=new_p,
                            previous_or_second_price_tl=old_p,
                            discount_percent=pct,
                            direct_url=dp_url,
                            image_url=str(img_el.get("src", "")) if img_el else "",
                            is_official_seller=True,
                            badge_label="📉 PİYASA FİYATININ ALTINDA FIRSAT!",
                        )
                    )
            else:
                for item in extract_json_ld_products(html)[:10]:
                    if len(found) >= limit:
                        break
                    p_val = item.get("price_tl")
                    old_p = item.get("high_price_tl")
                    t_str = str(item.get("title") or "").strip()
                    u_str = urljoin(base_url, str(item.get("url") or ""))
                    if not p_val or not old_p or float(p_val) < 200 or float(old_p) <= float(p_val) or not t_str:
                        continue
                    pct = int(round(((float(old_p) - float(p_val)) / float(old_p)) * 100.0))
                    if pct < min_discount_percent:
                        continue
                    found.append(
                        PopularDeal(
                            deal_key=canonicalize_deal_key(u_str),
                            title=t_str[:150],
                            seller=seller_name,
                            price_tl=float(p_val),
                            previous_or_second_price_tl=float(old_p),
                            discount_percent=pct,
                            direct_url=u_str,
                            image_url=str(item.get("image_url") or ""),
                            is_official_seller=is_official_or_trusted_seller(seller_name, u_str),
                            badge_label="📉 PİYASA FİYATININ ALTINDA FIRSAT!",
                        )
                    )
        found.sort(key=lambda d: d.effective_price_tl)
        unique: list[PopularDeal] = []
        for d in found:
            if d.deal_key not in seen_keys:
                seen_keys.add(d.deal_key)
                unique.append(d)
        return unique[:limit]

    def _hunt_trendyol_bestsellers(self, limit: int = 5, min_discount_percent: int = 25) -> list[PopularDeal]:
        found: list[PopularDeal] = []
        feeds = list(self.TRENDYOL_BESTSELLER_FEEDS)
        random.shuffle(feeds)
        for feed_url, badge in feeds:
            if len(found) >= limit:
                break
            try:
                html = self.fetcher.get_text(feed_url)
            except Exception as exc:
                logger.debug("Trendyol çok satanlar okunamadı (%s): %s", feed_url, exc)
                continue
            soup = BeautifulSoup(html, "lxml")
            per_feed = 0
            for card in soup.select("a.product-card")[:12]:
                if len(found) >= limit or per_feed >= 2:
                    break
                txt = card.get_text(" ", strip=True)
                href = canonicalize_deal_key(urljoin("https://www.trendyol.com", str(card.get("href", ""))))
                m_prices = re.findall(r"(\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?)\s*TL", txt, re.IGNORECASE)
                if not m_prices:
                    continue
                price_val = parse_price_tl(m_prices[-1])
                if not price_val or price_val < 50:
                    continue
                old_price = parse_price_tl(m_prices[0]) if len(m_prices) >= 2 else None
                if old_price and old_price <= price_val:
                    old_price = None
                pct_m = re.search(r"(?:-\s*%\s*(\d{1,2})|%\s*(\d{1,2})\s*(?:indirim|tasarruf|düşüş))\b", txt, re.I)
                if old_price and old_price > price_val:
                    pct = max(1, int(round(((old_price - price_val) / old_price) * 100.0)))
                elif pct_m:
                    pct = int(pct_m.group(1) or pct_m.group(2))
                else:
                    continue
                if pct < min_discount_percent or (not old_price and pct > 75):
                    continue
                img_el = card.select_one("img[src]")
                img_url = str(img_el.get("src", "")) if img_el else ""
                title = (
                    (img_el.get("alt") if img_el and img_el.get("alt") else "")
                    or txt[:120]
                )
                title = " ".join(str(title).split())
                disc, c_info, c_code = extract_coupon_info(txt, price_val)
                found.append(
                    PopularDeal(
                        deal_key=href,
                        title=title[:150],
                        seller="Trendyol",
                        price_tl=price_val,
                        previous_or_second_price_tl=old_price,
                        discount_percent=pct,
                        direct_url=href,
                        image_url=img_url,
                        is_official_seller=False,
                        coupon_discount_tl=disc,
                        coupon_info=c_info,
                        coupon_code=c_code,
                        badge_label=badge,
                    )
                )
                per_feed += 1
        return found

    def _hunt_other_retailers(self, limit: int = 5, min_discount_percent: int = 5) -> list[PopularDeal]:
        found: list[PopularDeal] = []
        feeds = list(self.OTHER_RETAILER_FEEDS)
        random.shuffle(feeds)
        for feed_url, seller_name, base_url, badge in feeds:
            if len(found) >= limit:
                break
            try:
                html = self.fetcher.get_text(feed_url)
            except Exception as exc:
                logger.debug("%s fırsatları okunamadı (%s): %s", seller_name, feed_url, exc)
                continue

            ld_items = [
                x for x in extract_json_ld_products(html)
                if x.get("price_tl") and float(x["price_tl"]) >= 50 and x.get("in_stock", True)
            ]
            per_feed = 0
            for item in ld_items[:4]:
                if len(found) >= limit or per_feed >= 2:
                    break
                p_val = float(item["price_tl"])
                t_str = str(item.get("title") or "").strip()
                u_str = urljoin(base_url, str(item.get("url") or feed_url))
                img_url = str(item.get("image_url") or "")
                if not t_str or not u_str.startswith("http"):
                    continue
                old_p = item.get("high_price_tl")
                if not img_url or not old_p or float(old_p) <= p_val:
                    page_img, page_old = self._fetch_product_image_and_old_price(u_str, p_val)
                    if not img_url and page_img:
                        img_url = page_img
                    if page_old and (not old_p or page_old > p_val):
                        old_p = page_old
                if not old_p or float(old_p) <= p_val:
                    continue
                pct = max(1, int(round(((float(old_p) - p_val) / float(old_p)) * 100.0)))
                if pct < min_discount_percent:
                    continue
                disc, c_info, c_code = extract_coupon_info(t_str, p_val)
                found.append(
                    PopularDeal(
                        deal_key=canonicalize_deal_key(u_str),
                        title=t_str[:150],
                        seller=seller_name,
                        price_tl=p_val,
                        previous_or_second_price_tl=float(old_p),
                        discount_percent=pct,
                        direct_url=u_str,
                        image_url=img_url,
                        is_official_seller=is_official_or_trusted_seller(seller_name, u_str),
                        coupon_discount_tl=disc,
                        coupon_info=c_info,
                        coupon_code=c_code,
                        badge_label=badge,
                    )
                )
                per_feed += 1
        return found

    def _fetch_product_image_and_old_price(self, url: str, current_price: float) -> tuple[str, Optional[float]]:
        try:
            html = self.fetcher.get_text(url)
            soup = BeautifulSoup(html, "lxml")
            img_url = _extract_image_url(soup, html)
            old_p: Optional[float] = None
            for sel in ("span.prc-first", "span[class*='old']", "del", "s"):
                el = soup.select_one(sel)
                if el:
                    cand = parse_price_tl(el.get_text(" ", strip=True))
                    if cand and current_price < cand <= current_price * 2.5:
                        old_p = cand
                        break
            return img_url, old_p
        except Exception:
            return "", None

    def _resolve_detail_page(
        self,
        fallback_title: str,
        detail_url: str,
        discount_pct: int,
        fallback_price: Optional[float],
        badge_label: str = "SON 6 AYIN EN DÜŞÜK FİYATI!",
    ) -> Optional[PopularDeal]:
        try:
            p_html = self.fetcher.get_text(detail_url)
        except Exception as exc:
            logger.debug("Popüler fırsat detay sayfası okunamadı (%s): %s", detail_url, exc)
            return None

        soup = BeautifulSoup(p_html, "lxml")
        h1 = soup.select_one("h1")
        title = " ".join((h1.get_text(" ", strip=True) if h1 else fallback_title).split())

        image_url = _extract_image_url(soup, p_html)
        ld_offers = [
            x for x in extract_json_ld_products(p_html)
            if x.get("price_tl") and float(x["price_tl"]) > 20 and x.get("in_stock", True)
        ]
        ld_offers.sort(key=lambda x: float(x["price_tl"]))

        if not ld_offers:
            return None

        # Doğrudan mağaza linki olan en ucuz teklifi seç
        direct_offers = [x for x in ld_offers if is_direct_store_url(str(x.get("url") or ""))]
        best = direct_offers[0] if direct_offers else ld_offers[0]

        best_price = float(best["price_tl"])
        best_url = str(best.get("url") or detail_url).strip()
        if not is_direct_store_url(best_url):
            return None

        seller = normalize_seller_name(str(best.get("seller") or "Mağaza"), "Mağaza")

        # İkinci en ucuz fiyatı veya indirim öncesi referans fiyatı hesapla
        second_price: Optional[float] = None
        for other in ld_offers[1:]:
            op = float(other["price_tl"])
            if op > best_price * 1.02:
                second_price = op
                break
        if second_price is None and discount_pct > 0:
            second_price = round(best_price / max(0.2, (1.0 - discount_pct / 100.0)), 2)

        actual_pct = discount_pct
        if second_price and second_price > best_price:
            calc_pct = int(round(((second_price - best_price) / second_price) * 100.0))
            actual_pct = max(discount_pct, min(calc_pct, 75))

        disc_tl, c_info, c_code = extract_coupon_info(title, best_price)
        trade_in = extract_trade_in_info(title)
        deal_key = detail_url.split("?")[0]

        return PopularDeal(
            deal_key=deal_key,
            title=title,
            seller=seller,
            price_tl=best_price,
            previous_or_second_price_tl=second_price,
            discount_percent=actual_pct,
            direct_url=best_url,
            image_url=image_url,
            is_official_seller=is_official_or_trusted_seller(seller, best_url),
            coupon_discount_tl=disc_tl,
            coupon_info=c_info,
            coupon_code=c_code,
            trade_in_info=trade_in,
            badge_label=badge_label,
        )


def fetch_realtime_market_avg_price(
    title: str,
    current_price_tl: float,
    fetcher: HttpFetcher,
    fallback_prev_tl: Optional[float] = None,
) -> Optional[float]:
    """
    Bildirim yayınlanmadan hemen önce ürün adını piyasada (Akakçe / çoklu mağaza) anlık aratarak
    o ürünün gerçek ortalama piyasa fiyatını hesaplar.
    """
    import statistics
    from urllib.parse import quote_plus

    if current_price_tl <= 0 or not title:
        return fallback_prev_tl

    # Başlıktan arama için ilk 6 anlamlı kelimeyi al
    words = [w for w in re.split(r"\s+", title.strip()) if len(w) >= 2 and not w.isdigit()]
    query = " ".join(words[:6])
    prices: list[float] = []

    if len(query) >= 4:
        try:
            url = f"https://www.akakce.com/arama/?q={quote_plus(query)}"
            html = fetcher.get_text(url)
            soup = BeautifulSoup(html, "lxml")
            for p_el in soup.select("span.pt_v8, .pt_v8")[:8]:
                pv = parse_price_tl(p_el.get_text(" ", strip=True))
                if pv and current_price_tl * 0.70 <= pv <= current_price_tl * 2.8:
                    prices.append(pv)
        except Exception:
            pass

    if fallback_prev_tl and current_price_tl < fallback_prev_tl <= current_price_tl * 2.8:
        prices.append(float(fallback_prev_tl))

    if not prices:
        return None
    return round(float(statistics.mean(prices)), 2)


_ACTIVE_COUPONS_CACHE: tuple[float, set[str]] = (0.0, set())


def _get_live_active_coupons(fetcher: HttpFetcher) -> set[str]:
    """Canlı indirim kodları sayfasından süresi dolmamış aktif kupon kodlarını (5 dk önbellekli) çeker."""
    import time

    global _ACTIVE_COUPONS_CACHE
    now_ts = time.time()
    if now_ts - _ACTIVE_COUPONS_CACHE[0] < 300.0 and _ACTIVE_COUPONS_CACHE[1]:
        return _ACTIVE_COUPONS_CACHE[1]

    active: set[str] = set()
    try:
        html = fetcher.get_text("https://www.onual.com/indirim-kodlari")
        soup = BeautifulSoup(html, "lxml")
        txt = soup.get_text(" ", strip=True)
        for m in re.finditer(r"\b([A-Z0-9ÇĞİÖŞÜ]{4,18})\s+Kopyala\b", txt):
            code = m.group(1).strip().upper()
            if not code.startswith("ONUAL") and not code.isdigit():
                active.add(code)
    except Exception:
        pass
    _ACTIVE_COUPONS_CACHE = (now_ts, active)
    return active


def verify_coupon_live(
    coupon_code: str,
    coupon_info: str,
    product_url: str,
    fetcher: HttpFetcher,
) -> tuple[bool, str, str]:
    """
    Kupon kodunu veya kupon talimatını hedef mağaza sayfasında ve aktif kupon listesinde canlı test eder.
    Doğrulanamayan (emin olunmayan) kuponları kesinlikle eler.
    """
    from src.community_deals import decode_onual_coupon_param

    raw_code = (coupon_code or "").strip()
    raw_info = (coupon_info or "").strip()
    if raw_code:
        decoded_code, decoded_howto = decode_onual_coupon_param(raw_code)
        raw_code = decoded_code
        if decoded_howto and not raw_info:
            raw_info = decoded_howto

    if not raw_code and not raw_info:
        return True, "", ""

    page_html = ""
    if product_url and product_url.startswith("http"):
        try:
            page_html = fetcher.get_text(product_url)
        except Exception:
            page_html = ""

    low_html = page_html.lower()

    if raw_code:
        # 1) Hedef mağaza/kampanya sayfasında kod açıkça geçiyor mu?
        if low_html and raw_code.lower() in low_html:
            return True, raw_code, raw_info
        # 2) Canlı aktif kupon havuzunda (süresi dolmamış kodlar) yer alıyor mu?
        active_set = _get_live_active_coupons(fetcher)
        if raw_code.upper() in active_set:
            return True, raw_code, raw_info
        # Emin olunamayan kupon kodunu ekleme!
        logger.info("Doğrulanamayan kupon kodu elendi: %s (%s)", raw_code, product_url)
        return False, "", ""

    # Kupon kodu değil, mağaza içi kupon talimatıysa (ör. 'Ürünün altındaki 500 TL kuponu toplayın')
    if raw_info:
        if low_html and any(k in low_html for k in ("kupon", "coupon", "indirim", "kampanya")):
            return True, "", raw_info
        logger.info("Mağaza sayfasında doğrulanamayan kupon talimatı elendi: %s", raw_info)
        return False, "", ""

    return True, "", ""


def verify_and_attach_realtime_avg(
    deal: PopularDeal,
    fetcher: HttpFetcher,
    min_discount_percent: int = 25,
) -> bool:
    """
    Yayın öncesinde ürünün anlık piyasa ortalama fiyatını kontrol edip deal.market_avg_price_tl alanına yazar.
    Eğer eski fiyat birim fiyat hatası yüzünden şişirilmişse (ör. >2.8x) veya ürün piyasa ortalamasından
    pahalıysa fırsatı reddeder (False döndürür).
    """
    eff = deal.effective_price_tl
    if deal.previous_or_second_price_tl and deal.previous_or_second_price_tl > eff * 2.8:
        # 100g/kg birim fiyat hatası (ör. 274 TL rujda 9.163 TL/100g)
        deal.previous_or_second_price_tl = None
        deal.discount_percent = 0

    avg_p = fetch_realtime_market_avg_price(
        title=deal.title,
        current_price_tl=eff,
        fetcher=fetcher,
        fallback_prev_tl=deal.previous_or_second_price_tl,
    )
    deal.market_avg_price_tl = avg_p

    # Eğer ürün anlık piyasa ortalamasından daha pahalıysa gerçek fırsat değildir
    if avg_p and eff >= avg_p * 0.98 and (not deal.previous_or_second_price_tl or deal.previous_or_second_price_tl <= eff):
        return False

    if not deal.previous_or_second_price_tl and avg_p and avg_p > eff:
        deal.previous_or_second_price_tl = avg_p
        deal.discount_percent = max(1, int(round(((avg_p - eff) / avg_p) * 100.0)))

    if deal.previous_or_second_price_tl and deal.previous_or_second_price_tl > eff:
        real_pct = max(1, int(round(((deal.previous_or_second_price_tl - eff) / deal.previous_or_second_price_tl) * 100.0)))
        deal.discount_percent = min(deal.discount_percent or real_pct, real_pct)

    if deal.discount_percent < min_discount_percent or deal.discount_percent > 80:
        return False

    # Kupon varsa yayın öncesi canlı test et; doğrulanamazsa kuponu gönderiden çıkar
    if deal.coupon_code or deal.coupon_info:
        c_ok, v_code, v_info = verify_coupon_live(
            deal.coupon_code, deal.coupon_info, deal.direct_url, fetcher
        )
        deal.coupon_code = v_code
        deal.coupon_info = v_info
        if not c_ok:
            deal.coupon_discount_tl = 0.0
            deal.stack_summary = ""

    _enrich_deal_insights(deal)
    return True


def _enrich_deal_insights(deal: PopularDeal) -> None:
    """Üçlü birleştirme (mağaza + kupon + banka) ve çoklu alım birim fiyat analizini ürüne ekler."""
    from src.deal_stacker import calculate_multibuy_unit_price, compute_best_stack

    if not deal.multibuy_summary:
        mb = calculate_multibuy_unit_price(deal.title, deal.effective_price_tl)
        if mb is not None:
            deal.multibuy_summary = mb.summary_line
    if not deal.stack_summary:
        st = compute_best_stack(
            seller=deal.seller,
            title=deal.title,
            list_price_tl=deal.previous_or_second_price_tl,
            effective_price_tl=deal.effective_price_tl,
            coupon_discount_tl=deal.coupon_discount_tl,
            coupon_code=deal.coupon_code,
            coupon_info=deal.coupon_info,
        )
        if st is not None:
            deal.stack_summary = st.summary_line


def _filter_by_historical_prices(deals: list[PopularDeal], storage) -> list[PopularDeal]:
    """Veritabanındaki geçmiş fiyatlarla karşılaştırıp sahte/şişirilmiş indirimleri eler."""
    filtered: list[PopularDeal] = []
    for d in deals:
        try:
            with storage._connect() as conn:
                row = conn.execute(
                    "SELECT min_price_tl FROM bestseller_products WHERE product_key = ?",
                    (d.deal_key,),
                ).fetchone()
            if row and row["min_price_tl"]:
                hist_low = float(row["min_price_tl"])
                # Mevcut fiyat geçmiş en düşük fiyattan %10+ pahalıysa gerçek dip indirim değildir
                if d.effective_price_tl > hist_low * 1.10:
                    continue
        except Exception:
            pass
        filtered.append(d)
    return filtered


def is_meaningful_deal(deal: PopularDeal, min_discount_percent: int = 25, min_savings_tl: float = 100.0) -> bool:
    """Küçük/önemsiz indirimleri (ör. 11 TL / %5) eler; gerçek yüzde ve TL kazancı ister."""
    if deal.discount_percent < min_discount_percent:
        return False
    eff = deal.effective_price_tl
    prev = deal.previous_or_second_price_tl
    if prev and prev > eff:
        savings = prev - eff
    elif 0 < deal.discount_percent < 95:
        est_prev = eff / (1.0 - deal.discount_percent / 100.0)
        savings = est_prev - eff
    else:
        savings = 0.0
    return savings >= min_savings_tl


def _extract_image_url(soup: BeautifulSoup, raw_html: str) -> str:
    og = soup.select_one("meta[property='og:image']")
    if og and og.get("content"):
        u = str(og["content"]).strip()
        if u.startswith("//"):
            u = "https:" + u
        if u.startswith("http"):
            return u

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or "")
            img = data.get("image") if isinstance(data, dict) else None
            if isinstance(img, list) and img:
                img = img[0]
            if isinstance(img, str):
                if img.startswith("//"):
                    img = "https:" + img
                if img.startswith("http"):
                    return img
        except Exception:
            continue
    return ""

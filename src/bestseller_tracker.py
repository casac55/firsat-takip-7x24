from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from src.config import AppConfig
from src.fetchers import HttpFetcher
from src.filter import extract_coupon_info, is_official_or_trusted_seller, parse_price_tl
from src.popular_deals import canonicalize_deal_key
from src.scrapers.structured import extract_json_ld_products
from src.storage import PriceStorage

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class BestsellerItem:
    product_key: str
    title: str
    seller: str
    category: str
    url: str
    price_tl: float
    image_url: str = ""
    coupon_discount_tl: float = 0.0
    coupon_info: str = ""
    coupon_code: str = ""
    is_official_seller: bool = False

    @property
    def effective_price_tl(self) -> float:
        if self.coupon_discount_tl > 0:
            return round(max(1.0, self.price_tl - self.coupon_discount_tl), 2)
        return self.price_tl


@dataclass(slots=True)
class BestsellerDealDrop:
    item: BestsellerItem
    reason_badge: str
    prev_price_tl: Optional[float]
    period_low_tl: Optional[float]
    drop_percent: int
    is_glitch: bool = False


class BestsellerTracker:
    """
    Tüm büyük e-ticaret sitelerindeki (Amazon, Trendyol, N11, Teknosa, MediaMarkt, Pazarama)
    'Çok Satanlar' ve 'Çok Değerlendirilenler' listelerini sürekli veritabanında takip eder;
    son 1 aylık (30 gün) / 6 aylık dip fiyata veya anlamlı indirime düşenleri bildirir.
    """

    AMAZON_FEEDS = (
        ("https://www.amazon.com.tr/gp/bestsellers/electronics", "Amazon Çok Satanlar • Elektronik"),
        ("https://www.amazon.com.tr/gp/bestsellers/kitchen", "Amazon Çok Satanlar • Ev & Mutfak"),
        ("https://www.amazon.com.tr/gp/bestsellers/computers", "Amazon Çok Satanlar • Bilgisayar"),
        ("https://www.amazon.com.tr/gp/bestsellers/hpc", "Amazon Çok Satanlar • Kişisel Bakım"),
        ("https://www.amazon.com.tr/gp/bestsellers/beauty", "Amazon Çok Satanlar • Kozmetik"),
        ("https://www.amazon.com.tr/gp/bestsellers/sports", "Amazon Çok Satanlar • Spor"),
        ("https://www.amazon.com.tr/gp/bestsellers/apparel", "Amazon Çok Satanlar • Giyim"),
        ("https://www.amazon.com.tr/gp/bestsellers/shoes", "Amazon Çok Satanlar • Ayakkabı"),
        ("https://www.amazon.com.tr/gp/bestsellers/grocery", "Amazon Çok Satanlar • Süpermarket"),
        ("https://www.amazon.com.tr/gp/bestsellers/toys", "Amazon Çok Satanlar • Oyuncak"),
        ("https://www.amazon.com.tr/gp/bestsellers/diy", "Amazon Çok Satanlar • Yapı Market"),
        ("https://www.amazon.com.tr/gp/bestsellers/automotive", "Amazon Çok Satanlar • Oto Aksesuar"),
        ("https://www.amazon.com.tr/gp/most-wished-for/electronics", "Amazon En Çok İstenenler • Elektronik"),
    )
    TRENDYOL_FEEDS = (
        ("https://www.trendyol.com/sr?sst=BEST_SELLER&q=elektronik", "Trendyol Çok Satanlar • Elektronik"),
        ("https://www.trendyol.com/sr?sst=MOST_RATED&q=elektronik", "Trendyol Çok Değerlendirilen • Elektronik"),
        ("https://www.trendyol.com/sr?sst=BEST_SELLER&q=bilgisayar", "Trendyol Çok Satanlar • Bilgisayar"),
        ("https://www.trendyol.com/sr?sst=BEST_SELLER&q=ev+yasam", "Trendyol Çok Satanlar • Ev & Yaşam"),
        ("https://www.trendyol.com/sr?sst=MOST_RATED&q=ev+yasam", "Trendyol Çok Değerlendirilen • Ev & Yaşam"),
        ("https://www.trendyol.com/sr?sst=BEST_SELLER&q=kucuk+ev+aletleri", "Trendyol Çok Satanlar • Küçük Ev Aletleri"),
        ("https://www.trendyol.com/sr?sst=BEST_SELLER&q=kozmetik", "Trendyol Çok Satanlar • Kozmetik"),
        ("https://www.trendyol.com/sr?sst=BEST_SELLER&q=giyim", "Trendyol Çok Satanlar • Giyim"),
        ("https://www.trendyol.com/sr?sst=BEST_SELLER&q=ayakkabi", "Trendyol Çok Satanlar • Ayakkabı"),
        ("https://www.trendyol.com/sr?sst=BEST_SELLER&q=spor+outdoor", "Trendyol Çok Satanlar • Spor & Outdoor"),
        ("https://www.trendyol.com/sr?sst=BEST_SELLER&q=supermarket", "Trendyol Çok Satanlar • Süpermarket"),
        ("https://www.trendyol.com/sr?sst=BEST_SELLER&q=anne+bebek", "Trendyol Çok Satanlar • Anne & Bebek"),
    )
    RETAILER_FEEDS = (
        ("https://www.n11.com/kampanyalar", "N11", "https://www.n11.com", "N11 Çok Satan / Kampanya"),
        ("https://www.teknosa.com/arama/?s=en+cok+satan", "Teknosa", "https://www.teknosa.com", "Teknosa Çok Satanlar"),
        ("https://www.mediamarkt.com.tr/tr/search.html?query=populer", "MediaMarkt", "https://www.mediamarkt.com.tr", "MediaMarkt Popüler"),
        ("https://www.pazarama.com/arama?q=cok+satan", "Pazarama", "https://www.pazarama.com", "Pazarama Çok Satanlar"),
        ("https://www.idefix.com/arama?q=cok+satan", "İdefix", "https://www.idefix.com", "İdefix Çok Satanlar"),
    )

    def __init__(self, config: AppConfig, storage: PriceStorage, fetcher: HttpFetcher) -> None:
        self.config = config
        self.storage = storage
        self.fetcher = fetcher

    def collect_all_bestsellers(self) -> list[BestsellerItem]:
        items: list[BestsellerItem] = []
        items.extend(self._collect_amazon())
        items.extend(self._collect_trendyol())
        items.extend(self._collect_other_retailers())

        # product_key bazlı tekilleştir
        unique: dict[str, BestsellerItem] = {}
        for it in items:
            if it.product_key not in unique or it.effective_price_tl < unique[it.product_key].effective_price_tl:
                unique[it.product_key] = it
        return list(unique.values())

    def scan_and_detect_drops(self) -> tuple[int, list[BestsellerDealDrop]]:
        """Tüm çok satanları tarar, fiyat geçmişini kaydeder ve 1 aylık / 6 aylık / anlık düşüşleri döndürür."""
        items = self.collect_all_bestsellers()
        drops: list[BestsellerDealDrop] = []
        compare_days = self.config.bestseller_compare_days

        for item in items:
            eff = item.effective_price_tl
            stats = self.storage.record_bestseller_and_check_drop(
                product_key=item.product_key,
                title=item.title,
                seller=item.seller,
                category=item.category,
                url=item.url,
                image_url=item.image_url,
                effective_price_tl=eff,
                coupon_info=item.coupon_info,
                coupon_code=item.coupon_code,
                compare_days=compare_days,
            )

            prev_p = stats["prev_price_tl"]
            min_nd = stats["min_nd_tl"]
            min_180d = stats["min_180d_tl"]
            if prev_p is None or prev_p <= 0:
                continue

            from src.deal_stacker import is_price_glitch

            med_30d = self.storage.get_bestseller_30d_median(item.product_key, days=30)
            glitch, glitch_pct = is_price_glitch(eff, med_30d)

            diff = prev_p - eff
            pct = (diff / prev_p) * 100.0 if prev_p > 0 else 0.0
            is_drop = diff >= self.config.bestseller_min_drop_tl and pct >= self.config.bestseller_min_drop_percent

            badge = ""
            ref_low: Optional[float] = None
            if glitch:
                badge = "🚨⚡ FİYAT HATASI / %60+ ANİ DÜŞÜŞ!"
                ref_low = med_30d
                prev_p = med_30d
                pct = float(glitch_pct)
            elif min_180d is not None and eff < min_180d - 20.0 and pct >= 2.0:
                badge = "SON 6 AYIN EN DÜŞÜK FİYATI!"
                ref_low = min_180d
            elif min_nd is not None and eff < min_nd - 20.0 and pct >= 2.0:
                badge = f"SON {compare_days} GÜNÜN EN DÜŞÜK FİYATI!"
                ref_low = min_nd
            elif is_drop:
                badge = "ÇOK SATANLARDA FİYAT DÜŞTÜ!"
                ref_low = prev_p

            if badge:
                drops.append(
                    BestsellerDealDrop(
                        item=item,
                        reason_badge=badge,
                        prev_price_tl=prev_p,
                        period_low_tl=ref_low,
                        drop_percent=max(1, int(round(pct))),
                        is_glitch=glitch,
                    )
                )

        drops.sort(key=lambda d: (d.is_glitch, d.drop_percent), reverse=True)
        return len(items), drops

    def _collect_amazon(self) -> list[BestsellerItem]:
        found: list[BestsellerItem] = []
        for url, cat in self.AMAZON_FEEDS:
            try:
                html = self.fetcher.get_text(url)
            except Exception:
                continue
            soup = BeautifulSoup(html, "lxml")
            for card in soup.select("div.zg-grid-general-faceout")[:20]:
                link_el = card.select_one("a[href*='/dp/']")
                price_el = card.select_one("span._cDEzb_p13n-sc-price_3mJ9Z, span.p13n-sc-price, span.a-color-price")
                if not link_el or not price_el:
                    continue
                dp = canonicalize_deal_key(urljoin("https://www.amazon.com.tr", str(link_el.get("href", ""))))
                p_val = parse_price_tl(price_el.get_text(" ", strip=True))
                if not p_val or p_val < 50:
                    continue
                img_el = card.select_one("img[src]")
                title = (
                    (img_el.get("alt") if img_el and img_el.get("alt") else "")
                    or link_el.get_text(" ", strip=True)
                )
                title = " ".join(str(title).split())
                if len(title) < 6:
                    continue
                found.append(
                    BestsellerItem(
                        product_key=dp,
                        title=title[:150],
                        seller="Amazon.com.tr",
                        category=cat,
                        url=dp,
                        price_tl=p_val,
                        image_url=str(img_el.get("src", "")) if img_el else "",
                        is_official_seller=True,
                    )
                )
        return found

    def _collect_trendyol(self) -> list[BestsellerItem]:
        found: list[BestsellerItem] = []
        for url, cat in self.TRENDYOL_FEEDS:
            try:
                html = self.fetcher.get_text(url)
            except Exception:
                continue
            soup = BeautifulSoup(html, "lxml")
            for card in soup.select("a.product-card")[:20]:
                txt = card.get_text(" ", strip=True)
                href = canonicalize_deal_key(urljoin("https://www.trendyol.com", str(card.get("href", ""))))
                m_prices = re.findall(r"(\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?)\s*TL", txt, re.IGNORECASE)
                if not m_prices:
                    continue
                p_val = parse_price_tl(m_prices[-1])
                if not p_val or p_val < 50:
                    continue
                img_el = card.select_one("img[src]")
                title = (
                    (img_el.get("alt") if img_el and img_el.get("alt") else "")
                    or txt[:120]
                )
                title = " ".join(str(title).split())
                disc, c_info, c_code = extract_coupon_info(txt, p_val)
                found.append(
                    BestsellerItem(
                        product_key=href,
                        title=title[:150],
                        seller="Trendyol",
                        category=cat,
                        url=href,
                        price_tl=p_val,
                        image_url=str(img_el.get("src", "")) if img_el else "",
                        coupon_discount_tl=disc,
                        coupon_info=c_info,
                        coupon_code=c_code,
                    )
                )
        return found

    def _collect_other_retailers(self) -> list[BestsellerItem]:
        found: list[BestsellerItem] = []
        for url, seller_name, base_url, cat in self.RETAILER_FEEDS:
            try:
                html = self.fetcher.get_text(url)
            except Exception:
                continue
            ld_items = [
                x for x in extract_json_ld_products(html)
                if x.get("price_tl") and float(x["price_tl"]) >= 50 and x.get("in_stock", True)
            ]
            for item in ld_items[:12]:
                p_val = float(item["price_tl"])
                t_str = str(item.get("title") or "").strip()
                u_str = canonicalize_deal_key(urljoin(base_url, str(item.get("url") or url)))
                img_url = str(item.get("image_url") or "")
                if not t_str or not u_str.startswith("http"):
                    continue
                disc, c_info, c_code = extract_coupon_info(t_str, p_val)
                found.append(
                    BestsellerItem(
                        product_key=u_str,
                        title=t_str[:150],
                        seller=seller_name,
                        category=cat,
                        url=u_str,
                        price_tl=p_val,
                        image_url=img_url,
                        coupon_discount_tl=disc,
                        coupon_info=c_info,
                        coupon_code=c_code,
                        is_official_seller=is_official_or_trusted_seller(seller_name, u_str),
                    )
                )
        return found

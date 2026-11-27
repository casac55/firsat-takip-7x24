from __future__ import annotations

import logging
from bs4 import BeautifulSoup

from src.filter import parse_price_tl
from src.models import PriceOffer
from src.scrapers.base import BaseScraper
from src.scrapers.structured import extract_json_ld_products

logger = logging.getLogger(__name__)


class EpeyScraper(BaseScraper):
    name = "epey"
    BASE_URL = "https://www.epey.com"
    SEARCH_URL = "https://www.epey.com/ara/?ara=samsung+galaxy+s25+ultra"

    def scrape(self) -> list[PriceOffer]:
        html = self.fetcher.get_text(self.SEARCH_URL)
        soup = BeautifulSoup(html, "lxml")

        offers: list[PriceOffer] = []
        product_links: list[tuple[str, str]] = []

        # Arama sonuçlarından S25 Ultra telefon sayfalarını bul
        for a in soup.select("a.urunadi, a[href*='akilli-telefonlar/']"):
            title = a.get_text(" ", strip=True)
            href = a.get("href", "").strip()
            if not href or "s25" not in title.lower() or "ultra" not in title.lower():
                continue
            if href not in [u for _, u in product_links]:
                product_links.append((title, href))

        # Doğrudan arama sayfasındaki fiyatları da ekle
        for item in extract_json_ld_products(html):
            off = self.make_offer(
                title=item["title"],
                price_tl=item["price_tl"],
                url=item["url"],
                seller=item["seller"] or "Epey",
                base_url=self.BASE_URL,
                in_stock=item["in_stock"],
            )
            if off:
                offers.append(off)

        # Bulunan S25 Ultra varyant sayfalarına girip tüm mağaza fiyatlarını çek (en fazla 4 sayfa)
        for prod_title, href in product_links[:4]:
            full_url = href if href.startswith("http") else f"{self.BASE_URL}{href}"
            try:
                p_html = self.fetcher.get_text(full_url)
            except Exception as exc:
                logger.debug("Epey ürün sayfası okunamadı (%s): %s", full_url, exc)
                continue

            p_soup = BeautifulSoup(p_html, "lxml")
            h1 = p_soup.select_one("h1")
            page_title = h1.get_text(" ", strip=True) if h1 else prod_title

            # Epey fiyat satırları: .fiyatlar, a.git, span.urun_fiyat vb.
            for row in p_soup.select(".fiyatlar .fiyat_satir, .fiyat_listesi li, tr.fiyat"):
                price_el = row.select_one(".urun_fiyat, .fiyat, .price, span[class*='fiyat']")
                seller_el = row.select_one(".site_adi, .magaza, img[alt]")
                link_el = row.select_one("a[href]")

                price_val = parse_price_tl(price_el.get_text(" ", strip=True) if price_el else row.get_text(" ", strip=True))
                seller_name = ""
                if seller_el:
                    seller_name = seller_el.get("alt") or seller_el.get_text(" ", strip=True)
                offer_url = link_el.get("href", full_url) if link_el else full_url

                off = self.make_offer(
                    title=page_title,
                    price_tl=price_val,
                    url=offer_url,
                    seller=seller_name or "Epey",
                    base_url=self.BASE_URL,
                )
                if off:
                    offers.append(off)

            for item in extract_json_ld_products(p_html):
                off = self.make_offer(
                    title=item["title"] or page_title,
                    price_tl=item["price_tl"],
                    url=item["url"] or full_url,
                    seller=item["seller"] or "Epey",
                    base_url=self.BASE_URL,
                    in_stock=item["in_stock"],
                )
                if off:
                    offers.append(off)

        return offers

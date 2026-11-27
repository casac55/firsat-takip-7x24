from __future__ import annotations

import logging
from bs4 import BeautifulSoup

from src.filter import normalize_seller_name, parse_price_tl
from src.models import PriceOffer
from src.scrapers.base import BaseScraper
from src.scrapers.structured import extract_json_ld_products

logger = logging.getLogger(__name__)


class AkakceScraper(BaseScraper):
    name = "akakce"
    BASE_URL = "https://www.akakce.com"
    SEARCH_URL = "https://www.akakce.com/arama/?q=samsung+galaxy+s25+ultra"

    def scrape(self) -> list[PriceOffer]:
        offers: list[PriceOffer] = []
        product_urls: list[tuple[str, str, PriceOffer]] = []

        html = self.fetcher.get_text(self.SEARCH_URL)
        soup = BeautifulSoup(html, "lxml")

        for card in soup.select("li.p, ul#APL li, div.p-card, li[data-pr]"):
            title_el = card.select_one("h3.pn_v8, span.pn_v8, a[title]")
            price_el = card.select_one("span.pt_v8, span.pt_v9, span.pb_v8, .pt_v8")
            link_el = card.select_one("a[href]")
            if not title_el or not link_el:
                continue

            title = title_el.get("title") or title_el.get_text(" ", strip=True)
            href = link_el.get("href", "").strip()
            price_val = parse_price_tl(price_el.get_text(" ", strip=True) if price_el else "")

            off = self.make_offer(
                title=title,
                price_tl=price_val,
                url=href,
                seller="Akakçe",
                base_url=self.BASE_URL,
            )
            if off and href not in [u for _, u, _ in product_urls]:
                product_urls.append((title, href, off))

        # Ürün detay sayfalarına girip doğrudan satıcı linklerini (JSON-LD) ve kupon metinlerini topla
        for prod_title, href, fallback_offer in product_urls[:4]:
            full_url = href if href.startswith("http") else f"{self.BASE_URL}{href}"
            detail_offers: list[PriceOffer] = []
            try:
                p_html = self.fetcher.get_text(full_url)
                p_soup = BeautifulSoup(p_html, "lxml")
                h1 = p_soup.select_one("h1")
                page_title = h1.get_text(" ", strip=True) if h1 else prod_title

                # Satır metinlerini (kupon/indirim bilgisi için) satıcı bazlı indeksle
                row_texts_by_seller: dict[str, str] = {}
                html_row_offers: list[PriceOffer] = []

                for row in p_soup.select("ul#PL li, ul.pl_v8 li, li.c"):
                    price_el = row.select_one("span.pt_v8, .pt_v8, span[class*='pt_']")
                    if not price_el:
                        continue

                    img_el = row.select_one("span.v_v8 img[alt], .v_v8 img[alt], img[alt]")
                    v_span = row.select_one("span.v_v8, .v_v8")
                    img_alt = (img_el.get("alt") or "").strip() if img_el else ""
                    span_txt = v_span.get_text(" ", strip=True).lstrip("/ ").strip() if v_span else ""
                    seller_name = img_alt or span_txt or "Akakçe"
                    norm_s = normalize_seller_name(seller_name, self.name).lower()
                    row_txt = row.get_text(" ", strip=True)
                    row_texts_by_seller[norm_s] = row_txt

                    link_el = row.select_one("a[href]")
                    raw_href = link_el.get("href", "") if link_el else ""
                    # Sadece doğrudan mağaza linki varsa veya JSON-LD boşsa kullanılacak
                    off = self.make_offer(
                        title=page_title,
                        price_tl=parse_price_tl(price_el.get_text(" ", strip=True)),
                        url=raw_href or full_url,
                        seller=seller_name,
                        base_url=self.BASE_URL,
                        extra_text=row_txt,
                    )
                    if off:
                        html_row_offers.append(off)

                # 1) Öncelik: JSON-LD içindeki DOĞRUDAN mağaza bağlantıları (idefix.com, trendyol.com, hepsiburada.com vb.)
                for item in extract_json_ld_products(p_html):
                    if not item["seller"]:
                        continue
                    norm_s = normalize_seller_name(item["seller"], self.name).lower()
                    extra_txt = row_texts_by_seller.get(norm_s, "")
                    off = self.make_offer(
                        title=item["title"] or page_title,
                        price_tl=item["price_tl"],
                        url=item["url"] or full_url,
                        seller=item["seller"],
                        base_url=self.BASE_URL,
                        in_stock=item["in_stock"],
                        extra_text=extra_txt,
                    )
                    if off:
                        detail_offers.append(off)

                # JSON-LD bulunamadıysa HTML satırlarını fallback olarak kullan
                if not detail_offers and html_row_offers:
                    detail_offers.extend(html_row_offers)

            except Exception as exc:
                logger.debug("Akakçe ürün detayı alınamadı (%s): %s", full_url, exc)

            if detail_offers:
                offers.extend(detail_offers)
            else:
                offers.append(fallback_offer)

        return offers

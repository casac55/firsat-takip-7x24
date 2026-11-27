from __future__ import annotations

import json
import logging
import re
from urllib.parse import parse_qs, unquote, urlparse

from bs4 import BeautifulSoup

from src.filter import parse_price_tl
from src.models import PriceOffer
from src.scrapers.base import BaseScraper
from src.scrapers.structured import extract_json_ld_products

logger = logging.getLogger(__name__)


class CimriScraper(BaseScraper):
    name = "cimri"
    BASE_URL = "https://www.cimri.com"
    SEARCH_URL = "https://www.cimri.com/arama?q=samsung+galaxy+s25+ultra"
    DDG_FALLBACK_URL = "https://html.duckduckgo.com/html/?q=site:cimri.com+%22Samsung+Galaxy+S25+Ultra%22"

    def scrape(self) -> list[PriceOffer]:
        offers: list[PriceOffer] = []
        try:
            html = self.fetcher.get_text(self.SEARCH_URL)
            soup = BeautifulSoup(html, "lxml")

            # 1) JSON-LD ürünleri
            for item in extract_json_ld_products(html):
                off = self.make_offer(
                    title=item["title"],
                    price_tl=item["price_tl"],
                    url=item["url"],
                    seller=item["seller"] or "Cimri",
                    base_url=self.BASE_URL,
                    in_stock=item["in_stock"],
                )
                if off:
                    offers.append(off)

            # 2) __NEXT_DATA__ JSON state
            next_script = soup.find("script", id="__NEXT_DATA__")
            if next_script and next_script.string:
                try:
                    next_data = json.loads(next_script.string)
                    self._walk_next_data(next_data, offers)
                except json.JSONDecodeError:
                    pass

            # 3) HTML kart fallback
            for card in soup.select("article, div[class*='ProductCard'], div[class*='product-card']"):
                title_el = card.select_one("h3, h2, a[title], [class*='title']")
                price_el = card.select_one("[class*='price'], [class*='Price']")
                link_el = card.select_one("a[href]")
                if not title_el or not price_el or not link_el:
                    continue

                title = title_el.get("title") or title_el.get_text(" ", strip=True)
                price_val = parse_price_tl(price_el.get_text(" ", strip=True))
                off = self.make_offer(
                    title=title,
                    price_tl=price_val,
                    url=link_el.get("href", ""),
                    seller="Cimri",
                    base_url=self.BASE_URL,
                )
                if off:
                    offers.append(off)
        except Exception as exc:
            logger.debug("Cimri doğrudan erişim403/hata, arama indeksi yedeği devreye giriyor: %s", exc)

        if not offers:
            offers.extend(self._scrape_via_search_index())

        return offers

    def _scrape_via_search_index(self) -> list[PriceOffer]:
        """Cimri Cloudflare 403 engeline takıldığında arama motoru önbellek/indeksinden güncel fiyatları çeker."""
        found: list[PriceOffer] = []
        try:
            html = self.fetcher.get_text(self.DDG_FALLBACK_URL)
            soup = BeautifulSoup(html, "lxml")
            for res in soup.select("div.result, div.links_main"):
                a_el = res.select_one("a.result__a")
                snip_el = res.select_one(".result__snippet")
                if not a_el:
                    continue
                raw_href = str(a_el.get("href") or "")
                if "uddg=" in raw_href:
                    uddg = parse_qs(urlparse(raw_href).query).get("uddg")
                    if uddg:
                        raw_href = unquote(uddg[0])
                if "cimri.com" not in raw_href:
                    continue
                combined_txt = f"{a_el.get_text(' ', strip=True)} {snip_el.get_text(' ', strip=True) if snip_el else ''}"
                for m in re.finditer(r"(\d{2,3}\.\d{3}(?:,\d{1,2})?)\s*TL", combined_txt, re.I):
                    p_val = parse_price_tl(m.group(1))
                    off = self.make_offer(
                        title=combined_txt[:160],
                        price_tl=p_val,
                        url=raw_href,
                        seller="Cimri",
                        base_url=self.BASE_URL,
                    )
                    if off:
                        found.append(off)
                        break
        except Exception as exc:
            logger.debug("Cimri indeks yedeği hatası: %s", exc)
        return found

    def _walk_next_data(self, node: object, out: list[PriceOffer]) -> None:
        if isinstance(node, list):
            for item in node:
                self._walk_next_data(item, out)
            return
        if not isinstance(node, dict):
            return

        # Cimri ürün veya teklif objesi kalıpları
        title = node.get("title") or node.get("name") or node.get("productTitle")
        price = node.get("price") or node.get("minPrice") or node.get("salePrice")
        url = node.get("url") or node.get("slug") or node.get("href")
        merchant = (
            node.get("merchantName")
            or node.get("sellerName")
            or (node.get("merchant") or {}).get("name")
            if isinstance(node.get("merchant"), dict)
            else ""
        )

        if isinstance(title, str) and price is not None and isinstance(url, str):
            off = self.make_offer(
                title=title,
                price_tl=parse_price_tl(price),
                url=url,
                seller=str(merchant or "Cimri"),
                base_url=self.BASE_URL,
            )
            if off:
                out.append(off)

        for v in node.values():
            if isinstance(v, (dict, list)):
                self._walk_next_data(v, out)

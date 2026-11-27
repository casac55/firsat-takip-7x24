from __future__ import annotations

import json
import logging
from bs4 import BeautifulSoup

from src.filter import parse_price_tl
from src.models import PriceOffer
from src.scrapers.base import BaseScraper
from src.scrapers.structured import extract_json_ld_products

logger = logging.getLogger(__name__)


class _GenericRetailerScraper(BaseScraper):
    """JSON-LD + CSS seçici tabanlı genel mağaza scraper'ı."""

    base_url: str = ""
    search_url: str = ""
    default_seller: str = ""
    card_selector: str = ""
    title_selector: str = ""
    price_selector: str = ""
    link_selector: str = "a[href]"

    def scrape(self) -> list[PriceOffer]:
        html = self.fetcher.get_text(self.search_url)
        offers: list[PriceOffer] = []

        for item in extract_json_ld_products(html):
            off = self.make_offer(
                title=item["title"],
                price_tl=item["price_tl"],
                url=item["url"],
                seller=item["seller"] or self.default_seller,
                base_url=self.base_url,
                in_stock=item["in_stock"],
            )
            if off:
                offers.append(off)

        if self.card_selector:
            soup = BeautifulSoup(html, "lxml")
            for card in soup.select(self.card_selector):
                title_el = card.select_one(self.title_selector) if self.title_selector else None
                price_el = card.select_one(self.price_selector) if self.price_selector else None
                link_el = card.select_one(self.link_selector) if self.link_selector else None
                if not title_el or not price_el:
                    continue

                title = title_el.get("title") or title_el.get_text(" ", strip=True)
                price_val = parse_price_tl(price_el.get_text(" ", strip=True))
                href = link_el.get("href", self.search_url) if link_el else self.search_url

                off = self.make_offer(
                    title=title,
                    price_tl=price_val,
                    url=href,
                    seller=self.default_seller,
                    base_url=self.base_url,
                )
                if off:
                    offers.append(off)

        return offers


class AmazonTrScraper(_GenericRetailerScraper):
    name = "amazon_tr"
    base_url = "https://www.amazon.com.tr"
    search_url = "https://www.amazon.com.tr/s?k=samsung+galaxy+s25+ultra"
    default_seller = "Amazon.com.tr"
    card_selector = "div[data-component-type='s-search-result']"
    title_selector = "h2 a span, h2 span"
    price_selector = "span.a-price span.a-offscreen"
    link_selector = "h2 a[href], a.a-link-normal[href]"


class HepsiburadaScraper(_GenericRetailerScraper):
    name = "hepsiburada"
    base_url = "https://www.hepsiburada.com"
    search_url = "https://www.hepsiburada.com/ara?q=samsung+galaxy+s25+ultra"
    default_seller = "Hepsiburada"
    card_selector = "li[class*='productListContent'], div[data-test-id='product-card'], article"
    title_selector = "h3[data-test-id='product-card-name'], h3, h2, a[title]"
    price_selector = "div[data-test-id='price-current-price'], [class*='price']"
    link_selector = "a[href]"

    def scrape(self) -> list[PriceOffer]:
        import re

        html = self.fetcher.get_text(self.search_url)
        offers: list[PriceOffer] = []

        for item in extract_json_ld_products(html):
            off = self.make_offer(
                title=item["title"],
                price_tl=item["price_tl"],
                url=item["url"],
                seller=item["seller"] or self.default_seller,
                base_url=self.base_url,
                in_stock=item["in_stock"],
            )
            if off:
                offers.append(off)

        soup = BeautifulSoup(html, "lxml")
        for card in soup.select(self.card_selector):
            links = [
                a for a in card.select("a[href]")
                if len(a.get_text(" ", strip=True)) > 15 or a.get("title")
            ]
            link_el = links[0] if links else card.select_one("a[href]")
            if not link_el:
                continue
            title_el = card.select_one(self.title_selector)
            title = (
                (title_el.get("title") or title_el.get_text(" ", strip=True))
                if title_el
                else (link_el.get("title") or link_el.get_text(" ", strip=True))
            )
            card_txt = card.get_text(" ", strip=True)
            # Taksit tutarlarını ("3 x 24.666 TL") hariç tutup gerçek satış fiyatlarını al
            cleaned_txt = re.sub(r"\d+\s*x\s*\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?\s*TL", "", card_txt, flags=re.I)
            price_matches = re.findall(r"(\d{2,3}\.\d{3}(?:,\d{1,2})?)\s*TL", cleaned_txt, re.I)
            if not price_matches:
                continue
            # Kartın sonundaki fiyat indirimli/güncel satış fiyatıdır
            price_val = parse_price_tl(price_matches[-1])
            href = str(link_el.get("href") or self.search_url)
            off = self.make_offer(
                title=title,
                price_tl=price_val,
                url=href,
                seller=self.default_seller,
                base_url=self.base_url,
                extra_text=card_txt,
            )
            if off:
                offers.append(off)
        return offers


class TrendyolScraper(BaseScraper):
    name = "trendyol"
    BASE_URL = "https://www.trendyol.com"
    API_URL = (
        "https://apigw.trendyol.com/discovery-web-searchgw-service/v2/api/infinite-scroll/sr"
        "?q=samsung+galaxy+s25+ultra&qt=samsung+galaxy+s25+ultra&st=samsung+galaxy+s25+ultra"
        "&os=1&pi=1&culture=tr-TR&userGenderId=1&pId=0&scoringAlgorithmId=2&categoryRelevancyEnabled=false"
    )
    WEB_URL = "https://www.trendyol.com/sr?q=samsung+galaxy+s25+ultra"

    def scrape(self) -> list[PriceOffer]:
        offers: list[PriceOffer] = []
        # Önce public arama API'sini dene
        try:
            data = self.fetcher.get_json(self.API_URL)
            products = (
                data.get("result", {}).get("products", [])
                if isinstance(data, dict)
                else []
            )
            for prod in products:
                if not isinstance(prod, dict):
                    continue
                title = str(prod.get("name", "")).strip()
                brand = str((prod.get("brand") or {}).get("name", "")).strip()
                full_title = f"{brand} {title}".strip() if brand and brand.lower() not in title.lower() else title
                price_obj = prod.get("price") or {}
                price_val = parse_price_tl(
                    price_obj.get("discountedPrice")
                    or price_obj.get("sellingPrice")
                    or price_obj.get("originalPrice")
                )
                url = str(prod.get("url", "")).strip()
                merchant = str((prod.get("merchant") or {}).get("name", "")).strip()
                seller = f"Trendyol ({merchant})" if merchant and merchant.lower() != "trendyol" else "Trendyol"

                off = self.make_offer(
                    title=full_title,
                    price_tl=price_val,
                    url=url,
                    seller=seller,
                    base_url=self.BASE_URL,
                )
                if off:
                    offers.append(off)
            if offers:
                return offers
        except Exception as exc:
            logger.debug("Trendyol API başarısız, HTML fallback deneniyor: %s", exc)

        # HTML fallback
        html = self.fetcher.get_text(self.WEB_URL)
        for item in extract_json_ld_products(html):
            off = self.make_offer(
                title=item["title"],
                price_tl=item["price_tl"],
                url=item["url"],
                seller=item["seller"] or "Trendyol",
                base_url=self.BASE_URL,
                in_stock=item["in_stock"],
            )
            if off:
                offers.append(off)

        soup = BeautifulSoup(html, "lxml")
        for card in soup.select("div.p-card-wrppr, a.product-card"):
            if card.name == "a" and "product-card" in (card.get("class") or []):
                card_text = card.get_text(" ", strip=True)
                href = card.get("href", "")
                off = self.make_offer(
                    title=card_text,
                    price_tl=_extract_last_tl_price(card_text),
                    url=href,
                    seller="Trendyol",
                    base_url=self.BASE_URL,
                    extra_text=card_text,
                )
                if off:
                    offers.append(off)
                continue

            title_el = card.select_one("span.prdct-desc-cntnr-name, h3")
            price_el = card.select_one("div.prc-box-dscntd, div.price-item")
            link_el = card.select_one("a[href]")
            if not title_el or not price_el or not link_el:
                continue
            off = self.make_offer(
                title=title_el.get("title") or title_el.get_text(" ", strip=True),
                price_tl=parse_price_tl(price_el.get_text(" ", strip=True)),
                url=link_el.get("href", ""),
                seller="Trendyol",
                base_url=self.BASE_URL,
            )
            if off:
                offers.append(off)
        return offers


def _extract_last_tl_price(text: str) -> float | None:
    import re

    matches = re.findall(r"(\d{2,3}\.\d{3}(?:,\d{1,2})?)\s*TL", text, re.IGNORECASE)
    if not matches:
        return None
    return parse_price_tl(matches[-1])


class VatanScraper(_GenericRetailerScraper):
    name = "vatan"
    base_url = "https://www.vatanbilgisayar.com"
    search_url = "https://www.vatanbilgisayar.com/arama/samsung%20galaxy%20s25%20ultra/"
    default_seller = "Vatan Bilgisayar"
    card_selector = "div.product-list"
    title_selector = "div.product-list__product-name h3, .product-list__product-name"
    price_selector = "span.product-list__price"
    link_selector = "a.product-list__link, a[href]"
    DIRECT_PRODUCT_URLS = (
        "https://www.vatanbilgisayar.com/samsung-galaxy-s25-ultra-12-256-gb-akilli-telefon-titanyum-gri-149753.html",
    )

    def scrape(self) -> list[PriceOffer]:
        import re

        offers = super().scrape()
        if offers:
            return offers

        # Vatan arama sayfası sonuç döndürmezse doğrudan ürün sayfasından güncel fiyatı çek
        for purl in self.DIRECT_PRODUCT_URLS:
            try:
                html = self.fetcher.get_text(purl)
                soup = BeautifulSoup(html, "lxml")
                h1 = soup.select_one("h1")
                title = h1.get_text(" ", strip=True) if h1 else "Samsung Galaxy S25 Ultra 256 GB"
                m_p = re.search(r'data-price="(\d{2,3}\.\d{3}(?:,\d{1,2})?)"', html) or re.search(
                    r'"Price"\s*:\s*"(\d{5,6})"', html
                )
                price_val = parse_price_tl(m_p.group(1)) if m_p else None
                if not price_val:
                    candidates = [
                        parse_price_tl(e.get_text(" ", strip=True))
                        for e in soup.select(".product-list__price")
                    ]
                    valid_c = [c for c in candidates if c and c >= 45000]
                    price_val = valid_c[0] if valid_c else None
                off = self.make_offer(
                    title=title,
                    price_tl=price_val,
                    url=purl,
                    seller=self.default_seller,
                    base_url=self.base_url,
                )
                if off:
                    offers.append(off)
            except Exception as exc:
                logger.debug("Vatan doğrudan ürün sayfası okunamadı (%s): %s", purl, exc)
        return offers


class TeknosaScraper(_GenericRetailerScraper):
    name = "teknosa"
    base_url = "https://www.teknosa.com"
    search_url = "https://www.teknosa.com/arama/?s=samsung+galaxy+s25+ultra"
    default_seller = "Teknosa"
    card_selector = "div.prd, div[data-product-name]"
    title_selector = "h3.prd-title, a.prd-link"
    price_selector = "span.prc-last, span.prc"
    link_selector = "a.prd-link, a[href]"


class MediaMarktScraper(_GenericRetailerScraper):
    name = "mediamarkt"
    base_url = "https://www.mediamarkt.com.tr"
    search_url = "https://www.mediamarkt.com.tr/tr/search.html?query=samsung+galaxy+s25+ultra"
    default_seller = "MediaMarkt"
    card_selector = "div[data-test='mms-product-card'], article[data-test='mms-product-card']"
    title_selector = "p[data-test='product-title'], h2, h3"
    price_selector = "div[data-test='mms-price'], span[data-test='branded-price']"
    link_selector = "a[href]"


class SamsungTrScraper(BaseScraper):
    name = "samsung_tr"
    BASE_URL = "https://www.samsung.com"
    URLS = [
        "https://www.samsung.com/tr/smartphones/galaxy-s25-ultra/buy/",
        "https://www.samsung.com/tr/search/?searchvalue=galaxy+s25+ultra",
    ]

    def scrape(self) -> list[PriceOffer]:
        offers: list[PriceOffer] = []
        for url in self.URLS:
            try:
                html = self.fetcher.get_text(url)
            except Exception as exc:
                logger.debug("Samsung TR alınamadı (%s): %s", url, exc)
                continue

            for item in extract_json_ld_products(html):
                off = self.make_offer(
                    title=item["title"],
                    price_tl=item["price_tl"],
                    url=item["url"] or url,
                    seller="Samsung Türkiye",
                    base_url=self.BASE_URL,
                    in_stock=item["in_stock"],
                )
                if off:
                    offers.append(off)

            soup = BeautifulSoup(html, "lxml")
            for script in soup.find_all("script"):
                txt = script.string or ""
                if "S25 Ultra" in txt and ("price" in txt.lower() or "fiyat" in txt.lower()):
                    try:
                        # Gömülü JSON varsa taramaya çalış
                        start = txt.find("{")
                        end = txt.rfind("}")
                        if start != -1 and end > start:
                            data = json.loads(txt[start : end + 1])
                            self._scan_json(data, url, offers)
                    except Exception:
                        pass
        return offers

    def _scan_json(self, node: object, default_url: str, out: list[PriceOffer]) -> None:
        if isinstance(node, list):
            for el in node:
                self._scan_json(el, default_url, out)
            return
        if not isinstance(node, dict):
            return
        name = node.get("modelDisplayName") or node.get("displayName") or node.get("name")
        price = node.get("price") or node.get("promotionPrice") or node.get("salesPrice")
        url = node.get("pdpUrl") or node.get("url") or default_url
        if isinstance(name, str) and price is not None:
            off = self.make_offer(
                title=name,
                price_tl=parse_price_tl(price),
                url=str(url),
                seller="Samsung Türkiye",
                base_url=self.BASE_URL,
            )
            if off:
                out.append(off)
        for v in node.values():
            if isinstance(v, (dict, list)):
                self._scan_json(v, default_url, out)


class N11Scraper(_GenericRetailerScraper):
    name = "n11"
    base_url = "https://www.n11.com"
    search_url = "https://www.n11.com/arama?q=samsung+galaxy+s25+ultra"
    default_seller = "N11"
    card_selector = "li.column, div.pro"
    title_selector = "h3.productName, a.plink"
    price_selector = "span.newPrice ins, div.priceContainer ins"
    link_selector = "a.plink, a[href]"


class PazaramaScraper(_GenericRetailerScraper):
    name = "pazarama"
    base_url = "https://www.pazarama.com"
    search_url = "https://www.pazarama.com/arama?q=samsung+galaxy+s25+ultra"
    default_seller = "Pazarama"
    card_selector = "div.product-card, div[class*='product-card']"
    title_selector = "div.line-clamp-2, h2, h3"
    price_selector = "div.leading-tight, div[class*='price']"
    link_selector = "a[href]"

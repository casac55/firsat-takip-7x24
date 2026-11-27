from __future__ import annotations

from src.config import AppConfig
from src.fetchers import HttpFetcher
from src.scrapers.akakce import AkakceScraper
from src.scrapers.base import BaseScraper
from src.scrapers.cimri import CimriScraper
from src.scrapers.direct_stores import (
    AmazonTrScraper,
    HepsiburadaScraper,
    MediaMarktScraper,
    N11Scraper,
    PazaramaScraper,
    SamsungTrScraper,
    TeknosaScraper,
    TrendyolScraper,
    VatanScraper,
)
from src.scrapers.epey import EpeyScraper


SCRAPER_CLASSES: dict[str, type[BaseScraper]] = {
    "epey": EpeyScraper,
    "akakce": AkakceScraper,
    "cimri": CimriScraper,
    "amazon_tr": AmazonTrScraper,
    "hepsiburada": HepsiburadaScraper,
    "trendyol": TrendyolScraper,
    "vatan": VatanScraper,
    "teknosa": TeknosaScraper,
    "mediamarkt": MediaMarktScraper,
    "samsung_tr": SamsungTrScraper,
    "n11": N11Scraper,
    "pazarama": PazaramaScraper,
}


def build_enabled_scrapers(fetcher: HttpFetcher, config: AppConfig) -> list[BaseScraper]:
    scrapers: list[BaseScraper] = []
    for name, cls in SCRAPER_CLASSES.items():
        if config.sources.get(name, True):
            scrapers.append(cls(fetcher=fetcher, config=config))
    return scrapers

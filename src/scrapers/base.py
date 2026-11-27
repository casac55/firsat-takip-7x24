from __future__ import annotations

from abc import ABC, abstractmethod
from urllib.parse import urljoin

from src.config import AppConfig
from src.fetchers import HttpFetcher
from src.filter import (
    detect_variant,
    extract_color,
    extract_coupon_info,
    extract_trade_in_info,
    is_official_or_trusted_seller,
    is_valid_s25_ultra_phone,
    normalize_seller_name,
)
from src.models import PriceOffer


class BaseScraper(ABC):
    name: str = "base"

    def __init__(self, fetcher: HttpFetcher, config: AppConfig) -> None:
        self.fetcher = fetcher
        self.config = config

    @abstractmethod
    def scrape(self) -> list[PriceOffer]:
        """Hedef kaynaktan doğrulanmış S25 Ultra tekliflerini toplar."""

    def make_offer(
        self,
        *,
        title: str,
        price_tl: float | None,
        url: str,
        seller: str = "",
        variant: str | None = None,
        base_url: str = "",
        in_stock: bool = True,
        extra_text: str = "",
    ) -> PriceOffer | None:
        if not is_valid_s25_ultra_phone(
            title=title,
            price_tl=price_tl,
            min_price_tl=self.config.min_valid_price_tl,
            max_price_tl=self.config.max_valid_price_tl,
            exclude_refurbished=self.config.exclude_refurbished,
        ):
            return None

        resolved_variant = detect_variant(title, default=variant)
        if not resolved_variant or resolved_variant not in self.config.variants:
            return None

        full_url = urljoin(base_url, url) if base_url and url else url
        if not full_url:
            return None

        clean_seller = normalize_seller_name(seller, self.name)
        discount_tl, coupon_info, coupon_code = extract_coupon_info(
            f"{title} {extra_text}", float(price_tl)  # type: ignore[arg-type]
        )
        return PriceOffer(
            source=self.name,
            seller=clean_seller,
            variant=resolved_variant,
            title=" ".join(title.split()),
            price_tl=float(price_tl),  # type: ignore[arg-type]
            url=full_url,
            in_stock=in_stock,
            color=extract_color(title),
            is_official_seller=is_official_or_trusted_seller(clean_seller, full_url),
            coupon_discount_tl=discount_tl,
            coupon_info=coupon_info,
            coupon_code=coupon_code,
            trade_in_info=extract_trade_in_info(f"{title} {extra_text}"),
        )

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class AlertType(str, Enum):
    TARGET_PRICE = "TARGET_PRICE"
    ALL_TIME_LOW = "ALL_TIME_LOW"
    SIX_MONTH_LOW = "SIX_MONTH_LOW"
    COUPON_SIX_MONTH_LOW = "COUPON_SIX_MONTH_LOW"
    PRICE_DROP = "PRICE_DROP"
    BELOW_MARKET = "BELOW_MARKET"
    BACK_IN_STOCK = "BACK_IN_STOCK"


@dataclass(slots=True)
class PriceOffer:
    """Tek bir mağaza/kaynaktan gelen doğrulanmış S25 Ultra fiyat teklifi."""

    source: str          # Scraping yapılan kaynak (örn. epey, akakce, amazon_tr)
    seller: str          # Gerçek satıcı / mağaza (örn. Amazon.com.tr, Hepsiburada, MediaMarkt)
    variant: str         # "256 GB", "512 GB", "1 TB"
    title: str
    price_tl: float
    url: str
    in_stock: bool = True
    color: str = ""
    is_official_seller: bool = False
    coupon_discount_tl: float = 0.0
    coupon_info: str = ""
    coupon_code: str = ""
    trade_in_info: str = ""
    scraped_at: datetime = field(default_factory=datetime.now)

    @property
    def effective_price_tl(self) -> float:
        """Kupon / sepet indirimi düşülmüş net fiyat."""
        if self.coupon_discount_tl > 0:
            return round(max(1.0, self.price_tl - self.coupon_discount_tl), 2)
        return self.price_tl

    @property
    def dedup_key(self) -> tuple[str, str]:
        return (self.variant, self.seller.strip().lower())


@dataclass(slots=True)
class DealAlert:
    """Tespit edilen fiyat fırsatı."""

    offer: PriceOffer
    alert_types: list[AlertType]
    previous_min_tl: Optional[float] = None
    market_median_tl: Optional[float] = None
    all_time_low_tl: Optional[float] = None
    six_month_low_tl: Optional[float] = None
    target_price_tl: Optional[float] = None
    drop_tl: Optional[float] = None
    drop_percent: Optional[float] = None
    below_market_percent: Optional[float] = None
    fake_discount_warning: str = ""

    @property
    def is_wakeup_worthy(self) -> bool:
        return any(
            t in (AlertType.SIX_MONTH_LOW, AlertType.COUPON_SIX_MONTH_LOW)
            for t in self.alert_types
        )


@dataclass(slots=True)
class ScrapeOutcome:
    """Tek bir scraper çalıştırma sonucu."""

    source: str
    offers: list[PriceOffer] = field(default_factory=list)
    error: Optional[str] = None
    elapsed_seconds: float = 0.0

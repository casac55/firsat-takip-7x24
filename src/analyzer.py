from __future__ import annotations

import statistics
from typing import Optional

from src.config import AppConfig
from src.models import AlertType, DealAlert, PriceOffer
from src.storage import PriceStorage


_AGGREGATOR_DOMAINS = ("akakce.com", "cimri.com", "epey.com")


def is_direct_store_url(url: str) -> bool:
    low = (url or "").lower()
    return bool(low) and not any(d in low for d in _AGGREGATOR_DOMAINS)


def deduplicate_offers(offers: list[PriceOffer]) -> list[PriceOffer]:
    """
    Aynı varyant + satıcı için en düşük efektif fiyatlı teklifi tutar.
    Fiyatlar eşit/yakınsa Akakçe/Cimri/Epey yerine DOĞRUDAN mağaza linkini tercih eder.
    """
    best: dict[tuple[str, str], PriceOffer] = {}
    for offer in offers:
        key = offer.dedup_key
        existing = best.get(key)
        if existing is None:
            best[key] = offer
            continue

        existing_direct = is_direct_store_url(existing.url)
        offer_direct = is_direct_store_url(offer.url)

        # Yeni teklif doğrudan mağaza linkiyse ve mevcut teklif karşılaştırma sitesiyse
        if offer_direct and not existing_direct and offer.effective_price_tl <= existing.effective_price_tl + 20.0:
            best[key] = offer
        elif not offer_direct and existing_direct and existing.effective_price_tl <= offer.effective_price_tl + 20.0:
            continue
        elif offer.effective_price_tl < existing.effective_price_tl:
            best[key] = offer

    return sorted(best.values(), key=lambda o: (o.variant, o.effective_price_tl))


def analyze_deals(
    offers: list[PriceOffer],
    config: AppConfig,
    storage: PriceStorage,
    current_run_id: int,
) -> list[DealAlert]:
    """
    Tüm fırsat kriterlerini değerlendirir (durdurulmuş varyantlar hariç):
    1) TARGET_PRICE: Hedef fiyatın altında mı?
    2) SIX_MONTH_LOW / COUPON_SIX_MONTH_LOW: Son 6 aylık (180 gün) en düşük fiyatın altında mı?
    3) ALL_TIME_LOW: Veritabanındaki tüm zamanların en düşüğü mü?
    4) PRICE_DROP: Bir önceki taramaya göre anlamlı düşüş var mı?
    5) BELOW_MARKET: Piyasa medyanının belirlenen % altında mı?
    """
    if not offers:
        return []

    paused = storage.get_paused_variants()
    if "ALL" in paused:
        return []
    only_official = "ONLY_OFFICIAL" in paused

    by_variant: dict[str, list[PriceOffer]] = {}
    for offer in offers:
        if offer.variant in paused:
            continue
        if only_official and not offer.is_official_seller:
            continue
        by_variant.setdefault(offer.variant, []).append(offer)

    alerts: list[DealAlert] = []

    for variant, var_offers in by_variant.items():
        var_offers.sort(key=lambda o: o.effective_price_tl)
        cheapest = var_offers[0]

        prev_min = storage.get_previous_run_min(variant, current_run_id)
        hist_before_prev = storage.get_historical_min_before_previous_run(
            variant, current_run_id, days=30
        )
        all_time_low = storage.get_all_time_low(variant, exclude_run_id=current_run_id)
        db_6m_low = storage.get_six_month_low(
            variant, days=config.six_month_days, exclude_run_id=current_run_id
        )
        cfg_6m = config.six_month_baseline_tl.get(variant)
        six_month_low: Optional[float] = None
        if db_6m_low is not None and cfg_6m is not None:
            six_month_low = min(db_6m_low, cfg_6m)
        else:
            six_month_low = db_6m_low if db_6m_low is not None else cfg_6m

        rolling_median = storage.get_rolling_median(variant, days=7, exclude_run_id=current_run_id)
        current_prices = [o.effective_price_tl for o in var_offers]
        current_median = statistics.median(current_prices) if len(current_prices) >= 3 else None
        market_median: Optional[float] = rolling_median or current_median

        target_price = config.target_prices_tl.get(variant)

        candidates = [cheapest]
        for o in var_offers[1:]:
            is_target = target_price is not None and o.effective_price_tl <= target_price
            is_6m = six_month_low is not None and o.effective_price_tl < six_month_low - 50.0
            if (is_target or is_6m) and len(candidates) < 3:
                candidates.append(o)

        for offer in candidates:
            eff_price = offer.effective_price_tl
            reasons: list[AlertType] = []
            drop_tl: Optional[float] = None
            drop_pct: Optional[float] = None
            below_pct: Optional[float] = None
            fake_warning = ""

            if target_price is not None and eff_price <= target_price:
                reasons.append(AlertType.TARGET_PRICE)

            if six_month_low is not None and eff_price < six_month_low - 50.0:
                if offer.coupon_discount_tl > 0 and offer.price_tl >= six_month_low - 50.0:
                    reasons.append(AlertType.COUPON_SIX_MONTH_LOW)
                else:
                    reasons.append(AlertType.SIX_MONTH_LOW)

            if all_time_low is not None and eff_price < all_time_low - 50.0:
                reasons.append(AlertType.ALL_TIME_LOW)

            if prev_min is not None and eff_price < prev_min:
                diff = round(prev_min - eff_price, 2)
                pct = round((diff / prev_min) * 100.0, 2)
                if diff >= config.min_drop_tl or pct >= config.min_drop_percent:
                    reasons.append(AlertType.PRICE_DROP)
                    drop_tl = diff
                    drop_pct = pct
                    # Sahte indirim kontrolü: önceki taramada fiyat şişirilip tekrar eski normal seviyeye mi çekilmiş?
                    if (
                        hist_before_prev is not None
                        and prev_min >= hist_before_prev + 1000.0
                        and eff_price >= hist_before_prev - 100.0
                    ):
                        p_fmt = f"{int(round(prev_min)):,}".replace(",", ".")
                        h_fmt = f"{int(round(hist_before_prev)):,}".replace(",", ".")
                        fake_warning = (
                            f"Fiyat önce {p_fmt} TL'ye yükseltilip indirilmiş "
                            f"(Önceki normal dip: {h_fmt} TL)"
                        )

            if market_median is not None and market_median > 0 and eff_price < market_median:
                pct_below = round(((market_median - eff_price) / market_median) * 100.0, 2)
                if pct_below >= config.below_market_percent:
                    reasons.append(AlertType.BELOW_MARKET)
                    below_pct = pct_below

            if not reasons:
                continue

            if storage.was_recently_alerted(
                offer.variant, offer.seller, eff_price, config.cooldown_hours
            ):
                continue

            alerts.append(
                DealAlert(
                    offer=offer,
                    alert_types=reasons,
                    previous_min_tl=prev_min,
                    market_median_tl=market_median,
                    all_time_low_tl=all_time_low,
                    six_month_low_tl=six_month_low,
                    target_price_tl=target_price,
                    drop_tl=drop_tl,
                    drop_percent=drop_pct,
                    below_market_percent=below_pct,
                    fake_discount_warning=fake_warning,
                )
            )

    return alerts

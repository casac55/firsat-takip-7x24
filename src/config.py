from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass
class AppConfig:
    base_dir: Path
    config_path: Path
    product_name: str
    variants: list[str]
    min_valid_price_tl: float
    max_valid_price_tl: float
    exclude_refurbished: bool

    interval_minutes: int
    turbo_interval_minutes: int
    turbo_windows: list[str]
    daily_summary_times: list[str]
    notify_every_check: bool

    target_prices_tl: dict[str, float]
    min_drop_tl: float
    min_drop_percent: float
    below_market_percent: float
    six_month_days: int
    six_month_baseline_tl: dict[str, float]
    wakeup_enabled: bool
    wakeup_window: str
    wakeup_repeat_count: int
    wakeup_repeat_interval_seconds: int
    wakeup_pc_beep: bool
    wakeup_auto_open_browser: bool
    cooldown_hours: int

    dashboard_enabled: bool
    dashboard_host: str
    dashboard_port: int

    timeout_seconds: int
    max_workers: int
    use_curl_cffi: bool
    use_playwright_fallback: bool
    sources: dict[str, bool]

    enable_bot_commands: bool
    top_offers_per_variant: int

    database_path: Path

    daemon_paused: bool = False
    dashboard_password: str = ""

    channel_enabled: bool = True
    telegram_channel_id: str = "@firsat_takibi"
    telegram_channel_url: str = "https://t.me/firsat_takibi"
    auto_publish_s25_deals: bool = True
    auto_publish_popular_deals: bool = True
    auto_publish_community_deals: bool = True
    auto_publish_bestseller_drops: bool = True
    only_photo_deals: bool = True
    prevent_duplicate_deals: bool = True
    duplicate_repost_discount_pct: float = 5.0
    max_popular_posts_per_scan: int = 4
    min_popular_discount_percent: int = 25
    min_popular_savings_tl: float = 100.0
    always_show_ad_disclosure: bool = True
    show_disclosure_when_affiliate_active: bool = True

    amazon_associate_tag: str = ""
    hepsiburada_affiliate_tag: str = ""
    trendyol_affiliate_tag: str = ""
    admitad_deeplink_base: str = ""
    affiliate_utm_source: str = "telegram"
    affiliate_utm_medium: str = "firsat_takibi"

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    callmebot_telegram_user: str = ""
    x_api_key: str = ""
    x_api_secret: str = ""
    x_access_token: str = ""
    x_access_token_secret: str = ""
    x_client_id: str = ""
    x_client_secret: str = ""
    x_oauth2_access_token: str = ""
    x_oauth2_refresh_token: str = ""
    x_auto_publish_deals: bool = True
    x_max_tweets_per_day: int = 10
    x_min_discount_percent: int = 10
    x_min_interval_minutes: int = 45
    bestseller_tracker_enabled: bool = True
    bestseller_compare_days: int = 30
    bestseller_min_drop_percent: float = 4.0
    bestseller_min_drop_tl: float = 50.0
    depo_enabled: bool = True
    depo_min_discount_percent: int = 35
    depo_super_discount_percent: int = 75
    depo_min_price_tl: float = 100.0
    depo_max_price_tl: float = 150000.0
    depo_auto_notify_admin: bool = True
    depo_auto_publish_channel: bool = True
    depo_categories: list[str] = field(default_factory=lambda: ["tumu", "elektronik", "bilgisayar", "ev_mutfak"])
    depo_keywords: list[str] = field(default_factory=list)
    raw_yaml: dict[str, Any] = field(default_factory=dict)

    @property
    def has_any_affiliate_tag(self) -> bool:
        return bool(
            self.amazon_associate_tag
            or self.hepsiburada_affiliate_tag
            or self.trendyol_affiliate_tag
            or self.admitad_deeplink_base
        )

    def save_target_price(self, variant: str, new_price_tl: float) -> None:
        """Hedef fiyatı config.yaml içine kalıcı olarak kaydeder."""
        self.target_prices_tl[variant] = float(new_price_tl)
        alerts_sec = self.raw_yaml.setdefault("alerts", {})
        targets_sec = alerts_sec.setdefault("target_prices_tl", {})
        targets_sec[variant] = int(new_price_tl) if new_price_tl.is_integer() else float(new_price_tl)
        self.persist_yaml()

    def persist_yaml(self) -> None:
        with open(self.config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(self.raw_yaml, f, allow_unicode=True, sort_keys=False)

    def save_env_vars(self, updates: dict[str, str]) -> None:
        """Verilen anahtarları .env dosyasına kalıcı olarak yazar ve os.environ içine yansıtır."""
        import re

        env_path = self.base_dir / ".env"
        content = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
        for k, v in updates.items():
            os.environ[k] = v
            pat = re.compile(rf"^{re.escape(k)}=.*$", re.MULTILINE)
            if pat.search(content):
                content = pat.sub(lambda _, key=k, val=v: f"{key}={val}", content)
            else:
                content = content.rstrip() + f"\n{k}={v}\n"
        env_path.write_text(content.lstrip("\n"), encoding="utf-8")


def load_config(config_path: str | Path | None = None) -> AppConfig:
    env_path = BASE_DIR / ".env"
    if env_path.exists():
        load_dotenv(env_path)
    else:
        load_dotenv()

    resolved_config = Path(config_path) if config_path else (BASE_DIR / "config.yaml")
    with open(resolved_config, "r", encoding="utf-8") as f:
        data: dict[str, Any] = yaml.safe_load(f) or {}

    product = data.get("product", {})
    schedule = data.get("schedule", {})
    alerts = data.get("alerts", {})
    wakeup = alerts.get("wakeup_alarm", {})
    scraping = data.get("scraping", {})
    telegram = data.get("telegram", {})
    database = data.get("database", {})

    db_rel = Path(database.get("path", "data/prices.db"))
    db_path = db_rel if db_rel.is_absolute() else (BASE_DIR / db_rel)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    raw_targets = alerts.get("target_prices_tl", {})
    targets = {str(k): float(v) for k, v in raw_targets.items()}

    raw_baselines = alerts.get("six_month_baseline_tl", {})
    baselines = {str(k): float(v) for k, v in raw_baselines.items() if float(v or 0) > 0}

    return AppConfig(
        base_dir=BASE_DIR,
        config_path=resolved_config,
        product_name=str(product.get("name", "Samsung Galaxy S25 Ultra")),
        variants=[str(v) for v in product.get("variants", ["256 GB", "512 GB", "1 TB"])],
        min_valid_price_tl=float(product.get("min_valid_price_tl", 45000)),
        max_valid_price_tl=float(product.get("max_valid_price_tl", 160000)),
        exclude_refurbished=bool(product.get("exclude_refurbished", True)),
        interval_minutes=max(1, int(schedule.get("interval_minutes", 1))),
        turbo_interval_minutes=max(1, int(schedule.get("turbo_interval_minutes", 1))),
        turbo_windows=[str(w) for w in schedule.get("turbo_windows", ["09:00-10:30", "12:00-14:00", "17:30-22:30", "23:45-01:30"])],
        daily_summary_times=[str(t) for t in schedule.get("daily_summary_times", ["09:00", "18:00"])],
        notify_every_check=bool(schedule.get("notify_every_check", False)),
        target_prices_tl=targets,
        min_drop_tl=float(alerts.get("min_drop_tl", 500)),
        min_drop_percent=float(alerts.get("min_drop_percent", 1.5)),
        below_market_percent=float(alerts.get("below_market_percent", 5.0)),
        six_month_days=int(alerts.get("six_month_days", 180)),
        six_month_baseline_tl=baselines,
        wakeup_enabled=bool(wakeup.get("enabled", True)),
        wakeup_window=str(wakeup.get("active_window", "00:00-23:59")),
        wakeup_repeat_count=max(1, int(wakeup.get("repeat_count", 1))),
        wakeup_repeat_interval_seconds=max(5, int(wakeup.get("repeat_interval_seconds", 12))),
        wakeup_pc_beep=bool(wakeup.get("pc_beep", True)),
        wakeup_auto_open_browser=bool(wakeup.get("auto_open_browser", True)),
        cooldown_hours=int(alerts.get("cooldown_hours", 12)),
        dashboard_enabled=bool((data.get("dashboard") or {}).get("enabled", True)),
        dashboard_host=os.getenv(
            "DASHBOARD_HOST",
            str((data.get("dashboard") or {}).get("host", "127.0.0.1")),
        ).strip(),
        dashboard_port=int(
            os.getenv("DASHBOARD_PORT", str((data.get("dashboard") or {}).get("port", 8050)))
        ),
        daemon_paused=bool(schedule.get("daemon_paused", False)),
        dashboard_password=os.getenv(
            "DASHBOARD_PASSWORD",
            str((data.get("dashboard") or {}).get("password", "")),
        ).strip(),
        channel_enabled=bool((data.get("channel") or {}).get("enabled", True)),
        telegram_channel_id=os.getenv(
            "TELEGRAM_CHANNEL_ID",
            str((data.get("channel") or {}).get("channel_id", "@firsat_takibi")),
        ).strip(),
        telegram_channel_url=str(
            (data.get("channel") or {}).get("channel_url", "https://t.me/firsat_takibi")
        ).strip(),
        auto_publish_s25_deals=bool((data.get("channel") or {}).get("auto_publish_s25_deals", True)),
        auto_publish_popular_deals=bool((data.get("channel") or {}).get("auto_publish_popular_deals", True)),
        auto_publish_community_deals=bool((data.get("channel") or {}).get("auto_publish_community_deals", True)),
        auto_publish_bestseller_drops=bool((data.get("channel") or {}).get("auto_publish_bestseller_drops", True)),
        only_photo_deals=bool((data.get("channel") or {}).get("only_photo_deals", True)),
        prevent_duplicate_deals=bool((data.get("channel") or {}).get("prevent_duplicate_deals", True)),
        duplicate_repost_discount_pct=max(
            1.0, float((data.get("channel") or {}).get("duplicate_repost_discount_pct", 5.0))
        ),
        max_popular_posts_per_scan=max(1, int((data.get("channel") or {}).get("max_popular_posts_per_scan", 4))),
        min_popular_discount_percent=max(1, int((data.get("channel") or {}).get("min_discount_percent", 25))),
        min_popular_savings_tl=max(0.0, float((data.get("channel") or {}).get("min_savings_tl", 100.0))),
        always_show_ad_disclosure=bool((data.get("channel") or {}).get("always_show_ad_disclosure", True)),
        show_disclosure_when_affiliate_active=bool((data.get("channel") or {}).get("show_disclosure_when_affiliate_active", True)),
        amazon_associate_tag=os.getenv("AMAZON_ASSOCIATE_TAG", "").strip(),
        hepsiburada_affiliate_tag=os.getenv("HEPSIBURADA_AFFILIATE_TAG", "").strip(),
        trendyol_affiliate_tag=os.getenv("TRENDYOL_AFFILIATE_TAG", "").strip(),
        admitad_deeplink_base=os.getenv("ADMITAD_DEEPLINK_BASE", "").strip(),
        affiliate_utm_source=str((data.get("affiliate") or {}).get("utm_source", "telegram")),
        affiliate_utm_medium=str((data.get("affiliate") or {}).get("utm_medium", "firsat_takibi")),
        timeout_seconds=int(scraping.get("timeout_seconds", 20)),
        max_workers=int(scraping.get("max_workers", 5)),
        use_curl_cffi=bool(scraping.get("use_curl_cffi", True)),
        use_playwright_fallback=bool(scraping.get("use_playwright_fallback", False)),
        sources={str(k): bool(v) for k, v in scraping.get("sources", {}).items()},
        enable_bot_commands=bool(telegram.get("enable_bot_commands", True)),
        top_offers_per_variant=int(telegram.get("top_offers_per_variant", 5)),
        database_path=db_path,
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        callmebot_telegram_user=os.getenv("CALLMEBOT_TELEGRAM_USER", "").strip(),
        x_api_key=os.getenv("X_API_KEY", "").strip(),
        x_api_secret=os.getenv("X_API_SECRET", "").strip(),
        x_access_token=os.getenv("X_ACCESS_TOKEN", "").strip(),
        x_access_token_secret=os.getenv("X_ACCESS_TOKEN_SECRET", "").strip(),
        x_client_id=os.getenv("X_CLIENT_ID", "").strip(),
        x_client_secret=os.getenv("X_CLIENT_SECRET", "").strip(),
        x_oauth2_access_token=os.getenv("X_OAUTH2_ACCESS_TOKEN", "").strip(),
        x_oauth2_refresh_token=os.getenv("X_OAUTH2_REFRESH_TOKEN", "").strip(),
        x_auto_publish_deals=bool((data.get("x_twitter") or {}).get("auto_publish_deals", True)),
        x_max_tweets_per_day=max(1, int((data.get("x_twitter") or {}).get("max_tweets_per_day", 10))),
        x_min_discount_percent=max(1, int((data.get("x_twitter") or {}).get("min_discount_percent", 25))),
        x_min_interval_minutes=max(5, int((data.get("x_twitter") or {}).get("min_interval_minutes", 45))),
        bestseller_tracker_enabled=bool((data.get("bestseller_tracker") or {}).get("enabled", True)),
        bestseller_compare_days=max(7, int((data.get("bestseller_tracker") or {}).get("compare_window_days", 30))),
        bestseller_min_drop_percent=float((data.get("bestseller_tracker") or {}).get("min_drop_percent", 25.0)),
        bestseller_min_drop_tl=float((data.get("bestseller_tracker") or {}).get("min_drop_tl", 100.0)),
        depo_enabled=bool((data.get("amazon_depo") or {}).get("enabled", True)),
        depo_min_discount_percent=max(5, int((data.get("amazon_depo") or {}).get("min_discount_percent", 35))),
        depo_super_discount_percent=max(20, int((data.get("amazon_depo") or {}).get("super_discount_percent", 75))),
        depo_min_price_tl=max(0.0, float((data.get("amazon_depo") or {}).get("min_price_tl", 100.0))),
        depo_max_price_tl=max(100.0, float((data.get("amazon_depo") or {}).get("max_price_tl", 150000.0))),
        depo_auto_notify_admin=bool((data.get("amazon_depo") or {}).get("auto_notify_admin", True)),
        depo_auto_publish_channel=bool((data.get("amazon_depo") or {}).get("auto_publish_channel", True)),
        depo_categories=[
            str(c) for c in (data.get("amazon_depo") or {}).get("categories", ["tumu", "elektronik", "bilgisayar", "ev_mutfak"])
        ],
        depo_keywords=[str(k) for k in (data.get("amazon_depo") or {}).get("keywords", [])],
        raw_yaml=data,
    )

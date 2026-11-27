from __future__ import annotations

import re
import sqlite3
import statistics
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator, Optional

from src.models import AlertType, PriceOffer


class PriceStorage:
    """SQLite tabanlı fiyat geçmişi ve bildirim kilidi (cooldown) yöneticisi."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    total_offers INTEGER NOT NULL DEFAULT 0,
                    sources_ok INTEGER NOT NULL DEFAULT 0,
                    sources_failed INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS price_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    scraped_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    seller TEXT NOT NULL,
                    variant TEXT NOT NULL,
                    title TEXT NOT NULL,
                    price_tl REAL NOT NULL,
                    url TEXT NOT NULL,
                    in_stock INTEGER NOT NULL DEFAULT 1,
                    FOREIGN KEY (run_id) REFERENCES runs(id)
                );

                CREATE INDEX IF NOT EXISTS idx_snapshots_variant_time
                    ON price_snapshots(variant, scraped_at);

                CREATE INDEX IF NOT EXISTS idx_snapshots_run
                    ON price_snapshots(run_id);

                CREATE TABLE IF NOT EXISTS sent_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sent_at TEXT NOT NULL,
                    variant TEXT NOT NULL,
                    seller TEXT NOT NULL,
                    price_tl REAL NOT NULL,
                    alert_types TEXT NOT NULL,
                    url TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_alerts_lookup
                    ON sent_alerts(variant, seller, sent_at);

                CREATE TABLE IF NOT EXISTS daily_reports (
                    report_key TEXT PRIMARY KEY,
                    sent_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS watched_urls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    seller TEXT NOT NULL,
                    last_price_tl REAL NOT NULL,
                    min_price_tl REAL NOT NULL,
                    target_price_tl REAL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    last_checked_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS watched_url_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    watch_id INTEGER NOT NULL,
                    checked_at TEXT NOT NULL,
                    price_tl REAL NOT NULL,
                    effective_price_tl REAL,
                    coupon_info TEXT DEFAULT '',
                    in_stock INTEGER NOT NULL DEFAULT 1,
                    FOREIGN KEY (watch_id) REFERENCES watched_urls(id)
                );

                CREATE TABLE IF NOT EXISTS paused_variants (
                    variant TEXT PRIMARY KEY,
                    paused_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS watched_keywords (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query TEXT NOT NULL UNIQUE,
                    last_min_price_tl REAL NOT NULL DEFAULT 0,
                    all_time_low_tl REAL NOT NULL DEFAULT 0,
                    target_price_tl REAL,
                    last_seller TEXT DEFAULT '',
                    last_url TEXT DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    last_checked_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS seen_community_deals (
                    deal_key TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    seen_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS published_channel_deals (
                    deal_key TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    price_tl REAL NOT NULL,
                    url TEXT NOT NULL,
                    published_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pending_approvals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    deal_key TEXT NOT NULL UNIQUE,
                    post_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    seller TEXT NOT NULL DEFAULT '',
                    price_tl REAL NOT NULL DEFAULT 0,
                    coupon_code TEXT NOT NULL DEFAULT '',
                    url TEXT NOT NULL,
                    confidence INTEGER NOT NULL DEFAULT 55,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS x_tweets_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    deal_key TEXT NOT NULL UNIQUE,
                    tweet_url TEXT NOT NULL,
                    posted_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS bestseller_products (
                    product_key TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    seller TEXT NOT NULL,
                    category TEXT NOT NULL,
                    url TEXT NOT NULL,
                    image_url TEXT DEFAULT '',
                    last_price_tl REAL NOT NULL,
                    min_price_tl REAL NOT NULL,
                    last_seen_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS bestseller_price_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_key TEXT NOT NULL,
                    checked_at TEXT NOT NULL,
                    price_tl REAL NOT NULL,
                    coupon_info TEXT DEFAULT '',
                    coupon_code TEXT DEFAULT '',
                    FOREIGN KEY (product_key) REFERENCES bestseller_products(product_key)
                );

                CREATE INDEX IF NOT EXISTS idx_bs_history_lookup
                    ON bestseller_price_history(product_key, checked_at);

                CREATE TABLE IF NOT EXISTS amazon_depo_deals (
                    asin TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    depo_price_tl REAL NOT NULL,
                    market_price_tl REAL NOT NULL,
                    discount_percent INTEGER NOT NULL,
                    savings_tl REAL NOT NULL,
                    condition_label TEXT DEFAULT '',
                    condition_note TEXT DEFAULT '',
                    offer_listing_id TEXT DEFAULT '',
                    category TEXT DEFAULT '',
                    url TEXT NOT NULL,
                    image_url TEXT DEFAULT '',
                    price_source TEXT DEFAULT '',
                    is_super_steal INTEGER NOT NULL DEFAULT 0,
                    notified_price_tl REAL DEFAULT 0,
                    found_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS depo_watchlist (
                    asin TEXT PRIMARY KEY,
                    note TEXT DEFAULT '',
                    target_price_tl REAL DEFAULT 0,
                    min_discount_percent INTEGER DEFAULT 20,
                    last_depo_price_tl REAL DEFAULT 0,
                    in_depo_stock INTEGER DEFAULT 0,
                    added_at TEXT NOT NULL
                );
                """
            )
            # Mevcut veritabanları için güvenli kolon göçü
            for tbl, col, ddl in (
                ("price_snapshots", "effective_price_tl", "REAL"),
                ("price_snapshots", "coupon_info", "TEXT DEFAULT ''"),
                ("price_snapshots", "coupon_code", "TEXT DEFAULT ''"),
                ("price_snapshots", "color", "TEXT DEFAULT ''"),
                ("price_snapshots", "is_official_seller", "INTEGER DEFAULT 0"),
                ("price_snapshots", "trade_in_info", "TEXT DEFAULT ''"),
                ("watched_urls", "in_stock", "INTEGER DEFAULT 1"),
                ("watched_url_history", "effective_price_tl", "REAL"),
                ("watched_url_history", "coupon_info", "TEXT DEFAULT ''"),
                ("watched_url_history", "coupon_code", "TEXT DEFAULT ''"),
                ("amazon_depo_deals", "condition_note", "TEXT DEFAULT ''"),
                ("amazon_depo_deals", "offer_listing_id", "TEXT DEFAULT ''"),
            ):
                cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall()}
                if col not in cols:
                    conn.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {ddl}")

    def create_run(self, started_at: Optional[datetime] = None) -> int:
        ts = (started_at or datetime.now()).isoformat(timespec="seconds")
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO runs (started_at) VALUES (?)",
                (ts,),
            )
            return int(cur.lastrowid)

    def finalize_run(
        self, run_id: int, total_offers: int, sources_ok: int, sources_failed: int
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET total_offers = ?, sources_ok = ?, sources_failed = ?
                WHERE id = ?
                """,
                (total_offers, sources_ok, sources_failed, run_id),
            )

    def save_offers(self, run_id: int, offers: list[PriceOffer]) -> None:
        if not offers:
            return
        rows = [
            (
                run_id,
                o.scraped_at.isoformat(timespec="seconds"),
                o.source,
                o.seller,
                o.variant,
                o.title,
                o.price_tl,
                o.effective_price_tl,
                o.coupon_info,
                o.coupon_code,
                o.color,
                1 if o.is_official_seller else 0,
                o.trade_in_info,
                o.url,
                1 if o.in_stock else 0,
            )
            for o in offers
        ]
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO price_snapshots
                    (run_id, scraped_at, source, seller, variant, title, price_tl, effective_price_tl, coupon_info, coupon_code, color, is_official_seller, trade_in_info, url, in_stock)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def get_previous_run_min(self, variant: str, current_run_id: int) -> Optional[float]:
        """Bir önceki başarılı taramadaki en düşük fiyatı döndürür."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT MIN(ps.price_tl) AS min_price
                FROM price_snapshots ps
                WHERE ps.variant = ?
                  AND ps.run_id = (
                      SELECT MAX(id) FROM runs
                      WHERE id < ? AND total_offers > 0
                  )
                """,
                (variant, current_run_id),
            ).fetchone()
            return float(row["min_price"]) if row and row["min_price"] is not None else None

    def get_all_time_low(self, variant: str, exclude_run_id: Optional[int] = None) -> Optional[float]:
        with self._connect() as conn:
            if exclude_run_id is not None:
                row = conn.execute(
                    "SELECT MIN(COALESCE(effective_price_tl, price_tl)) AS min_price FROM price_snapshots WHERE variant = ? AND run_id < ?",
                    (variant, exclude_run_id),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT MIN(COALESCE(effective_price_tl, price_tl)) AS min_price FROM price_snapshots WHERE variant = ?",
                    (variant,),
                ).fetchone()
            return float(row["min_price"]) if row and row["min_price"] is not None else None

    def get_six_month_low(
        self, variant: str, days: int = 180, exclude_run_id: Optional[int] = None
    ) -> Optional[float]:
        since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        with self._connect() as conn:
            if exclude_run_id is not None:
                row = conn.execute(
                    """
                    SELECT MIN(COALESCE(effective_price_tl, price_tl)) AS min_price
                    FROM price_snapshots
                    WHERE variant = ? AND scraped_at >= ? AND run_id < ?
                    """,
                    (variant, since, exclude_run_id),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT MIN(COALESCE(effective_price_tl, price_tl)) AS min_price
                    FROM price_snapshots
                    WHERE variant = ? AND scraped_at >= ?
                    """,
                    (variant, since),
                ).fetchone()
            return float(row["min_price"]) if row and row["min_price"] is not None else None

    def pause_variant(self, variant: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO paused_variants (variant, paused_at) VALUES (?, ?)",
                (variant, datetime.now().isoformat(timespec="seconds")),
            )

    def resume_variant(self, variant: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM paused_variants WHERE variant = ?", (variant,))

    def get_paused_variants(self) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT variant FROM paused_variants").fetchall()
        return {str(r["variant"]) for r in rows}

    def is_only_official_sellers(self) -> bool:
        return "ONLY_OFFICIAL" in self.get_paused_variants()

    def set_only_official_sellers(self, enabled: bool) -> None:
        if enabled:
            self.pause_variant("ONLY_OFFICIAL")
        else:
            self.resume_variant("ONLY_OFFICIAL")

    def get_rolling_median(self, variant: str, days: int = 7, exclude_run_id: Optional[int] = None) -> Optional[float]:
        since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        with self._connect() as conn:
            if exclude_run_id is not None:
                rows = conn.execute(
                    """
                    SELECT price_tl FROM price_snapshots
                    WHERE variant = ? AND scraped_at >= ? AND run_id < ?
                    """,
                    (variant, since, exclude_run_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT price_tl FROM price_snapshots WHERE variant = ? AND scraped_at >= ?",
                    (variant, since),
                ).fetchall()
        prices = [float(r["price_tl"]) for r in rows]
        if len(prices) < 3:
            return None
        return float(statistics.median(prices))

    def was_recently_alerted(
        self, variant: str, seller: str, price_tl: float, cooldown_hours: int
    ) -> bool:
        since = (datetime.now() - timedelta(hours=cooldown_hours)).isoformat(timespec="seconds")
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT price_tl FROM sent_alerts
                WHERE variant = ? AND LOWER(seller) = LOWER(?) AND sent_at >= ?
                ORDER BY id DESC LIMIT 1
                """,
                (variant, seller, since),
            ).fetchone()
        if not row:
            return False
        # Daha önce aynı veya daha düşük fiyata bildirim gönderildiyse tekrar spam yapma
        last_price = float(row["price_tl"])
        return price_tl >= last_price - 50.0

    def record_alert(
        self,
        variant: str,
        seller: str,
        price_tl: float,
        alert_types: list[AlertType],
        url: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sent_alerts (sent_at, variant, seller, price_tl, alert_types, url)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now().isoformat(timespec="seconds"),
                    variant,
                    seller,
                    price_tl,
                    ",".join(a.value for a in alert_types),
                    url,
                ),
            )

    def has_daily_report_sent(self, report_key: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM daily_reports WHERE report_key = ?",
                (report_key,),
            ).fetchone()
            return row is not None

    def mark_daily_report_sent(self, report_key: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO daily_reports (report_key, sent_at) VALUES (?, ?)",
                (report_key, datetime.now().isoformat(timespec="seconds")),
            )

    def get_latest_offers_by_variant(self, limit_per_variant: int = 5) -> dict[str, list[PriceOffer]]:
        """Son başarılı taramadaki teklifleri varyant bazlı ucuzdan pahalıya döndürür."""
        with self._connect() as conn:
            run_row = conn.execute(
                "SELECT id FROM runs WHERE total_offers > 0 ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if not run_row:
                return {}
            run_id = int(run_row["id"])
            rows = conn.execute(
                """
                SELECT source, seller, variant, title, price_tl,
                       COALESCE(effective_price_tl, price_tl) AS eff_price,
                       COALESCE(coupon_info, '') AS coupon_info,
                       COALESCE(coupon_code, '') AS coupon_code,
                       COALESCE(color, '') AS color,
                       COALESCE(is_official_seller, 0) AS is_official_seller,
                       COALESCE(trade_in_info, '') AS trade_in_info,
                       url, in_stock, scraped_at
                FROM price_snapshots
                WHERE run_id = ?
                ORDER BY COALESCE(effective_price_tl, price_tl) ASC
                """,
                (run_id,),
            ).fetchall()

        only_official = self.is_only_official_sellers()
        grouped: dict[str, list[PriceOffer]] = {}
        seen_sellers: dict[str, set[str]] = {}
        for r in rows:
            if only_official and not bool(r["is_official_seller"]):
                continue
            var = r["variant"]
            seller_key = r["seller"].strip().lower()
            seen_sellers.setdefault(var, set())
            if seller_key in seen_sellers[var]:
                continue
            seen_sellers[var].add(seller_key)
            grouped.setdefault(var, [])
            if len(grouped[var]) < limit_per_variant:
                p_tl = float(r["price_tl"])
                e_tl = float(r["eff_price"])
                grouped[var].append(
                    PriceOffer(
                        source=r["source"],
                        seller=r["seller"],
                        variant=var,
                        title=r["title"],
                        price_tl=p_tl,
                        url=r["url"],
                        in_stock=bool(r["in_stock"]),
                        color=str(r["color"] or ""),
                        is_official_seller=bool(r["is_official_seller"]),
                        coupon_discount_tl=max(0.0, round(p_tl - e_tl, 2)),
                        coupon_info=str(r["coupon_info"] or ""),
                        coupon_code=str(r["coupon_code"] or ""),
                        trade_in_info=str(r["trade_in_info"] or ""),
                        scraped_at=datetime.fromisoformat(r["scraped_at"]),
                    )
                )
        return grouped

    # --- Özel Ürün Linki Takibi ---

    def add_or_update_watched_url(
        self,
        url: str,
        title: str,
        seller: str,
        price_tl: float,
        target_price_tl: Optional[float] = None,
        in_stock: bool = True,
    ) -> tuple[int, bool]:
        """Linki takibe ekler (varsa günceller/yeniden aktifleştirir). (id, is_new) döndürür."""
        now = datetime.now().isoformat(timespec="seconds")
        stock_int = 1 if in_stock else 0
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id, min_price_tl, target_price_tl FROM watched_urls WHERE url = ?",
                (url,),
            ).fetchone()
            if existing:
                watch_id = int(existing["id"])
                old_min = float(existing["min_price_tl"])
                if price_tl > 0 and old_min > 0:
                    new_min = min(old_min, price_tl)
                else:
                    new_min = price_tl if price_tl > 0 else old_min
                final_target = target_price_tl if target_price_tl is not None else existing["target_price_tl"]
                conn.execute(
                    """
                    UPDATE watched_urls
                    SET title = ?, seller = ?, last_price_tl = ?, min_price_tl = ?,
                        target_price_tl = ?, in_stock = ?, active = 1, last_checked_at = ?
                    WHERE id = ?
                    """,
                    (title, seller, price_tl, new_min, final_target, stock_int, now, watch_id),
                )
                is_new = False
            else:
                cur = conn.execute(
                    """
                    INSERT INTO watched_urls
                        (url, title, seller, last_price_tl, min_price_tl, target_price_tl, in_stock, active, created_at, last_checked_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (url, title, seller, price_tl, price_tl, target_price_tl, stock_int, now, now),
                )
                watch_id = int(cur.lastrowid)
                is_new = True

            conn.execute(
                """
                INSERT INTO watched_url_history (watch_id, checked_at, price_tl, effective_price_tl, in_stock)
                VALUES (?, ?, ?, ?, ?)
                """,
                (watch_id, now, price_tl, price_tl, stock_int),
            )
            return watch_id, is_new

    def list_watched_urls(self, active_only: bool = True) -> list[dict]:
        with self._connect() as conn:
            sql = "SELECT * FROM watched_urls"
            if active_only:
                sql += " WHERE active = 1"
            sql += " ORDER BY id ASC"
            rows = conn.execute(sql).fetchall()
        return [dict(r) for r in rows]

    def get_watched_url(self, watch_id: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM watched_urls WHERE id = ? AND active = 1",
                (watch_id,),
            ).fetchone()
        return dict(row) if row else None

    def set_watched_url_target(self, watch_id: int, target_price_tl: float) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE watched_urls SET target_price_tl = ? WHERE id = ? AND active = 1",
                (target_price_tl, watch_id),
            )
            return cur.rowcount > 0

    def remove_watched_url(self, watch_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE watched_urls SET active = 0 WHERE id = ? AND active = 1",
                (watch_id,),
            )
            return cur.rowcount > 0

    def update_watched_url_price(
        self,
        watch_id: int,
        title: str,
        price_tl: float,
        in_stock: bool,
        effective_price_tl: Optional[float] = None,
        coupon_info: str = "",
        coupon_code: str = "",
    ) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        eff = effective_price_tl if effective_price_tl is not None else price_tl
        stock_int = 1 if in_stock else 0
        with self._connect() as conn:
            row = conn.execute(
                "SELECT min_price_tl, last_price_tl FROM watched_urls WHERE id = ?",
                (watch_id,),
            ).fetchone()
            old_min = float(row["min_price_tl"]) if row else 0.0
            old_last = float(row["last_price_tl"]) if row else 0.0
            if eff > 0 and old_min > 0:
                new_min = min(old_min, eff)
            else:
                new_min = eff if eff > 0 else old_min
            new_last = eff if eff > 0 else old_last

            conn.execute(
                """
                UPDATE watched_urls
                SET title = ?, last_price_tl = ?, min_price_tl = ?, in_stock = ?, last_checked_at = ?
                WHERE id = ?
                """,
                (title, new_last, new_min, stock_int, now, watch_id),
            )
            conn.execute(
                """
                INSERT INTO watched_url_history (watch_id, checked_at, price_tl, effective_price_tl, coupon_info, coupon_code, in_stock)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (watch_id, now, price_tl, eff, coupon_info, coupon_code, stock_int),
            )

    def get_variant_price_series(self, variant: str, days: int = 180) -> list[tuple[str, float]]:
        """Grafik çizimi için her taramadaki en düşük efektif fiyat serisini döndürür."""
        since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT MIN(scraped_at) AS ts, MIN(COALESCE(effective_price_tl, price_tl)) AS min_p
                FROM price_snapshots
                WHERE variant = ? AND scraped_at >= ?
                GROUP BY run_id
                ORDER BY run_id ASC
                """,
                (variant, since),
            ).fetchall()
        out: list[tuple[str, float]] = []
        for r in rows:
            if r["min_p"] is None:
                continue
            raw_ts = str(r["ts"] or "")
            lbl = raw_ts[5:16].replace("T", " ") if len(raw_ts) >= 16 else raw_ts
            out.append((lbl, float(r["min_p"])))
        return out

    def get_watched_url_price_series(self, watch_id: int, days: int = 180) -> list[tuple[str, float]]:
        since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT checked_at, COALESCE(effective_price_tl, price_tl) AS eff_p
                FROM watched_url_history
                WHERE watch_id = ? AND checked_at >= ? AND COALESCE(effective_price_tl, price_tl) > 0
                ORDER BY id ASC
                """,
                (watch_id, since),
            ).fetchall()
        out: list[tuple[str, float]] = []
        for r in rows:
            raw_ts = str(r["checked_at"] or "")
            lbl = raw_ts[5:16].replace("T", " ") if len(raw_ts) >= 16 else raw_ts
            out.append((lbl, float(r["eff_p"])))
        return out

    def get_recent_snapshots_for_export(self, limit: int = 1500) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT scraped_at, variant, COALESCE(color, '') AS color, seller,
                       COALESCE(is_official_seller, 0) AS is_official_seller,
                       price_tl, COALESCE(effective_price_tl, price_tl) AS effective_price_tl,
                       COALESCE(coupon_info, '') AS coupon_info,
                       COALESCE(coupon_code, '') AS coupon_code,
                       source, title, url
                FROM price_snapshots
                ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_watched_url_six_month_low(self, watch_id: int, days: int = 180) -> Optional[float]:
        since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT MIN(COALESCE(effective_price_tl, price_tl)) AS min_price
                FROM watched_url_history
                WHERE watch_id = ? AND checked_at >= ?
                """,
                (watch_id, since),
            ).fetchone()
        return float(row["min_price"]) if row and row["min_price"] is not None else None

    def get_cached_snapshot_by_url(self, url: str) -> Optional[dict]:
        """Canlı çekim anlık hata verirse veritabanındaki son kayıttan başlık ve fiyatı kurtarır."""
        clean = url.split("?")[0].strip()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT title, seller, price_tl, COALESCE(effective_price_tl, price_tl) AS eff_price, coupon_info
                FROM price_snapshots
                WHERE url = ? OR url LIKE ?
                ORDER BY id DESC LIMIT 1
                """,
                (url, f"{clean}%"),
            ).fetchone()
        return dict(row) if row else None

    def get_historical_min_before_previous_run(
        self, variant: str, current_run_id: int, days: int = 30
    ) -> Optional[float]:
        """Sahte indirim tespiti için bir önceki taramadan da önceki 30 günlük normal en düşük fiyatı döndürür."""
        since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        with self._connect() as conn:
            prev_row = conn.execute(
                "SELECT MAX(id) AS prev_id FROM runs WHERE id < ? AND total_offers > 0",
                (current_run_id,),
            ).fetchone()
            if not prev_row or prev_row["prev_id"] is None:
                return None
            prev_id = int(prev_row["prev_id"])
            row = conn.execute(
                """
                SELECT MIN(COALESCE(effective_price_tl, price_tl)) AS min_p
                FROM price_snapshots
                WHERE variant = ? AND scraped_at >= ? AND run_id < ?
                """,
                (variant, since, prev_id),
            ).fetchone()
        return float(row["min_p"]) if row and row["min_p"] is not None else None

    # --- Kelime Bazlı Çoklu Ürün Arama Takibi ---

    def add_or_update_watched_keyword(
        self,
        query: str,
        price_tl: float,
        seller: str,
        url: str,
        target_price_tl: Optional[float] = None,
    ) -> tuple[int, bool]:
        now = datetime.now().isoformat(timespec="seconds")
        clean_q = " ".join(query.strip().split())
        with self._connect() as conn:
            ex = conn.execute(
                "SELECT id, all_time_low_tl, target_price_tl FROM watched_keywords WHERE LOWER(query) = LOWER(?)",
                (clean_q,),
            ).fetchone()
            if ex:
                kw_id = int(ex["id"])
                old_low = float(ex["all_time_low_tl"] or 0.0)
                new_low = min(old_low, price_tl) if (old_low > 0 and price_tl > 0) else (price_tl or old_low)
                final_target = target_price_tl if target_price_tl is not None else ex["target_price_tl"]
                conn.execute(
                    """
                    UPDATE watched_keywords
                    SET query = ?, last_min_price_tl = ?, all_time_low_tl = ?, target_price_tl = ?,
                        last_seller = ?, last_url = ?, active = 1, last_checked_at = ?
                    WHERE id = ?
                    """,
                    (clean_q, price_tl, new_low, final_target, seller, url, now, kw_id),
                )
                return kw_id, False
            cur = conn.execute(
                """
                INSERT INTO watched_keywords
                    (query, last_min_price_tl, all_time_low_tl, target_price_tl, last_seller, last_url, active, created_at, last_checked_at)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (clean_q, price_tl, price_tl, target_price_tl, seller, url, now, now),
            )
            return int(cur.lastrowid), True

    def list_watched_keywords(self, active_only: bool = True) -> list[dict]:
        with self._connect() as conn:
            sql = "SELECT * FROM watched_keywords"
            if active_only:
                sql += " WHERE active = 1"
            sql += " ORDER BY id ASC"
            rows = conn.execute(sql).fetchall()
        return [dict(r) for r in rows]

    def remove_watched_keyword(self, kw_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE watched_keywords SET active = 0 WHERE id = ? AND active = 1",
                (kw_id,),
            )
            return cur.rowcount > 0

    def is_community_deal_seen(self, deal_key: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM seen_community_deals WHERE deal_key = ?",
                (deal_key,),
            ).fetchone()
            return row is not None

    def mark_community_deal_seen(self, deal_key: str, title: str, url: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO seen_community_deals (deal_key, title, url, seen_at) VALUES (?, ?, ?, ?)",
                (deal_key, title, url, datetime.now().isoformat(timespec="seconds")),
            )

    def is_channel_deal_published(
        self,
        deal_key: str,
        title: str = "",
        price_tl: Optional[float] = None,
        url: str = "",
        repost_discount_pct: float = 5.0,
    ) -> bool:
        from src.popular_deals import canonicalize_deal_key

        canon_key = canonicalize_deal_key(deal_key)
        canon_url = canonicalize_deal_key(url) if url else canon_key
        probe_str = f"{canon_key} {canon_url} {url}"
        sku_m = re.search(r"(?:/dp/|urun-p-|-(?:p|pm)-)([A-Z0-9]{5,16})", probe_str, re.IGNORECASE)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT price_tl FROM published_channel_deals
                WHERE deal_key IN (?, ?) OR url IN (?, ?)
                ORDER BY published_at DESC LIMIT 1
                """,
                (deal_key, canon_key, canon_url, url or canon_url),
            ).fetchone()
            if not row and sku_m:
                token = sku_m.group(1).upper()
                row = conn.execute(
                    "SELECT price_tl FROM published_channel_deals WHERE UPPER(deal_key) LIKE ? OR UPPER(url) LIKE ? ORDER BY published_at DESC LIMIT 1",
                    (f"%{token}%", f"%{token}%"),
                ).fetchone()
            if not row and title.strip():
                since = (datetime.now() - timedelta(days=7)).isoformat(timespec="seconds")
                fp_target = _title_fingerprint(title)
                if len(fp_target) >= 10:
                    recent_rows = conn.execute(
                        "SELECT title, price_tl FROM published_channel_deals WHERE published_at >= ? ORDER BY published_at DESC LIMIT 200",
                        (since,),
                    ).fetchall()
                    for r in recent_rows:
                        fp_old = _title_fingerprint(str(r["title"] or ""))
                        if fp_old and (fp_old == fp_target or fp_old[:32] == fp_target[:32]):
                            row = r
                            break
            if not row:
                return False
            # Eğer fiyat önceki paylaşılan fiyattan en az belirlenen oranda daha ucuzsa tekrar yayınlanabilir
            old_p = float(row["price_tl"] or 0.0)
            ratio = max(0.05, 1.0 - (max(1.0, float(repost_discount_pct)) / 100.0))
            if price_tl is not None and old_p > 0 and price_tl <= old_p * ratio:
                return False
            return True

    def delete_published_channel_deal(self, deal_key: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM published_channel_deals WHERE deal_key = ?", (deal_key,))
            return cur.rowcount > 0

    def set_watched_keyword_target(self, kw_id: int, target_price_tl: Optional[float]) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE watched_keywords SET target_price_tl = ? WHERE id = ? AND active = 1",
                (target_price_tl, kw_id),
            )
            return cur.rowcount > 0

    def clear_sent_alerts(self) -> int:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM sent_alerts")
            return int(cur.rowcount or 0)

    def get_db_stats(self) -> dict[str, int]:
        with self._connect() as conn:
            def _cnt(tbl: str) -> int:
                row = conn.execute(f"SELECT COUNT(*) AS c FROM {tbl}").fetchone()
                return int(row["c"]) if row else 0

            return {
                "runs": _cnt("runs"),
                "snapshots": _cnt("price_snapshots"),
                "alerts": _cnt("sent_alerts"),
                "channel_posts": _cnt("published_channel_deals"),
                "bestsellers": _cnt("bestseller_products"),
                "watched_urls": _cnt("watched_urls"),
                "watched_keywords": _cnt("watched_keywords"),
                "depo_deals": _cnt("amazon_depo_deals"),
            }

    def upsert_depo_deals(self, deals: list) -> int:
        if not deals:
            return 0
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            for d in deals:
                conn.execute(
                    """
                    INSERT INTO amazon_depo_deals
                        (asin, title, depo_price_tl, market_price_tl, discount_percent, savings_tl,
                         condition_label, condition_note, offer_listing_id, category, url, image_url,
                         price_source, is_super_steal, found_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(asin) DO UPDATE SET
                        title = excluded.title,
                        depo_price_tl = excluded.depo_price_tl,
                        market_price_tl = excluded.market_price_tl,
                        discount_percent = excluded.discount_percent,
                        savings_tl = excluded.savings_tl,
                        condition_label = excluded.condition_label,
                        condition_note = excluded.condition_note,
                        offer_listing_id = excluded.offer_listing_id,
                        category = excluded.category,
                        url = excluded.url,
                        image_url = CASE WHEN excluded.image_url != '' THEN excluded.image_url ELSE amazon_depo_deals.image_url END,
                        price_source = excluded.price_source,
                        is_super_steal = excluded.is_super_steal,
                        found_at = excluded.found_at
                    """,
                    (
                        d.asin,
                        d.title,
                        float(d.depo_price_tl),
                        float(d.market_price_tl),
                        int(d.discount_percent),
                        float(d.savings_tl),
                        d.condition_label,
                        getattr(d, "condition_note", ""),
                        getattr(d, "offer_listing_id", ""),
                        d.category,
                        d.url,
                        d.image_url,
                        d.price_source,
                        1 if d.is_super_steal else 0,
                        now,
                    ),
                )
        return len(deals)

    def remove_depo_asins(self, asins: set[str]) -> int:
        if not asins:
            return 0
        with self._connect() as conn:
            placeholders = ",".join("?" for _ in asins)
            cur = conn.execute(
                f"DELETE FROM amazon_depo_deals WHERE asin IN ({placeholders})",
                tuple(a.strip().upper() for a in asins),
            )
            return int(cur.rowcount or 0)

    def add_depo_watchlist(
        self, asin: str, note: str = "", target_price_tl: float = 0.0, min_discount_percent: int = 20
    ) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO depo_watchlist (asin, note, target_price_tl, min_discount_percent, added_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(asin) DO UPDATE SET
                    note = CASE WHEN excluded.note != '' THEN excluded.note ELSE depo_watchlist.note END,
                    target_price_tl = excluded.target_price_tl,
                    min_discount_percent = excluded.min_discount_percent
                """,
                (asin.strip().upper(), note.strip(), float(target_price_tl or 0.0), int(min_discount_percent), now),
            )

    def list_depo_watchlist(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM depo_watchlist ORDER BY added_at DESC").fetchall()
        return [dict(r) for r in rows]

    def update_depo_watchlist_status(self, asin: str, price_tl: float, in_stock: bool, note: str = "") -> None:
        with self._connect() as conn:
            if note:
                conn.execute(
                    "UPDATE depo_watchlist SET last_depo_price_tl = ?, in_depo_stock = ?, note = ? WHERE asin = ?",
                    (float(price_tl), 1 if in_stock else 0, note[:120], asin.strip().upper()),
                )
            else:
                conn.execute(
                    "UPDATE depo_watchlist SET last_depo_price_tl = ?, in_depo_stock = ? WHERE asin = ?",
                    (float(price_tl), 1 if in_stock else 0, asin.strip().upper()),
                )

    def remove_depo_watchlist(self, asin: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM depo_watchlist WHERE asin = ?", (asin.strip().upper(),))
            return cur.rowcount > 0

    def list_depo_deals(self, min_discount: int = 0, limit: int = 60) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM amazon_depo_deals
                WHERE discount_percent >= ?
                ORDER BY discount_percent DESC, savings_tl DESC, found_at DESC
                LIMIT ?
                """,
                (int(min_discount), int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_depo_deal(self, asin: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM amazon_depo_deals WHERE asin = ?",
                (asin.strip().upper(),),
            ).fetchone()
        return dict(row) if row else None

    def was_depo_asin_notified(self, asin: str, current_price_tl: float) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT notified_price_tl FROM amazon_depo_deals WHERE asin = ?",
                (asin.strip().upper(),),
            ).fetchone()
        if not row:
            return False
        prev = float(row["notified_price_tl"] or 0.0)
        return prev > 0 and current_price_tl > prev * 0.92

    def mark_depo_asin_notified(self, asin: str, price_tl: float) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE amazon_depo_deals SET notified_price_tl = ? WHERE asin = ?",
                (float(price_tl), asin.strip().upper()),
            )

    def prune_stale_depo_deals(self, active_asins: set[str]) -> int:
        """Son canlı Depo taramasında artık stokta görünmeyen (tükenen) ürünleri panelden kaldırır."""
        with self._connect() as conn:
            if not active_asins:
                return 0
            placeholders = ",".join("?" for _ in active_asins)
            cur = conn.execute(
                f"DELETE FROM amazon_depo_deals WHERE asin NOT IN ({placeholders})",
                tuple(active_asins),
            )
            return int(cur.rowcount or 0)

    def delete_depo_deal(self, asin: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM amazon_depo_deals WHERE asin = ?", (asin.strip().upper(),))
            return cur.rowcount > 0

    def clear_depo_deals(self) -> int:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM amazon_depo_deals")
            return int(cur.rowcount or 0)

    def mark_channel_deal_published(
        self, deal_key: str, title: str, price_tl: float, url: str
    ) -> None:
        from src.popular_deals import canonicalize_deal_key

        canon_key = canonicalize_deal_key(deal_key)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO published_channel_deals
                    (deal_key, title, price_tl, url, published_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (canon_key, title, price_tl, url, datetime.now().isoformat(timespec="seconds")),
            )

    def count_x_tweets_today(self) -> int:
        today_prefix = datetime.now().strftime("%Y-%m-%d")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM x_tweets_log WHERE posted_at LIKE ?",
                (f"{today_prefix}%",),
            ).fetchone()
        return int(row["cnt"]) if row else 0

    def is_x_deal_posted(self, deal_key: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM x_tweets_log WHERE deal_key = ?",
                (deal_key,),
            ).fetchone()
        return row is not None

    def mark_x_deal_posted(self, deal_key: str, tweet_url: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO x_tweets_log (deal_key, tweet_url, posted_at) VALUES (?, ?, ?)",
                (deal_key, tweet_url, datetime.now().isoformat(timespec="seconds")),
            )

    def get_last_x_tweet_time(self) -> Optional[datetime]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT posted_at FROM x_tweets_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if not row or not row["posted_at"]:
            return None
        try:
            return datetime.fromisoformat(str(row["posted_at"]))
        except ValueError:
            return None

    # --- Çok Satanlar / Çok Değerlendirilenler Fiyat Geçmişi ---

    def record_bestseller_and_check_drop(
        self,
        *,
        product_key: str,
        title: str,
        seller: str,
        category: str,
        url: str,
        image_url: str,
        effective_price_tl: float,
        coupon_info: str = "",
        coupon_code: str = "",
        compare_days: int = 30,
    ) -> dict:
        """
        Ürünü çok satanlar geçmişine kaydeder ve önceki fiyat / son N günlük (varsayılan 30 gün)
        ve 180 günlük en düşük fiyat istatistiklerini döndürür.
        """
        now = datetime.now()
        now_s = now.isoformat(timespec="seconds")
        since_n = (now - timedelta(days=compare_days)).isoformat(timespec="seconds")
        since_180 = (now - timedelta(days=180)).isoformat(timespec="seconds")

        with self._connect() as conn:
            prev_row = conn.execute(
                "SELECT last_price_tl, min_price_tl FROM bestseller_products WHERE product_key = ?",
                (product_key,),
            ).fetchone()
            prev_price = float(prev_row["last_price_tl"]) if prev_row else None

            n_row = conn.execute(
                "SELECT MIN(price_tl) AS min_p, COUNT(*) AS cnt FROM bestseller_price_history WHERE product_key = ? AND checked_at >= ?",
                (product_key, since_n),
            ).fetchone()
            min_nd = float(n_row["min_p"]) if (n_row and n_row["min_p"] is not None) else None
            hist_cnt = int(n_row["cnt"]) if n_row else 0

            row_180 = conn.execute(
                "SELECT MIN(price_tl) AS min_p FROM bestseller_price_history WHERE product_key = ? AND checked_at >= ?",
                (product_key, since_180),
            ).fetchone()
            min_180d = float(row_180["min_p"]) if (row_180 and row_180["min_p"] is not None) else None

            new_min = min(float(prev_row["min_price_tl"]), effective_price_tl) if prev_row else effective_price_tl
            conn.execute(
                """
                INSERT INTO bestseller_products
                    (product_key, title, seller, category, url, image_url, last_price_tl, min_price_tl, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(product_key) DO UPDATE SET
                    title = excluded.title,
                    seller = excluded.seller,
                    category = excluded.category,
                    url = excluded.url,
                    image_url = CASE WHEN excluded.image_url != '' THEN excluded.image_url ELSE bestseller_products.image_url END,
                    last_price_tl = excluded.last_price_tl,
                    min_price_tl = MIN(bestseller_products.min_price_tl, excluded.last_price_tl),
                    last_seen_at = excluded.last_seen_at
                """,
                (product_key, title, seller, category, url, image_url, effective_price_tl, new_min, now_s),
            )
            conn.execute(
                """
                INSERT INTO bestseller_price_history (product_key, checked_at, price_tl, coupon_info, coupon_code)
                VALUES (?, ?, ?, ?, ?)
                """,
                (product_key, now_s, effective_price_tl, coupon_info, coupon_code),
            )

        return {
            "is_new": prev_row is None,
            "history_count": hist_cnt,
            "prev_price_tl": prev_price,
            "min_nd_tl": min_nd,
            "min_180d_tl": min_180d,
        }

    def count_tracked_bestsellers(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS cnt FROM bestseller_products").fetchone()
        return int(row["cnt"]) if row else 0

    def get_bestseller_30d_median(self, product_key: str, days: int = 30) -> Optional[float]:
        since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT price_tl FROM bestseller_price_history WHERE product_key = ? AND checked_at >= ?",
                (product_key, since),
            ).fetchall()
        prices = [float(r["price_tl"]) for r in rows if float(r["price_tl"] or 0) > 0]
        if len(prices) < 3:
            return None
        return float(statistics.median(prices))

    def get_recent_channel_deals(self, limit: int = 12) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT deal_key, title, price_tl, url, published_at
                FROM published_channel_deals
                ORDER BY published_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_last_run_info(self) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, started_at, total_offers, sources_ok, sources_failed
                FROM runs
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
        return dict(row) if row else None

    def enqueue_pending_approval(
        self,
        *,
        deal_key: str,
        post_type: str,
        title: str,
        seller: str,
        price_tl: float,
        coupon_code: str,
        url: str,
        confidence: int,
    ) -> Optional[int]:
        from src.popular_deals import canonicalize_deal_key

        canon = canonicalize_deal_key(deal_key)
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            ex = conn.execute(
                "SELECT id FROM pending_approvals WHERE deal_key = ?",
                (canon,),
            ).fetchone()
            if ex:
                return None
            cur = conn.execute(
                """
                INSERT INTO pending_approvals
                    (deal_key, post_type, title, seller, price_tl, coupon_code, url, confidence, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (canon, post_type, title, seller, price_tl, coupon_code, url, confidence, now),
            )
            return int(cur.lastrowid)

    def list_pending_approvals(self, limit: int = 15) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pending_approvals WHERE status = 'pending' ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def resolve_pending_approval(self, approval_id: int, approved: bool) -> Optional[dict]:
        new_status = "approved" if approved else "rejected"
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pending_approvals WHERE id = ? AND status = 'pending'",
                (approval_id,),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE pending_approvals SET status = ? WHERE id = ?",
                (new_status, approval_id),
            )
        return dict(row)


def _title_fingerprint(text: str) -> str:
    """Noktalama, boşluk ve Türkçe karakter farklarını silerek başlık parmak izi üretir."""
    tr_map = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")
    cleaned = re.sub(r"[^a-z0-9]", "", (text or "").translate(tr_map).lower())
    return cleaned[:42]

from __future__ import annotations

import html
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from src.config import AppConfig
from src.storage import PriceStorage


@dataclass(slots=True)
class HabitReport:
    total_published_deals: int
    total_tracked_bestsellers: int
    total_watched_urls: int
    total_watched_keywords: int
    top_stores: list[tuple[str, int]] = field(default_factory=list)
    top_categories: list[tuple[str, int]] = field(default_factory=list)
    peak_hours: list[tuple[int, int]] = field(default_factory=list)
    price_brackets: dict[str, int] = field(default_factory=dict)
    recommended_turbo_windows: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)


def _infer_store(url: str) -> str:
    low = (url or "").lower()
    if "amazon." in low:
        return "Amazon.com.tr"
    if "trendyol." in low:
        return "Trendyol"
    if "hepsiburada." in low:
        return "Hepsiburada"
    if "mediamarkt." in low:
        return "MediaMarkt"
    if "teknosa." in low:
        return "Teknosa"
    if "n11." in low:
        return "N11"
    if "pazarama." in low:
        return "Pazarama"
    if "idefix." in low:
        return "İdefix"
    if "migros." in low:
        return "Migros"
    if "akakce." in low:
        return "Akakçe"
    return "Diğer"


def _infer_category(title: str) -> str:
    low = (title or "").lower()
    if any(k in low for k in ("ruj", "deodorant", "krem", "şampuan", "parfüm", "nemlendirici", "cilt", "makyaj", "saç", "lumea", "dyson")):
        return "Kozmetik & Kadın Bakım"
    if any(k in low for k in ("kadın", "elbise", "çanta", "takı", "ceket", "mont", "ayakkabı", "tişört", "pantolon", "çorap", "boxer", "spor")):
        return "Giyim, Moda & Spor"
    if any(k in low for k in ("bebek", "bez", "mama", "oyuncak", "lego", "blok")):
        return "Anne, Bebek & Hobi"
    if any(k in low for k in ("telefon", "iphone", "galaxy", "kulaklık", "tws", "tv", "laptop", "monitör", "ssd", "power supply", "kamera")):
        return "Elektronik & Teknoloji"
    if any(k in low for k in ("blender", "süpürge", "termos", "kahve", "airfryer", "mutfak", "tencere", "ütü", "çeyiz")):
        return "Ev & Mutfak"
    if any(k in low for k in ("deterjan", "çay", "yağ", "pil", "kapsül", "vitamin")):
        return "Süpermarket & Günlük"
    return "Genel"


def analyze_shopping_habits(storage: PriceStorage, config: AppConfig) -> HabitReport:
    """
    Veritabanındaki yayınlanan fırsatları, çok satanlar geçmişini, takip edilen linkleri ve
    aranan kelimeleri analiz ederek alışveriş alışkanlıklarını ve optimizasyon önerilerini çıkarır.
    """
    store_counter: Counter[str] = Counter()
    cat_counter: Counter[str] = Counter()
    hour_counter: Counter[int] = Counter()
    brackets = {"<500 TL": 0, "500-2.000 TL": 0, "2.000-10.000 TL": 0, "10.000+ TL": 0}

    with storage._connect() as conn:
        pub_rows = conn.execute(
            "SELECT title, price_tl, url, published_at FROM published_channel_deals ORDER BY published_at DESC LIMIT 300"
        ).fetchall()
        bs_rows = conn.execute(
            "SELECT title, seller, category, last_price_tl FROM bestseller_products"
        ).fetchall()
        alert_rows = conn.execute(
            "SELECT sent_at, seller FROM sent_alerts ORDER BY id DESC LIMIT 200"
        ).fetchall()

    watched_urls = storage.list_watched_urls(active_only=True)
    watched_kws = storage.list_watched_keywords(active_only=True)

    for r in pub_rows:
        title = str(r["title"] or "")
        url = str(r["url"] or "")
        price = float(r["price_tl"] or 0.0)
        ts = str(r["published_at"] or "")
        store_counter[_infer_store(url)] += 1
        cat_counter[_infer_category(title)] += 1
        if len(ts) >= 13 and ts[11:13].isdigit():
            hour_counter[int(ts[11:13])] += 1
        if price > 0:
            if price < 500:
                brackets["<500 TL"] += 1
            elif price < 2000:
                brackets["500-2.000 TL"] += 1
            elif price < 10000:
                brackets["2.000-10.000 TL"] += 1
            else:
                brackets["10.000+ TL"] += 1

    for r in alert_rows:
        ts = str(r["sent_at"] or "")
        if len(ts) >= 13 and ts[11:13].isdigit():
            hour_counter[int(ts[11:13])] += 1

    for w in watched_urls:
        cat_counter[_infer_category(str(w.get("title") or ""))] += 2
    for kw in watched_kws:
        cat_counter[_infer_category(str(kw.get("query") or ""))] += 2

    top_stores = store_counter.most_common(5)
    top_categories = cat_counter.most_common(5)
    peak_hours = hour_counter.most_common(4)

    # En yoğun saatlerden otomatik Turbo penceresi önerileri üret
    rec_windows: list[str] = []
    for hr, _ in sorted(peak_hours, key=lambda x: x[0]):
        start_h = hr
        end_h = (hr + 1) % 24
        win = f"{start_h:02d}:00-{end_h:02d}:59"
        if win not in rec_windows:
            rec_windows.append(win)
    if not rec_windows:
        rec_windows = list(config.turbo_windows)

    recs: list[str] = []
    if top_categories:
        fav_cat, fav_cnt = top_categories[0]
        recs.append(
            f"En yüksek ilgi ve fırsat yoğunluğu '{fav_cat}' kategorisinde ({fav_cnt} kayıt); "
            f"kanal paylaşımlarında ve kelime takiplerinde bu kategoriye ağırlık verilmesi önerilir."
        )
    if top_stores:
        best_store, _ = top_stores[0]
        recs.append(
            f"Gerçek indirimlerin en sık yakalandığı mağaza: {best_store}. "
            f"Affiliate/ortaklık etiketinin bu mağazada güncel tutulması kazancı artırır."
        )
    if peak_hours:
        hr_str = ", ".join(f"{h:02d}:00 ({c})" for h, c in peak_hours[:3])
        recs.append(
            f"Verilere göre en çok indirim yakalanan saatler: {hr_str}. "
            f"Bu saatlerde Turbo tarama (3 dk) aktif tutulmalıdır."
        )
    if brackets["<500 TL"] > brackets["2.000-10.000 TL"] + brackets["10.000+ TL"]:
        recs.append(
            "Düşük fiyatlı ürün oranı yüksek; yüksek komisyon ve net kazanç için 1.000 TL+ elektronik/ev aletleri "
            "kategorilerinde %25+ fırsatları öne çıkarmak daha verimli olur."
        )
    if not watched_kws:
        recs.append(
            "Henüz özel kelime takibi eklenmemiş. En çok aranan 3-5 ürünü (ör. 'AirPods', 'Roborock', 'Stanley') "
            "kelime takibine eklemek anlık dip fiyat bildirimlerini hızlandırır."
        )
    if not recs:
        recs.append("Sistem şu an tüm kategorilerde %25+ indirim ve geçmiş fiyat doğrulamasıyla dengeli çalışıyor.")

    return HabitReport(
        total_published_deals=len(pub_rows),
        total_tracked_bestsellers=len(bs_rows),
        total_watched_urls=len(watched_urls),
        total_watched_keywords=len(watched_kws),
        top_stores=top_stores,
        top_categories=top_categories,
        peak_hours=peak_hours,
        price_brackets=brackets,
        recommended_turbo_windows=rec_windows,
        recommendations=recs,
    )


def apply_habit_recommendations(config: AppConfig, report: HabitReport) -> list[str]:
    """Öğrenilen en yoğun alışveriş saatlerini Turbo tarama takvimine otomatik entegre eder."""
    merged = list(config.turbo_windows)
    added: list[str] = []
    for win in report.recommended_turbo_windows:
        if win not in merged and len(merged) < 6:
            merged.append(win)
            added.append(win)
    if added:
        config.turbo_windows = merged
        sch = config.raw_yaml.setdefault("schedule", {})
        sch["turbo_windows"] = merged
        config.persist_yaml()
    return added


def format_habit_report_html(report: HabitReport) -> str:
    stores_txt = ", ".join(f"{html.escape(s)} ({c})" for s, c in report.top_stores) or "-"
    cats_txt = ", ".join(f"{html.escape(k)} ({c})" for k, c in report.top_categories) or "-"
    hours_txt = ", ".join(f"{h:02d}:00 ({c})" for h, c in report.peak_hours) or "-"
    rec_lines = "\n".join(f"💡 {html.escape(r)}" for r in report.recommendations)
    return (
        "🧠 <b>ALIŞVERİŞ ALIŞKANLIĞI &amp; SİSTEM GELİŞİM RAPORU</b>\n\n"
        f"📊 <b>Özet:</b> {report.total_published_deals} yayınlanan fırsat • "
        f"{report.total_tracked_bestsellers} çok satan takip • "
        f"{report.total_watched_urls} link • {report.total_watched_keywords} kelime\n"
        f"🏪 <b>Öne Çıkan Mağazalar:</b> {stores_txt}\n"
        f"📂 <b>Popüler Kategoriler:</b> {cats_txt}\n"
        f"⏰ <b>En Verimli Saatler:</b> {hours_txt}\n\n"
        f"<b>Akıllı Öneriler:</b>\n{rec_lines}"
    )

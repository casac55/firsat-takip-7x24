from __future__ import annotations

import csv
import io
from src.storage import PriceStorage


def generate_csv_report_bytes(storage: PriceStorage, limit: int = 1500) -> bytes:
    """
    Tüm S25 Ultra fiyat geçmişini ve özel takip edilen ürün linklerini
    Türkçe Excel uyumlu (UTF-8-BOM, noktalı virgül ayraçlı) CSV olarak dışa aktarır.
    """
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")

    writer.writerow([
        "Tarih/Saat",
        "Varyant",
        "Renk",
        "Mağaza/Satıcı",
        "Resmi Satıcı Mı?",
        "Liste Fiyatı (TL)",
        "Net Kuponlu Fiyat (TL)",
        "Kupon Bilgisi",
        "Kupon Kodu",
        "Kaynak",
        "Ürün Başlığı",
        "Doğrudan Ürün Linki",
    ])

    for r in storage.get_recent_snapshots_for_export(limit=limit):
        writer.writerow([
            r.get("scraped_at", ""),
            r.get("variant", ""),
            r.get("color", ""),
            r.get("seller", ""),
            "Evet" if r.get("is_official_seller") else "Hayır",
            r.get("price_tl", ""),
            r.get("effective_price_tl", ""),
            r.get("coupon_info", ""),
            r.get("coupon_code", ""),
            r.get("source", ""),
            r.get("title", ""),
            r.get("url", ""),
        ])

    watched = storage.list_watched_urls(active_only=False)
    if watched:
        writer.writerow([])
        writer.writerow(["--- TAKİP EDİLEN ÖZEL ÜRÜN LİNKLERİ ---"])
        writer.writerow([
            "ID",
            "Durum",
            "Mağaza",
            "Güncel Fiyat (TL)",
            "En Düşük Fiyat (TL)",
            "Hedef Fiyat (TL)",
            "Stok Durumu",
            "Son Kontrol",
            "Ürün Başlığı",
            "Link",
        ])
        for w in watched:
            writer.writerow([
                w.get("id", ""),
                "Aktif" if w.get("active") else "Silindi",
                w.get("seller", ""),
                w.get("last_price_tl", ""),
                w.get("min_price_tl", ""),
                w.get("target_price_tl") or "",
                "Stokta" if w.get("in_stock", 1) else "Tükendi / Stok Bekleniyor",
                w.get("last_checked_at", ""),
                w.get("title", ""),
                w.get("url", ""),
            ])

    # utf-8-sig ile Excel Türkçe karakterleri bozmadan doğrudan açar
    return buf.getvalue().encode("utf-8-sig")

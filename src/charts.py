from __future__ import annotations

import io
from typing import Optional

from PIL import Image, ImageDraw, ImageFont


def render_price_chart_png(
    *,
    title: str,
    points: list[tuple[str, float]],
    target_price_tl: Optional[float] = None,
    six_month_low_tl: Optional[float] = None,
) -> bytes:
    """
    Fiyat geçmişi noktalarından [(tarih_etiketi, fiyat_tl), ...] koyu temalı
    görsel PNG grafik üretir.
    """
    width, height = 920, 480
    bg_color = (16, 20, 28)
    card_color = (24, 30, 42)
    grid_color = (45, 55, 72)
    text_color = (235, 240, 248)
    sub_color = (160, 174, 192)
    line_color = (56, 189, 248)      # Cyan / Mavi
    point_color = (14, 165, 233)
    min_color = (34, 197, 94)        # Yeşil (en düşük)
    target_color = (244, 63, 94)     # Kırmızı/Pembe (hedef)
    six_m_color = (250, 204, 21)     # Sarı (6 aylık taban)

    img = Image.new("RGB", (width, height), bg_color)
    draw = ImageDraw.Draw(img)

    font_title = _load_font(20, bold=True)
    font_body = _load_font(14)
    font_small = _load_font(12)

    # Çerçeve
    draw.rounded_rectangle((16, 16, width - 16, height - 16), radius=14, fill=card_color)
    draw.text((36, 30), title[:62], fill=text_color, font=font_title)

    if not points:
        draw.text((36, 220), "Henüz grafik çizmek için yeterli fiyat verisi yok.", fill=sub_color, font=font_body)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    prices = [p[1] for p in points]
    min_p = min(prices)
    max_p = max(prices)
    cur_p = prices[-1]

    # İstatistik üst barı
    stats_text = (
        f"Güncel: {_fmt_tl(cur_p)}   |   En Düşük: {_fmt_tl(min_p)}   |   En Yüksek: {_fmt_tl(max_p)}"
    )
    if target_price_tl:
        stats_text += f"   |   Hedef: {_fmt_tl(target_price_tl)}"
    draw.text((36, 62), stats_text, fill=sub_color, font=font_body)

    # Grafik çizim alanı
    left, top, right, bottom = 105, 105, width - 45, height - 65
    plot_w = right - left
    plot_h = bottom - top

    all_vals = list(prices)
    if target_price_tl and target_price_tl > 0:
        all_vals.append(target_price_tl)
    if six_month_low_tl and six_month_low_tl > 0:
        all_vals.append(six_month_low_tl)

    y_min = min(all_vals)
    y_max = max(all_vals)
    spread = max(500.0, y_max - y_min)
    y_low = max(0.0, y_min - spread * 0.15)
    y_high = y_max + spread * 0.15

    def to_y(val: float) -> int:
        ratio = (val - y_low) / max(1.0, (y_high - y_low))
        return int(bottom - ratio * plot_h)

    def to_x(idx: int, total: int) -> int:
        if total <= 1:
            return left + plot_w // 2
        return int(left + (idx / (total - 1)) * plot_w)

    # Yatay ızgara çizgileri (5 kademe)
    for step in range(5):
        val = y_low + (y_high - y_low) * (step / 4.0)
        yy = to_y(val)
        draw.line((left, yy, right, yy), fill=grid_color, width=1)
        draw.text((26, yy - 7), f"{int(round(val)):,}".replace(",", "."), fill=sub_color, font=font_small)

    # 6 aylık taban çizgisi
    if six_month_low_tl and six_month_low_tl > 0:
        y6 = to_y(six_month_low_tl)
        if top <= y6 <= bottom:
            _draw_dashed_line(draw, left, y6, right, y6, six_m_color)
            draw.text((right - 175, y6 - 16), f"6 Ay Dip: {_fmt_tl(six_month_low_tl)}", fill=six_m_color, font=font_small)

    # Hedef fiyat çizgisi
    if target_price_tl and target_price_tl > 0:
        yt = to_y(target_price_tl)
        if top <= yt <= bottom:
            _draw_dashed_line(draw, left, yt, right, yt, target_color)
            draw.text((left + 8, yt - 16), f"Hedef: {_fmt_tl(target_price_tl)}", fill=target_color, font=font_small)

    # Fiyat eğrisi koordinatları
    coords = [(to_x(i, len(points)), to_y(p[1])) for i, p in enumerate(points)]
    if len(coords) >= 2:
        draw.line(coords, fill=line_color, width=3)

    for i, (cx, cy) in enumerate(coords):
        val = points[i][1]
        c = min_color if abs(val - min_p) < 0.01 else point_color
        r = 5 if abs(val - min_p) < 0.01 or i == len(coords) - 1 else 3
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=c)

    # X ekseni tarih etiketleri (ilk, orta, son)
    indices = {0, len(points) // 2, len(points) - 1}
    for idx in sorted(indices):
        cx = to_x(idx, len(points))
        lbl = points[idx][0]
        draw.text((max(left, min(cx - 35, right - 75)), bottom + 12), lbl, fill=sub_color, font=font_small)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _draw_dashed_line(draw: ImageDraw.ImageDraw, x1: int, y1: int, x2: int, y2: int, color: tuple[int, int, int]) -> None:
    dash = 10
    gap = 6
    x = x1
    while x < x2:
        draw.line((x, y1, min(x + dash, x2), y2), fill=color, width=2)
        x += dash + gap


def _fmt_tl(val: float) -> str:
    return f"{int(round(val)):,} TL".replace(",", ".")


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        ["C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf"]
        if bold
        else ["C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf"]
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except Exception:
            continue
    return ImageFont.load_default()

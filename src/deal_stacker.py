from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


@dataclass(slots=True)
class StackResult:
    base_price_tl: float
    after_deal_and_coupon_tl: float
    bank_name: str
    bank_bonus_tl: float
    net_final_tl: float
    total_savings_tl: float
    total_savings_pct: int
    summary_line: str


@dataclass(slots=True)
class MultiBuyInfo:
    promo_label: str
    unit_count: int
    effective_total_tl: float
    unit_price_tl: float
    effective_discount_pct: int
    summary_line: str


def _fmt_tl(val: float) -> str:
    return f"{int(round(val)):,} TL".replace(",", ".") if abs(val - round(val)) < 0.01 else f"{val:,.2f} TL".replace(",", "X").replace(".", ",").replace("X", ".")


def is_price_glitch(current_price_tl: float, median_30d_tl: Optional[float]) -> tuple[bool, int]:
    """Son 30 gün medyanına göre %60 veya daha fazla ani düşüş varsa fiyat hatası (glitch) olarak işaretler."""
    if not median_30d_tl or median_30d_tl <= 0 or current_price_tl <= 0:
        return False, 0
    if current_price_tl >= median_30d_tl:
        return False, 0
    drop_pct = int(round(((median_30d_tl - current_price_tl) / median_30d_tl) * 100.0))
    return drop_pct >= 60, drop_pct


def calculate_multibuy_unit_price(title: str, price_tl: float) -> Optional[MultiBuyInfo]:
    """
    2 Al 1 Öde, 1 Alana 1 Bedava, 3 Al 2 Öde, 2. ürüne %50, Sepette %50 ve çoklu paketlerde
    adet başı gerçek birim fiyatı hesaplar.
    """
    if price_tl <= 0 or not title:
        return None
    low = title.lower()

    # 1) 1 Alana 1 Bedava / 2 Al 1 Öde
    if re.search(r"(1\s*alana\s*1\s*bedava|2\s*al\s*1\s*öde|2\s*al\s*1\s*ode)", low):
        unit_p = round(price_tl / 2.0, 2)
        return MultiBuyInfo(
            promo_label="2 Al 1 Öde / 1+1",
            unit_count=2,
            effective_total_tl=price_tl,
            unit_price_tl=unit_p,
            effective_discount_pct=50,
            summary_line=f"Çoklu Alım (2 Al 1 Öde): 2 adet toplam {_fmt_tl(price_tl)} → Adet başı net {_fmt_tl(unit_p)} (-%50)",
        )

    # 2) 3 Al 2 Öde
    if re.search(r"3\s*al\s*2\s*öde|3\s*al\s*2\s*ode", low):
        eff_total = round(price_tl * 2.0, 2)
        unit_p = round(eff_total / 3.0, 2)
        return MultiBuyInfo(
            promo_label="3 Al 2 Öde",
            unit_count=3,
            effective_total_tl=eff_total,
            unit_price_tl=unit_p,
            effective_discount_pct=33,
            summary_line=f"Çoklu Alım (3 Al 2 Öde): 3 adet toplam {_fmt_tl(eff_total)} → Adet başı net {_fmt_tl(unit_p)} (-%33)",
        )

    # 3) 2. ürüne %X indirim
    m2 = re.search(r"2\.?\s*(?:ürüne|urune|ürün|urun)\s*%\s*(\d{1,2})", low)
    if m2:
        pct2 = min(90, max(10, int(m2.group(1))))
        eff_total = round(price_tl + price_tl * (1.0 - pct2 / 100.0), 2)
        unit_p = round(eff_total / 2.0, 2)
        eff_pct = int(round((pct2 / 2.0)))
        return MultiBuyInfo(
            promo_label=f"2. Ürüne %{pct2}",
            unit_count=2,
            effective_total_tl=eff_total,
            unit_price_tl=unit_p,
            effective_discount_pct=eff_pct,
            summary_line=f"Çoklu Alım (2. Ürüne %{pct2}): Adet başı net {_fmt_tl(unit_p)} (-%{eff_pct})",
        )

    # 4) Sepette %X ek indirim (başlıkta geçiyorsa)
    ms = re.search(r"sepette\s*(?:ek\s*)?%\s*(\d{1,2})", low)
    if ms:
        sp = min(70, max(5, int(ms.group(1))))
        net_p = round(price_tl * (1.0 - sp / 100.0), 2)
        return MultiBuyInfo(
            promo_label=f"Sepette %{sp}",
            unit_count=1,
            effective_total_tl=net_p,
            unit_price_tl=net_p,
            effective_discount_pct=sp,
            summary_line=f"Sepette %{sp} İndirimle Net: {_fmt_tl(net_p)}",
        )

    # 5) Çoklu paket (ör. 10'lu Paket, 24 Adet)
    mp = re.search(r"\b(\d{1,3})\s*(?:'?[lıiuü]{2}\s*paket|adet)\b", low)
    if mp:
        cnt = int(mp.group(1))
        if 2 <= cnt <= 200:
            unit_p = round(price_tl / cnt, 2)
            return MultiBuyInfo(
                promo_label=f"{cnt}'li Paket",
                unit_count=cnt,
                effective_total_tl=price_tl,
                unit_price_tl=unit_p,
                effective_discount_pct=0,
                summary_line=f"Paket Birim Fiyatı ({cnt} adet): Adet başı {_fmt_tl(unit_p)}",
            )

    return None


def compute_best_stack(
    *,
    seller: str,
    title: str,
    list_price_tl: Optional[float],
    effective_price_tl: float,
    coupon_discount_tl: float = 0.0,
    coupon_code: str = "",
    coupon_info: str = "",
) -> Optional[StackResult]:
    """
    Mağaza indirimi + kupon kodu + uygun banka puan/iade kampanyasını eşleştirerek
    net son fiyatı ve toplam kazancı hesaplar.
    """
    if effective_price_tl <= 0:
        return None

    base = list_price_tl if (list_price_tl and list_price_tl > effective_price_tl) else effective_price_tl
    after_coupon = effective_price_tl

    # Türkiye e-ticaret banka puan/iade kademeleri (Garanti Bonus / İş Maximum / Yapı Kredi World)
    bank_name = ""
    bank_bonus = 0.0
    low_seller = (seller or "").lower()
    is_major_ecom = any(
        s in low_seller
        for s in ("amazon", "trendyol", "hepsiburada", "mediamarkt", "teknosa", "vatan", "n11", "pazarama", "idefix", "migros", "gratis", "watsons", "mavi")
    )
    if is_major_ecom:
        if after_coupon >= 10000:
            bank_name = "Maximum / Bonus / World (E-Ticaret Puan)"
            bank_bonus = 500.0
        elif after_coupon >= 5000:
            bank_name = "Garanti Bonus / Maximum (E-Ticaret Puan)"
            bank_bonus = 300.0
        elif after_coupon >= 2000:
            bank_name = "Bonus / World / Axess (E-Ticaret Puan)"
            bank_bonus = 150.0

    net_final = round(max(1.0, after_coupon - bank_bonus), 2)
    total_sav = round(max(0.0, base - net_final), 2)
    has_extra_layer = bool(bank_bonus > 0 or coupon_code or coupon_discount_tl > 0 or coupon_info)
    if total_sav <= 0 or not has_extra_layer:
        return None

    total_pct = min(95, max(1, int(round((total_sav / base) * 100.0)))) if base > 0 else 0
    steps: list[str] = []
    step_no = 1

    has_coupon_step = bool(coupon_code or coupon_discount_tl > 0 or coupon_info)
    pre_coupon_price = round(after_coupon + coupon_discount_tl, 2) if coupon_discount_tl > 0 else after_coupon

    if has_coupon_step and coupon_discount_tl <= 0:
        # after_coupon zaten kupon düşülmüş son sepet fiyatıysa 1. adımda normal/indirimli ürün sayfasına gitmeyi anlat
        steps.append(
            f"{step_no}️⃣ Ürün Sayfası: Normal fiyatı {_fmt_tl(base)} olan ürün sayfasına gidin."
            if base > after_coupon
            else f"{step_no}️⃣ Ürün Sayfası: Ürün sayfasına gidin."
        )
        step_no += 1
    elif base > pre_coupon_price:
        steps.append(f"{step_no}️⃣ Mağaza İndirimi: {_fmt_tl(base)} yerine indirimli {_fmt_tl(pre_coupon_price)} fiyatla sepete ekleyin.")
        step_no += 1
    else:
        steps.append(f"{step_no}️⃣ Sepet Adımı: Ürünü {_fmt_tl(pre_coupon_price)} fiyatla sepete ekleyin.")
        step_no += 1

    if has_coupon_step:
        if coupon_code:
            steps.append(f'{step_no}️⃣ Kupon Adımı: Sepette "{coupon_code}" kupon kodunu uygulayın → Fiyat {_fmt_tl(after_coupon)}\'ye düşer.')
        elif coupon_info:
            steps.append(f"{step_no}️⃣ Kupon Adımı: {coupon_info} → Fiyat {_fmt_tl(after_coupon)}'ye düşer.")
        else:
            steps.append(f"{step_no}️⃣ Kupon Adımı: Sepette mağaza kuponunu aktif edin → Fiyat {_fmt_tl(after_coupon)}'ye düşer.")
        step_no += 1

    if bank_bonus > 0:
        b_short = bank_name.split("(")[0].strip()
        steps.append(
            f"{step_no}️⃣ Banka Adımı: Ödeme öncesi banka uygulamasından ({b_short} / BonusFlaş / Juzdan / World Mobil) "
            f"e-ticaret kampanyasına katılıp kartla ödeyin → -{_fmt_tl(bank_bonus)} puan/iade kazanın."
        )

    steps.append(f"🎯 Net Maliyet: {_fmt_tl(net_final)} (Toplam {_fmt_tl(total_sav)} • -%{total_pct} kazanç!)")
    summary = "\n".join(steps)

    return StackResult(
        base_price_tl=base,
        after_deal_and_coupon_tl=after_coupon,
        bank_name=bank_name,
        bank_bonus_tl=bank_bonus,
        net_final_tl=net_final,
        total_savings_tl=total_sav,
        total_savings_pct=total_pct,
        summary_line=summary,
    )


def compute_deal_confidence(
    *,
    post_type: str,
    title: str,
    price_tl: float = 0.0,
    prev_price_tl: Optional[float] = None,
    discount_pct: int = 0,
    coupon_code: str = "",
    is_official_seller: bool = False,
) -> tuple[int, str]:
    """
    Gönderinin güven skorunu (0-100) hesaplar:
    - 70 ve üzeri: Doğrudan @firsat_takibi kanalına yayınlanabilir (Yüksek Güven)
    - 70 altı: Önce yönetici onayına (Telegram özel mesaj + Web Panel) düşer
    """
    score = 50
    reasons: list[str] = []

    if post_type in ("popular", "bestseller", "s25"):
        score += 15
        if price_tl >= 100 and prev_price_tl and prev_price_tl > price_tl:
            score += 20
            reasons.append("doğrulanmış eski fiyat")
        if discount_pct >= 25:
            score += 10
            reasons.append(f"-%{discount_pct} indirim")
        if is_official_seller:
            score += 5
            reasons.append("resmi satıcı")
    elif post_type in ("influencer", "gamified", "community"):
        score = 55
        if post_type == "community" and coupon_code and len(coupon_code) >= 6:
            score += 12
        if coupon_code and len(coupon_code) >= 5 and any(c.isdigit() for c in coupon_code):
            score += 8
            reasons.append(f"doğrulanmış kupon: {coupon_code}")
        if price_tl >= 100:
            score += 8
            reasons.append("fiyat bilgili")

    score = max(10, min(100, score))
    label = "Yüksek Güven (Otomatik Yayın)" if score >= 70 else "Onay Gerektirir (Önce Yöneticiye Sor)"
    if reasons:
        label += f" [{', '.join(reasons)}]"
    return score, label

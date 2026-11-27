from __future__ import annotations

import re
from typing import Optional


# Türkçe / İngilizce başlık normalizasyonu için küçük harf dönüşümü
def normalize_tr(text: str) -> str:
    replacements = str.maketrans({
        "İ": "i",
        "I": "ı",
        "Ş": "ş",
        "Ğ": "ğ",
        "Ü": "ü",
        "Ö": "ö",
        "Ç": "ç",
    })
    return text.translate(replacements).lower()


_ACCESSORY_KEYWORDS = (
    "kilif", "kılıf", "case", "kapak", "ekran koruyucu", "koruyucu cam",
    "temperli", "tamperli", "hayalet cam", "sarj", "şarj", "adaptor",
    "adaptör", "kablo", "powerbank", "kalem", "s pen", "s-pen", "spen",
    "kamera lens", "lens koruyucu", "stand", "tutucu", "kordon", "batarya",
    "pil ", "ekran degisimi", "ekran değişimi", "anakart", "kasa ",
    "arka kapak", "sim tepsisi", "kulaklik", "kulaklık", "magsafe",
    "halka", "sticker", "skin", "cüzdan", "cuzdan", "folio", "bumper",
    "oyuncak", "maket", "dummy",
)

_OTHER_MODEL_PATTERNS = (
    r"\bs25\s*\+",
    r"\bs25\s*plus\b",
    r"\bs25\s*edge\b",
    r"\bs25\s*fe\b",
    r"\bs24\b",
    r"\bs23\b",
    r"\bs22\b",
    r"\bs26\b",
    r"\ba55\b",
    r"\ba56\b",
    r"\ba35\b",
    r"\ba36\b",
    r"\bz\s*fold",
    r"\bz\s*flip",
)

_REFURB_KEYWORDS = (
    "yenilenmis", "yenilenmiş", "refurbished", "2. el", "2.el",
    "ikinci el", "teshir", "teşhir", "outlet", "hasarli", "hasarlı",
    "yurt disi", "yurt dışı", "yurtdisi", "yurtdışı", "kayitsiz",
    "kayıtsız", "swap", "demo cihaz", "demo ürün", "demo urun",
)

_S25_ULTRA_RE = re.compile(r"(?:\bs25\s*ultra\b|\bsm[-\s]?s938\w*\b)", re.IGNORECASE)
_VARIANT_256_RE = re.compile(r"\b256\s*(?:gb|g)\b", re.IGNORECASE)
_VARIANT_512_RE = re.compile(r"\b512\s*(?:gb|g)\b", re.IGNORECASE)
_VARIANT_1TB_RE = re.compile(r"\b(?:1\s*tb|1024\s*gb)\b", re.IGNORECASE)


def detect_variant(text: str, default: Optional[str] = None) -> Optional[str]:
    """Başlık veya metinden depolama kapasitesini çıkarır."""
    if not text:
        return default
    if _VARIANT_1TB_RE.search(text):
        return "1 TB"
    if _VARIANT_512_RE.search(text):
        return "512 GB"
    if _VARIANT_256_RE.search(text):
        return "256 GB"
    return default


def parse_price_tl(raw: str | int | float | None) -> Optional[float]:
    """Türkçe ve uluslararası fiyat stringlerini TL float değerine çevirir."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        val = float(raw)
        return val if val > 0 else None

    s = str(raw).strip()
    if not s:
        return None

    # İlk fiyat benzeri sayı bloğunu yakala (örn. "79.999,00 TL" -> "79.999,00")
    match = re.search(r"\d[\d\.\,\s]*\d|\d+", s)
    if not match:
        return None

    num_str = re.sub(r"\s+", "", match.group(0))

    # Hem nokta hem virgül varsa
    if "." in num_str and "," in num_str:
        if num_str.rfind(",") > num_str.rfind("."):
            # TR format: 79.999,50
            num_str = num_str.replace(".", "").replace(",", ".")
        else:
            # US format: 79,999.50
            num_str = num_str.replace(",", "")
    elif "," in num_str:
        parts = num_str.split(",")
        # Son kısım 2 hane ise ondalık (79999,50), 3 hane ise binlik (79,999)
        if len(parts[-1]) <= 2:
            num_str = num_str.replace(",", ".")
        else:
            num_str = num_str.replace(",", "")
    elif "." in num_str:
        parts = num_str.split(".")
        # 79.999 -> binlik ayracı; 79999.50 -> ondalık
        if len(parts[-1]) == 3 and all(len(p) <= 3 for p in parts):
            num_str = num_str.replace(".", "")

    try:
        val = float(num_str)
        return round(val, 2) if val > 0 else None
    except ValueError:
        return None


_EXPLICIT_TL_RE = re.compile(
    r"(?:^|[\s\:\(\-])(\d{1,3}(?:[\.\s]\d{3})*(?:,\d{1,2})?|\d{2,6}(?:,\d{1,2})?)\s*(?:TL|₺)\b",
    re.IGNORECASE,
)


def parse_explicit_tl_price(text: str) -> Optional[float]:
    """
    Yalnızca yanında açıkça 'TL' veya '₺' birimi bulunan gerçek fiyatları çıkarır.
    Model/ürün kodlarını (örn. '12283517') asla fiyat sanmaz.
    """
    if not text:
        return None
    for m in _EXPLICIT_TL_RE.finditer(text):
        val = parse_price_tl(m.group(1))
        if val is not None and 10.0 <= val <= 300000.0:
            return val
    return None


def is_valid_s25_ultra_phone(
    title: str,
    price_tl: Optional[float],
    min_price_tl: float = 45000.0,
    max_price_tl: float = 160000.0,
    exclude_refurbished: bool = True,
) -> bool:
    """Başlığın gerçek bir Samsung Galaxy S25 Ultra telefonu olup olmadığını doğrular."""
    if not title or price_tl is None:
        return False

    if price_tl < min_price_tl or price_tl > max_price_tl:
        return False

    norm = normalize_tr(title)

    # S25 Ultra ifadesi mutlaka olmalı
    if not _S25_ULTRA_RE.search(norm):
        return False

    # Aksesuar / yan ürün elemesi
    for kw in _ACCESSORY_KEYWORDS:
        if kw in norm:
            return False

    # Başka model numaraları varsa (S25+, S24 vb. - genelde çoklu uyumlu aksesuar) ele
    for pat in _OTHER_MODEL_PATTERNS:
        if re.search(pat, norm):
            return False

    # Yenilenmiş / 2.el / yurtdışı elemesi
    if exclude_refurbished:
        for kw in _REFURB_KEYWORDS:
            if kw in norm:
                return False

    return True


def normalize_seller_name(seller: str, fallback_source: str) -> str:
    """Mağaza / satıcı adını temiz ve tutarlı hale getirir."""
    raw = (seller or "").strip()
    if not raw:
        raw = fallback_source

    low = normalize_tr(raw)
    mapping = {
        "amazon": "Amazon.com.tr",
        "amazon.com.tr": "Amazon.com.tr",
        "hepsiburada": "Hepsiburada",
        "trendyol": "Trendyol",
        "mediamarkt": "MediaMarkt",
        "media markt": "MediaMarkt",
        "vatan": "Vatan Bilgisayar",
        "vatan bilgisayar": "Vatan Bilgisayar",
        "teknosa": "Teknosa",
        "samsung": "Samsung Türkiye",
        "samsung türkiye": "Samsung Türkiye",
        "n11": "N11",
        "pazarama": "Pazarama",
        "pttavm": "PttAVM",
        "idefix": "İdefix",
        "ciceksepeti": "ÇiçekSepeti",
        "çiçeksepeti": "ÇiçekSepeti",
        "turkcell": "Turkcell Pasaj",
        "pasaj": "Turkcell Pasaj",
        "vodafone": "Vodafone",
    }
    for key, canonical in mapping.items():
        if key == low or (len(key) >= 4 and key in low):
            return canonical
    return raw[:60]


_SEPETTE_PRICE_RE = re.compile(
    r"sepette\s*(?:fiyat)?\s*[:\-]?\s*(\d{2,3}(?:\.\d{3})*(?:,\d{1,2})?)\s*(?:tl|₺)",
    re.IGNORECASE,
)
_COUPON_TL_RE = re.compile(
    r"(\d+(?:\.\d{3})*(?:,\d{1,2})?)\s*(?:tl|₺)\s*(?:ek\s*)?(?:indirim|kupon)",
    re.IGNORECASE,
)
_COUPON_PCT_RE = re.compile(
    r"(?:sepette|kupon(?:la)?)?\s*%\s*(\d{1,2})\s*(?:indirim|kupon)",
    re.IGNORECASE,
)
_COUPON_CODE_PATTERNS = (
    re.compile(
        r"[\"'\u201c\u201d]([A-Z0-9]{4,20})[\"'\u201c\u201d]\s*(?:kupon|indirim)?\s*kodu(?:yla)?",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:kupon|indirim|promosyon|kampanya)?\s*kodu\s*[:\-]?\s*[\"'\u201c\u201d]?([A-Z0-9ÇĞİÖŞÜ]{3,20})[\"'\u201c\u201d]?",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bkod\s*[:\-]\s*[\"'\u201c\u201d]?([A-Z0-9]{4,20})[\"'\u201c\u201d]?",
        re.IGNORECASE,
    ),
)
_IGNORED_CODE_WORDS = {
    "KUPON", "INDIRIM", "İNDİRİM", "SEPETTE", "FIRSAT", "FİYAT", "FIYAT",
    "KODU", "KODUYLA", "YOK", "VAR", "ICIN", "İÇİN", "OZEL", "ÖZEL", "ILE", "İLE", "VE",
    "YENI", "YENİ", "AKTIF", "AKTİF", "TUM", "TÜM", "HEMEN", "SADECE", "BUGUN", "BUGÜN",
    "GECERLI", "GEÇERLİ", "KADAR", "EKSTRA", "SUPER", "SÜPER", "SON", "ILK", "İLK",
    "KOPYALAYIN", "KOPYALA", "KULLANIN", "SECILI", "SEÇİLİ", "UYELER", "ÜYELER", "LIRAKOD",
    "SAMSUNG", "ULTRA", "GALAXY", "TELEFON", "KARGO", "BEDAVA", "AMAZON", "TRENDYOL",
    "ISTEYEN", "PAYLASIMI", "PARMAK", "KAMPANYA", "SAATLERI", "VERILECEK", "BITTI", "TANITIM",
    "SPONSORLU", "ICERIK", "MOBIL", "UYGULAMA",
}
_ALNUM_COUPON_RE = re.compile(r"\b([A-Z]{2,14}[0-9]{2,6}|[0-9]{1,4}[A-Z]{3,14}|[A-Z0-9]{5,18})\b")
_PRICE_OR_UNIT_TOKEN_RE = re.compile(r"^\d+(?:TL|TRY|USD|EUR|GB|TB|MB|ML|KG|GR|MM|CM|ADET|INÇ|INC|W|HZ)$", re.IGNORECASE)


def _is_valid_coupon_candidate(cand: str) -> bool:
    if len(cand) < 4 or cand.isdigit():
        return False
    # Gerçek e-ticaret kupon kodları ASCII harf/rakam olur (İSTEYEN, PAYLAŞIMI gibi Türkçe kelimeleri ele)
    if not cand.isascii():
        return False
    if cand in _IGNORED_CODE_WORDS:
        return False
    if _PRICE_OR_UNIT_TOKEN_RE.match(cand):
        return False
    if re.match(r"^(?:1080P|2160P|720P|4K|5G|80PLUS|USB\d|DDR\d|HDMI\d)$", cand):
        return False
    return True


def extract_coupon_code(text: str) -> str:
    """Metin içinden kupon kodunu (örn. 'PERSEMBE1500', '75AMZN', 'KUPON2000') çıkarır."""
    if not text:
        return ""
    for idx, pat in enumerate(_COUPON_CODE_PATTERNS):
        for m in pat.finditer(text):
            raw_cand = m.group(1).strip()
            # Tırnaksız eşleşmelerde küçük harfli Türkçe kelimeleri ("kopyalayın", "ile") ele
            if idx > 0 and raw_cand != raw_cand.upper() and not any(c.isdigit() for c in raw_cand):
                continue
            candidate = raw_cand.upper()
            if _is_valid_coupon_candidate(candidate):
                return candidate
    # Büyük harf + rakam birleşimli klasik kodları yakala (ör. PERSEMBE1500, 75AMZN)
    if re.search(r"(kupon|kod|indirim|promosyon|sepet|ödeme)", text, re.IGNORECASE):
        for m in _ALNUM_COUPON_RE.finditer(text):
            cand = m.group(1).strip().upper()
            if _is_valid_coupon_candidate(cand) and any(c.isalpha() for c in cand) and any(c.isdigit() for c in cand):
                return cand
    return ""


def extract_coupon_info(text: str, base_price_tl: float) -> tuple[float, str, str]:
    """
    Metin içinden kupon/sepet indirimi tutarını, açıklamasını ve kupon kodunu çıkarır.
    Dönüş: (indirim_tl, açıklama_etiketi, kupon_kodu)
    """
    if not text or base_price_tl <= 0:
        return 0.0, "", ""

    best_discount = 0.0
    best_label = ""
    code = extract_coupon_code(text)

    # 1) "Sepette 67.999 TL" gibi doğrudan sepette nihai fiyat
    for m in _SEPETTE_PRICE_RE.finditer(text):
        sep_price = parse_price_tl(m.group(1))
        if sep_price and (base_price_tl * 0.5) <= sep_price < base_price_tl:
            diff = round(base_price_tl - sep_price, 2)
            if diff > best_discount:
                best_discount = diff
                best_label = f"Sepette {m.group(1)} TL"

    # 2) "2.000 TL kupon" / "1.500 TL indirim"
    for m in _COUPON_TL_RE.finditer(text):
        disc = parse_price_tl(m.group(1))
        if disc and 50.0 <= disc <= base_price_tl * 0.45:
            if disc > best_discount:
                best_discount = disc
                best_label = f"{m.group(1)} TL Kupon/İndirim"

    # 3) "%5 kupon" / "sepette %10 indirim"
    for m in _COUPON_PCT_RE.finditer(text):
        pct = int(m.group(1))
        if 1 <= pct <= 45:
            disc = round(base_price_tl * pct / 100.0, 2)
            if disc > best_discount:
                best_discount = disc
                best_label = f"%{pct} Kupon/Sepet İndirimi"

    if code and not best_label:
        best_label = "Kupon Fırsatı"

    return best_discount, best_label, code


_COLOR_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Titanyum Siyah", ("titanyum siyah", "titanium black", "siyah", "black")),
    ("Titanyum Gri", ("titanyum gri", "titanium gray", "titanium grey", "gri", "gray", "grey")),
    ("Titanyum Mavi", ("titanyum mavi", "titanium blue", "buz mavisi", "mavi", "blue")),
    ("Titanyum Gümüş", ("titanyum gümüş", "titanyum gumus", "titanium silver", "gümüş", "gumus", "silver", "beyaz", "white")),
    ("Titanyum Yeşil", ("titanyum yeşil", "titanyum yesil", "titanium green", "jade", "yeşil", "yesil", "green")),
    ("Titanyum Altın/Pembe", ("titanyum pembe", "titanyum altın", "rose", "pink", "gold", "pembe", "altın", "altin")),
)


def extract_color(title: str) -> str:
    """Ürün başlığından Galaxy S25 Ultra rengini tespit eder."""
    if not title:
        return ""
    norm = normalize_tr(title)
    for canonical, keywords in _COLOR_RULES:
        for kw in keywords:
            if re.search(rf"\b{re.escape(kw)}\b", norm):
                return canonical
    return ""


_OFFICIAL_RETAILERS = {
    "amazon.com.tr",
    "samsung türkiye",
    "teknosa",
    "mediamarkt",
    "vatan bilgisayar",
    "migros sanal market ekstra",
    "migros",
    "turkcell pasaj",
    "vodafone",
}


def is_official_or_trusted_seller(seller: str, url: str = "") -> bool:
    """
    Satıcının doğrudan resmi perakendeci (Amazon, Samsung, Teknosa, MediaMarkt, Vatan, Migros)
    veya pazaryerinin kendi resmi satıcısı olup olmadığını belirler.
    """
    norm_s = normalize_tr(seller).strip()
    low_url = (url or "").lower()

    if norm_s in _OFFICIAL_RETAILERS:
        # Amazon'da başka alt satıcı belirtilmişse kontrol et
        if "(" in norm_s and "amazon" not in norm_s.split("(", 1)[1]:
            return False
        return True

    # Hepsiburada kendi satıcısı (URL'de farklı mağaza parametresi yoksa veya Hepsiburada ise)
    if norm_s == "hepsiburada":
        if "magaza=" in low_url and "magaza=hepsiburada" not in low_url:
            return False
        return True

    # Trendyol resmi/yetkili satıcı (merchantId=968 Trendyol kendi satıcısıdır)
    if norm_s == "trendyol":
        if "merchantid=968" in low_url or "merchantid=" not in low_url:
            return True
        return False

    return False


_TRADE_IN_AMOUNT_RE = re.compile(
    r"(?:ek\s*)?(?:takas|değişim|degisim|eskiyi\s*getir|eskiyi\s*yenile)[^\d]{0,30}(\d{1,2}\.\d{3}|\d{4,5})\s*(?:tl|₺)",
    re.IGNORECASE,
)
_AMOUNT_TRADE_IN_RE = re.compile(
    r"(\d{1,2}\.\d{3}|\d{4,5})\s*(?:tl|₺)[^\.\n]{0,30}(?:ek\s*)?(?:takas|değişim|degisim)\s*(?:desteği|destegi|indirimi|fırsatı)?",
    re.IGNORECASE,
)


def extract_trade_in_info(text: str) -> str:
    """Metinden 'Eskiyi Getir / Ek Takas Desteği' kampanyasını yakalar."""
    if not text:
        return ""
    for pat in (_AMOUNT_TRADE_IN_RE, _TRADE_IN_AMOUNT_RE):
        m = pat.search(text)
        if m:
            amt = parse_price_tl(m.group(1))
            if amt and 500 <= amt <= 40000:
                amt_fmt = f"{int(round(amt)):,}".replace(",", ".")
                return f"+{amt_fmt} TL Ek Takas Desteği"
    low = normalize_tr(text)
    if "ek takas" in low or "eskiyi getir" in low or "eskiyi yenile" in low:
        return "Eskiyi Getir / Takas Desteği Kampanyası"
    return ""

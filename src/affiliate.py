from __future__ import annotations

from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

from src.config import AppConfig

# Karşılaştırma sitelerinin kendi komisyon etiketlerini temizle
_STRIP_PARAMS = {
    "tag",
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_content",
    "utm_term",
    "v",
    "ref",
    "aff_sub",
    "wt_pc",
}


def convert_to_affiliate_url(url: str, config: AppConfig) -> str:
    """
    Doğrudan mağaza linkindeki üçüncü taraf (Akakçe/Cimri vb.) referans kodlarını temizler
    ve kullanıcının kendi Affiliate (Satış Ortaklığı) parametrelerini ekler.
    """
    if not url or not url.startswith("http"):
        return url

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()

    # Mevcut query parametrelerinden yabancı affiliate kodlarını ayıkla
    clean_pairs = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in _STRIP_PARAMS
    ]
    params = dict(clean_pairs)

    # 1) Amazon.com.tr Gelir Ortaklığı
    if "amazon.com.tr" in host:
        if config.amazon_associate_tag:
            params["tag"] = config.amazon_associate_tag
            params["linkCode"] = "ll1"
        params["utm_source"] = config.affiliate_utm_source
        params["utm_medium"] = config.affiliate_utm_medium

    # 2) Hepsiburada LinkGelir / Affiliate
    elif "hepsiburada.com" in host:
        if config.hepsiburada_affiliate_tag:
            params["wt_pc"] = config.hepsiburada_affiliate_tag
        params["utm_source"] = config.affiliate_utm_source
        params["utm_medium"] = config.affiliate_utm_medium

    # 3) Trendyol Partner / Influencer
    elif "trendyol.com" in host:
        if config.trendyol_affiliate_tag:
            params["utm_campaign"] = config.trendyol_affiliate_tag
        params["utm_source"] = config.affiliate_utm_source
        params["utm_medium"] = config.affiliate_utm_medium

    # 4) Diğer mağazalar (Teknosa, MediaMarkt, İdefix, Pazarama, N11, Migros)
    else:
        params["utm_source"] = config.affiliate_utm_source
        params["utm_medium"] = config.affiliate_utm_medium

    rebuilt = urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            urlencode(params, doseq=True),
            "",
        )
    )

    # Admitad / ReklamAction / GelirOrtakları deeplink sarmalayıcı tanımlıysa uygula
    if config.admitad_deeplink_base and "amazon.com.tr" not in host:
        base = config.admitad_deeplink_base.rstrip("?&")
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}ulp={quote(rebuilt, safe='')}"

    return rebuilt

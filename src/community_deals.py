from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from src.fetchers import HttpFetcher
from src.filter import extract_coupon_code, normalize_seller_name, normalize_tr, parse_explicit_tl_price, parse_price_tl

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CommunityDeal:
    deal_key: str
    title: str
    url: str
    source: str
    price_tl: float | None = None
    previous_price_tl: float | None = None
    discount_percent: int = 0
    image_url: str = ""
    seller: str = ""
    category: str = ""
    is_six_month_low: bool = False
    coupon_code: str = ""
    coupon_info: str = ""
    matched_keyword: str = ""


class CommunityDealsScanner:
    """
    DonanımHaber Sıcak Fırsatlar ve kamuya açık popüler Telegram fırsat kanallarını (t.me/s/...)
    tarayıp mağaza özel kuponlarını (ör. PULSAR1000 / app.hb.biz), kısa linkleri (onu.al -> direkt mağaza)
    ve son 6 ayın en düşük fiyatlı sıcak fırsatlarını yakalar.
    """

    DH_URL = "https://forum.donanimhaber.com/sicak-firsatlar--f193"
    DH_BASE = "https://forum.donanimhaber.com"
    PUBLIC_TG_CHANNELS = (
        "onual_firsat",
        "indirimkaplani",
    )
    STORE_URL_DOMAINS = (
        "app.hb.biz",
        "hepsiburada.com",
        "ty.gl",
        "trendyol.com",
        "amzn.to",
        "amazon.com.tr",
        "onu.al",
        "n11.com",
        "teknosa.com",
        "mediamarkt.com.tr",
        "pazarama.com",
        "idefix.com",
        "vatanbilgisayar.com",
    )

    def __init__(self, fetcher: HttpFetcher) -> None:
        self.fetcher = fetcher

    def scan(
        self, extra_keywords: list[str] | None = None, include_all_hot_prices: bool = False
    ) -> list[CommunityDeal]:
        keywords = [
            "s25 ultra",
            "galaxy s25",
            "samsung",
            "kupon kodu",
            "indirim kodu",
        ]
        if extra_keywords:
            for kw in extra_keywords:
                clean = normalize_tr(kw).strip()
                if len(clean) >= 3 and clean not in keywords:
                    keywords.append(clean)

        deals: list[CommunityDeal] = []
        try:
            html = self.fetcher.get_text(self.DH_URL)
            soup = BeautifulSoup(html, "lxml")

            for a in soup.select("a[href]"):
                href = (a.get("href") or "").strip()
                if not href or "javascript:" in href or href.startswith("#"):
                    continue
                title = " ".join(a.get_text(" ", strip=True).split())
                if len(title) < 12 or len(title) > 220:
                    continue

                # DH içindeki sponsorlu reklamlar ve fiyat karşılaştırma widget linklerini ele
                low_t = title.lower()
                if "sponsorlu içerik" in low_t or "sponsorlu icerik" in low_t or low_t.startswith("sponsorlu"):
                    continue
                if "fiyatları ve özellikleri" in low_t or "fiyatlari ve ozellikleri" in low_t:
                    continue
                if "utm_source=" in href.lower() or "utm_campaign=" in href.lower():
                    continue

                norm_t = normalize_tr(title)
                matched = ""
                for kw in keywords:
                    if kw in norm_t:
                        matched = kw
                        break

                code = extract_coupon_code(title)
                price = parse_explicit_tl_price(title)

                if not matched:
                    if include_all_hot_prices and price is not None and (
                        code or "indirim" in norm_t or "fırsat" in norm_t or "firsat" in norm_t or "kupon" in norm_t or "%" in norm_t
                    ):
                        matched = "sicak_firsat"
                    else:
                        continue

                full_url = urljoin(self.DH_BASE, href)
                deal_key = full_url.split("?")[0]

                if not any(d.deal_key == deal_key for d in deals):
                    deals.append(
                        CommunityDeal(
                            deal_key=deal_key,
                            title=title,
                            url=full_url,
                            source="DonanımHaber Sıcak Fırsatlar",
                            price_tl=price,
                            coupon_code=code,
                            matched_keyword=matched,
                        )
                    )
        except Exception as exc:
            logger.debug("Topluluk fırsat taraması hatası: %s", exc)

        # 2) OnuAl Canlı Web Akışı (Telegram kanallarına düşmeden 15-45 dk önce ana sayfaya düşen '1 dakika önce' fırsatlarını yakalar ve direkt mağaza linkine çözer)
        deals.extend(self._scan_onual_live_web_feed())

        # 3) Kamuya açık Telegram fırsat kanalları (t.me/s/...) ve kısa link (app.hb.biz vb.) çözücü
        deals.extend(self._scan_public_telegram_channels(include_all_hot_prices=include_all_hot_prices))

        unique: list[CommunityDeal] = []
        seen_keys: set[str] = set()
        for d in deals:
            if d.deal_key not in seen_keys:
                seen_keys.add(d.deal_key)
                unique.append(d)
        return unique[:15]

    def _scan_onual_live_web_feed(self, max_items: int = 6) -> list[CommunityDeal]:
        """
        www.onual.com ana sayfasındaki anlık ('1 dakika önce') fırsat akışını tarar;
        #fiyat ve #kupon parametrelerini ayıklayıp 'Ürüne Git' linkini doğrudan mağaza URL'sine çözer.
        Böylece fırsatlar @onual_firsat Telegram kanalında paylaşılmadan önce yakalanır.
        """
        found: list[CommunityDeal] = []
        try:
            html = self.fetcher.get_text("https://www.onual.com/")
        except Exception as exc:
            logger.debug("OnuAl canlı web akışı okunamadı: %s", exc)
            return found

        soup = BeautifulSoup(html, "lxml")
        unwrapped_count = 0
        for a in soup.find_all("a", href=True):
            if len(found) >= max_items or unwrapped_count >= max_items:
                break
            href = str(a.get("href") or "").strip()
            if "urun/" not in href or "#fiyat=" not in href:
                continue

            card_txt = " ".join(a.get_text(" ", strip=True).split())
            low_txt = card_txt.lower()
            if "fiyat yükseldi" in low_txt or "stok tükendi" in low_txt:
                continue

            is_6m = "6 ayın en düşük" in low_txt or "30 günün en düşük" in low_txt
            pct_m = re.search(r"ortalama fiyatın\s*%\s*(\d{1,2})\s*altında", low_txt)
            pct = int(pct_m.group(1)) if pct_m else (25 if is_6m else 0)
            if not is_6m and pct < 20 and "bugünün" not in low_txt:
                continue

            path_part, frag_part = href.split("#", 1)
            frag_qs = parse_qs(frag_part)
            price_val = parse_price_tl((frag_qs.get("fiyat") or [""])[0])
            if not price_val or price_val <= 0:
                continue
            raw_kupon = str((frag_qs.get("kupon") or [""])[0]).strip()
            coupon, coupon_howto = decode_onual_coupon_param(raw_kupon)

            # Çoklu alım varsa (ör. coklu=4-144 -> 4 adet alımda 144 TL) birim fiyatı kullan
            coklu = (frag_qs.get("coklu") or [""])[0]
            if "-" in coklu:
                unit_p = parse_price_tl(coklu.split("-", 1)[1])
                if unit_p and 0 < unit_p < price_val:
                    price_val = unit_p

            # Kart içindeki ürün görseli ve mağaza logosu
            img_url = ""
            seller = ""
            for img in a.find_all("img"):
                src = str(img.get("data-src") or img.get("src") or "").strip()
                if "pazaryeri/" in src and "-logo" in src:
                    st_slug = src.split("pazaryeri/", 1)[1].split("-logo")[0]
                    seller = normalize_seller_name(st_slug, "Mağaza")
                elif src.startswith("http") and "facebook.com" not in src and "logo" not in src.lower() and not img_url:
                    img_url = src

            # URL slug'ından veya karttan temiz başlık üret
            slug = path_part.split("urun/", 1)[-1].split("-p-")[0]
            fallback_title = " ".join(w.capitalize() for w in slug.split("-") if w)
            detail_url = urljoin("https://www.onual.com/", path_part)

            unwrapped_count += 1
            unwrapped = self._resolve_landing_page(detail_url, fallback_title, card_txt, price_val)
            direct_url = unwrapped["direct_url"]
            # Eğer doğrudan mağaza linki çözülemediyse onual linkini paylaşma
            if not direct_url or "onual.com" in direct_url.lower() or "onu.al" in direct_url.lower():
                continue

            final_title = unwrapped["title"] or fallback_title
            prev_price = unwrapped["previous_price_tl"]
            if not prev_price and pct > 0:
                prev_price = round(price_val / max(0.25, 1.0 - pct / 100.0), 2)
            if prev_price and prev_price > price_val:
                pct = max(pct, int(round(((prev_price - price_val) / prev_price) * 100.0)))

            if not seller:
                seller = normalize_seller_name(direct_url, "Mağaza")

            from src.popular_deals import canonicalize_deal_key

            deal_key = canonicalize_deal_key(direct_url)
            found.append(
                CommunityDeal(
                    deal_key=deal_key,
                    title=final_title[:160],
                    url=direct_url,
                    source="Anlık Fiyat Radarı",
                    price_tl=price_val,
                    previous_price_tl=prev_price,
                    discount_percent=pct,
                    image_url=img_url or unwrapped["image_url"],
                    seller=seller,
                    is_six_month_low=is_6m,
                    coupon_code=coupon or unwrapped["coupon_code"],
                    coupon_info=coupon_howto,
                    matched_keyword="kupon_kampanya" if (coupon or coupon_howto) else "sicak_firsat",
                )
            )
        return found

    def _scan_public_telegram_channels(self, include_all_hot_prices: bool = True) -> list[CommunityDeal]:
        found: list[CommunityDeal] = []
        for ch in self.PUBLIC_TG_CHANNELS:
            try:
                html = self.fetcher.get_text(f"https://t.me/s/{ch}")
            except Exception as exc:
                logger.debug("Telegram web önizleme okunamadı (%s): %s", ch, exc)
                continue
            soup = BeautifulSoup(html, "lxml")
            for wrap in reversed(soup.select(".tgme_widget_message_wrap")[-10:]):
                txt_el = wrap.select_one(".tgme_widget_message_text")
                if not txt_el:
                    continue
                raw_txt = " ".join(txt_el.get_text(" ", strip=True).split())
                if len(raw_txt) < 15:
                    continue

                store_href = ""
                for a in txt_el.select("a[href]"):
                    h = str(a.get("href") or "").strip()
                    if any(dom in h.lower() for dom in self.STORE_URL_DOMAINS):
                        store_href = h
                        break
                if not store_href:
                    continue

                code = extract_coupon_code(raw_txt)
                price = parse_explicit_tl_price(raw_txt)
                if not code and price is None and not include_all_hot_prices:
                    continue

                is_6m_low = "6 ayın en düşük" in raw_txt.lower() or "30 günün en düşük" in raw_txt.lower()
                avg_pct_m = re.search(r"ortalama fiyatın\s*%\s*(\d{1,2})\s*altında", raw_txt.lower())
                msg_disc_pct = int(avg_pct_m.group(1)) if avg_pct_m else 0
                clean_msg_title = _extract_clean_tg_title(txt_el, raw_txt)
                cat_m = re.search(r"📂\s*([^\n🛍🔗💬🏷]+)", raw_txt)
                sel_m = re.search(r"🛍️?\s*([^\n🔗📂💬🏷]+)", raw_txt)
                category = cat_m.group(1).strip()[:50] if cat_m else ""
                seller = sel_m.group(1).strip()[:40] if sel_m else ""

                # Telegram mesajındaki ürün görselini (background-image:url(...)) yedek olarak çıkar
                tg_img_url = ""
                photo_el = wrap.select_one("a.tgme_widget_message_photo_wrap[style]")
                if photo_el:
                    bg_m = re.search(r"background-image\s*:\s*url\(['\"]?(https?://[^'\")]+)['\"]?\)", str(photo_el.get("style") or ""))
                    if bg_m:
                        tg_img_url = bg_m.group(1).strip()

                # Kısa link (onu.al / app.hb.biz / ty.gl) ise doğrudan mağaza ürün sayfasına, görsele ve eski fiyata çöz
                unwrapped = self._resolve_landing_page(store_href, clean_msg_title, raw_txt, price)
                final_url = unwrapped["direct_url"] or store_href
                # Oracle gibi bulut IP'lerinde onu.al 403 verirse, ürün başlığından doğrudan Hepsiburada/N11 ürün linkini çöz!
                if any(bad in final_url.lower() for bad in ("onu.al", "onual.com", "zxro.com")):
                    resolved_store = self._resolve_direct_store_url_by_title(clean_msg_title, seller)
                    if resolved_store:
                        final_url = resolved_store
                    else:
                        continue
                final_title = unwrapped["title"] or clean_msg_title or raw_txt[:140]
                if not code and unwrapped["coupon_code"]:
                    code = unwrapped["coupon_code"]
                if price is None and unwrapped["price_tl"] is not None:
                    price = unwrapped["price_tl"]
                prev_price = unwrapped["previous_price_tl"]
                if not prev_price and price and msg_disc_pct >= 15:
                    prev_price = round(price / max(0.25, 1.0 - msg_disc_pct / 100.0), 2)
                elif not prev_price and price and is_6m_low:
                    prev_price = round(price * 1.30, 2)
                img_url = unwrapped["image_url"] or tg_img_url
                if not seller:
                    seller = normalize_seller_name(final_url, "Mağaza")
                else:
                    seller = normalize_seller_name(seller, seller)

                if not code and price is None:
                    continue

                disc_pct = msg_disc_pct
                if price and prev_price and prev_price > price:
                    disc_pct = max(disc_pct, int(round(((prev_price - price) / prev_price) * 100.0)))

                from src.popular_deals import canonicalize_deal_key

                deal_key = canonicalize_deal_key(final_url)
                found.append(
                    CommunityDeal(
                        deal_key=deal_key,
                        title=final_title[:160],
                        url=final_url,
                        source=f"Topluluk Radarı (@{ch})",
                        price_tl=price,
                        previous_price_tl=prev_price,
                        discount_percent=disc_pct,
                        image_url=img_url,
                        seller=seller,
                        category=category,
                        is_six_month_low=is_6m_low,
                        coupon_code=code,
                        matched_keyword="kupon_kampanya" if code else "sicak_firsat",
                    )
                )
        return found

    def _resolve_landing_page(
        self,
        url: str,
        clean_title: str,
        raw_txt: str,
        current_price: float | None,
    ) -> dict:
        out = {
            "title": clean_title,
            "direct_url": url,
            "coupon_code": "",
            "price_tl": current_price,
            "previous_price_tl": None,
            "image_url": "",
        }
        low_url = url.lower()
        low_fb = raw_txt.lower()
        is_shortlink = any(d in low_url for d in ("onu.al", "onual.com/urun/", "app.hb.biz", "ty.gl", "amzn.to"))
        needs_title = is_shortlink or any(
            k in low_fb for k in ("farklı model", "bu link üzerinden", "kontrol edebilirsiniz")
        )
        if not needs_title:
            return out

        try:
            html = self.fetcher.get_text(url)
            soup = BeautifulSoup(html, "lxml")

            # 1) onu.al veya yönlendirici sayfalardaki gerçek mağaza linkini ("id=buton" / "Ürüne Git" / zxro.com?url=...) çıkar
            btn_el = soup.find(id="buton")
            candidate_links = ([btn_el] if btn_el else []) + list(soup.find_all("a", href=True))
            for a in candidate_links:
                if not a:
                    continue
                href = str(a.get("href") or "").strip()
                a_txt = a.get_text(" ", strip=True).lower()
                if "url=" in href:
                    q_url = parse_qs(urlparse(href).query).get("url")
                    if q_url and q_url[0].startswith("http"):
                        out["direct_url"] = unquote(q_url[0])
                        if a.get("id") == "buton" or "ürüne git" in a_txt or "urune git" in a_txt:
                            break
                elif ("ürüne git" in a_txt or "urune git" in a_txt) and href.startswith("http") and "onu.al" not in href and "onual.com" not in href:
                    out["direct_url"] = href
                    break

            # Mağaza arama linklerini (ör. hepsiburada.com/ara?q=HBC... veya n11.com/arama?q=...) doğrudan ürün sayfasına yükselt
            sku_el = soup.select_one("span.tag-asin")
            sku_code = sku_el.get_text(strip=True) if sku_el else ""
            out["direct_url"] = self._upgrade_to_direct_product_url(out["direct_url"], url, sku_code)

            # 2) Ürün görseli çıkar (facebook pikseli ve site logoları hariç)
            for img in soup.find_all("img"):
                src = str(img.get("data-src") or img.get("src") or "").strip()
                if src.startswith("http") and "facebook.com" not in src and "logo" not in src.lower():
                    out["image_url"] = src
                    break

            # 3) Sayfa başlığı
            og = soup.select_one("meta[property='og:title']")
            h1 = soup.select_one("h1")
            t_el = soup.select_one("title")
            page_title = ""
            for cand in (
                h1.get_text(" ", strip=True) if h1 else "",
                og.get("content") if og else "",
                t_el.get_text(" ", strip=True) if t_el else "",
            ):
                clean = " ".join(str(cand or "").split())
                clean = re.sub(r"\s*-\s*OnuAl.*$", "", clean, flags=re.I)
                if len(clean) >= 12 and "loading" not in clean.lower() and "güvenlik" not in clean.lower():
                    page_title = clean
                    break
            if page_title and (not clean_title or "farklı model" in clean_title.lower() or "app.hb.biz" in low_url):
                out["title"] = page_title

            # 4) Kupon ve eski/grafik tepe fiyatı çıkar
            page_txt = soup.get_text(" ", strip=True)
            out["coupon_code"] = extract_coupon_code(page_title) or extract_coupon_code(page_txt[:1500])
            if out["price_tl"] is None:
                out["price_tl"] = parse_explicit_tl_price(page_title)
            if out["price_tl"] is None:
                from src.url_tracker import parse_product_page

                parsed_snap = parse_product_page(html, out["direct_url"])
                if parsed_snap and parsed_snap.price_tl > 0:
                    out["price_tl"] = parsed_snap.price_tl

            ref_cur = out["price_tl"]
            if ref_cur and ref_cur > 0:
                candidates: list[float] = []
                for m in re.finditer(r"(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?)\s*TL", page_txt, re.I):
                    pv = parse_price_tl(m.group(1))
                    if pv and ref_cur * 1.12 <= pv <= ref_cur * 3.5:
                        candidates.append(pv)
                if candidates:
                    out["previous_price_tl"] = max(candidates)
        except Exception as exc:
            logger.debug("Kısa link çözümlenemedi (%s): %s", url, exc)
        return out

    def _resolve_direct_store_url_by_title(self, title: str, seller_hint: str = "") -> str:
        """
        onu.al aracı linki bulut IP'sinde 403 verirse ürün başlığıyla Hepsiburada/N11 üzerinden
        doğrudan mağaza ürün sayfası URL'sini çözümler.
        """
        from urllib.parse import quote_plus

        tokens = re.findall(r"[A-Za-zÇĞİÖŞÜçğıöşü0-9]+", title or "")
        if len(tokens) < 2:
            return ""
        q = " ".join(tokens[:5])
        low_s = (seller_hint or "").lower()
        search_order = (
            [("hepsiburada", f"https://www.hepsiburada.com/ara?q={quote_plus(q)}"), ("n11", f"https://www.n11.com/arama?q={quote_plus(q)}")]
            if "n11" not in low_s
            else [("n11", f"https://www.n11.com/arama?q={quote_plus(q)}"), ("hepsiburada", f"https://www.hepsiburada.com/ara?q={quote_plus(q)}")]
        )
        for store, su in search_order:
            try:
                html = self.fetcher.get_text(su)
                soup = BeautifulSoup(html, "lxml")
                for a in soup.select("a[href]"):
                    h = str(a.get("href") or "").strip()
                    if store == "hepsiburada" and ("-p-HB" in h or "-pm-HB" in h):
                        return urljoin("https://www.hepsiburada.com", h.split("?")[0])
                    if store == "n11" and "/urun/" in h:
                        return urljoin("https://www.n11.com", h.split("?")[0])
            except Exception:
                continue
        return ""

    def _upgrade_to_direct_product_url(self, target_url: str, source_page_url: str, sku_code: str = "") -> str:
        """
        OnuAl'ın çıkardığı mağaza arama linklerini (ör. hepsiburada.com/ara?q=HBC0000E3NYHO veya n11.com/arama?q=...)
        doğrudan ürün detay sayfasına (ör. hepsiburada.com/...-pm-HBC0000E3NYHO veya n11.com/urun/...) dönüştürür.
        """
        if not target_url:
            return target_url

        # 1) Hepsiburada: /ara?q=HBC... -> /<slug>-pm-HBC... veya -p-HBCV...
        if "hepsiburada.com/ara" in target_url:
            q_val = (parse_qs(urlparse(target_url).query).get("q") or [sku_code])[0].strip().upper()
            if re.fullmatch(r"HBC?V?[A-Z0-9]{8,16}", q_val):
                slug = "urun"
                if "urun/" in source_page_url:
                    slug = source_page_url.split("urun/", 1)[-1].split("-p-")[0].split("#")[0].strip("/") or "urun"
                sep = "-p-" if q_val.startswith(("HBCV", "HBV")) else "-pm-"
                return f"https://www.hepsiburada.com/{slug}{sep}{q_val}"

        # 2) N11: /arama?q=... -> arama sayfasındaki ilk /urun/... linki
        if "n11.com/arama" in target_url:
            try:
                n_html = self.fetcher.get_text(target_url)
                n_soup = BeautifulSoup(n_html, "lxml")
                for a in n_soup.find_all("a", href=True):
                    h = str(a.get("href") or "").strip()
                    if "/urun/" in h:
                        return urljoin("https://www.n11.com", h.split("?")[0])
            except Exception:
                pass

        return target_url


def decode_onual_coupon_param(raw_kupon: str) -> tuple[str, str]:
    """
    OnuAl'ın #kupon=... parametresindeki özel yönlendirme kısaltmalarını (ALT500, ALTY20, TAKIP, SEPETTE100 vb.)
    gerçek kullanıcı talimatına çevirir. Yalnızca gerçek kopyalanabilir kodları coupon_code olarak döndürür.
    """
    if not raw_kupon:
        return "", ""
    norm = (
        raw_kupon.strip()
        .upper()
        .replace("İ", "I")
        .replace("Ş", "S")
        .replace("Ğ", "G")
        .replace("Ü", "U")
        .replace("Ö", "O")
        .replace("Ç", "C")
    )
    if norm.startswith("ONUAL"):
        return "", ""

    m = re.fullmatch(r"ALTY(\d+)", norm)
    if m:
        return "", f"Ürünün altındaki %{m.group(1)} indirim kuponunu toplayıp sepette uygulayın"
    m = re.fullmatch(r"SEPETTEY(\d+)", norm)
    if m:
        return "", f"Sepette çıkan %{m.group(1)} indirim kuponunu uygulayın"
    m = re.fullmatch(r"ALT(\d+)", norm)
    if m:
        return "", f"Ürünün altındaki {m.group(1)} TL'lik indirim kuponunu toplayıp sepette uygulayın"
    if norm == "ALT":
        return "", "Ürünün altındaki indirim kuponunu toplayıp sepette uygulayın"
    if norm == "TAKIP":
        return "", "Mağazayı takip ederek mağaza takip kuponunu alın ve sepette uygulayın"
    if norm == "KULLAN":
        return "", "Ürün sayfasında kampanya mesajının yanındaki Kullan/Uygula butonuna tıklayın"
    if norm == "VISA":
        return "", "Ödeme aşamasında Visa kart bilgilerinizi girerek indirimden yararlanın"
    m = re.fullmatch(r"SEPETTE(\d+)", norm)
    if m:
        return "", f"Ürünü sepete atın ve sepette çıkan {m.group(1)} TL'lik kuponu uygulayın"
    if norm == "SEPETTE":
        return "", "Ürünü sepete atın ve sepette çıkan kuponu uygulayın"
    m = re.fullmatch(r"TIKHIZI(\d+)", norm)
    if m:
        return "", f"Tık Hızı kampanyasından {m.group(1)} TL'lik indirim kuponunu alıp sepette uygulayın"
    if norm == "SEPET":
        return "", "Otomatik sepet indirimi: Ürünü sepete atıp ödeme aşamasına ilerleyin"

    return raw_kupon.strip().upper(), ""


def _extract_clean_tg_title(txt_el, raw_txt: str) -> str:
    """Telegram mesajının ilk satırındaki temiz ürün başlığını ayıklar."""
    raw_lines = [ln.strip() for ln in txt_el.get_text("\n", strip=True).splitlines() if ln.strip()]
    for ln in raw_lines:
        if any(ln.startswith(p) for p in ("🏷", "💬", "📂", "🛍", "🔗", "http", "Sepette ", "Link:")):
            continue
        if len(ln) >= 10:
            return " ".join(ln.split())
    return raw_txt[:120]

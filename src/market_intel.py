from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from src.fetchers import HttpFetcher
from src.filter import extract_coupon_info, parse_price_tl
from src.scrapers.structured import extract_json_ld_products

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CampaignIntelItem:
    source_type: str  # "banka" | "marka" | "hashtag_sinyal"
    source_name: str
    title: str
    url: str
    benefit_summary: str = ""
    coupon_code: str = ""
    category: str = ""


@dataclass(slots=True)
class UpcomingCampaignPrediction:
    window_label: str
    campaign_name: str
    focus_categories: str
    action_advice: str


@dataclass(slots=True)
class MarketIntelReport:
    bank_campaigns: list[CampaignIntelItem] = field(default_factory=list)
    brand_deals: list[CampaignIntelItem] = field(default_factory=list)
    hashtag_signals: list[CampaignIntelItem] = field(default_factory=list)
    predictions: list[UpcomingCampaignPrediction] = field(default_factory=list)


class MarketIntelligenceHunter:
    """
    Sadece pazaryerlerine bağlı kalmadan:
    1) Banka kampanyalarını (Bonus, Maximum, World, Axess, Bankkart)
    2) Markaların kendi resmi sitelerini (özellikle kadın giyim, kozmetik, ev & kişisel bakım)
    3) Hashtag / #reklam / #işbirliği / influencer kupon sinyallerini
    4) Takvim ve geçmiş verilere göre yaklaşan kampanya tahminlerini
    tek merkezde analiz eder.
    """

    BANK_FEEDS = (
        ("Garanti Bonus", "https://www.bonus.com.tr/kampanyalar", "https://www.bonus.com.tr"),
        ("İş Bankası Maximum", "https://www.maximum.com.tr/kampanyalar", "https://www.maximum.com.tr"),
        ("Yapı Kredi World", "https://www.worldcard.com.tr/kampanyalar", "https://www.worldcard.com.tr"),
        ("Akbank Axess", "https://www.axess.com.tr/axess/kampanyalar", "https://www.axess.com.tr"),
        ("Ziraat Bankkart", "https://www.bankkart.com.tr/kampanyalar", "https://www.bankkart.com.tr"),
    )

    OFFICIAL_BRAND_FEEDS = (
        ("Gratis", "https://www.gratis.com/kampanyalar", "https://www.gratis.com", "Kozmetik & Kadın Bakım"),
        ("Watsons", "https://www.watsons.com.tr/kampanyalar", "https://www.watsons.com.tr", "Kozmetik & Cilt Bakımı"),
        ("Mavi", "https://www.mavi.com/kadin/indirim", "https://www.mavi.com", "Kadın Giyim & Moda"),
        ("Koton", "https://www.koton.com/kadin-indirim", "https://www.koton.com", "Kadın Giyim & Moda"),
        ("LC Waikiki", "https://www.lcw.com/indirimli-urunler", "https://www.lcw.com", "Giyim & Anne-Bebek"),
        ("Karaca", "https://www.karaca.com/kampanyalar", "https://www.karaca.com", "Ev & Çeyiz / Mutfak"),
        ("Dyson Türkiye", "https://www.dyson.com.tr/firsatlar", "https://www.dyson.com.tr", "Saç Bakım & Ev Teknolojisi"),
        ("Samsung Türkiye", "https://www.samsung.com/tr/offer/", "https://www.samsung.com", "Elektronik"),
    )

    COMMUNITY_SIGNAL_FEEDS = (
        ("DonanımHaber Sıcak Fırsatlar", "https://forum.donanimhaber.com/sicak-firsatlar--f193", "https://forum.donanimhaber.com"),
    )

    BANK_KEYWORDS = (
        "e-ticaret", "online", "market", "giyim", "kozmetik", "trendyol",
        "hepsiburada", "amazon", "n11", "pazarama", "teknoloji", "puan",
        "iade", "indirim", "taksit", "bonus", "maxipuan", "worldpuan", "chip-para",
    )

    HASHTAG_SIGNAL_RE = re.compile(
        r"(#\w+|#reklam|#işbirliği|#isbirligi|kupon|kod(?:u)?|iade|cashback|banka|bonus|maxipuan|worldpuan|sepet(?:te)?\s*%\s*\d+|2\s*al\s*1\s*öde|1\s*alana\s*1\s*bedava)",
        re.IGNORECASE,
    )

    GAMIFIED_RE = re.compile(
        r"(çark|hediye\s*çark|şanslı\s*çark|kazı\s*kazan|tıkla\s*kazan|tık\s*hızı|sürpriz\s*kutu|kupon\s*yağmuru|puan\s*kazan|oyna\s*kazan|günün\s*şansı)",
        re.IGNORECASE,
    )
    GAMIFIED_FEEDS = (
        ("Trendyol", "https://www.trendyol.com/kampanyalar", "https://www.trendyol.com"),
        ("Hepsiburada", "https://www.hepsiburada.com/kampanyalar", "https://www.hepsiburada.com"),
        ("N11", "https://www.n11.com/kampanyalar", "https://www.n11.com"),
        ("Pazarama", "https://www.pazarama.com/kampanyalar", "https://www.pazarama.com"),
        ("Gratis", "https://www.gratis.com/kampanyalar", "https://www.gratis.com"),
        ("Watsons", "https://www.watsons.com.tr/kampanyalar", "https://www.watsons.com.tr"),
        ("LC Waikiki", "https://www.lcw.com/kampanyalar", "https://www.lcw.com"),
        ("DonanımHaber Sıcak Fırsatlar", "https://forum.donanimhaber.com/sicak-firsatlar--f193", "https://forum.donanimhaber.com"),
    )

    def __init__(self, fetcher: HttpFetcher) -> None:
        self.fetcher = fetcher

    def collect_all(self, now: Optional[datetime] = None) -> MarketIntelReport:
        return MarketIntelReport(
            bank_campaigns=self.scan_bank_campaigns(limit_per_bank=3),
            brand_deals=self.scan_official_brand_sites(limit_per_brand=2),
            hashtag_signals=self.scan_hashtag_and_promo_signals(limit=8),
            predictions=predict_upcoming_campaigns(now or datetime.now()),
        )

    def scan_bank_campaigns(self, limit_per_bank: int = 3) -> list[CampaignIntelItem]:
        found: list[CampaignIntelItem] = []
        seen_titles: set[str] = set()
        for bank_name, feed_url, base_url in self.BANK_FEEDS:
            try:
                raw_html = self.fetcher.get_text(feed_url)
            except Exception as exc:
                logger.debug("Banka kampanyası okunamadı (%s): %s", bank_name, exc)
                continue
            soup = BeautifulSoup(raw_html, "lxml")
            cnt = 0
            for a in soup.select("a[href]"):
                if cnt >= limit_per_bank:
                    break
                txt = " ".join(a.get_text(" ", strip=True).split())
                if len(txt) < 18 or len(txt) > 160:
                    continue
                low = txt.lower()
                if not any(k in low for k in self.BANK_KEYWORDS):
                    continue
                if not re.search(r"(\d+\s*TL|%\s*\d+|taksit|puan|bonus|iade)", txt, re.I):
                    continue
                norm = low[:60]
                if norm in seen_titles:
                    continue
                seen_titles.add(norm)
                href = urljoin(base_url, str(a.get("href") or feed_url))
                benefit_m = re.search(r"(\d[\d\.]*\s*TL[^,.;]*|%\s*\d+[^,.;]*)", txt, re.I)
                found.append(
                    CampaignIntelItem(
                        source_type="banka",
                        source_name=bank_name,
                        title=txt[:130],
                        url=href,
                        benefit_summary=benefit_m.group(1).strip() if benefit_m else "Banka Puan / İade",
                        category="Banka & Ödeme",
                    )
                )
                cnt += 1
        return found

    def scan_official_brand_sites(self, limit_per_brand: int = 2) -> list[CampaignIntelItem]:
        found: list[CampaignIntelItem] = []
        for brand_name, feed_url, base_url, cat in self.OFFICIAL_BRAND_FEEDS:
            try:
                raw_html = self.fetcher.get_text(feed_url)
            except Exception as exc:
                logger.debug("Resmi marka sitesi okunamadı (%s): %s", brand_name, exc)
                continue

            cnt = 0
            for item in extract_json_ld_products(raw_html):
                if cnt >= limit_per_brand:
                    break
                p_val = item.get("price_tl")
                old_p = item.get("high_price_tl")
                t_str = str(item.get("title") or "").strip()
                u_str = urljoin(base_url, str(item.get("url") or feed_url))
                if not t_str or not p_val:
                    continue
                benefit = f"{float(p_val):,.0f} TL".replace(",", ".")
                if old_p and float(old_p) > float(p_val):
                    pct = int(round(((float(old_p) - float(p_val)) / float(old_p)) * 100.0))
                    if pct < 15:
                        continue
                    benefit = f"-%{pct} ({benefit})"
                found.append(
                    CampaignIntelItem(
                        source_type="marka",
                        source_name=brand_name,
                        title=t_str[:130],
                        url=u_str,
                        benefit_summary=benefit,
                        category=cat,
                    )
                )
                cnt += 1

            # JSON-LD yoksa sayfadaki kampanya/indirim başlıklarını yakala
            if cnt == 0:
                soup = BeautifulSoup(raw_html, "lxml")
                for a in soup.select("a[href]"):
                    if cnt >= limit_per_brand:
                        break
                    txt = " ".join(a.get_text(" ", strip=True).split())
                    if len(txt) < 16 or len(txt) > 130:
                        continue
                    if not re.search(r"(%\s*\d+|indirim|kampanya|hediye|kupon|sepet)", txt, re.I):
                        continue
                    href = urljoin(base_url, str(a.get("href") or feed_url))
                    _, _, code = extract_coupon_info(txt, 1000.0)
                    found.append(
                        CampaignIntelItem(
                            source_type="marka",
                            source_name=brand_name,
                            title=txt,
                            url=href,
                            benefit_summary="Resmi Marka Kampanyası",
                            coupon_code=code,
                            category=cat,
                        )
                    )
                    cnt += 1
        return found

    def scan_gamified_campaigns(self, limit: int = 8) -> list[CampaignIntelItem]:
        """Sitelerin çark çevirme, tıkla kazan, kazı kazan ve tık hızı kupon kampanyalarını yakalar."""
        found: list[CampaignIntelItem] = []
        seen: set[str] = set()
        for src_name, feed_url, base_url in self.GAMIFIED_FEEDS:
            if len(found) >= limit:
                break
            try:
                raw_html = self.fetcher.get_text(feed_url)
            except Exception as exc:
                logger.debug("Çark/tıkla-kazan akışı okunamadı (%s): %s", src_name, exc)
                continue
            soup = BeautifulSoup(raw_html, "lxml")
            for a in soup.select("a[href]"):
                if len(found) >= limit:
                    break
                txt = " ".join(a.get_text(" ", strip=True).split())
                if len(txt) < 12 or len(txt) > 150:
                    continue
                m = self.GAMIFIED_RE.search(txt)
                if not m:
                    continue
                norm = txt.lower()[:60]
                if norm in seen:
                    continue
                seen.add(norm)
                href = urljoin(base_url, str(a.get("href") or feed_url))
                _, _, code = extract_coupon_info(txt, 1000.0)
                found.append(
                    CampaignIntelItem(
                        source_type="cark_kazan",
                        source_name=src_name,
                        title=txt,
                        url=href,
                        benefit_summary=f"Etkinlik: {m.group(0)}",
                        coupon_code=code,
                        category="Çark & Tıkla Kazan",
                    )
                )
        return found

    def scan_influencer_hashtag_codes(self, limit: int = 8) -> list[CampaignIntelItem]:
        """Kamuya açık sosyal medya/web aramalarından #reklam #işbirliği influencer indirim kodlarını tarar."""
        queries = (
            "https://html.duckduckgo.com/html/?q=%23reklam+%23i%C5%9Fbirli%C4%9Fi+%22indirim+kodu%22+trendyol+OR+hepsiburada+OR+gratis",
            "https://html.duckduckgo.com/html/?q=%23i%C5%9Fbirli%C4%9Fi+%22kupon+kodu%22+amazon+OR+watsons+OR+mavi",
            "https://forum.donanimhaber.com/sicak-firsatlar--f193",
        )
        found: list[CampaignIntelItem] = []
        seen_codes: set[str] = set()
        for q_url in queries:
            if len(found) >= limit:
                break
            try:
                raw_html = self.fetcher.get_text(q_url)
            except Exception as exc:
                logger.debug("Influencer/hashtag araması okunamadı (%s): %s", q_url, exc)
                continue
            soup = BeautifulSoup(raw_html, "lxml")
            for a in soup.select("a.result__a, a[href]"):
                if len(found) >= limit:
                    break
                txt = " ".join(a.get_text(" ", strip=True).split())
                if len(txt) < 12 or len(txt) > 160:
                    continue
                href = urljoin(q_url, str(a.get("href") or q_url))
                # DuckDuckGo yönlendirme linkini gerçek hedef URL'ye çöz
                if "duckduckgo.com/l/" in href:
                    uddg = parse_qs(urlparse(href).query).get("uddg")
                    if uddg and uddg[0].startswith("http"):
                        href = unquote(uddg[0])

                _, _, code = extract_coupon_info(txt, 1000.0)
                if not code and href.startswith("http") and ("kupon" in txt.lower() or "kod" in txt.lower()):
                    code = self._extract_code_from_target_page(href)
                if not code or code in seen_codes:
                    continue
                seen_codes.add(code)
                found.append(
                    CampaignIntelItem(
                        source_type="influencer_kod",
                        source_name="Sosyal Medya / Influencer Radarı",
                        title=txt,
                        url=href,
                        benefit_summary=f"Doğrulanmış Kod: {code}",
                        coupon_code=code,
                        category="#reklam / Influencer Kodu",
                    )
                )
        return found

    def _extract_code_from_target_page(self, url: str) -> str:
        """LiraKod vb. kupon sayfalarında açılan sayfa içinden gerçek kupon kodunu (ör. 75AMZN) çeker."""
        try:
            page_html = self.fetcher.get_text(url)
            psoup = BeautifulSoup(page_html, "lxml")
            for attr in ("data-code", "data-clipboard-text", "data-coupon", "value"):
                for el in psoup.select(f"[{attr}]"):
                    cand = str(el.get(attr) or "").strip().upper()
                    if re.fullmatch(r"[A-Z0-9]{4,18}", cand) and any(c.isalpha() for c in cand):
                        _, _, checked = extract_coupon_info(f'"{cand}" kupon kodu', 1000.0)
                        if checked:
                            return checked
            page_txt = " ".join(psoup.get_text(" ", strip=True).split())[:4000]
            _, _, code = extract_coupon_info(page_txt, 1000.0)
            return code
        except Exception:
            return ""

    def scan_hashtag_and_promo_signals(self, limit: int = 8) -> list[CampaignIntelItem]:
        found: list[CampaignIntelItem] = []
        seen: set[str] = set()
        for src_name, feed_url, base_url in self.COMMUNITY_SIGNAL_FEEDS:
            try:
                raw_html = self.fetcher.get_text(feed_url)
            except Exception as exc:
                logger.debug("Sinyal akışı okunamadı (%s): %s", src_name, exc)
                continue
            soup = BeautifulSoup(raw_html, "lxml")
            for a in soup.select("a[href]"):
                if len(found) >= limit:
                    break
                txt = " ".join(a.get_text(" ", strip=True).split())
                if len(txt) < 16 or len(txt) > 150:
                    continue
                m = self.HASHTAG_SIGNAL_RE.search(txt)
                if not m:
                    continue
                norm = txt.lower()[:60]
                if norm in seen:
                    continue
                seen.add(norm)
                href = urljoin(base_url, str(a.get("href") or feed_url))
                p_val = parse_price_tl(txt)
                _, _, code = extract_coupon_info(txt, p_val or 1000.0)
                found.append(
                    CampaignIntelItem(
                        source_type="hashtag_sinyal",
                        source_name=src_name,
                        title=txt,
                        url=href,
                        benefit_summary=f"Sinyal: {m.group(0)}",
                        coupon_code=code,
                        category="Hashtag / Kod Sinyali",
                    )
                )
        return found


def predict_upcoming_campaigns(now: datetime) -> list[UpcomingCampaignPrediction]:
    """Takvim ve Türkiye e-ticaret döngülerine göre önümüzdeki 1-14 günlük kampanya tahminleri üretir."""
    preds: list[UpcomingCampaignPrediction] = []
    m, d = now.month, now.day

    # Maaş dönemleri (her ayın 1-5 ve 15-18 arası)
    if 1 <= d <= 5 or 14 <= d <= 18:
        preds.append(
            UpcomingCampaignPrediction(
                window_label="Aktif Maaş Dönemi",
                campaign_name="Maaş Günü Sepet & Banka Puan Kampanyaları",
                focus_categories="Elektronik, Kozmetik, Kadın Giyim, Süpermarket",
                action_advice="Banka (Bonus/Maximum/World) + mağaza kuponu birleştiren yüksek tutarlı fırsatları öne çıkarın.",
            )
        )
    elif d >= 25:
        preds.append(
            UpcomingCampaignPrediction(
                window_label="Ay Sonu / Yeni Ay Başı (3-7 gün)",
                campaign_name="Ay Sonu Stok Eritme + Yeni Ay Banka Kota Yenileme",
                focus_categories="Küçük Ev Aletleri, Kozmetik, Giyim",
                action_advice="Ayın 1'inde yenilenecek banka e-ticaret puan kampanyalarını önceden duyurun.",
            )
        )

    # Mevsimsel / Büyük Kampanya Pencereleri
    if m == 11 and d >= 20:
        preds.append(
            UpcomingCampaignPrediction(
                window_label="Bu Hafta (Kasım Sonu / Cyber Monday)",
                campaign_name="Efsane Cuma & Siber Pazartesi Son Fırsat Dalgası",
                focus_categories="Teknoloji, Dyson/Kişisel Bakım, Kozmetik, Kadın Moda, TV",
                action_advice="Gece 00:00 ve öğle 12:00 flaş stok eritme indirimlerinde 3 dk Turbo taramayı koruyun.",
            )
        )
    elif m == 12:
        preds.append(
            UpcomingCampaignPrediction(
                window_label="Aralık / Yılbaşı Dönemi",
                campaign_name="Yılbaşı Hediye & Kozmetik Setleri + Yıl Sonu Stok Eritme",
                focus_categories="Parfüm, Kozmetik Setleri, Saat/Aksesuar, Kulaklık, Kahve Makineleri",
                action_advice="Gratis/Watsons/Sephora ve kadın hediye kategorilerinde %30+ set indirimlerini tarayın.",
            )
        )
    elif m == 2 and d <= 14:
        preds.append(
            UpcomingCampaignPrediction(
                window_label="14 Şubat Yaklaşıyor",
                campaign_name="Sevgililer Günü Kozmetik, Parfüm & Moda Kampanyaları",
                focus_categories="Kadın Giyim, Parfüm, Takı/Saat, Saç Şekillendirici",
                action_advice="Resmi marka sitelerindeki '2. ürüne %50' ve kupon kombinasyonlarını öne çıkarın.",
            )
        )
    elif m == 3 and d <= 8:
        preds.append(
            UpcomingCampaignPrediction(
                window_label="8 Mart Dünya Kadınlar Günü",
                campaign_name="Kadınlara Özel Moda, Kozmetik & Kişisel Bakım İndirimleri",
                focus_categories="Kadın Giyim, Cilt Bakımı, Dyson/Philips, Çanta/Ayakkabı",
                action_advice="Kadın kategorisi akışlarına öncelik verip marka sitelerini sıklaştırın.",
            )
        )

    # Her zaman geçerli proaktif tahmin
    preds.append(
        UpcomingCampaignPrediction(
            window_label="Haftalık Döngü (Perşembe-Pazar)",
            campaign_name="Hafta Sonu Flaş İndirimleri & Süpermarket Çoklu Alım",
            focus_categories="Süpermarket, Kozmetik (1 Alana 1 Bedava), Ev & Yaşam",
            action_advice="Birim fiyat düşüren '2 Al 1 Öde / Sepette %40' kampanyalarını banka iadesiyle birleştirin.",
        )
    )
    return preds


def format_market_intel_html(report: MarketIntelReport) -> str:
    lines = ["🔮 <b>PİYASA, BANKA, MARKA &amp; KAMPANYA İSTİHBARATI</b>\n"]

    if report.predictions:
        lines.append("📅 <b>Yaklaşan Kampanya Tahminleri:</b>")
        for p in report.predictions[:3]:
            lines.append(
                f"• <b>{html.escape(p.window_label)}:</b> {html.escape(p.campaign_name)}\n"
                f"  <i>Odak:</i> {html.escape(p.focus_categories)}"
            )
        lines.append("")

    if report.bank_campaigns:
        lines.append("🏦 <b>Banka Kampanyaları:</b>")
        for b in report.bank_campaigns[:5]:
            lines.append(
                f"• <b>{html.escape(b.source_name)}:</b> "
                f"<a href=\"{html.escape(b.url, quote=True)}\">{html.escape(b.title[:85])}</a> "
                f"(<i>{html.escape(b.benefit_summary)}</i>)"
            )
        lines.append("")

    if report.brand_deals:
        lines.append("👗 <b>Resmi Marka &amp; Kadın/Yaşam Fırsatları:</b>")
        for m in report.brand_deals[:6]:
            c_txt = f' [Kod: "<b>{html.escape(m.coupon_code)}</b>"]' if m.coupon_code else ""
            lines.append(
                f"• <b>{html.escape(m.source_name)}</b> ({html.escape(m.category)}): "
                f"<a href=\"{html.escape(m.url, quote=True)}\">{html.escape(m.title[:80])}</a> "
                f"<b>{html.escape(m.benefit_summary)}</b>{c_txt}"
            )
        lines.append("")

    if report.hashtag_signals:
        lines.append("🏷 <b>Hashtag / Kupon / Reklam Sinyalleri:</b>")
        for s in report.hashtag_signals[:5]:
            c_txt = f' [Kod: "<b>{html.escape(s.coupon_code)}</b>"]' if s.coupon_code else ""
            lines.append(
                f"• <a href=\"{html.escape(s.url, quote=True)}\">{html.escape(s.title[:85])}</a>{c_txt}"
            )

    return "\n".join(lines)

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from datetime import datetime

from src.affiliate import convert_to_affiliate_url
from src.analyzer import analyze_deals, deduplicate_offers
from src.channel_publisher import ChannelPublisher
from src.charts import render_price_chart_png
from src.config import load_config
from src.dashboard import _render_dashboard_html
from src.exporter import generate_csv_report_bytes
from src.filter import (
    detect_variant,
    extract_color,
    extract_coupon_info,
    extract_trade_in_info,
    is_official_or_trusted_seller,
    is_valid_s25_ultra_phone,
    parse_explicit_tl_price,
    parse_price_tl,
)
from src.models import AlertType, PriceOffer
from src.notifier import TelegramNotifier, format_tl
from src.scrapers.structured import extract_json_ld_products
from src.storage import PriceStorage
from src.url_tracker import fetch_watched_product, parse_product_page
from src.wakeup import generate_alarm_wav_bytes, get_dynamic_interval_minutes, is_within_wakeup_window
from src.x_publisher import XPublisher


class TestFilterAndParsing(unittest.TestCase):
    def test_parse_price_tl_formats(self) -> None:
        self.assertEqual(parse_price_tl("79.999,00 TL"), 79999.0)
        self.assertEqual(parse_price_tl("74.999 TL"), 74999.0)
        self.assertEqual(parse_price_tl("82.499,90 ₺"), 82499.9)
        self.assertEqual(parse_price_tl("89999.50"), 89999.5)
        self.assertIsNone(parse_price_tl("Stokta yok"))
        # Model numaraları 'TL' birimi olmadan fiyat sanılmamalı
        self.assertIsNone(
            parse_explicit_tl_price("Jack & Jones Erkek Kapüsonlu Puffer Ceket - Global - 12283517 Sky Captain XL")
        )
        self.assertEqual(parse_explicit_tl_price("Jack & Jones Ceket 1.899 TL indirim"), 1899.0)

    def test_detect_variant(self) -> None:
        self.assertEqual(detect_variant("Samsung Galaxy S25 Ultra 256GB Titanyum"), "256 GB")
        self.assertEqual(detect_variant("Samsung Galaxy S25 Ultra 512 GB Siyah"), "512 GB")
        self.assertEqual(detect_variant("Samsung Galaxy S25 Ultra 1TB Gri"), "1 TB")
        self.assertEqual(detect_variant("Samsung Galaxy S25 Ultra 1024 GB"), "1 TB")
        self.assertIsNone(detect_variant("Samsung Galaxy S25 Ultra"))

    def test_valid_s25_ultra_filter(self) -> None:
        self.assertTrue(is_valid_s25_ultra_phone("Samsung Galaxy S25 Ultra 256 GB 5G", 74999.0))
        self.assertTrue(is_valid_s25_ultra_phone("Samsung SM-S938B Galaxy S25 Ultra 512GB", 84999.0))

        # Aksesuarlar ve diğer modeller elenmeli
        self.assertFalse(is_valid_s25_ultra_phone("Samsung Galaxy S25 Ultra Silikon Kılıf", 55000.0))
        self.assertFalse(is_valid_s25_ultra_phone("Galaxy S25 Ultra Ekran Koruyucu Cam", 499.0))
        self.assertFalse(is_valid_s25_ultra_phone("Samsung Galaxy S25+ Plus 256 GB", 65000.0))
        self.assertFalse(is_valid_s25_ultra_phone("Samsung Galaxy S24 Ultra 256 GB", 62000.0))
        self.assertFalse(is_valid_s25_ultra_phone("Yenilenmiş Samsung Galaxy S25 Ultra 256 GB", 68000.0))
        self.assertFalse(is_valid_s25_ultra_phone("Samsung Galaxy S25 Ultra 256 GB", 12000.0))

    def test_json_ld_extraction(self) -> None:
        html = """
        <html><head>
        <script type="application/ld+json">
        {
          "@context": "https://schema.org",
          "@type": "Product",
          "name": "Samsung Galaxy S25 Ultra 256 GB Titanyum Siyah",
          "url": "https://example.com/s25-ultra-256",
          "image": ["https://cdn.example.com/s25.jpg"],
          "offers": {
            "@type": "Offer",
            "price": "74999.00",
            "priceCurrency": "TRY",
            "availability": "https://schema.org/InStock",
            "seller": {"@type": "Organization", "name": "Amazon.com.tr"}
          }
        }
        </script>
        </head><body></body></html>
        """
        items = extract_json_ld_products(html)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["price_tl"], 74999.0)
        self.assertEqual(items[0]["seller"], "Amazon.com.tr")
        self.assertEqual(items[0]["image_url"], "https://cdn.example.com/s25.jpg")


class TestAnalyzerAndStorage(unittest.TestCase):
    def test_all_deal_criteria_and_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg_path = tmp_path / "config.yaml"
            cfg_path.write_text(
                """
product:
  variants: ["256 GB", "512 GB", "1 TB"]
  min_valid_price_tl: 45000
  max_valid_price_tl: 160000
alerts:
  target_prices_tl:
    "256 GB": 73000
  min_drop_tl: 500
  min_drop_percent: 1.5
  below_market_percent: 5.0
  cooldown_hours: 12
database:
  path: "prices.db"
""",
                encoding="utf-8",
            )
            config = load_config(cfg_path)
            storage = PriceStorage(tmp_path / "prices.db")

            # Run 1: Piyasa oluşuyor (medyan ~80.000 TL)
            r1 = storage.create_run()
            initial_offers = [
                PriceOffer("epey", "Hepsiburada", "256 GB", "S25 Ultra 256GB", 79000.0, "https://hb.com/1"),
                PriceOffer("epey", "Trendyol", "256 GB", "S25 Ultra 256GB", 80000.0, "https://ty.com/1"),
                PriceOffer("epey", "MediaMarkt", "256 GB", "S25 Ultra 256GB", 81000.0, "https://mm.com/1"),
            ]
            storage.save_offers(r1, initial_offers)
            storage.finalize_run(r1, len(initial_offers), 1, 0)

            # Run 2: Amazon 72.000 TL ile giriyor -> TARGET + ALL_TIME_LOW + PRICE_DROP + BELOW_MARKET!
            r2 = storage.create_run()
            new_offers = deduplicate_offers(
                [
                    PriceOffer("amazon_tr", "Amazon.com.tr", "256 GB", "Samsung Galaxy S25 Ultra 256 GB", 72000.0, "https://amz.com/1"),
                    PriceOffer("epey", "Hepsiburada", "256 GB", "Samsung Galaxy S25 Ultra 256 GB", 79500.0, "https://hb.com/1"),
                    PriceOffer("epey", "Trendyol", "256 GB", "Samsung Galaxy S25 Ultra 256 GB", 80000.0, "https://ty.com/1"),
                ]
            )
            storage.save_offers(r2, new_offers)
            storage.finalize_run(r2, len(new_offers), 2, 0)

            alerts = analyze_deals(new_offers, config, storage, current_run_id=r2)
            self.assertEqual(len(alerts), 1)
            reasons = set(alerts[0].alert_types)
            self.assertIn(AlertType.TARGET_PRICE, reasons)
            self.assertIn(AlertType.ALL_TIME_LOW, reasons)
            self.assertIn(AlertType.PRICE_DROP, reasons)
            self.assertIn(AlertType.BELOW_MARKET, reasons)

            # Bildirim kaydedildikten sonra aynı fiyat tekrar spam yapmamalı
            storage.record_alert("256 GB", "Amazon.com.tr", 72000.0, alerts[0].alert_types, "https://amz.com/1")
            r3 = storage.create_run()
            storage.save_offers(r3, new_offers)
            storage.finalize_run(r3, len(new_offers), 2, 0)
            alerts_repeat = analyze_deals(new_offers, config, storage, current_run_id=r3)
            self.assertEqual(len(alerts_repeat), 0)

    def test_message_formatting(self) -> None:
        self.assertEqual(format_tl(74999.0), "74.999 TL")
        notifier = TelegramNotifier("tok", "123")
        msg = notifier.build_summary_message(
            {
                "256 GB": [
                    PriceOffer("epey", "A & B <Store>", "256 GB", "S25 Ultra 256GB", 74999.0, "https://example.com/?a=1&b=2")
                ]
            }
        )
        self.assertIn("74.999 TL", msg)
        self.assertIn("A &amp; B &lt;Store&gt;", msg)

    def test_custom_url_parsing_and_storage(self) -> None:
        html = """
        <html>
          <head><title>Sony WH-1000XM5 Kulaklık</title>
          <meta property="product:price:amount" content="12499.00" /></head>
          <body><h1>Sony WH-1000XM5 Kablosuz Kulaklık</h1></body>
        </html>
        """
        snap = parse_product_page(html, "https://www.amazon.com.tr/dp/B09XS7JWHH")
        self.assertIsNotNone(snap)
        assert snap is not None
        self.assertEqual(snap.price_tl, 12499.0)
        self.assertEqual(snap.seller, "Amazon.com.tr")
        self.assertIn("Sony WH-1000XM5", snap.title)

        with tempfile.TemporaryDirectory() as tmp:
            storage = PriceStorage(Path(tmp) / "prices.db")
            wid, is_new = storage.add_or_update_watched_url(
                snap.url, snap.title, snap.seller, snap.price_tl, target_price_tl=11000.0
            )
            self.assertTrue(is_new)
            self.assertEqual(len(storage.list_watched_urls()), 1)
            storage.update_watched_url_price(wid, snap.title, 10900.0, True)
            row = storage.list_watched_urls()[0]
            self.assertEqual(row["last_price_tl"], 10900.0)
            self.assertEqual(row["min_price_tl"], 10900.0)
            self.assertTrue(storage.remove_watched_url(wid))
            self.assertEqual(len(storage.list_watched_urls()), 0)

    def test_coupon_six_month_low_and_pause(self) -> None:
        disc1, lbl1, code1 = extract_coupon_info("Samsung S25 Ultra Sepette 67.500 TL", 70000.0)
        self.assertEqual(disc1, 2500.0)
        self.assertIn("Sepette", lbl1)
        self.assertEqual(code1, "")

        disc2, _, code2 = extract_coupon_info('Sepette 2.000 TL Kupon Fırsatı Kupon Kodu: "KASIM2000"', 70000.0)
        self.assertEqual(disc2, 2000.0)
        self.assertEqual(code2, "KASIM2000")

        disc3, _, _ = extract_coupon_info("Kuponla %10 indirim", 70000.0)
        self.assertEqual(disc3, 7000.0)

        # Doğrudan mağaza linki, karşılaştırma sitesi (akakce/cimri) linkine tercih edilmeli
        deduped = deduplicate_offers(
            [
                PriceOffer("akakce", "İdefix", "256 GB", "S25 Ultra 256GB", 69999.0, "https://www.akakce.com/urun.html"),
                PriceOffer("akakce", "İdefix", "256 GB", "S25 Ultra 256GB", 69999.0, "https://www.idefix.com/s25-ultra-p-123"),
            ]
        )
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0].url, "https://www.idefix.com/s25-ultra-p-123")

        self.assertTrue(is_within_wakeup_window("00:00-08:30", datetime(2026, 11, 24, 1, 15)))
        self.assertFalse(is_within_wakeup_window("00:00-08:30", datetime(2026, 11, 24, 10, 0)))

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg_path = tmp_path / "config.yaml"
            cfg_path.write_text(
                """
product:
  variants: ["256 GB", "512 GB", "1 TB"]
alerts:
  target_prices_tl:
    "256 GB": 60000
  six_month_days: 180
  cooldown_hours: 12
database:
  path: "prices.db"
""",
                encoding="utf-8",
            )
            config = load_config(cfg_path)
            storage = PriceStorage(tmp_path / "prices.db")

            # Run 1: 6 aylık en düşük 69.000 TL kaydediliyor
            r1 = storage.create_run()
            storage.save_offers(
                r1,
                [
                    PriceOffer("akakce", "Hepsiburada", "256 GB", "S25 Ultra 256GB", 69000.0, "https://hb.com/1"),
                ],
            )
            storage.finalize_run(r1, 1, 1, 0)

            # Run 2: Liste fiyatı 70.000 TL ama 3.000 TL kuponla 67.000 TL -> COUPON_SIX_MONTH_LOW!
            r2 = storage.create_run()
            coupon_offer = PriceOffer(
                "akakce",
                "Trendyol",
                "256 GB",
                "S25 Ultra 256GB",
                70000.0,
                "https://ty.com/1",
                coupon_discount_tl=3000.0,
                coupon_info="3.000 TL Kupon",
            )
            storage.save_offers(r2, [coupon_offer])
            storage.finalize_run(r2, 1, 1, 0)

            alerts = analyze_deals([coupon_offer], config, storage, current_run_id=r2)
            self.assertEqual(len(alerts), 1)
            self.assertIn(AlertType.COUPON_SIX_MONTH_LOW, alerts[0].alert_types)
            self.assertTrue(alerts[0].is_wakeup_worthy)

            # Varyant durdurulduğunda artık alarm üretmemeli
            storage.pause_variant("256 GB")
            alerts_paused = analyze_deals([coupon_offer], config, storage, current_run_id=r2)
            self.assertEqual(len(alerts_paused), 0)

    def test_keyboards_and_cache_fallback(self) -> None:
        notifier = TelegramNotifier("tok", "123")
        self.assertIn("inline_keyboard", notifier.build_main_menu_keyboard())
        self.assertIn("inline_keyboard", notifier.build_targets_keyboard(["256 GB", "512 GB"]))
        self.assertIn("inline_keyboard", notifier.build_toggle_tracking_keyboard(["256 GB"], {"256 GB"}))
        self.assertIn("inline_keyboard", notifier.build_watched_url_keyboard([1]))

        class _FailingFetcher:
            def get_text(self, url: str) -> str:
                raise RuntimeError("Simulated SSL_connect error")

        with tempfile.TemporaryDirectory() as tmp:
            storage = PriceStorage(Path(tmp) / "prices.db")
            rid = storage.create_run()
            storage.save_offers(
                rid,
                [
                    PriceOffer(
                        "akakce",
                        "İdefix",
                        "256 GB",
                        "Samsung Galaxy S25 Ultra 256 GB",
                        69999.0,
                        "https://www.akakce.com/cep-telefonu/en-ucuz-samsung-galaxy-s25-ultra-fiyati,651960345.html",
                    )
                ],
            )
            storage.finalize_run(rid, 1, 1, 0)
            snap = fetch_watched_product(
                "https://www.akakce.com/cep-telefonu/en-ucuz-samsung-galaxy-s25-ultra-fiyati,651960345.html",
                _FailingFetcher(),  # type: ignore[arg-type]
                storage=storage,
            )
            self.assertEqual(snap.price_tl, 69999.0)
            self.assertEqual(snap.seller, "İdefix")

    def test_all_seven_enhancements(self) -> None:
        # 1) Görsel PNG fiyat grafiği
        png = render_price_chart_png(
            title="Samsung Galaxy S25 Ultra (256 GB)",
            points=[("11-22 10:00", 71999.0), ("11-23 09:00", 69999.0)],
            target_price_tl=68000.0,
            six_month_low_tl=69500.0,
        )
        self.assertTrue(png.startswith(b"\x89PNG"))

        # 2) Renk ve resmi satıcı tespiti
        self.assertEqual(extract_color("Samsung Galaxy S25 Ultra 256 GB Titanyum Gri"), "Titanyum Gri")
        self.assertEqual(extract_color("Galaxy S25 Ultra 512GB Siyah"), "Titanyum Siyah")
        self.assertTrue(is_official_or_trusted_seller("Amazon.com.tr", "https://www.amazon.com.tr/dp/1"))
        self.assertTrue(is_official_or_trusted_seller("Hepsiburada", "https://www.hepsiburada.com/urun-p-1"))
        self.assertFalse(
            is_official_or_trusted_seller("Hepsiburada", "https://www.hepsiburada.com/urun-p-1?magaza=BaskaMagaza")
        )

        # 3) Tükenen / stokta olmayan ürün linki ("Stok Gelince Uyandır")
        oos_html = "<html><head><title>PS5 Pro Konsol</title></head><body><h1>PS5 Pro</h1><div>Bu ürün tükendi, gelince haber ver</div></body></html>"
        oos_snap = parse_product_page(oos_html, "https://www.teknosa.com/ps5-pro-p-1")
        self.assertIsNotNone(oos_snap)
        assert oos_snap is not None
        self.assertFalse(oos_snap.in_stock)
        self.assertEqual(oos_snap.price_tl, 0.0)

        # 4) Sesli alarm WAV üretimi
        wav = generate_alarm_wav_bytes(duration_sec=0.5)
        self.assertTrue(wav.startswith(b"RIFF"))

        # 5) Turbo mod & Excel CSV dışa aktarma
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg_path = tmp_path / "config.yaml"
            cfg_path.write_text(
                """
schedule:
  interval_minutes: 20
  turbo_interval_minutes: 4
  turbo_windows: ["23:45-01:30", "18:00-21:00"]
database:
  path: "prices.db"
""",
                encoding="utf-8",
            )
            cfg = load_config(cfg_path)
            self.assertEqual(get_dynamic_interval_minutes(cfg, datetime(2026, 11, 24, 0, 15)), (4, True))
            self.assertEqual(get_dynamic_interval_minutes(cfg, datetime(2026, 11, 24, 14, 0)), (20, False))

            st = PriceStorage(tmp_path / "prices.db")
            rid = st.create_run()
            st.save_offers(
                rid,
                [
                    PriceOffer(
                        "akakce",
                        "Amazon.com.tr",
                        "256 GB",
                        "S25 Ultra 256 GB Titanyum Siyah",
                        69999.0,
                        "https://www.amazon.com.tr/dp/1",
                        color="Titanyum Siyah",
                        is_official_seller=True,
                    )
                ],
            )
            st.finalize_run(rid, 1, 1, 0)
            csv_bytes = generate_csv_report_bytes(st)
            self.assertIn("Titanyum Siyah".encode("utf-8"), csv_bytes)

    def test_six_new_advanced_features(self) -> None:
        # 1) Eskiyi Getir / Ek Takas Desteği algılama
        self.assertEqual(
            extract_trade_in_info("Samsung Galaxy S25 Ultra 8.000 TL Ek Takas Desteği Fırsatı"),
            "+8.000 TL Ek Takas Desteği",
        )

        # 2) Direkt mağaza URL butonu kontrolü
        notifier = TelegramNotifier("tok", "123")
        kb = notifier.build_deal_keyboard(["256 GB"], direct_url="https://www.idefix.com/urun-1")
        self.assertEqual(kb["inline_keyboard"][0][0]["url"], "https://www.idefix.com/urun-1")

        # 3) Sahte İndirim (Fiyat Şişirme) Dedektörü + Kelime Takibi + Topluluk Fırsatları + Web Dashboard
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg_path = tmp_path / "config.yaml"
            cfg_path.write_text(
                """
product:
  variants: ["256 GB"]
alerts:
  min_drop_tl: 500
  min_drop_percent: 1.5
  cooldown_hours: 12
database:
  path: "prices.db"
""",
                encoding="utf-8",
            )
            cfg = load_config(cfg_path)
            st = PriceStorage(tmp_path / "prices.db")

            # Run 1: Normal fiyat 70.000 TL
            r1 = st.create_run()
            st.save_offers(r1, [PriceOffer("akakce", "MağazaA", "256 GB", "S25 Ultra 256GB", 70000.0, "https://a.com/1")])
            st.finalize_run(r1, 1, 1, 0)

            # Run 2: Fiyat 76.000 TL'ye şişiriliyor
            r2 = st.create_run()
            st.save_offers(r2, [PriceOffer("akakce", "MağazaA", "256 GB", "S25 Ultra 256GB", 76000.0, "https://a.com/1")])
            st.finalize_run(r2, 1, 1, 0)

            # Run 3: Fiyat 71.000 TL'ye "indiriliyor" -> PRICE_DROP ama Sahte İndirim Uyarısı üretmeli!
            r3 = st.create_run()
            fake_drop_offer = PriceOffer("akakce", "MağazaA", "256 GB", "S25 Ultra 256GB", 71000.0, "https://a.com/1")
            st.save_offers(r3, [fake_drop_offer])
            st.finalize_run(r3, 1, 1, 0)

            alerts = analyze_deals([fake_drop_offer], cfg, st, current_run_id=r3)
            self.assertEqual(len(alerts), 1)
            self.assertIn("yükseltilip indirilmiş", alerts[0].fake_discount_warning)

            # Kelime takip CRUD
            kid, is_new = st.add_or_update_watched_keyword("AirPods Pro 2", 9499.0, "Amazon.com.tr", "https://amazon.com.tr/1")
            self.assertTrue(is_new)
            self.assertEqual(len(st.list_watched_keywords()), 1)

            # Topluluk fırsat tekilleştirme
            self.assertFalse(st.is_community_deal_seen("https://forum.donanimhaber.com/konu-1"))
            st.mark_community_deal_seen("https://forum.donanimhaber.com/konu-1", "S25 Ultra Kupon", "https://forum.donanimhaber.com/konu-1")
            self.assertTrue(st.is_community_deal_seen("https://forum.donanimhaber.com/konu-1"))

            # Web Dashboard HTML render (tüm yönetim kontrolleriyle)
            dash_html = _render_dashboard_html(cfg, st)
            self.assertIn("Telegram Bot &amp; Kanal Yönetim Merkezi", dash_html)
            self.assertIn("AirPods Pro 2", dash_html)
            self.assertIn("/api/channel/send-post", dash_html)
            self.assertIn("/api/affiliate/save", dash_html)

            # Affiliate link dönüştürücü (Akakçe tag'ini silip kendi tag'imizi eklemeli)
            cfg.amazon_associate_tag = "firsattakibi-21"
            aff = convert_to_affiliate_url(
                "https://www.amazon.com.tr/dp/B0GSVQHRDT?tag=akakcetr-21&th=1", cfg
            )
            self.assertNotIn("akakcetr-21", aff)
            self.assertIn("tag=firsattakibi-21", aff)

            # Kanal yayıncı viral klavye ve tekilleştirme
            pub = ChannelPublisher(cfg, st, notifier)
            v_kb = pub.build_viral_channel_keyboard(aff, "Galaxy S25 Ultra", "69.999 TL")
            self.assertEqual(len(v_kb["inline_keyboard"]), 2)
            self.assertFalse(st.is_channel_deal_published("deal-1"))
            st.mark_channel_deal_published("deal-1", "Test Ürün", 1999.0, aff)
            self.assertTrue(st.is_channel_deal_published("deal-1"))

            # X (Twitter) tweet formatlama (280 karakter sınırı ve kupon kodu tırnaklı)
            xp = XPublisher(cfg)
            tw = xp.format_deal_tweet(
                title="Samsung Galaxy S25 Ultra 256 GB Titanyum Siyah Akıllı Telefon",
                price_tl=67999.0,
                seller="Amazon.com.tr",
                direct_url=aff,
                discount_pct=8,
                coupon_code="KASIM2000",
                include_channel_link=True,
            )
            self.assertLessEqual(len(tw), 280)
            self.assertIn('"KASIM2000"', tw)
            self.assertIn("t.me/firsat_takibi", tw)
            self.assertIn("#reklam #işbirliği", tw)
            self.assertIn("#reklam #işbirliği", pub._disclosure_suffix())
            htags = pub._build_hashtags(title="Bioderma Krem", seller="Amazon.com.tr", coupon_code="KOD50")
            self.assertIn("#amazon", htags)
            self.assertIn("#kozmetik", htags)
            self.assertIn("#kuponkodu", htags)
            tw_bio = xp.format_deal_tweet(
                title="Samsung Galaxy S25 Ultra 256 GB",
                price_tl=67999.0,
                seller="Amazon.com.tr",
                direct_url=aff,
                include_channel_link=False,
            )
            self.assertIn("profildeki Telegram", tw_bio)

            self.assertIsNone(st.get_last_x_tweet_time())
            st.mark_x_deal_posted("x-deal-1", "https://x.com/i/web/status/123")
            self.assertIsNotNone(st.get_last_x_tweet_time())
            self.assertEqual(st.count_x_tweets_today(), 1)

            # Çok satanlar / çok değerlendirilenler 30 günlük (1 aylık) fiyat düşüşü takibi
            s1 = st.record_bestseller_and_check_drop(
                product_key="https://www.amazon.com.tr/dp/B0FT1WW7HV",
                title="JBL Tour Pro 3",
                seller="Amazon.com.tr",
                category="Amazon Çok Satanlar • Elektronik",
                url="https://www.amazon.com.tr/dp/B0FT1WW7HV",
                image_url="",
                effective_price_tl=15100.0,
                compare_days=30,
            )
            self.assertTrue(s1["is_new"])
            s2 = st.record_bestseller_and_check_drop(
                product_key="https://www.amazon.com.tr/dp/B0FT1WW7HV",
                title="JBL Tour Pro 3",
                seller="Amazon.com.tr",
                category="Amazon Çok Satanlar • Elektronik",
                url="https://www.amazon.com.tr/dp/B0FT1WW7HV",
                image_url="",
                effective_price_tl=12399.0,
                compare_days=30,
            )
            self.assertFalse(s2["is_new"])
            self.assertEqual(s2["prev_price_tl"], 15100.0)
            self.assertEqual(s2["min_nd_tl"], 15100.0)
            self.assertEqual(st.count_tracked_bestsellers(), 1)

    def test_canonical_dedup_and_no_fake_discount(self) -> None:
        from src.popular_deals import PopularDealsHunter, canonicalize_deal_key

        u1 = "https://www.amazon.com.tr/Duracell-AA/dp/B089QXTQND/ref=zg_bs_g_electronics_d_sccl_3/704-8400330-0731040"
        u2 = "https://www.amazon.com.tr/Duracell-AA/dp/B089QXTQND/ref=zg_bs_g_electronics_d_sccl_3/700-5551830-9173509"
        self.assertEqual(canonicalize_deal_key(u1), "https://www.amazon.com.tr/dp/B089QXTQND")
        self.assertEqual(canonicalize_deal_key(u1), canonicalize_deal_key(u2))

        # Hepsiburada ve Trendyol farklı slug URL'leri aynı SKU/ID anahtarına birleşmeli (Ecowell örneği)
        hb_slug = "https://www.hepsiburada.com/ecowell-500ml-organik-bebek-temizleme-jeli-parabensiz-beyaz-renk-0-1-yas-icin-dogal-malzeme-p-HBV00000UG9R6?utm_source=telegram"
        hb_short = "https://www.hepsiburada.com/urun-p-HBV00000UG9R6?utm_medium=firsat_takibi"
        self.assertEqual(canonicalize_deal_key(hb_slug), "https://www.hepsiburada.com/urun-p-HBV00000UG9R6")
        self.assertEqual(canonicalize_deal_key(hb_slug), canonicalize_deal_key(hb_short))
        self.assertEqual(
            canonicalize_deal_key("https://www.trendyol.com/marka/urun-adi-p-84736291?boutiqueId=61"),
            "https://www.trendyol.com/urun-p-84736291",
        )

        with tempfile.TemporaryDirectory() as tmp:
            st = PriceStorage(Path(tmp) / "dedup.db")
            st.mark_channel_deal_published(u1, "Duracell AA Pil 10lu", 389.0, u1)
            self.assertTrue(st.is_channel_deal_published(u2, title="Duracell AA Pil 10lu", price_tl=389.0))
            self.assertTrue(
                st.is_channel_deal_published(
                    "https://www.trendyol.com/duracell-p-999",
                    title="Duracell AA Pil 10lu",
                    price_tl=385.0,
                )
            )
            # En az %5 daha ucuzsa tekrar yayınlanabilmeli
            self.assertFalse(st.is_channel_deal_published(u2, title="Duracell AA Pil 10lu", price_tl=350.0))

            # Türkçe karakter / noktalama farkı ve farklı kaynak anahtarı (TG:... vs doğrudan URL) kopyayı yakalamalı
            st.mark_channel_deal_published(
                "ONUAL_WEB:ecowell",
                "Ecowell 500ml Organik Bebek Temizleme Jeli Parabensiz Beyaz Renk 0 1 Yas Icin Dogal Malzeme",
                139.0,
                hb_slug,
            )
            self.assertTrue(
                st.is_channel_deal_published(
                    hb_short,
                    title="Ecowell 500Ml Organik Bebek Temizleme Jeli, Parabensiz, Beyaz Renk, 0-1 Yaş İçin Doğal Malzeme",
                    price_tl=139.0,
                    url=hb_short,
                )
            )

            # Fotoğrafsız ürünler kesinlikle kanala gönderilmemeli
            from src.channel_publisher import ChannelPublisher
            from src.config import load_config
            from src.notifier import TelegramNotifier
            from src.popular_deals import PopularDeal

            cfg = load_config()
            cfg.telegram_channel_id = "@firsat_takibi"
            cfg.channel_enabled = True
            notif = TelegramNotifier(bot_token="fake:token", chat_id="123")
            pub = ChannelPublisher(cfg, st, notif)
            no_photo_deal = PopularDeal(
                deal_key="https://www.hepsiburada.com/urun-p-HBCV0000123456",
                title="Fotoğrafsız Test Ürünü",
                seller="Hepsiburada",
                price_tl=199.0,
                previous_or_second_price_tl=399.0,
                discount_percent=50,
                direct_url="https://www.hepsiburada.com/urun-p-HBCV0000123456",
                image_url="",
            )
            self.assertFalse(pub.publish_popular_deal(no_photo_deal))

        # İndirimsiz çok satan ürünlerde sahte %10 üretilmemeli
        class _FakeFetcher:
            def get_text(self, url: str) -> str:
                if "trendyol" in url:
                    return (
                        '<a class="product-card" href="/test-p-123">'
                        '<img src="https://cdn.example.com/a.jpg" alt="Test Ürünü"/>'
                        "<span>499,00 TL</span></a>"
                    )
                return ""

        hunter = PopularDealsHunter(_FakeFetcher())  # type: ignore[arg-type]
        self.assertEqual(hunter._hunt_trendyol_bestsellers(limit=3, min_discount_percent=5), [])

        # Düşük tutarlı (%5 / 11 TL) indirimler elenmeli
        from src.popular_deals import PopularDeal, is_meaningful_deal

        small_deal = PopularDeal(
            deal_key="small-1",
            title="Laptop Stand",
            seller="Amazon.com.tr",
            price_tl=208.90,
            previous_or_second_price_tl=219.90,
            discount_percent=5,
            direct_url="https://www.amazon.com.tr/dp/B000000001",
        )
        big_deal = PopularDeal(
            deal_key="big-1",
            title="Kulaklık",
            seller="Amazon.com.tr",
            price_tl=1599.0,
            previous_or_second_price_tl=1999.0,
            discount_percent=20,
            direct_url="https://www.amazon.com.tr/dp/B000000002",
        )
        self.assertFalse(is_meaningful_deal(small_deal, min_discount_percent=10, min_savings_tl=100.0))
        self.assertTrue(is_meaningful_deal(big_deal, min_discount_percent=10, min_savings_tl=100.0))

    def test_habit_analyzer_and_recommendations(self) -> None:
        from src.habit_analyzer import analyze_shopping_habits, format_habit_report_html

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg_path = tmp_path / "config.yaml"
            cfg_path.write_text("database:\n  path: prices.db\n", encoding="utf-8")
            cfg = load_config(cfg_path)
            st = PriceStorage(tmp_path / "prices.db")
            st.mark_channel_deal_published(
                "https://www.amazon.com.tr/dp/B079XX76RM",
                "Bioderma Nemlendirici Krem",
                1100.0,
                "https://www.amazon.com.tr/dp/B079XX76RM",
            )
            st.mark_channel_deal_published(
                "https://www.trendyol.com/tcl-tv-p-1",
                "TCL 55 İnç Smart TV",
                55999.0,
                "https://www.trendyol.com/tcl-tv-p-1",
            )
            rep = analyze_shopping_habits(st, cfg)
            self.assertEqual(rep.total_published_deals, 2)
            self.assertTrue(len(rep.recommendations) >= 1)
            html_out = format_habit_report_html(rep)
            self.assertIn("ALIŞVERİŞ ALIŞKANLIĞI", html_out)

    def test_market_intel_and_predictions(self) -> None:
        from datetime import datetime
        from src.market_intel import MarketIntelligenceHunter, format_market_intel_html, predict_upcoming_campaigns

        preds = predict_upcoming_campaigns(datetime(2026, 11, 25, 12, 30))
        self.assertTrue(any("Efsane Cuma" in p.campaign_name for p in preds))

        class _FakeIntelFetcher:
            def get_text(self, url: str) -> str:
                if "bonus.com.tr" in url:
                    return '<a href="/kampanya-1">E-ticaret alışverişlerinize 250 TL Bonus fırsatı</a>'
                if "donanimhaber" in url:
                    return '<a href="/konu-1">#reklam #işbirliği Amazon kupon kodu SEPETYUZ ile %30 indirim</a>'
                return ""

        rep = MarketIntelligenceHunter(_FakeIntelFetcher()).collect_all(now=datetime(2026, 11, 25))  # type: ignore[arg-type]
        self.assertEqual(len(rep.bank_campaigns), 1)
        self.assertEqual(len(rep.hashtag_signals), 1)
        html_txt = format_market_intel_html(rep)
        self.assertIn("Banka Kampanyaları", html_txt)

    def test_gamified_campaign_scan_and_publish(self) -> None:
        from src.market_intel import MarketIntelligenceHunter

        class _FakeGameFetcher:
            def get_text(self, url: str) -> str:
                if "trendyol" in url:
                    return '<a href="/hediye-carki">Trendyol Şanslı Çark Çevir 500 TL Kupon Kazan</a>'
                return ""

        items = MarketIntelligenceHunter(_FakeGameFetcher()).scan_gamified_campaigns(limit=5)  # type: ignore[arg-type]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].source_type, "cark_kazan")
        self.assertIn("Çark", items[0].title)

    def test_stacking_glitch_multibuy_and_influencer(self) -> None:
        from src.deal_stacker import calculate_multibuy_unit_price, compute_best_stack, is_price_glitch
        from src.market_intel import MarketIntelligenceHunter

        # 1) Üçlü Birleştirme (Stacking)
        st_res = compute_best_stack(
            seller="Trendyol",
            title="Sony WH-1000XM5",
            list_price_tl=5000.0,
            effective_price_tl=3500.0,
            coupon_code="SONBAHAR",
        )
        self.assertIsNotNone(st_res)
        assert st_res is not None
        self.assertLess(st_res.net_final_tl, 3500.0)
        self.assertIn("Net", st_res.summary_line)

        # 2) Fiyat Hatası (%60+ ani düşüş)
        is_gl, pct = is_price_glitch(3800.0, 10000.0)
        self.assertTrue(is_gl)
        self.assertEqual(pct, 62)
        self.assertFalse(is_price_glitch(7000.0, 10000.0)[0])

        # 3) Birim Fiyat & Çoklu Alım (2 Al 1 Öde)
        mb = calculate_multibuy_unit_price("Gratis Yüz Temizleme Jeli 2 Al 1 Öde", 300.0)
        self.assertIsNotNone(mb)
        assert mb is not None
        self.assertEqual(mb.unit_price_tl, 150.0)
        self.assertEqual(mb.effective_discount_pct, 50)

        # 4) Influencer #reklam Kodu Radarı
        class _FakeInfFetcher:
            def get_text(self, url: str) -> str:
                if "duckduckgo" in url:
                    return '<a class="result__a" href="https://www.trendyol.com">#reklam #işbirliği "MODA350" kupon kodu ile 350 TL indirim</a>'
                return ""

        codes = MarketIntelligenceHunter(_FakeInfFetcher()).scan_influencer_hashtag_codes(limit=5)  # type: ignore[arg-type]
        self.assertEqual(len(codes), 1)
        self.assertEqual(codes[0].coupon_code, "MODA350")
        self.assertEqual(
            extract_coupon_info("Amazon İndirim Kodu YENİ KUPON KODU AKTİF! PERSEMBE1500- Tüm ürünlerde", 5000.0)[2],
            "PERSEMBE1500",
        )
        # "400TL" gibi tutar ifadeleri kupon kodu sayılmamalı, açılan sayfadaki 75AMZN yakalanmalı
        self.assertEqual(
            extract_coupon_info("Amazon 400TL'ye 125TL Kupon Kodu [Seçili Üyeler] - LiraKod", 400.0)[2],
            "",
        )
        self.assertEqual(
            extract_coupon_info("Bu kodu kopyalayın ve ödeme sırasında kullanın 75AMZN", 400.0)[2],
            "75AMZN",
        )

    def test_price_asc_below_market_scan(self) -> None:
        from src.popular_deals import PopularDealsHunter

        class _FakeAscFetcher:
            def get_text(self, url: str) -> str:
                if "PRICE_BY_ASC" in url:
                    return (
                        '<a class="product-card" href="/kulaklik-p-555">'
                        '<img src="https://cdn.example.com/k.jpg" alt="JBL Kulaklık"/>'
                        "<span>2.000,00 TL</span><span>1.200,00 TL</span></a>"
                    )
                return ""

        deals = PopularDealsHunter(_FakeAscFetcher())._hunt_price_asc_below_market(limit=4, min_discount_percent=25)  # type: ignore[arg-type]
        self.assertEqual(len(deals), 1)
        self.assertEqual(deals[0].discount_percent, 40)
        self.assertIn("PİYASA FİYATININ ALTINDA", deals[0].badge_label)

    def test_confidence_score_and_pending_approvals(self) -> None:
        from src.deal_stacker import compute_deal_confidence

        hi_score, _ = compute_deal_confidence(
            post_type="popular",
            title="TCL 55 TV",
            price_tl=55999.0,
            prev_price_tl=80149.0,
            discount_pct=34,
            is_official_seller=True,
        )
        lo_score, _ = compute_deal_confidence(
            post_type="influencer",
            title="Amazon Kupon",
            coupon_code="75AMZN",
        )
        self.assertGreaterEqual(hi_score, 70)
        self.assertLess(lo_score, 70)

        with tempfile.TemporaryDirectory() as tmp:
            st = PriceStorage(Path(tmp) / "approvals.db")
            aid = st.enqueue_pending_approval(
                deal_key="INF:75AMZN",
                post_type="influencer",
                title="Amazon 75AMZN Kodu",
                seller="Influencer",
                price_tl=0.0,
                coupon_code="75AMZN",
                url="https://www.amazon.com.tr",
                confidence=lo_score,
            )
            self.assertIsNotNone(aid)
            assert aid is not None
            self.assertEqual(len(st.list_pending_approvals()), 1)
            resolved = st.resolve_pending_approval(aid, approved=True)
            self.assertIsNotNone(resolved)
            self.assertEqual(len(st.list_pending_approvals()), 0)

    def test_repaired_scrapers_and_smoke_test(self) -> None:
        from src.models import ScrapeOutcome
        from src.runner import PriceTrackerRunner
        from src.scrapers.cimri import CimriScraper
        from src.scrapers.direct_stores import HepsiburadaScraper, VatanScraper

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg_path = tmp_path / "config.yaml"
            cfg_path.write_text("database:\n  path: prices.db\n", encoding="utf-8")
            cfg = load_config(cfg_path)
            st = PriceStorage(tmp_path / "prices.db")
            notifier = TelegramNotifier("", "")

            class _FakeRepairFetcher:
                def get_text(self, url: str) -> str:
                    if "hepsiburada.com" in url:
                        return (
                            '<li class="productListContent-1">'
                            '<a href="/samsung-galaxy-s25-ultra-512-gb-p-1">Samsung Galaxy S25 Ultra 512 GB 12 GB Ram</a>'
                            "<span>Peşin fiyatına 3 x 24.666 TL</span>"
                            "<span>84.599,06 TL %13 73.999 TL</span></li>"
                        )
                    if "vatanbilgisayar.com/arama" in url:
                        return "<div>ARADIĞINIZ KRİTERLERE UYGUN ÜRÜN BULUNAMADI</div>"
                    if "vatanbilgisayar.com/samsung-galaxy-s25-ultra" in url:
                        return '<h1>Samsung Galaxy S25 Ultra 5G 12/256 Gb Akıllı Telefon Titanyum Gri</h1><div data-price="93.499"></div>'
                    if "cimri.com/arama" in url:
                        raise RuntimeError("HTTP 403")
                    if "duckduckgo.com" in url and "cimri.com" in url:
                        return (
                            '<div class="result"><a class="result__a" href="https://www.cimri.com/s25-ultra">'
                            "Samsung Galaxy S25 Ultra 256 GB</a>"
                            '<div class="result__snippet">En ucuz 69.204,24 TL fiyatlarla</div></div>'
                        )
                    return ""

            fetcher = _FakeRepairFetcher()
            hb_offers = HepsiburadaScraper(fetcher=fetcher, config=cfg).scrape()  # type: ignore[arg-type]
            self.assertEqual(len(hb_offers), 1)
            self.assertEqual(hb_offers[0].price_tl, 73999.0)

            vt_offers = VatanScraper(fetcher=fetcher, config=cfg).scrape()  # type: ignore[arg-type]
            self.assertEqual(len(vt_offers), 1)
            self.assertEqual(vt_offers[0].price_tl, 93499.0)

            cm_offers = CimriScraper(fetcher=fetcher, config=cfg).scrape()  # type: ignore[arg-type]
            self.assertEqual(len(cm_offers), 1)
            self.assertEqual(cm_offers[0].price_tl, 69204.24)

            runner = PriceTrackerRunner(cfg, st, notifier)
            smoke = runner.run_live_smoke_test(
                existing_outcomes=[
                    ScrapeOutcome("hepsiburada", hb_offers, None, 1.1),
                    ScrapeOutcome("vatan", vt_offers, None, 0.8),
                    ScrapeOutcome("cimri", cm_offers, None, 1.2),
                ],
                send_telegram=False,
            )
            self.assertEqual(smoke["healthy"], 3)
            self.assertIn("DUMAN TESTİ", smoke["report_html"])

    def test_public_telegram_channel_and_hb_shortlink_resolver(self) -> None:
        from src.community_deals import CommunityDealsScanner

        class _FakeTgFetcher:
            def get_text(self, url: str) -> str:
                if "t.me/s/indirimkaplani" in url:
                    return (
                        '<div class="tgme_widget_message_wrap">'
                        '<div class="tgme_widget_message_text">'
                        "27 farklı model mevcut. Sepette PULSAR1000 koduyla fiyatı 1000 TL daha düşer. "
                        '<a href="https://app.hb.biz/Yld0l8uXQmRK">https://app.hb.biz/Yld0l8uXQmRK</a>'
                        "</div></div>"
                    )
                if "app.hb.biz/Yld0l8uXQmRK" in url:
                    return (
                        "<title>Turkuaz Time Satıcılı Seçili Ürünlerde PULSAR1000 Kodu İle "
                        "2500 Tl ve Üzerine 1000 TL İndirim 52191 Kampanyası - Hepsiburada</title>"
                    )
                return ""

        scanner = CommunityDealsScanner(_FakeTgFetcher())  # type: ignore[arg-type]
        found = scanner._scan_public_telegram_channels(include_all_hot_prices=True)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].coupon_code, "PULSAR1000")
        self.assertIn("Turkuaz Time", found[0].title)
        self.assertEqual(found[0].url, "https://app.hb.biz/Yld0l8uXQmRK")

        # onu.al/fkP3 -> doğrudan Hepsiburada mağaza linki, ürün görseli ve eski fiyat (545 TL -> 252 TL, -%54) çözümleme
        class _FakeOnuAlFetcher:
            def get_text(self, url: str) -> str:
                if "t.me/s/onual_firsat" in url:
                    return (
                        '<div class="tgme_widget_message_wrap"><div class="tgme_widget_message_text">'
                        "Magly Magnetic Cars Manyetik Araç Oyun Seti\n"
                        "🏷️ 252 TL (Son 6 Ayın En Düşük Fiyatı)\n"
                        "📂 Anne, Bebek, Oyuncak\n🛍️ Hepsiburada\n"
                        '<a href="https://onu.al/fkP3">https://onu.al/fkP3</a>'
                        "</div></div>"
                    )
                if "onu.al/fkP3" in url:
                    return (
                        "<h1>Magly Magnetic Cars Manyetik Araç Oyun Seti</h1>"
                        '<img src="https://productimages.hepsiburada.net/s/777/424-600/110002682443461.jpg"/>'
                        "<div>Fiyat Değişimi: 545 TL -> 252 TL</div>"
                        '<a href="https://zxro.com/u/?redirect=1&url=https%3A%2F%2Fwww.hepsiburada.com%2Fara%3Fq%3DHBC0000E3NYHO">Ürüne Git</a>'
                    )
                return ""

        oa_found = CommunityDealsScanner(_FakeOnuAlFetcher())._scan_public_telegram_channels(include_all_hot_prices=True)  # type: ignore[arg-type]
        self.assertEqual(len(oa_found), 1)
        self.assertEqual(oa_found[0].url, "https://www.hepsiburada.com/urun-pm-HBC0000E3NYHO")
        self.assertEqual(oa_found[0].price_tl, 252.0)
        self.assertEqual(oa_found[0].previous_price_tl, 545.0)
        self.assertEqual(oa_found[0].discount_percent, 54)
        self.assertIn("productimages.hepsiburada.net", oa_found[0].image_url)

        # OnuAl canlı web akışından ('1 dakika önce' Telegram'a düşmeden önce) #kupon ve direkt mağaza linki çözümleme
        class _FakeOnuAlWebFetcher:
            def get_text(self, url: str) -> str:
                if url == "https://www.onual.com/":
                    return (
                        '<a href="urun/karaca-blendfit-go-personal-smoothie-blender-p-3344440#fiyat=1198&amp;kupon=MEGA300&amp;link_y=2cqx0sxdzhr4q">'
                        "<span>Son 6 Ayın En Düşük Fiyatı</span><span>12 dakika önce</span>"
                        '<img src="https://cdn.dsmcdn.com/karaca-blender.jpg"/>'
                        '<img src="./images/pazaryeri/trendyol-logo.svg"/>'
                        "</a>"
                    )
                if "onual.com/urun/karaca-blendfit" in url:
                    return (
                        "<h1>Karaca Blendfit Go Personal Kişisel Smoothie Blender</h1>"
                        "<div>Fiyat Değişimi: 1.899 TL -> 1.198 TL</div>"
                        '<a href="https://zxro.com/u/?redirect=1&url=https%3A%2F%2Fwww.trendyol.com%2Fkaraca%2Fblendfit-p-12345">Ürüne Git</a>'
                    )
                return ""

        web_found = CommunityDealsScanner(_FakeOnuAlWebFetcher())._scan_onual_live_web_feed(max_items=3)  # type: ignore[arg-type]
        self.assertEqual(len(web_found), 1)
        self.assertEqual(web_found[0].url, "https://www.trendyol.com/karaca/blendfit-p-12345")
        self.assertEqual(web_found[0].coupon_code, "MEGA300")
        self.assertEqual(web_found[0].price_tl, 1198.0)
        self.assertEqual(web_found[0].previous_price_tl, 1899.0)
        self.assertEqual(web_found[0].seller, "Trendyol")

        # ALT500 / TAKIP / SEPETTE100 gibi özel kupon parametreleri sahte kod değil açık talimat üretmeli
        from src.community_deals import decode_onual_coupon_param

        c_code, c_howto = decode_onual_coupon_param("ALT500")
        self.assertEqual(c_code, "")
        self.assertIn("Ürünün altındaki 500 TL'lik indirim kuponunu", c_howto)
        real_code, real_howto = decode_onual_coupon_param("MEGA300")
        self.assertEqual(real_code, "MEGA300")
        self.assertEqual(real_howto, "")

    def test_unit_price_false_discount_rejected_and_realtime_market_avg(self) -> None:
        from src.popular_deals import PopularDeal, PopularDealsHunter, verify_and_attach_realtime_avg

        # 1) Amazon'daki '(9.163,33 TL / 100 g)' birim fiyatı eski fiyat sanılmamalı
        class _FakeUnitFetcher:
            def get_text(self, url: str) -> str:
                if "amazon.com.tr/dp/B097CB4WPM" in url:
                    return (
                        '<div id="corePriceDisplay_desktop_feature_div">'
                        '<span>(<span class="a-price a-text-price"><span class="a-offscreen">9.163,33 TL</span></span> / 100 g)</span>'
                        "</div>"
                    )
                if "akakce.com/arama" in url:
                    return (
                        '<span class="pt_v8">380,00 TL</span>'
                        '<span class="pt_v8">410,00 TL</span>'
                        '<span class="pt_v8">395,00 TL</span>'
                    )
                return ""

        hunter = PopularDealsHunter(_FakeUnitFetcher())  # type: ignore[arg-type]
        old_p, pct = hunter._fetch_amazon_discount("https://www.amazon.com.tr/dp/B097CB4WPM", 274.90)
        self.assertIsNone(old_p)
        self.assertEqual(pct, 0)

        # 2) Yayın öncesi anlık ortalama fiyat kontrolü: 9.163,33 TL sahte eski fiyatı sıfırlayıp
        # gerçek piyasa ortalamasını (395 TL) hesaplamalı ve %30 gerçek indirimle doğrulamalı
        flormar = PopularDeal(
            deal_key="https://www.amazon.com.tr/dp/B097CB4WPM",
            title="Flormar Nemlendirici Parlak Ruj Sheer Up Lipstick",
            seller="Amazon.com.tr",
            price_tl=274.90,
            previous_or_second_price_tl=9163.33,
            discount_percent=97,
            direct_url="https://www.amazon.com.tr/dp/B097CB4WPM",
        )
        ok = verify_and_attach_realtime_avg(flormar, _FakeUnitFetcher(), min_discount_percent=25)  # type: ignore[arg-type]
        self.assertTrue(ok)
        self.assertEqual(flormar.market_avg_price_tl, 395.0)
        self.assertEqual(flormar.previous_or_second_price_tl, 395.0)
        self.assertEqual(flormar.discount_percent, 30)

        # 3) Canlı doğrulanamayan (emin olunmayan) kuponlar gönderiden çıkarılmalı, doğrulanmış olanlar kalmalı
        from src.popular_deals import verify_coupon_live

        class _FakeCouponVerifierFetcher:
            def get_text(self, url: str) -> str:
                if "indirim-kodlari" in url:
                    return "<div>10 saat kaldı | OYUNDA75 Kopyala</div>"
                if "hepsiburada.com/kampanya" in url:
                    return "<html><body>Turkuaz Time PULSAR1000 kodu ile 1000 TL indirim</body></html>"
                return "<html><body>Normal ürün sayfası</body></html>"

        vf = _FakeCouponVerifierFetcher()
        ok1, c1, _ = verify_coupon_live("PULSAR1000", "", "https://www.hepsiburada.com/kampanya", vf)  # type: ignore[arg-type]
        self.assertTrue(ok1)
        self.assertEqual(c1, "PULSAR1000")

        ok2, c2, _ = verify_coupon_live("OYUNDA75", "", "https://www.hepsiburada.com/normal-urun", vf)  # type: ignore[arg-type]
        self.assertTrue(ok2)
        self.assertEqual(c2, "OYUNDA75")

        ok_bad, c_bad, _ = verify_coupon_live("SAHTEKOD99", "", "https://www.hepsiburada.com/normal-urun", vf)  # type: ignore[arg-type]
        self.assertFalse(ok_bad)
        self.assertEqual(c_bad, "")

    def test_full_dashboard_control_center_and_oracle_bundle(self) -> None:
        import io
        import zipfile
        import requests
        import yaml
        from src.config import load_config
        from src.dashboard import start_dashboard_server

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg = load_config()
            cfg.config_path = tmp_path / "config.yaml"
            cfg.persist_yaml()
            cfg.dashboard_host = "127.0.0.1"
            cfg.dashboard_port = 0
            cfg.dashboard_password = "TestOraclePassword123"
            st = PriceStorage(tmp_path / "dash.db")

            srv = start_dashboard_server(cfg, st)
            self.assertIsNotNone(srv)
            assert srv is not None
            try:
                port = srv.server_port
                base = f"http://127.0.0.1:{port}"

                # Şifresiz API çağrısı 401 dönmeli, GET ise giriş ekranı göstermeli
                r_unauth = requests.post(f"{base}/api/daemon/toggle", json={"paused": True}, timeout=5)
                self.assertEqual(r_unauth.status_code, 401)
                r_login_page = requests.get(base, timeout=5)
                self.assertIn("Yönetim Merkezi Girişi", r_login_page.text)

                # Giriş yapıp oturum çerezi al
                sess = requests.Session()
                r_login = sess.post(f"{base}/api/auth/login", json={"password": "TestOraclePassword123"}, timeout=5)
                self.assertEqual(r_login.status_code, 200)
                self.assertTrue(r_login.json()["ok"])

                # 8 sekmeli tam yönetim paneli açılmalı
                r_home = sess.get(base, timeout=5)
                self.assertIn("Oracle Cloud 7/24 Yayın", r_home.text)
                self.assertIn("Kanal Stüdyosu", r_home.text)
                self.assertIn("Mağaza Kaynakları", r_home.text)

                # Panelden ayarları değiştir ve kalıcılığı doğrula
                r_ch = sess.post(
                    f"{base}/api/channel/settings",
                    json={"only_photo_deals": True, "prevent_duplicate_deals": True, "min_discount_percent": 30},
                    timeout=5,
                )
                self.assertTrue(r_ch.json()["ok"])
                self.assertEqual(cfg.min_popular_discount_percent, 30)

                r_src = sess.post(
                    f"{base}/api/sources/settings",
                    json={"sources": {"n11": False, "akakce": True}, "timeout_seconds": 25, "max_workers": 6},
                    timeout=5,
                )
                self.assertTrue(r_src.json()["ok"])
                self.assertFalse(cfg.sources["n11"])
                self.assertEqual(cfg.timeout_seconds, 25)

                # Oracle 7/24 ZIP paketini indir ve içeriğini doğrula
                r_zip = sess.get(f"{base}/api/oracle/bundle.zip?pwd=CloudSecret99", timeout=15)
                self.assertEqual(r_zip.status_code, 200)
                with zipfile.ZipFile(io.BytesIO(r_zip.content), "r") as zf:
                    names = set(zf.namelist())
                    self.assertIn("main.py", names)
                    self.assertIn("deploy/oracle_setup.sh", names)
                    self.assertIn("Dockerfile", names)
                    self.assertIn("docker-compose.yml", names)
                    cloud_cfg = yaml.safe_load(zf.read("config.yaml").decode("utf-8"))
                    self.assertEqual(cloud_cfg["dashboard"]["host"], "0.0.0.0")
                    env_inside = zf.read(".env").decode("utf-8")
                    self.assertIn("DASHBOARD_PASSWORD=CloudSecret99", env_inside)
            finally:
                srv.shutdown()
                srv.server_close()

        # Mağaza 403 verdiğinde URL slug -> Akakçe kurtarma ve allow_fallback testi
        from src.url_tracker import fetch_watched_product

        class _Fake403MediaMarktFetcher:
            def get_text(self, url: str) -> str:
                if "mediamarkt.com.tr" in url:
                    raise RuntimeError("HTTP 403")
                if "akakce.com/arama" in url:
                    return (
                        '<li class="p_v8"><a href="/oyun-kolu/en-ucuz-logitech-f710-fiyati,1.html" '
                        'title="Logitech G F710 Kablosuz Gamepad"><span class="pt_v8">1.499,00 TL</span></a></li>'
                    )
                return ""

        mm_url = "https://www.mediamarkt.com.tr/tr/product/_logitech-g-f710-cift-titresimli-geri-beslemeli-kablosuz-gamepad-gri-1101068.html"
        snap_ok = fetch_watched_product(mm_url, _Fake403MediaMarktFetcher(), allow_fallback=True)  # type: ignore[arg-type]
        self.assertEqual(snap_ok.price_tl, 1499.0)
        self.assertIn("Logitech", snap_ok.title)
        self.assertEqual(snap_ok.url, mm_url)

    def test_amazon_depo_hunter_storage_and_dashboard(self) -> None:
        from src.amazon_depo import AmazonDepoHunter
        from src.config import load_config

        class _FakeDepoFetcher:
            def get_text(self, url: str) -> str:
                if "warehouse-deals" in url:
                    return (
                        '<div data-component-type="s-search-result" data-asin="B0CZDCWM48">'
                        "<h2>JBL GO4 Bluetooth Hoparlör Su Geçirmez</h2>"
                        '<img class="s-image" src="https://m.media-amazon.com/images/I/jblgo4.jpg"/>'
                        '<div data-cy="secondary-offer-recipe">Diğer satın alma seçenekleri 499,00 TL (1 İkinci El ürün)</div>'
                        "</div>"
                    )
                if "aodAjaxMain" in url:
                    return (
                        '<span id="aod-asin-title-text">JBL GO4 Bluetooth Hoparlör Su Geçirmez</span>'
                        '<img id="aod-asin-image-id" src="https://m.media-amazon.com/images/I/jblgo4.jpg"/>'
                        '<div id="aod-offer">'
                        '<span class="a-price-whole">2.499,</span><span class="a-price-fraction">00</span>'
                        '<div id="aod-offer-heading">Yeni</div><div id="aod-offer-soldBy">Satıcı Amazon.com.tr</div>'
                        "</div>"
                        '<div id="aod-offer">'
                        '<span class="a-price-whole">499,</span><span class="a-price-fraction">00</span>'
                        '<div id="aod-offer-heading">İkinci El - Yeni Gibi</div>'
                        '<div id="aod-offer-soldBy">Satıcı Amazon Depo</div>'
                        "<span data-aod-atc-action='{\"oid\":\"YS9ssVumaFVfEncryptedDepoOID123==\"}'></span>"
                        "</div>"
                    )
                return ""

        hunter = AmazonDepoHunter(_FakeDepoFetcher())  # type: ignore[arg-type]
        deals, sold_out = hunter.hunt(
            categories=["tumu"],
            scan_popular_pool=False,
            min_discount_percent=40,
            super_discount_percent=75,
        )
        self.assertEqual(len(deals), 1)
        self.assertEqual(sold_out, set())
        self.assertEqual(deals[0].asin, "B0CZDCWM48")
        self.assertEqual(deals[0].discount_percent, 80)
        self.assertEqual(deals[0].offer_listing_id, "YS9ssVumaFVfEncryptedDepoOID123==")
        self.assertTrue(deals[0].is_super_steal)
        self.assertIn("smid=A215JX4S9CANSO", deals[0].url)
        self.assertIn("aod=1&smid=A215JX4S9CANSO", deals[0].cart_url())

        with tempfile.TemporaryDirectory() as tmp:
            st = PriceStorage(Path(tmp) / "depo.db")
            st.add_depo_watchlist("B0D92CF1P5", note="Karcher K4", min_discount_percent=30)
            self.assertEqual(len(st.list_depo_watchlist()), 1)
            st.upsert_depo_deals(deals)
            saved = st.list_depo_deals(min_discount=75)
            self.assertEqual(len(saved), 1)
            cfg = load_config()
            html_out = _render_dashboard_html(cfg, st)
            self.assertIn("Amazon Depo Özel Radarı", html_out)
            self.assertIn("JBL GO4 Bluetooth Hoparlör", html_out)
            self.assertIn("-%80", html_out)
            self.assertIn("aod=1&amp;smid=A215JX4S9CANSO", html_out)
            self.assertIn("amazon-depo-fast.user.js", html_out)
            removed = st.prune_stale_depo_deals({"B0NEWASIN1"})
            self.assertEqual(removed, 1)
            self.assertEqual(len(st.list_depo_deals()), 0)


if __name__ == "__main__":
    unittest.main()

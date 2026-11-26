"""Unit tests for Phase 1 critical fixes: Booksy, Google enrichment verification,
SPAeden filtering, merge_results, append_to_csv_master fill-blanks, anti-ICP, and adjacent industries."""

import csv
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from attio_sync import (
    SOUNDYOU_INDUSTRIES,
    classify_soundyou_industries,
    is_non_icp_business,
)
from scraper import (
    _humanize_booksy_slug_from_url,
    _is_booksy_noise_name,
    _is_confident_google_match,
    _needs_website_enrichment,
    append_to_csv_master,
    extract_booksy_profile_data,
    get_booksy_category_slugs,
    is_catalog_url,
    merge_results,
    parse_booksy_listings,
    parse_spaeden_rankings,
)


class TestBooksyFixes:
    def test_rating_badges_detected_as_noise(self):
        assert _is_booksy_noise_name("5.0") is True
        assert _is_booksy_noise_name("4.9") is True
        assert _is_booksy_noise_name("4,8 (120)") is True
        assert _is_booksy_noise_name("Nowy") is True
        assert _is_booksy_noise_name("Sakura Kama Kobido") is False

    def test_humanize_booksy_slug_from_url(self):
        url = "https://booksy.com/pl-pl/337252_sakura-kama-kobido_masaz_23280_lodz"
        assert _humanize_booksy_slug_from_url(url) == "Sakura Kama Kobido"

        url_spa = "https://booksy.com/pl-pl/301773_apolonia-relax-spa_masaz_23280_lodz"
        assert _humanize_booksy_slug_from_url(url_spa) == "Apolonia Relax SPA"

    def test_parse_booksy_ignores_rating_link_and_takes_real_name(self):
        html = """
        <html><body>
          <div class="card">
            <a href="/pl-pl/301773_apolonia-relax-spa_masaz_23280_lodz#ba_s=sr_1">5.0</a>
            <a href="/pl-pl/301773_apolonia-relax-spa_masaz_23280_lodz#ba_s=sr_1">
              <h3>Apolonia Relax SPA</h3>
            </a>
          </div>
          <div class="card">
            <a href="/pl-pl/329423_sculptura-health-studio_masaz_23280_lodz#ba_s=sr_1">4.9</a>
          </div>
        </body></html>
        """
        results = parse_booksy_listings(html, "masaz")
        assert len(results) == 2
        names = {r["name"] for r in results}
        assert "5.0" not in names
        assert "4.9" not in names
        assert "Apolonia Relax SPA" in names
        assert "Sculptura Health Studio" in names

    def test_extract_booksy_profile_nuxt3_and_jsonld(self):
        html = """
        <html><body>
          <h1>Studio Kobido Harmonia</h1>
          <script type="application/ld+json">
            {
              "@type": "BeautySalon",
              "name": "Studio Kobido Harmonia",
              "telephone": "+48 501 202 303",
              "email": "kontakt@kobido-harmonia.pl",
              "url": "https://kobido-harmonia.pl",
              "sameAs": [
                "https://www.instagram.com/kobido.harmonia/",
                "https://www.facebook.com/kobidoharmonia"
              ],
              "address": {
                "streetAddress": "ul. Piotrkowska 10",
                "postalCode": "90-001",
                "addressLocality": "Łódź"
              }
            }
          </script>
        </body></html>
        """
        session = MagicMock()
        resp = MagicMock(status_code=200, text=html)
        resp.raise_for_status.return_value = None
        session.get.return_value = resp

        data = extract_booksy_profile_data("https://booksy.com/pl-pl/123_test_masaz_1_lodz", session=session)
        assert data["name"] == "Studio Kobido Harmonia"
        assert data["website"] == "https://kobido-harmonia.pl"
        assert data["phone"] == "+48 501 202 303"
        assert "kontakt@kobido-harmonia.pl" in data["emails"]
        assert data["instagram"] == "https://www.instagram.com/kobido.harmonia"
        assert data["facebook"] == "https://www.facebook.com/kobidoharmonia"

    def test_booksy_category_slugs_multi_and_non_booksy_empty(self):
        assert "kosmetyczka" in get_booksy_category_slugs("uroda")
        assert "paznokcie" in get_booksy_category_slugs("uroda")
        assert "barber-shop" in get_booksy_category_slugs("fryzjer")
        assert get_booksy_category_slugs("hotel") == []
        assert get_booksy_category_slugs("kawiarnia") == []
        assert get_booksy_category_slugs("restaurant") == []
        assert get_booksy_category_slugs("sklep") == []


class TestGoogleEnrichmentAndCatalogGuards:
    def test_freshastatus_is_catalog_url(self):
        assert is_catalog_url("https://www.freshastatus.com/") is True
        assert is_catalog_url("https://enklawa-spa.pl/") is False

    def test_confident_google_match_rejects_known_false_positives(self):
        assert _is_confident_google_match(
            "SHANTI SPA", "Salon Santai SPA", candidate_address="ul. X, Łódź", location="Łódź"
        ) is False
        assert _is_confident_google_match(
            "Spa Anety Rosińskiej", "Salon Santai", candidate_address="ul. X, Łódź", location="Łódź"
        ) is False
        assert _is_confident_google_match(
            "Namassage Wrocław", "Hypnos Lounge Spa Wrocław", candidate_address="Wrocław", location="Wrocław"
        ) is False
        assert _is_confident_google_match(
            "edycja Perfect SPA Awards", "DoubleTree by Hilton Lodz", candidate_address="Łódź", location="Łódź"
        ) is False

    def test_confident_google_match_accepts_genuine_venues(self):
        assert _is_confident_google_match(
            "Enklawa Day SPA", "Enklawa Institute & Day Spa", candidate_address="ul. Z, Szczecin", location="Szczecin"
        ) is True
        assert _is_confident_google_match(
            "Klinika Urody Evita SPA", "Evita Klinika Urody", candidate_address="ul. A, Szczecin", location="Szczecin"
        ) is True

    def test_needs_website_enrichment_includes_catalog_and_social(self):
        assert _needs_website_enrichment({"website": ""}) is True
        assert _needs_website_enrichment({"website": "https://booksy.com/pl-pl/123_abc"}) is True
        assert _needs_website_enrichment({"website": "https://www.facebook.com/moj_salon"}) is True
        assert _needs_website_enrichment({"website": "https://www.instagram.com/moj_salon"}) is True
        assert _needs_website_enrichment({"website": "https://moj-salon.pl"}) is False


class TestSpaedenMergeAndCsvFillBlanks:
    def test_spaeden_drops_awards_noise_heading(self):
        text = """
        1. edycja Perfect SPA Awards – Warszawa
        2. Bristol Art & Medical SPA – Busko-Zdrój
        """
        results = parse_spaeden_rankings(text)
        names = [r["name"] for r in results]
        assert not any("edycja" in n.lower() for n in names)
        assert any("Bristol" in n for n in names)

    def test_merge_results_matches_by_phone_and_upgrades_catalog_website(self):
        booksy_rec = {
            "name": "Harmonia Ciała",
            "formatted_address": "",
            "formatted_phone_number": "505 572 521",
            "website": "https://booksy.com/pl-pl/1_harmonia_masaz_1_wroclaw",
            "facebook": "https://www.facebook.com/harmoniaciala",
            "emails": ["booksy@harmonia.pl"],
            "sources": ["booksy"],
        }
        google_rec = {
            "name": "Harmonia Ciała Wrocław SPA",
            "formatted_address": "ul. Rynek 1, Wrocław",
            "formatted_phone_number": "+48 505 572 521",
            "website": "https://harmonia-ciala.pl",
            "emails": ["recepcja@harmonia-ciala.pl"],
            "sources": ["google"],
        }
        merged = merge_results([booksy_rec], [google_rec])
        assert len(merged) == 1
        rec = merged[0]
        assert rec["website"] == "https://harmonia-ciala.pl"
        assert rec["facebook"] == "https://www.facebook.com/harmoniaciala"
        assert "booksy@harmonia.pl" in rec["emails"]
        assert "recepcja@harmonia-ciala.pl" in rec["emails"]
        assert set(rec["sources"]) == {"booksy", "google"}

    def test_append_to_csv_master_fills_blanks_without_overwriting(self, tmp_path):
        csv_path = str(tmp_path / "SoundYouLeads.csv")
        initial = [{
            "branża": "SPA & Wellness",
            "województwo": "Dolnośląskie",
            "miasto": "Wrocław",
            "name": "Oaza Spokoju",
            "formatted_address": "ul. Leśna 3, Wrocław",
            "formatted_phone_number": "600 100 200",
            "website": "https://booksy.com/pl-pl/99_oaza_masaz_1_wroclaw",
            "emails": [],
            "sources": ["booksy"],
        }]
        append_to_csv_master(initial, csv_path)

        enriched = [{
            "branża": "SPA & Wellness",
            "województwo": "Dolnośląskie",
            "miasto": "Wrocław",
            "name": "Oaza Spokoju SPA",
            "formatted_address": "INNY ADRES NIE NADPISUJ",
            "formatted_phone_number": "+48 600 100 200",
            "website": "https://oaza-spokoju.pl",
            "instagram": "https://www.instagram.com/oazaspokoju",
            "emails": ["kontakt@oaza-spokoju.pl"],
            "sources": ["google"],
        }]
        append_to_csv_master(enriched, csv_path)

        with open(csv_path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1
        assert rows[0]["Name"] == "Oaza Spokoju"
        assert rows[0]["Address"] == "ul. Leśna 3, Wrocław"
        assert rows[0]["Website"] == "https://oaza-spokoju.pl"
        assert rows[0]["Instagram"] == "https://www.instagram.com/oazaspokoju"
        assert rows[0]["Email 1"] == "kontakt@oaza-spokoju.pl"


class TestAntiIcpAndAdjacentIndustries:
    def test_anti_icp_blocks_erotic_and_sauna_manufacturers(self):
        assert is_non_icp_business(name="Hypnos Lounge Spa | Erotic Massage | Nuru Massage")[0] is True
        assert is_non_icp_business(name="Alan - Sauny, Łaźnie, Kominki")[0] is True
        assert is_non_icp_business(name="SPA System", website="http://www.hydromasaze.com/")[0] is True
        assert is_non_icp_business(name="Kandara Day SPA", website="https://kandara.pl")[0] is False

    def test_pet_grooming_routed_out_of_human_spa_and_hair(self):
        assert classify_soundyou_industries("spa", name="Psie SPA Burek") == ["Lokal Usługowy / Inne"]
        assert classify_soundyou_industries("fryzjer", name="Psi Fryzjer Łapka") == ["Lokal Usługowy / Inne"]

    @pytest.mark.parametrize("raw,name,expected", [
        ("grota solna", "Grota Solna Solanka", "SPA & Wellness"),
        ("normobaria", "AX MED Normobaria", "SPA & Wellness"),
        ("head spa", "Ikigai Head Spa", "Kobido & Masaż"),
        ("reformer pilates", "Studio Reformer Pilates", "Joga / Pilates / Medytacja"),
        ("klinika weterynaryjna", "Przychodnia Weterynaryjna VitaVet", "Poczekalnia / Gabinet"),
        ("salon sukien ślubnych", "Atelier Sukien Ślubnych Bella", "Sklep / Retail"),
        ("studio ems", "BodyTech Studio EMS", "Fitness / Siłownia"),
        ("wine bar", "Winiarnia & Wine Bar Korkociąg", "Restauracje"),
        ("matcha", "Matcha Bar & Lodziarnia", "Kawiarnie"),
        ("glamping", "Leśny Glamping Mazury", "Hotele i Pensjonaty"),
    ])
    def test_adjacent_industries_map_strictly_to_canonical_12(self, raw, name, expected):
        inds = classify_soundyou_industries(raw, name=name)
        assert expected in inds
        assert all(i in SOUNDYOU_INDUSTRIES for i in inds)

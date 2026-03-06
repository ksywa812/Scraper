"""Tests for pure logic functions — no mocks, no network, no browser."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from scraper import (
    strip_accents,
    slugify,
    is_catalog_url,
    map_query_to_category,
    normalize_query_for_sources,
    fresha_business_type_for_query,
    category_keywords_for_query,
    fixly_path_for_query,
    cylex_search_urls,
    is_probably_path,
    _deobfuscate_emails,
    annotate_sources,
)


# ---------------------------------------------------------------------------
# strip_accents
# ---------------------------------------------------------------------------

class TestStripAccents:
    def test_polish_text(self):
        assert strip_accents("łódź") == "lodz"

    def test_all_polish_chars(self):
        result = strip_accents("ąćęłńóśźż")
        assert result == "acelnoszz"

    def test_uppercase_polish(self):
        result = strip_accents("ŁÓDŹ")
        assert result == "LODZ"

    def test_empty_string(self):
        assert strip_accents("") == ""

    def test_plain_ascii_unchanged(self):
        assert strip_accents("hello world") == "hello world"


# ---------------------------------------------------------------------------
# slugify
# ---------------------------------------------------------------------------

class TestSlugify:
    def test_spaces_become_hyphens(self):
        assert slugify("salon wellness") == "salon-wellness"

    def test_polish_chars(self):
        assert slugify("łódź") == "lodz"

    def test_uppercase_lowercased(self):
        assert slugify("Centrum Spa") == "centrum-spa"

    def test_digits_preserved(self):
        assert "2" in slugify("spa 2000")

    def test_empty_string(self):
        assert slugify("") == ""

    def test_none_safe(self):
        assert slugify(None) == ""

    def test_multiple_spaces_collapsed(self):
        assert slugify("spa   centrum") == "spa-centrum"

    def test_multiple_hyphens_collapsed(self):
        result = slugify("spa--centrum")
        assert "--" not in result


# ---------------------------------------------------------------------------
# is_catalog_url
# ---------------------------------------------------------------------------

class TestIsCatalogUrl:
    def test_booksy(self):
        assert is_catalog_url("https://booksy.com/pl-pl/some-salon") is True

    def test_pkt(self):
        assert is_catalog_url("https://www.pkt.pl/firmy/salon") is True

    def test_panoramafirm(self):
        assert is_catalog_url("https://panoramafirm.pl/firma/123") is True

    def test_fresha(self):
        assert is_catalog_url("https://www.fresha.com/pl/a/salon") is True

    def test_own_website_returns_false(self):
        assert is_catalog_url("https://salon-wellness.pl") is False

    def test_none_returns_false(self):
        assert is_catalog_url(None) is False

    def test_empty_string_returns_false(self):
        assert is_catalog_url("") is False


# ---------------------------------------------------------------------------
# map_query_to_category
# ---------------------------------------------------------------------------

class TestMapQueryToCategory:
    def test_spa(self):
        assert map_query_to_category("spa") == "spa"

    def test_masaz(self):
        assert map_query_to_category("masaż") == "spa"

    def test_joga(self):
        assert map_query_to_category("joga") == "joga"

    def test_fizjoterapia(self):
        assert map_query_to_category("fizjoterapia") == "fizjoterapia"

    def test_uroda(self):
        assert map_query_to_category("uroda") == "uroda"

    def test_fryzjer(self):
        assert map_query_to_category("fryzjer") == "fryzjer"

    def test_hotel(self):
        assert map_query_to_category("hotel spa") == "spa"

    def test_unknown_returns_none(self):
        assert map_query_to_category("nieznana kategoria xyz") is None

    def test_none_returns_none(self):
        assert map_query_to_category(None) is None

    def test_empty_returns_none(self):
        assert map_query_to_category("") is None


# ---------------------------------------------------------------------------
# normalize_query_for_sources
# ---------------------------------------------------------------------------

class TestNormalizeQueryForSources:
    def test_known_category_returns_category(self):
        assert normalize_query_for_sources("spa") == "spa"

    def test_unknown_returns_passthrough(self):
        query = "super unique xyz"
        assert normalize_query_for_sources(query) == query

    def test_masaz_maps_to_spa(self):
        assert normalize_query_for_sources("masaż relaksacyjny") == "spa"


# ---------------------------------------------------------------------------
# fresha_business_type_for_query
# ---------------------------------------------------------------------------

class TestFreshaBusinessTypeForQuery:
    def test_spa(self):
        assert fresha_business_type_for_query("spa") == "spas"

    def test_wellness(self):
        assert fresha_business_type_for_query("wellness") == "spas"

    def test_fizjoterapia(self):
        assert fresha_business_type_for_query("fizjoterapia") == "therapy-centers"

    def test_uroda(self):
        assert fresha_business_type_for_query("uroda") == "beauty-salons"

    def test_fryzjer(self):
        assert fresha_business_type_for_query("fryzjer") == "hair-salons"

    def test_joga(self):
        assert fresha_business_type_for_query("joga") == "yoga"

    def test_unknown_defaults_to_spas(self):
        assert fresha_business_type_for_query("cokolwiek") == "spas"


# ---------------------------------------------------------------------------
# category_keywords_for_query
# ---------------------------------------------------------------------------

class TestCategoryKeywordsForQuery:
    def test_spa_returns_list(self):
        kws = category_keywords_for_query("spa")
        assert isinstance(kws, list)
        assert len(kws) > 0

    def test_spa_contains_masaz(self):
        kws = category_keywords_for_query("spa")
        assert any("masaz" in k or "masaż" in k for k in kws)

    def test_hotel_returns_hotel_keyword(self):
        kws = category_keywords_for_query("hotel")
        assert any("hotel" in k for k in kws)

    def test_unknown_returns_empty_list(self):
        kws = category_keywords_for_query("xyz nieznana fraza")
        assert kws == []


# ---------------------------------------------------------------------------
# fixly_path_for_query
# ---------------------------------------------------------------------------

class TestFixlyPathForQuery:
    def test_spa(self):
        assert fixly_path_for_query("spa") == "kategoria/spa"

    def test_wellness(self):
        assert fixly_path_for_query("wellness") == "kategoria/spa"

    def test_uroda(self):
        assert fixly_path_for_query("uroda") == "kategoria/uroda"

    def test_fryzjer(self):
        assert fixly_path_for_query("fryzjer") == "kategoria/fryzjer"

    def test_joga(self):
        assert fixly_path_for_query("joga") == "kategoria/joga"

    def test_fizjoterapia(self):
        assert fixly_path_for_query("fizjoterapia") == "kategoria/fizjoterapia"

    def test_unknown_returns_none(self):
        assert fixly_path_for_query("fryzura xyz") is None


# ---------------------------------------------------------------------------
# cylex_search_urls
# ---------------------------------------------------------------------------

class TestCylexSearchUrls:
    def test_returns_list(self):
        urls = cylex_search_urls("spa", "Wrocław")
        assert isinstance(urls, list)
        assert len(urls) > 0

    def test_query_encoded_in_url(self):
        urls = cylex_search_urls("spa", "Wrocław")
        combined = " ".join(urls)
        assert "spa" in combined

    def test_empty_query_returns_empty(self):
        urls = cylex_search_urls("", "Wrocław")
        assert urls == []

    def test_city_slug_in_some_url(self):
        urls = cylex_search_urls("wellness", "Kraków")
        combined = " ".join(urls)
        assert "krakow" in combined.lower() or "krak" in combined.lower()


# ---------------------------------------------------------------------------
# is_probably_path
# ---------------------------------------------------------------------------

class TestIsProbablyPath:
    def test_windows_absolute_path(self):
        assert is_probably_path("C:\\plik.xlsx") is True

    def test_relative_xlsx(self):
        assert is_probably_path("results.xlsx") is True

    def test_csv_extension(self):
        assert is_probably_path("output.csv") is True

    def test_json_extension(self):
        assert is_probably_path("data.json") is True

    def test_plain_word_is_false(self):
        assert is_probably_path("spa") is False

    def test_none_is_false(self):
        assert is_probably_path(None) is False

    def test_empty_is_false(self):
        assert is_probably_path("") is False


# ---------------------------------------------------------------------------
# _deobfuscate_emails
# ---------------------------------------------------------------------------

class TestDeobfuscateEmails:
    def test_bracket_at(self):
        result = _deobfuscate_emails("kontakt [at] firma.pl")
        assert "@" in result
        assert "[at]" not in result

    def test_paren_at(self):
        result = _deobfuscate_emails("info (at) example.pl")
        assert "@" in result

    def test_bracket_dot(self):
        result = _deobfuscate_emails("kontakt@firma [dot] pl")
        assert "." in result
        assert "[dot]" not in result

    def test_paren_dot(self):
        result = _deobfuscate_emails("info@example (dot) com")
        assert "." in result
        assert "(dot)" not in result

    def test_uppercase_AT_standalone(self):
        result = _deobfuscate_emails("kontakt AT firma.pl")
        assert "@" in result

    def test_uppercase_DOT_standalone(self):
        result = _deobfuscate_emails("kontakt@firma DOT pl")
        assert "." in result

    def test_mixed_obfuscation(self):
        result = _deobfuscate_emails("biuro [at] salon [dot] pl")
        assert "@" in result
        assert "[at]" not in result
        assert "[dot]" not in result

    def test_plain_email_unchanged(self):
        original = "test@example.com"
        assert _deobfuscate_emails(original) == original


# ---------------------------------------------------------------------------
# annotate_sources
# ---------------------------------------------------------------------------

class TestAnnotateSources:
    def test_adds_source_to_results(self):
        results = [{"name": "Salon A"}]
        annotate_sources(results, "panorama")
        assert "panorama" in results[0]["sources"]

    def test_does_not_duplicate_source(self):
        results = [{"name": "Salon A", "sources": ["panorama"]}]
        annotate_sources(results, "panorama")
        assert results[0]["sources"].count("panorama") == 1

    def test_empty_list_does_not_raise(self):
        annotate_sources([], "panorama")

    def test_multiple_results_all_annotated(self):
        results = [{"name": "A"}, {"name": "B"}]
        annotate_sources(results, "pkt")
        assert all("pkt" in r["sources"] for r in results)

    def test_adds_to_existing_sources(self):
        results = [{"name": "Salon A", "sources": ["booksy"]}]
        annotate_sources(results, "krs")
        assert "booksy" in results[0]["sources"]
        assert "krs" in results[0]["sources"]

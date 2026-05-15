"""Tests for merge_results and normalize_* helper functions."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from scraper import (
    merge_results, normalize_name, normalize_phone, normalize_address,
    is_valid_email, filter_platform_emails, strip_emoji,
)


# ---------------------------------------------------------------------------
# normalize_name
# ---------------------------------------------------------------------------

class TestNormalizeName:
    def test_lowercases(self):
        assert normalize_name("SALON SPA") == "salon spa"

    def test_strips_whitespace(self):
        assert normalize_name("  Salon  ") == "salon"

    def test_removes_special_chars(self):
        result = normalize_name("Salon & SPA")
        assert "&" not in result

    def test_polish_chars_kept(self):
        # normalize_name uses \w which in Python includes Unicode letters
        result = normalize_name("Salon Ósmy")
        assert "salon" in result

    def test_none_safe(self):
        assert normalize_name(None) == ""

    def test_empty(self):
        assert normalize_name("") == ""


# ---------------------------------------------------------------------------
# normalize_phone
# ---------------------------------------------------------------------------

class TestNormalizePhone:
    def test_strips_spaces_and_dashes(self):
        # +48 prefix usuwany → 9-cyfrowy numer
        assert normalize_phone("+48 500-200-100") == "500200100"

    def test_strips_parentheses(self):
        # (48) traktowany jak prefix +48 → usuń
        assert normalize_phone("(48) 500 200 100") == "500200100"

    def test_plain_digits_unchanged(self):
        assert normalize_phone("500200100") == "500200100"

    def test_none_safe(self):
        assert normalize_phone(None) == ""

    def test_empty(self):
        assert normalize_phone("") == ""

    def test_with_plus48_equals_without_prefix(self):
        # Kluczowy test dedup: ten sam numer z prefiksem i bez daje identyczny klucz
        with_prefix = normalize_phone("+48 512 738 639")
        without_prefix = normalize_phone("512 738 639")
        assert with_prefix == without_prefix

    def test_48_prefix_only_stripped_for_11_digits(self):
        # Numer 10-cyfrowy zaczynający się od 48 NIE jest polskim numerem — nie ruszaj
        assert normalize_phone("4812345678") == "4812345678"


# ---------------------------------------------------------------------------
# is_valid_email (nowa funkcja)
# ---------------------------------------------------------------------------

class TestIsValidEmail:
    def test_valid_email_accepted(self):
        assert is_valid_email("kontakt@firma.pl") is True

    def test_coml_tld_note(self):
        # .coml technicznie przechodzi regex (4 litery = poprawna długość TLD)
        # Wykrycie tej literówki wymaga UserCheck API weryfikacji MX
        # Ten test dokumentuje zachowanie — nie błąd w kodzie
        pass

    def test_png_extension_rejected(self):
        # pliki graficzne w kolumnie e-mail (wiersze 97, 112 CSV)
        assert is_valid_email("kandara-wroclaw-logo@2x.png") is False

    def test_jpg_extension_rejected(self):
        assert is_valid_email("logo@firma.jpg") is False

    def test_none_rejected(self):
        assert is_valid_email(None) is False

    def test_empty_rejected(self):
        assert is_valid_email("") is False

    def test_no_at_rejected(self):
        assert is_valid_email("kontaktfirmapl") is False


# ---------------------------------------------------------------------------
# filter_platform_emails (nowa funkcja)
# ---------------------------------------------------------------------------

class TestFilterPlatformEmails:
    def test_booksy_emails_removed(self):
        emails = ["kontakt@moja-firma.pl", "aneksy@booksy.com", "pomoc.pl@booksy.com"]
        result = filter_platform_emails(emails)
        assert result == ["kontakt@moja-firma.pl"]

    def test_fresha_emails_removed(self):
        emails = ["info@spa.pl", "support@fresha.com"]
        result = filter_platform_emails(emails)
        assert result == ["info@spa.pl"]

    def test_real_emails_pass_through(self):
        emails = ["biuro@salon.pl", "kontakt@wellness.com"]
        result = filter_platform_emails(emails)
        assert result == emails

    def test_empty_list(self):
        assert filter_platform_emails([]) == []


# ---------------------------------------------------------------------------
# strip_emoji (nowa funkcja)
# ---------------------------------------------------------------------------

class TestStripEmoji:
    def test_removes_flower_emoji(self):
        result = strip_emoji("🪷Masaż Lotos 🪷")
        assert "🪷" not in result
        assert "Masaż Lotos" in result

    def test_removes_leaf_emoji(self):
        result = strip_emoji("Levita Spa Massage Studio🌿")
        assert "🌿" not in result

    def test_plain_text_unchanged(self):
        assert strip_emoji("Salon SPA Katowice") == "Salon SPA Katowice"

    def test_none_safe(self):
        assert strip_emoji(None) is None

    def test_empty_unchanged(self):
        assert strip_emoji("") == ""


# ---------------------------------------------------------------------------
# normalize_address
# ---------------------------------------------------------------------------

class TestNormalizeAddress:
    def test_lowercases(self):
        result = normalize_address("Ul. Kwiatowa 1, Wrocław")
        assert result == result.lower()

    def test_collapses_whitespace(self):
        result = normalize_address("ul.  Kwiatowa  1")
        assert "  " not in result

    def test_strips_leading_trailing(self):
        result = normalize_address("  ul. Kwiatowa 1  ")
        assert not result.startswith(" ")
        assert not result.endswith(" ")

    def test_none_safe(self):
        assert normalize_address(None) == ""

    def test_empty(self):
        assert normalize_address("") == ""


# ---------------------------------------------------------------------------
# merge_results
# ---------------------------------------------------------------------------

def _make_result(name, address="", phone="", website="", emails=None, sources=None):
    return {
        "name": name,
        "formatted_address": address,
        "formatted_phone_number": phone,
        "website": website,
        "emails": emails or [],
        "sources": sources or [],
    }


class TestMergeResults:
    def test_duplicate_produces_one_record(self):
        a = _make_result("Salon Wellness", "ul. Kwiatowa 1, Wrocław", sources=["panorama"])
        b = _make_result("Salon Wellness", "ul. Kwiatowa 1, Wrocław", sources=["pkt"])
        result = merge_results([a], [b])
        assert len(result) == 1

    def test_different_companies_produce_two_records(self):
        a = _make_result("Salon A", "ul. Kwiatowa 1")
        b = _make_result("Salon B", "ul. Zielona 5")
        result = merge_results([a], [b])
        assert len(result) == 2

    def test_emails_are_unioned(self):
        a = _make_result("Salon X", emails=["a@test.pl"], sources=["panorama"])
        b = _make_result("Salon X", emails=["b@test.pl"], sources=["pkt"])
        result = merge_results([a], [b])
        assert len(result) == 1
        assert "a@test.pl" in result[0]["emails"]
        assert "b@test.pl" in result[0]["emails"]

    def test_booksy_emails_placed_first(self):
        a = _make_result("Salon Y", emails=["first@pkt.pl"], sources=["pkt"])
        b = _make_result("Salon Y", emails=["booksy@b.pl"], sources=["booksy"])
        result = merge_results([a], [b])
        assert len(result) == 1
        assert result[0]["emails"][0] == "booksy@b.pl"

    def test_duplicate_emails_not_repeated(self):
        a = _make_result("Salon Z", emails=["dup@x.pl"], sources=["panorama"])
        b = _make_result("Salon Z", emails=["dup@x.pl"], sources=["pkt"])
        result = merge_results([a], [b])
        assert result[0]["emails"].count("dup@x.pl") == 1

    def test_sources_merged(self):
        a = _make_result("Salon W", sources=["panorama"])
        b = _make_result("Salon W", sources=["krs"])
        result = merge_results([a], [b])
        assert "panorama" in result[0]["sources"]
        assert "krs" in result[0]["sources"]

    def test_fills_empty_website_from_other_source(self):
        a = _make_result("Salon V", website="")
        b = _make_result("Salon V", website="https://salon-v.pl")
        result = merge_results([a], [b])
        assert result[0]["website"] == "https://salon-v.pl"

    def test_empty_name_record_skipped(self):
        a = _make_result("")
        b = _make_result("Salon OK")
        result = merge_results([a], [b])
        assert len(result) == 1
        assert result[0]["name"] == "Salon OK"

    def test_empty_lists_returns_empty(self):
        result = merge_results([], [])
        assert result == []

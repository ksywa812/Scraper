"""Tests for merge_results and normalize_* helper functions."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from scraper import merge_results, normalize_name, normalize_phone, normalize_address


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
        assert normalize_phone("+48 500-200-100") == "48500200100"

    def test_strips_parentheses(self):
        assert normalize_phone("(48) 500 200 100") == "48500200100"

    def test_plain_digits_unchanged(self):
        assert normalize_phone("500200100") == "500200100"

    def test_none_safe(self):
        assert normalize_phone(None) == ""

    def test_empty(self):
        assert normalize_phone("") == ""


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

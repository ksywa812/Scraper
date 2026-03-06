"""Tests for HTML parsing functions using fixture HTML strings."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from bs4 import BeautifulSoup
from scraper import (
    parse_booksy_listings,
    parse_spaeden_rankings,
    extract_fresha_name_address,
    clean_booksy_name,
)


# ---------------------------------------------------------------------------
# clean_booksy_name
# ---------------------------------------------------------------------------

class TestCleanBooksyName:
    def test_removes_promowany(self):
        assert "Promowany" not in clean_booksy_name("Promowany Salon SPA")

    def test_splits_on_dash(self):
        result = clean_booksy_name("Salon SPA - Kraków")
        assert result == "Salon SPA"

    def test_removes_km_suffix(self):
        result = clean_booksy_name("Salon SPA 2.5 km")
        assert "km" not in result

    def test_removes_opinion_count(self):
        result = clean_booksy_name("Salon SPA 150 opinii")
        assert "opinii" not in result

    def test_empty_string_returns_empty(self):
        assert clean_booksy_name("") == ""

    def test_none_returns_empty(self):
        assert clean_booksy_name(None) == ""

    def test_plain_name_unchanged(self):
        result = clean_booksy_name("Salon Wellness")
        assert result == "Salon Wellness"


# ---------------------------------------------------------------------------
# parse_booksy_listings
# ---------------------------------------------------------------------------

class TestParseBooksyListings:
    def _make_booksy_html(self, href, text):
        return f"""
        <html><body>
          <a href="{href}">{text}</a>
        </body></html>
        """

    def test_returns_result_for_valid_link(self, sample_booksy_html):
        results = parse_booksy_listings(sample_booksy_html, "masaz")
        assert len(results) >= 1

    def test_result_has_name(self, sample_booksy_html):
        results = parse_booksy_listings(sample_booksy_html, "masaz")
        assert results[0]["name"]

    def test_result_has_profile_url(self, sample_booksy_html):
        results = parse_booksy_listings(sample_booksy_html, "masaz")
        assert "booksy.com" in results[0]["profile_url"]

    def test_address_extracted_from_bullet(self, sample_booksy_html):
        results = parse_booksy_listings(sample_booksy_html, "masaz")
        assert results[0]["formatted_address"]

    def test_empty_html_returns_empty_list(self):
        results = parse_booksy_listings("<html><body></body></html>", "masaz")
        assert results == []

    def test_wrong_category_slug_no_match(self, sample_booksy_html):
        # The fixture uses category "masaz" in the href
        results = parse_booksy_listings(sample_booksy_html, "fryzjer")
        assert results == []


# ---------------------------------------------------------------------------
# parse_spaeden_rankings
# ---------------------------------------------------------------------------

class TestParseSpaedenRankings:
    def test_extracts_numbered_entry(self, sample_spaeden_text):
        results = parse_spaeden_rankings(sample_spaeden_text)
        names = [r["name"] for r in results]
        assert any("Wellness Palace" in n for n in names)

    def test_extracts_bullet_entry(self, sample_spaeden_text):
        results = parse_spaeden_rankings(sample_spaeden_text)
        names = [r["name"] for r in results]
        assert any("Relaxa" in n for n in names)

    def test_address_extracted(self, sample_spaeden_text):
        results = parse_spaeden_rankings(sample_spaeden_text)
        addresses = [r["formatted_address"] for r in results]
        assert any("Kraków" in a or "Gdańsk" in a or "Wrocław" in a for a in addresses)

    def test_no_duplicates(self, sample_spaeden_text):
        results = parse_spaeden_rankings(sample_spaeden_text)
        names = [r["name"] for r in results]
        assert len(names) == len(set(names))

    def test_empty_text_returns_empty_list(self):
        assert parse_spaeden_rankings("") == []

    def test_result_has_required_keys(self, sample_spaeden_text):
        results = parse_spaeden_rankings(sample_spaeden_text)
        for r in results:
            assert "name" in r
            assert "formatted_address" in r
            assert "emails" in r


# ---------------------------------------------------------------------------
# extract_fresha_name_address
# ---------------------------------------------------------------------------

class TestExtractFreshaNameAddress:
    def test_extracts_name(self, sample_fresha_container):
        name, _ = extract_fresha_name_address(sample_fresha_container)
        assert name == "Spa Harmonia"

    def test_extracts_address(self, sample_fresha_container):
        _, address = extract_fresha_name_address(sample_fresha_container)
        assert "Różana" in address or "Poznań" in address

    def test_empty_container_returns_empty_strings(self):
        from bs4 import BeautifulSoup
        html = "<div></div>"
        soup = BeautifulSoup(html, "html.parser")
        container = soup.find("div")
        name, address = extract_fresha_name_address(container)
        assert name == ""
        assert address == ""

    def test_show_number_token_filtered(self, sample_fresha_container):
        name, address = extract_fresha_name_address(sample_fresha_container)
        assert "Show number" not in name
        assert "Show number" not in address

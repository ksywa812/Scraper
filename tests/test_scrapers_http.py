"""Tests for HTTP scraper functions — mock session, no real network."""
import sys
import os
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from scraper import (
    _extract_krs_number,
    fetch_krs_aktualny,
    enrich_with_krs,
    scrape_panorama_firm,
    scrape_pkt_pl,
    scrape_krs_api,
    scrape_aleo,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_session(pages):
    """Create a mock session that returns successive pages on each .get() call.

    `pages` is a list of (html_text, status_code) tuples. After the list is
    exhausted the session returns an empty-200 response (simulates no more pages).
    """
    session = MagicMock()
    responses = []

    for html, status in pages:
        resp = MagicMock()
        resp.status_code = status
        resp.text = html
        resp.headers = {"Content-Type": "text/html"}
        if status >= 400:
            from requests.exceptions import HTTPError
            resp.raise_for_status.side_effect = HTTPError(f"{status} Error")
        else:
            resp.raise_for_status.return_value = None
        responses.append(resp)

    # Empty page to stop pagination
    empty = MagicMock()
    empty.status_code = 200
    empty.text = "<html><body></body></html>"
    empty.headers = {"Content-Type": "text/html"}
    empty.raise_for_status.return_value = None

    session.get.side_effect = responses + [empty] * 10
    return session


# ---------------------------------------------------------------------------
# Panorama Firm
# ---------------------------------------------------------------------------

PANORAMA_HTML = """
<html><body>
  <div class="card company-item">
    <h2 class="company-name">
      <a href="/firma/salon-wellness-12345">Salon Wellness Centrum</a>
    </h2>
    <div class="address">ul. Kwiatowa 5, 50-001 Wrocław</div>
    <a data-company-phone="+48 71 123 45 67"></a>
    <a class="icon-website" href="https://salon-wellness.pl"></a>
  </div>
</body></html>
"""


class TestScrapePanoramaFirm:
    def test_returns_non_empty_list(self):
        session = _make_mock_session([(PANORAMA_HTML, 200)])
        results = scrape_panorama_firm("spa", "Wrocław", max_pages=1, session=session)
        assert len(results) >= 1

    def test_result_has_name(self):
        session = _make_mock_session([(PANORAMA_HTML, 200)])
        results = scrape_panorama_firm("spa", "Wrocław", max_pages=1, session=session)
        assert results[0]["name"] == "Salon Wellness Centrum"

    def test_result_has_address(self):
        session = _make_mock_session([(PANORAMA_HTML, 200)])
        results = scrape_panorama_firm("spa", "Wrocław", max_pages=1, session=session)
        assert "Kwiatowa" in results[0]["formatted_address"]

    def test_result_has_phone(self):
        session = _make_mock_session([(PANORAMA_HTML, 200)])
        results = scrape_panorama_firm("spa", "Wrocław", max_pages=1, session=session)
        assert "+48" in results[0]["formatted_phone_number"]

    def test_result_has_website(self):
        session = _make_mock_session([(PANORAMA_HTML, 200)])
        results = scrape_panorama_firm("spa", "Wrocław", max_pages=1, session=session)
        assert results[0]["website"] == "https://salon-wellness.pl"

    def test_result_has_profile_url(self):
        session = _make_mock_session([(PANORAMA_HTML, 200)])
        results = scrape_panorama_firm("spa", "Wrocław", max_pages=1, session=session)
        assert "panoramafirm.pl" in results[0]["profile_url"]

    def test_http_500_returns_empty_list(self):
        session = _make_mock_session([("", 500)])
        results = scrape_panorama_firm("spa", "Wrocław", max_pages=1, session=session)
        assert results == []

    def test_empty_page_returns_empty_list(self):
        session = _make_mock_session([("<html><body></body></html>", 200)])
        results = scrape_panorama_firm("spa", "Wrocław", max_pages=1, session=session)
        assert results == []


# ---------------------------------------------------------------------------
# PKT.pl
# ---------------------------------------------------------------------------

PKT_HTML = """
<html><body>
  <ul>
    <li class="list-items">
      <h2 class="company-name">
        <a href="/firmy/wellness/salon-aktywni-67890">Salon Aktywni</a>
      </h2>
      <address class="rest-address">ul. Zielona 10, 31-001 Kraków</address>
      <a class="icon-telephone">500 100 200</a>
      <a class="company-url" href="https://aktywni.pl">aktywni.pl</a>
    </li>
  </ul>
</body></html>
"""


class TestScrapePktPl:
    def test_returns_non_empty_list(self):
        session = _make_mock_session([(PKT_HTML, 200)])
        results = scrape_pkt_pl("spa", "Kraków", max_pages=1, session=session)
        assert len(results) >= 1

    def test_result_has_name(self):
        session = _make_mock_session([(PKT_HTML, 200)])
        results = scrape_pkt_pl("spa", "Kraków", max_pages=1, session=session)
        assert results[0]["name"] == "Salon Aktywni"

    def test_result_has_address(self):
        session = _make_mock_session([(PKT_HTML, 200)])
        results = scrape_pkt_pl("spa", "Kraków", max_pages=1, session=session)
        assert "Zielona" in results[0]["formatted_address"]

    def test_result_has_profile_url(self):
        session = _make_mock_session([(PKT_HTML, 200)])
        results = scrape_pkt_pl("spa", "Kraków", max_pages=1, session=session)
        assert "pkt.pl" in results[0]["profile_url"]

    def test_result_has_website(self):
        session = _make_mock_session([(PKT_HTML, 200)])
        results = scrape_pkt_pl("spa", "Kraków", max_pages=1, session=session)
        assert results[0]["website"] == "https://aktywni.pl"

    def test_http_error_returns_empty_list(self):
        session = _make_mock_session([("", 503)])
        results = scrape_pkt_pl("spa", "Kraków", max_pages=1, session=session)
        assert results == []


# ---------------------------------------------------------------------------
# KRS API
# ---------------------------------------------------------------------------

KRS_JSON = {
    "lista": [
        {
            "dane": {
                "nazwa": "Centrum Wellness Sp. z o.o.",
                "adres": {
                    "ulica": "ul. Słoneczna",
                    "nrDomu": "7",
                    "kodPocztowy": "00-001",
                    "miejscowosc": "Warszawa",
                },
                "email": "kontakt@centrum-wellness.pl",
                "stronaInternetowa": "centrum-wellness.pl",
                "numerKRS": "0000123456",
            }
        }
    ]
}


class TestScrapeKrsApi:
    def test_returns_list(self):
        session = MagicMock()
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = KRS_JSON
        session.get.return_value = resp

        results = scrape_krs_api("spa", "Warszawa", session=session)
        assert isinstance(results, list)

    def test_parses_name(self):
        session = MagicMock()
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = KRS_JSON
        session.get.return_value = resp

        results = scrape_krs_api("spa", "Warszawa", session=session)
        assert len(results) >= 1
        assert results[0]["name"] == "Centrum Wellness Sp. z o.o."

    def test_parses_address(self):
        session = MagicMock()
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = KRS_JSON
        session.get.return_value = resp

        results = scrape_krs_api("spa", "Warszawa", session=session)
        assert "Warszawa" in results[0]["formatted_address"]

    def test_parses_email(self):
        session = MagicMock()
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = KRS_JSON
        session.get.return_value = resp

        results = scrape_krs_api("spa", "Warszawa", session=session)
        assert "kontakt@centrum-wellness.pl" in results[0]["emails"]

    def test_api_failure_returns_empty_list(self):
        session = MagicMock()
        session.get.side_effect = Exception("network error")
        results = scrape_krs_api("spa", "Warszawa", session=session)
        assert results == []

    def test_required_keys_present(self):
        session = MagicMock()
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = KRS_JSON
        session.get.return_value = resp

        results = scrape_krs_api("spa", "Warszawa", session=session)
        for key in ("name", "formatted_address", "website", "emails", "profile_url"):
            assert key in results[0]


# ---------------------------------------------------------------------------
# Aleo
# ---------------------------------------------------------------------------

ALEO_HTML = """
<html><body>
  <div class="company-item">
    <h2><a href="/pl/firma/salon-aleo-99">Salon Aleo</a></h2>
    <address>ul. Aleo 1, Wrocław</address>
    <a href="tel:+48500600700">+48500600700</a>
    <a href="https://salon-aleo.pl">salon-aleo.pl</a>
    <a href="mailto:biuro@salon-aleo.pl">biuro@salon-aleo.pl</a>
  </div>
</body></html>
"""


class TestScrapeAleo:
    def test_returns_list(self):
        session = _make_mock_session([(ALEO_HTML, 200)])
        results = scrape_aleo("spa", "Wrocław", session=session, max_pages=1)
        assert isinstance(results, list)

    def test_parses_name(self):
        session = _make_mock_session([(ALEO_HTML, 200)])
        results = scrape_aleo("spa", "Wrocław", session=session, max_pages=1)
        assert len(results) >= 1
        assert results[0]["name"] == "Salon Aleo"

    def test_parses_email_from_mailto(self):
        session = _make_mock_session([(ALEO_HTML, 200)])
        results = scrape_aleo("spa", "Wrocław", session=session, max_pages=1)
        assert "biuro@salon-aleo.pl" in results[0]["emails"]

    def test_http_error_returns_empty_list(self):
        session = _make_mock_session([("", 500)])
        results = scrape_aleo("spa", "Wrocław", session=session, max_pages=1)
        assert results == []


# ---------------------------------------------------------------------------
# _extract_krs_number
# ---------------------------------------------------------------------------

class TestExtractKrsNumber:
    def test_extracts_from_ekrs_url(self):
        url = "https://ekrs.ms.gov.pl/web/wyszukiwarka-krs/strona-glowna?numer=0000123456"
        assert _extract_krs_number(url) == "0000123456"

    def test_extracts_from_krs_path(self):
        assert _extract_krs_number("/krs/0000999888") == "0000999888"

    def test_extracts_bare_10digit(self):
        assert _extract_krs_number("0000123456") == "0000123456"

    def test_empty_string_returns_empty(self):
        assert _extract_krs_number("") == ""

    def test_none_returns_empty(self):
        assert _extract_krs_number(None) == ""

    def test_url_without_krs_returns_empty(self):
        assert _extract_krs_number("https://salon-wellness.pl") == ""


# ---------------------------------------------------------------------------
# fetch_krs_aktualny
# ---------------------------------------------------------------------------

# Minimal OdpisAktualny-like JSON response
KRS_AKTUALNY_JSON = {
    "odpis": {
        "dane": {
            "nazwa": "Centrum Wellness Sp. z o.o.",
            "adresSiedziby": {
                "ulica": "ul. Słoneczna",
                "nrDomu": "7",
                "kodPocztowy": "00-001",
                "miejscowosc": "Warszawa",
            },
            "kontakt": {
                "email": "kontakt@centrum-wellness.pl",
                "telefon": "+48 22 123 45 67",
                "stronaWWW": "https://centrum-wellness.pl",
            },
        }
    }
}


def _krs_aktualny_session(json_data=None, status=200):
    session = MagicMock()
    resp = MagicMock()
    resp.status_code = status
    resp.raise_for_status.return_value = None
    resp.json.return_value = json_data or {}
    if status >= 400:
        from requests.exceptions import HTTPError
        resp.raise_for_status.side_effect = HTTPError(f"{status}")
    session.get.return_value = resp
    return session


class TestFetchKrsAktualny:
    def test_returns_dict_on_success(self):
        session = _krs_aktualny_session(KRS_AKTUALNY_JSON)
        result = fetch_krs_aktualny("0000123456", session=session)
        assert isinstance(result, dict)

    def test_parses_name(self):
        session = _krs_aktualny_session(KRS_AKTUALNY_JSON)
        result = fetch_krs_aktualny("0000123456", session=session)
        assert result["name"] == "Centrum Wellness Sp. z o.o."

    def test_parses_email(self):
        session = _krs_aktualny_session(KRS_AKTUALNY_JSON)
        result = fetch_krs_aktualny("0000123456", session=session)
        assert "kontakt@centrum-wellness.pl" in result["emails"]

    def test_parses_phone(self):
        session = _krs_aktualny_session(KRS_AKTUALNY_JSON)
        result = fetch_krs_aktualny("0000123456", session=session)
        assert result["formatted_phone_number"] == "+48 22 123 45 67"

    def test_parses_website(self):
        session = _krs_aktualny_session(KRS_AKTUALNY_JSON)
        result = fetch_krs_aktualny("0000123456", session=session)
        assert result["website"] == "https://centrum-wellness.pl"

    def test_404_returns_none(self):
        session = _krs_aktualny_session(status=404)
        result = fetch_krs_aktualny("0000000000", session=session)
        assert result is None

    def test_network_error_returns_none(self):
        session = MagicMock()
        session.get.side_effect = Exception("timeout")
        result = fetch_krs_aktualny("0000123456", session=session)
        assert result is None

    def test_empty_krs_num_returns_none(self):
        result = fetch_krs_aktualny("")
        assert result is None

    def test_profile_url_contains_krs_number(self):
        session = _krs_aktualny_session(KRS_AKTUALNY_JSON)
        result = fetch_krs_aktualny("0000123456", session=session)
        assert "0000123456" in result["profile_url"]


# ---------------------------------------------------------------------------
# enrich_with_krs
# ---------------------------------------------------------------------------

class TestEnrichWithKrs:
    def test_enriches_email_when_missing(self):
        session = _krs_aktualny_session(KRS_AKTUALNY_JSON)
        records = [{
            "name": "Centrum Wellness",
            "emails": [],
            "website": "",
            "formatted_phone_number": "",
            "formatted_address": "",
            "profile_url": "https://ekrs.ms.gov.pl/web/wyszukiwarka-krs/strona-glowna?numer=0000123456",
        }]
        enrich_with_krs(records, session=session)
        assert "kontakt@centrum-wellness.pl" in records[0]["emails"]

    def test_enriches_website_when_missing(self):
        session = _krs_aktualny_session(KRS_AKTUALNY_JSON)
        records = [{
            "name": "Centrum Wellness",
            "emails": [],
            "website": "",
            "formatted_phone_number": "",
            "formatted_address": "",
            "profile_url": "https://ekrs.ms.gov.pl/web/wyszukiwarka-krs/strona-glowna?numer=0000123456",
        }]
        enrich_with_krs(records, session=session)
        assert records[0]["website"] == "https://centrum-wellness.pl"

    def test_skips_record_without_krs_in_profile_url(self):
        session = _krs_aktualny_session(KRS_AKTUALNY_JSON)
        records = [{
            "name": "Salon Bez KRS",
            "emails": [],
            "website": "",
            "formatted_phone_number": "",
            "formatted_address": "",
            "profile_url": "https://panoramafirm.pl/firma/salon",
        }]
        enrich_with_krs(records, session=session)
        # session.get should NOT be called — no KRS number found
        session.get.assert_not_called()

    def test_skips_record_with_complete_data(self):
        session = _krs_aktualny_session(KRS_AKTUALNY_JSON)
        records = [{
            "name": "Pełny Salon",
            "emails": ["a@b.pl"],
            "website": "https://x.pl",
            "formatted_phone_number": "500100200",
            "formatted_address": "ul. X 1",
            "profile_url": "https://ekrs.ms.gov.pl/web/wyszukiwarka-krs/strona-glowna?numer=0000123456",
        }]
        enrich_with_krs(records, session=session)
        # All fields already filled — should skip KRS lookup
        session.get.assert_not_called()

    def test_empty_list_returns_zero(self):
        assert enrich_with_krs([]) == 0

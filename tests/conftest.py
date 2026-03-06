"""Shared pytest fixtures for scraper tests."""
import sqlite3
import sys
import os
from unittest.mock import MagicMock

import pytest

# Make sure scraper.py is importable from the Scraper/ directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# HTML fixtures — minimal but valid HTML that mirrors real site structures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_panorama_html():
    """Minimal Panorama Firm listing HTML with one company card."""
    return """
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


@pytest.fixture
def sample_pkt_html():
    """Minimal PKT.pl listing HTML with one list item."""
    return """
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


@pytest.fixture
def sample_booksy_html():
    """Minimal Booksy listing HTML with one profile link."""
    return """
    <html><body>
      <a href="/pl-pl/99999_spa-relaks_masaz_warszawa">
        Spa Relaks • ul. Marszałkowska 1, Warszawa
      </a>
    </body></html>
    """


@pytest.fixture
def sample_spaeden_text():
    """Sample SPAeden plain text ranking with numbered and bullet entries."""
    return (
        "1. Hotel Wellness Palace – Kraków\n"
        "2. Spa & Beauty Resort – Gdańsk\n"
        "• Day Spa Relaxa – Wrocław\n"
    )


@pytest.fixture
def sample_fresha_container():
    """A BeautifulSoup Tag-like object imitating a Fresha listing container."""
    from bs4 import BeautifulSoup
    html = """
    <div>
      <p>Spa Harmonia</p>
      <p>ul. Różana 3, Poznań, Poland</p>
      <p>Show number</p>
    </div>
    """
    soup = BeautifulSoup(html, "html.parser")
    return soup.find("div")


@pytest.fixture
def sample_krs_response():
    """Minimal KRS API JSON response structure."""
    return {
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


# ---------------------------------------------------------------------------
# Mock HTTP session fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_response_factory():
    """Returns a factory for creating mock HTTP response objects."""
    def _make(text="", status_code=200, json_data=None):
        resp = MagicMock()
        resp.status_code = status_code
        resp.text = text
        resp.headers = {"Content-Type": "text/html"}
        if json_data is not None:
            resp.json.return_value = json_data
        if status_code >= 400:
            from requests.exceptions import HTTPError
            resp.raise_for_status.side_effect = HTTPError(f"{status_code} Error")
        else:
            resp.raise_for_status.return_value = None
        return resp
    return _make


@pytest.fixture
def mock_session(mock_response_factory):
    """A MagicMock requests.Session that returns an empty 200 response by default."""
    session = MagicMock()
    session.get.return_value = mock_response_factory(text="<html></html>")
    return session


# ---------------------------------------------------------------------------
# Temporary SQLite cache fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_cache_db(tmp_path):
    """Create a temporary SQLite email cache database."""
    db_path = str(tmp_path / "test_cache.sqlite")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS email_cache "
        "(url TEXT PRIMARY KEY, emails TEXT, fetched_at REAL)"
    )
    conn.commit()
    yield conn
    conn.close()

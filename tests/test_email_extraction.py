"""Tests for email extraction — uses mock HTTP session, no real network."""
import sqlite3
import sys
import os
import time
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from scraper import (
    extract_emails_from_website,
    cache_get_emails,
    cache_set_emails,
    _deobfuscate_emails,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_session(html, status_code=200, content_type="text/html"):
    session = MagicMock()
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = html
    resp.headers = {"Content-Type": content_type}
    if status_code >= 400:
        from requests.exceptions import HTTPError
        resp.raise_for_status.side_effect = HTTPError(f"{status_code} Error")
    else:
        resp.raise_for_status.return_value = None
    session.get.return_value = resp
    return session


@pytest.fixture
def cache_db(tmp_path):
    """Temp SQLite cache with the exact schema used by scraper.py."""
    db_path = str(tmp_path / "cache.sqlite")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE email_cache "
        "(url TEXT PRIMARY KEY, emails TEXT, created_at INTEGER)"
    )
    conn.commit()
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# extract_emails_from_website
# ---------------------------------------------------------------------------

class TestExtractEmailsFromWebsite:
    def test_plain_email_in_html(self):
        html = "<html><body>Contact: test@example.pl</body></html>"
        session = _mock_session(html)
        emails = extract_emails_from_website("https://example.pl", session=session)
        assert "test@example.pl" in emails

    def test_no_email_returns_empty_list(self):
        html = "<html><body>No contact info here.</body></html>"
        session = _mock_session(html)
        emails = extract_emails_from_website("https://example.pl", session=session)
        assert emails == []

    def test_obfuscated_email_decoded(self):
        html = "<html><body>biuro [at] salon [dot] pl</body></html>"
        session = _mock_session(html)
        emails = extract_emails_from_website("https://salon.pl", session=session)
        assert any("@" in e for e in emails)

    def test_data_email_attribute_extracted(self):
        html = '<html><body><span data-email="info@firma.pl"></span></body></html>'
        session = _mock_session(html)
        emails = extract_emails_from_website("https://firma.pl", session=session)
        assert "info@firma.pl" in emails

    def test_http_error_returns_empty_list(self):
        session = _mock_session("", status_code=404)
        emails = extract_emails_from_website("https://example.pl", session=session)
        assert emails == []

    def test_non_html_content_type_returns_empty(self):
        session = _mock_session(b"binary", content_type="application/pdf")
        emails = extract_emails_from_website("https://example.pl", session=session)
        assert emails == []

    def test_empty_url_returns_empty_list(self):
        emails = extract_emails_from_website("")
        assert emails == []

    def test_none_url_returns_empty_list(self):
        emails = extract_emails_from_website(None)
        assert emails == []

    def test_cache_hit_skips_http(self, cache_db):
        # Pre-populate cache
        cache_set_emails(cache_db, "https://cached.pl", ["cached@cached.pl"])
        session = _mock_session("<html></html>")

        emails = extract_emails_from_website(
            "https://cached.pl", session=session, cache_conn=cache_db
        )
        # Should return cached data without calling session.get
        assert "cached@cached.pl" in emails
        session.get.assert_not_called()

    def test_result_cached_after_fetch(self, cache_db):
        html = "<html><body>stored@firma.pl</body></html>"
        session = _mock_session(html)
        extract_emails_from_website(
            "https://firma-x.pl", session=session, cache_conn=cache_db
        )
        cached = cache_get_emails(cache_db, "https://firma-x.pl")
        assert cached is not None
        assert "stored@firma.pl" in cached


# ---------------------------------------------------------------------------
# cache_get_emails / cache_set_emails roundtrip
# ---------------------------------------------------------------------------

class TestEmailCache:
    def test_roundtrip_single_email(self, cache_db):
        cache_set_emails(cache_db, "https://x.pl", ["a@b.pl"])
        result = cache_get_emails(cache_db, "https://x.pl")
        assert result == ["a@b.pl"]

    def test_roundtrip_multiple_emails(self, cache_db):
        emails = ["a@b.pl", "c@d.pl"]
        cache_set_emails(cache_db, "https://multi.pl", emails)
        result = cache_get_emails(cache_db, "https://multi.pl")
        assert set(result) == set(emails)

    def test_cache_miss_returns_none(self, cache_db):
        result = cache_get_emails(cache_db, "https://missing.pl")
        assert result is None

    def test_overwrite_existing_entry(self, cache_db):
        cache_set_emails(cache_db, "https://x.pl", ["old@x.pl"])
        cache_set_emails(cache_db, "https://x.pl", ["new@x.pl"])
        result = cache_get_emails(cache_db, "https://x.pl")
        assert "new@x.pl" in result

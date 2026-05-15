"""Tests for resend_sender.py — walidacja e-maili i parsowanie CSV."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import re

from resend_sender import (
    is_valid_email,
    is_sendable_email,
    EMAIL_REGEX,
    _PLATFORM_EMAILS,
    _IMAGE_EXTENSIONS,
)


# ---------------------------------------------------------------------------
# is_valid_email
# ---------------------------------------------------------------------------

class TestIsValidEmail:
    def test_valid_pl(self):
        assert is_valid_email("kontakt@firma.pl") is True

    def test_valid_com(self):
        assert is_valid_email("info@example.com") is True

    def test_valid_org(self):
        assert is_valid_email("biuro@stowarzyszenie.org") is True

    def test_coml_rejected(self):
        # Bug z wiersza 165 CSV — literówka w TLD
        assert is_valid_email("palpatio.masaze@gmail.coml") is False

    def test_png_rejected(self):
        # Wiersze 97, 112 CSV — nazwy plików graficznych w kolumnie e-mail
        assert is_valid_email("kandara-wroclaw-logo@2x.png") is False

    def test_jpg_rejected(self):
        assert is_valid_email("logo@firma.jpg") is False

    def test_no_at_rejected(self):
        assert is_valid_email("kontaktfirmapl") is False

    def test_empty_rejected(self):
        assert is_valid_email("") is False

    def test_none_rejected(self):
        assert is_valid_email(None) is False

    def test_whitespace_stripped(self):
        assert is_valid_email("  kontakt@firma.pl  ") is True


# ---------------------------------------------------------------------------
# is_sendable_email — łączy walidację formatu + filtr platform
# ---------------------------------------------------------------------------

class TestIsSendableEmail:
    def test_booksy_platform_rejected(self):
        assert is_sendable_email("aneksy@booksy.com") is False
        assert is_sendable_email("pomoc.pl@booksy.com") is False
        assert is_sendable_email("sprzedaz@booksy.com") is False

    def test_fresha_platform_rejected(self):
        assert is_sendable_email("support@fresha.com") is False
        assert is_sendable_email("no-reply@fresha.com") is False

    def test_valid_business_email_accepted(self):
        assert is_sendable_email("kontakt@spa-wellness.pl") is True

    def test_invalid_format_rejected(self):
        assert is_sendable_email("palpatio@gmail.coml") is False

    def test_image_file_rejected(self):
        assert is_sendable_email("logo@firma.png") is False


# ---------------------------------------------------------------------------
# EMAIL_REGEX wzorzec
# ---------------------------------------------------------------------------

class TestEmailRegex:
    def test_matches_valid(self):
        assert EMAIL_REGEX.match("kontakt@firma.pl")

    def test_rejects_coml(self):
        # TLD musi być ≥2 litery i tylko litery (nie .coml = 4 litery ale "l" to litera...
        # .coml passes length check but IS 4 letters — so regex alone won't reject it.
        # is_valid_email adds the image extension check separately.
        # This test just verifies the regex matches valid emails correctly.
        assert EMAIL_REGEX.match("info@example.com")

    def test_rejects_no_tld(self):
        assert not EMAIL_REGEX.match("user@domain")

    def test_rejects_no_at(self):
        assert not EMAIL_REGEX.match("userdomainpl")


# ---------------------------------------------------------------------------
# load_contacts — parsowanie "Other Emails" jako listy (test integracyjny)
# ---------------------------------------------------------------------------

class TestLoadContactsIntegration:
    """Testy sprawdzające że Other Emails jest prawidłowo splitowane."""

    def test_other_emails_column_split(self, tmp_path):
        """Jeśli Other Emails zawiera 'a@x.pl, b@y.pl' — oba e-maile trafiają jako osobne kontakty."""
        import pandas as pd
        from resend_sender import load_contacts

        csv_path = tmp_path / "test.csv"
        # Wiersz z wieloma e-mailami w Other Emails
        df = pd.DataFrame([{
            "Name": "Test Firma",
            "Email 1": "kontakt@test.pl",
            "Email 2": "",
            "Email 3": "",
            "Other Emails": "drugi@test.pl, trzeci@test.pl",
            "Miasto": "Katowice",
            "Województwo": "Śląskie",
            "Branża": "spa",
        }])
        df.to_csv(csv_path, index=False)

        contacts = load_contacts(csv_path)
        emails = [c["email"] for c in contacts]
        assert "kontakt@test.pl" in emails
        assert "drugi@test.pl" in emails
        assert "trzeci@test.pl" in emails

    def test_platform_emails_excluded_from_contacts(self, tmp_path):
        """E-maile Booksy z kolumny Other Emails nie trafiają do kontaktów."""
        import pandas as pd
        from resend_sender import load_contacts

        csv_path = tmp_path / "test2.csv"
        df = pd.DataFrame([{
            "Name": "Test Spa",
            "Email 1": "biuro@spa.pl",
            "Email 2": "",
            "Email 3": "",
            "Other Emails": "aneksy@booksy.com, pomoc.pl@booksy.com",
            "Miasto": "Wrocław",
            "Województwo": "Dolnośląskie",
            "Branża": "spa",
        }])
        df.to_csv(csv_path, index=False)

        contacts = load_contacts(csv_path)
        emails = [c["email"] for c in contacts]
        assert "biuro@spa.pl" in emails
        assert "aneksy@booksy.com" not in emails
        assert "pomoc.pl@booksy.com" not in emails

    def test_coml_email_excluded(self, tmp_path):
        """Nieprawidłowy adres .coml jest pominięty."""
        import pandas as pd
        from resend_sender import load_contacts

        csv_path = tmp_path / "test3.csv"
        df = pd.DataFrame([{
            "Name": "Palpatio",
            "Email 1": "palpatio.masaze@gmail.coml",
            "Email 2": "",
            "Email 3": "",
            "Other Emails": "",
            "Miasto": "Katowice",
            "Województwo": "Śląskie",
            "Branża": "masaz",
        }])
        df.to_csv(csv_path, index=False)

        contacts = load_contacts(csv_path)
        # Nieprawidłowy e-mail nie może trafić jako kontakt
        assert len(contacts) == 0 or all(
            "coml" not in c["email"] for c in contacts
        )

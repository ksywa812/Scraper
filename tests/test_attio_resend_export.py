"""Unit tests for attio_resend_export.py (Step 6a: Attio -> Resend campaign export)."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import attio_resend_export as exmod


class FakeAttioExportClient:
    def __init__(self, *, companies=None, people=None):
        self.companies = list(companies or [])
        self.people = list(people or [])

    def query_all_records(self, obj_slug: str):
        if obj_slug == "companies":
            return list(self.companies)
        if obj_slug == "people":
            return list(self.people)
        return []


class TestAttioResendExport:
    def test_filters_by_industry_excludes_trials_clients_dnc_cooldown_and_caps_per_company(self, tmp_path: Path):
        now = datetime(2026, 11, 26, 10, 0, 0, tzinfo=timezone.utc)
        attio = FakeAttioExportClient(
            companies=[
                # 1. Kwalifikuje się (Kawiarnie, Nowy, brak wysyłki)
                {
                    "id": {"record_id": "c-ok"},
                    "values": {
                        "name": [{"value": "Kawiarnia Złoty Ziarno"}],
                        "domains": [{"domain": "zlotyziarno.pl"}],
                        "soundyou_industry": [{"option": {"title": "Kawiarnie"}}],
                        "lead_status": [{"status": {"title": "Nowy"}}],
                        "primary_location": [{"locality": "Wrocław", "region": "Dolnośląskie"}],
                    },
                },
                # 2. Wykluczona — aktywny Trial SoundYou!
                {
                    "id": {"record_id": "c-trial"},
                    "values": {
                        "name": [{"value": "Kawiarnia Na Trialu"}],
                        "domains": [{"domain": "natrialu.pl"}],
                        "soundyou_industry": [{"option": {"title": "Kawiarnie"}}],
                        "lead_status": [{"status": {"title": "Trial SoundYou"}}],
                    },
                },
                # 3. Wykluczona — Cooldown (wysłano 5 dni temu, limit 30 dni)
                {
                    "id": {"record_id": "c-cooldown"},
                    "values": {
                        "name": [{"value": "Kawiarnia Niedawna"}],
                        "domains": [{"domain": "niedawna.pl"}],
                        "soundyou_industry": [{"option": {"title": "Kawiarnie"}}],
                        "lead_status": [{"status": {"title": "Wysłano kampanię"}}],
                        "resend_last_sent_at": [{"value": "2026-11-21T10:00:00Z"}],
                    },
                },
                # 4. Inna branża (Fryzjer) — odfiltrowana gdy prosimy o Kawiarnie
                {
                    "id": {"record_id": "c-other-ind"},
                    "values": {
                        "name": [{"value": "Barber Król"}],
                        "domains": [{"domain": "barberkrol.pl"}],
                        "soundyou_industry": [{"option": {"title": "Salon Fryzjerski / Barber"}}],
                        "lead_status": [{"status": {"title": "Nowy"}}],
                    },
                },
            ],
            people=[
                # 3 skrzynki w kwalifikującej się kawiarni -> limit max_per_company=2 wybierze kontakt@ i biuro@ zamiast faktury@!
                {
                    "id": {"record_id": "p-ok-1"},
                    "values": {
                        "email_addresses": [{"email_address": "faktury@zlotyziarno.pl"}],
                        "company": [{"target_record_id": "c-ok"}],
                        "resend_unsubscribed": [{"value": False}],
                    },
                },
                {
                    "id": {"record_id": "p-ok-2"},
                    "values": {
                        "email_addresses": [{"email_address": "kontakt@zlotyziarno.pl"}],
                        "company": [{"target_record_id": "c-ok"}],
                        "resend_unsubscribed": [{"value": False}],
                    },
                },
                {
                    "id": {"record_id": "p-ok-3"},
                    "values": {
                        "email_addresses": [{"email_address": "biuro@zlotyziarno.pl"}],
                        "company": [{"target_record_id": "c-ok"}],
                        "resend_unsubscribed": [{"value": False}],
                    },
                },
                # Wypisany kontakt w tej samej firmie -> odrzucony
                {
                    "id": {"record_id": "p-ok-unsub"},
                    "values": {
                        "email_addresses": [{"email_address": "wypisany@zlotyziarno.pl"}],
                        "company": [{"target_record_id": "c-ok"}],
                        "resend_unsubscribed": [{"value": True}],
                    },
                },
                {"id": {"record_id": "p-trial"}, "values": {"email_addresses": [{"email_address": "hi@natrialu.pl"}], "company": [{"target_record_id": "c-trial"}]}},
                {"id": {"record_id": "p-cool"}, "values": {"email_addresses": [{"email_address": "hi@niedawna.pl"}], "company": [{"target_record_id": "c-cooldown"}]}},
                {"id": {"record_id": "p-hair"}, "values": {"email_addresses": [{"email_address": "hi@barberkrol.pl"}], "company": [{"target_record_id": "c-other-ind"}]}},
            ],
        )

        # Używamy angielskiego aliasu "cafe" -> musi zmapować na "Kawiarnie"
        contacts, meta = exmod.select_campaign_recipients_from_attio(
            attio,
            industries=["cafe"],
            cooldown_days=30,
            max_per_company=2,
            now_utc=now,
        )
        assert [c["email"] for c in contacts] == ["biuro@zlotyziarno.pl", "kontakt@zlotyziarno.pl"]
        st = meta["stats"]
        assert st["skipped_status_Trial SoundYou"] == 1
        assert st["skipped_cooldown"] == 1
        assert st["skipped_industry_mismatch"] == 1
        assert st["skipped_unsubscribed"] == 1
        assert st["skipped_max_per_company_cap"] == 1

        csv_file = exmod.write_campaign_csv(contacts, tmp_path / "out.csv")
        assert csv_file.exists()
        text = csv_file.read_text(encoding="utf-8")
        assert "Kawiarnia Złoty Ziarno" in text
        assert "faktury@zlotyziarno.pl" not in text

"""Unit tests for resend_attio_sync.py (Step 3: Resend -> Attio feedback loop)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import resend_attio_sync as rmod


class FakeAttioForResend:
    def __init__(self, *, people=None, companies=None, sends=None):
        self.people = list(people or [])
        self.companies = list(companies or [])
        self.sends = list(sends or [])
        self.calls: list[tuple[str, str, dict | None]] = []
        self._seq = 0

    def query_all_records(self, obj_slug: str):
        if obj_slug == "people":
            return list(self.people)
        if obj_slug == "companies":
            return list(self.companies)
        if obj_slug == rmod.SENDS_OBJECT_SLUG:
            return list(self.sends)
        return []

    def request(self, path: str, payload=None, *, method=None):
        m = method or ("POST" if payload is not None else "GET")
        self.calls.append((m, path, payload))
        if m == "POST":
            self._seq += 1
            return {"id": {"record_id": f"new-send-{self._seq}"}}
        if m == "PATCH":
            return {"id": {"record_id": path.rsplit("/", 1)[-1]}}
        raise AssertionError(f"Unexpected call: {m} {path}")


class FakeResendClient:
    def __init__(self, *, emails=None, contacts=None):
        self._emails = list(emails or [])
        self._contacts = list(contacts or [])
        self.last_audience_id: str | None = None

    def list_recent_emails(self, max_emails: int = 1000):
        return self._emails[:max_emails]

    def list_contacts(self, audience_id: str = ""):
        self.last_audience_id = audience_id
        return list(self._contacts)

    def list_audience_contacts(self, audience_id: str):
        return self.list_contacts(audience_id)


class TestResendAttioFeedbackSync:
    def test_lead_status_progression_protects_higher_stages(self):
        clicked_agg = {
            "resend_sends_count": 2,
            "resend_any_opened": True,
            "resend_any_clicked": True,
            "resend_last_delivery_status": "delivered",
        }
        # Nowy + kliknięcie -> Zainteresowany
        assert rmod.compute_next_lead_status("Nowy", clicked_agg) == "Zainteresowany"
        assert rmod.compute_next_lead_status("Wysłano kampanię", clicked_agg) == "Zainteresowany"

        # Wyższe etapy (Trial / Klient / Rozmowa) NIGDY nie są cofane
        assert rmod.compute_next_lead_status("Trial SoundYou", clicked_agg) is None
        assert rmod.compute_next_lead_status("Klient", clicked_agg, all_contacts_unsubscribed=True) is None
        assert rmod.compute_next_lead_status("Rozmowa / Demo", clicked_agg) is None

        # Wysłano bez kliknięcia -> Wysłano kampanię
        sent_agg = {
            "resend_sends_count": 1,
            "resend_any_opened": True,
            "resend_any_clicked": False,
            "resend_last_delivery_status": "delivered",
        }
        assert rmod.compute_next_lead_status("Nowy", sent_agg) == "Wysłano kampanię"

        # Wyłącznie bounced bez otwarć -> Odrzucony / DNC
        bounced_agg = {
            "resend_sends_count": 1,
            "resend_any_opened": False,
            "resend_any_clicked": False,
            "resend_last_delivery_status": "bounced",
        }
        assert rmod.compute_next_lead_status("Nowy", bounced_agg) == "Odrzucony / DNC"

    def test_syncs_new_clicks_updates_historical_send_without_duplication_and_handles_unsubscribe(self):
        attio = FakeAttioForResend(
            people=[
                {
                    "id": {"record_id": "pers-click"},
                    "values": {
                        "email_addresses": [{"email_address": "spa@hoteltopacz.pl"}],
                        "company": [{"target_record_id": "comp-click"}],
                        "resend_unsubscribed": [{"value": False}],
                    },
                },
                {
                    "id": {"record_id": "pers-unsub"},
                    "values": {
                        "email_addresses": [{"email_address": "wypisany@salon.pl"}],
                        "company": [{"target_record_id": "comp-unsub"}],
                        "resend_unsubscribed": [{"value": False}],
                    },
                },
                {
                    "id": {"record_id": "pers-client"},
                    "values": {
                        "email_addresses": [{"email_address": "vip@klient.pl"}],
                        "company": [{"target_record_id": "comp-client"}],
                        "resend_unsubscribed": [{"value": False}],
                    },
                },
            ],
            companies=[
                {
                    "id": {"record_id": "comp-click"},
                    "values": {
                        "name": [{"value": "Zamek Topacz"}],
                        "lead_status": [{"status": {"title": "Nowy"}}],
                    },
                },
                {
                    "id": {"record_id": "comp-unsub"},
                    "values": {
                        "name": [{"value": "Salon Rezygnacja"}],
                        "lead_status": [{"status": {"title": "Wysłano kampanię"}}],
                    },
                },
                {
                    "id": {"record_id": "comp-client"},
                    "values": {
                        "name": [{"value": "Klient Płacący SPA"}],
                        "lead_status": [{"status": {"title": "Klient"}}],
                    },
                },
            ],
            sends=[
                # Historyczny rekord (inny hash klucza, ale ta sama osoba + kampania + sekunda wysyłki!)
                {
                    "id": {"record_id": "hist-send-1"},
                    "values": {
                        "resend_message_key": [{"value": "legacy_hash_abc123"}],
                        "resend_campaign": [{"value": "Spa Music Offer"}],
                        "resend_sent_at": [{"value": "2026-09-28T11:40:47.000Z"}],
                        "resend_delivery_status": [{"value": "delivered"}],
                        "resend_opened": [{"value": False}],
                        "resend_clicked": [{"value": False}],
                        "resend_person": [{"target_record_id": "pers-click"}],
                    },
                }
            ],
        )

        resend = FakeResendClient(
            emails=[
                # 1. Ten sam historyczny mail z nowym kliknięciem -> PATCH zamiast duplikatu POST!
                {
                    "id": "em_hist_1",
                    "to": ["spa@hoteltopacz.pl"],
                    "subject": "Spa Music Offer",
                    "created_at": "2026-09-28T11:40:47Z",
                    "last_event": "clicked",
                },
                # 2. Zupełnie nowy mail do płacącego klienta -> tworzy rekord wysyłki, ale NIE cofa statusu "Klient"!
                {
                    "id": "em_new_2",
                    "to": ["vip@klient.pl"],
                    "subject": "Jesienna Kampania",
                    "created_at": "2026-11-26T09:00:00Z",
                    "last_event": "clicked",
                },
            ],
            contacts=[
                {"email": "wypisany@salon.pl", "unsubscribed": True},
            ],
        )

        report = rmod.sync_resend_feedback_to_attio(
            attio, resend, audience_id="aud_123", dry_run=False
        )
        st = report["stats"]
        assert st["updated_send_records"] == 1
        assert st["created_send_records"] == 1
        assert st["marked_unsubscribed_people"] == 1

        # Sprawdź przejścia statusów: Zamek Topacz -> Zainteresowany, Salon Rezygnacja -> Odrzucony / DNC, Klient -> BEZ ZMIAN!
        transitions = {t["company_id"]: t["to"] for t in report["status_transitions"]}
        assert transitions == {
            "comp-click": "Zainteresowany",
            "comp-unsub": "Odrzucony / DNC",
        }

    def test_fetches_global_resend_contacts_without_audience_id_and_protects_byly_klient(self):
        attio = FakeAttioForResend(
            people=[
                {
                    "id": {"record_id": "pers-global"},
                    "values": {
                        "email_addresses": [{"email_address": "kontakt@salon-global.pl"}],
                        "company": [{"target_record_id": "comp-global"}],
                        "resend_unsubscribed": [{"value": False}],
                    },
                },
                {
                    "id": {"record_id": "pers-former"},
                    "values": {
                        "email_addresses": [{"email_address": "dawny@klient.pl"}],
                        "company": [{"target_record_id": "comp-former"}],
                        "resend_unsubscribed": [{"value": False}],
                    },
                },
            ],
            companies=[
                {
                    "id": {"record_id": "comp-global"},
                    "values": {
                        "name": [{"value": "Salon Global"}],
                        "lead_status": [{"status": {"title": "Wysłano kampanię"}}],
                    },
                },
                {
                    "id": {"record_id": "comp-former"},
                    "values": {
                        "name": [{"value": "Dawny Klient SPA"}],
                        "lead_status": [{"status": {"title": "Były klient"}}],
                    },
                },
            ],
            sends=[],
        )
        resend = FakeResendClient(
            emails=[
                {
                    "id": "em_former_1",
                    "to": ["dawny@klient.pl"],
                    "subject": "Winback",
                    "created_at": "2026-11-26T10:00:00Z",
                    "last_event": "clicked",
                }
            ],
            contacts=[
                {"email": "kontakt@salon-global.pl", "unsubscribed": True},
                {"email": "dawny@klient.pl", "unsubscribed": True},
            ],
        )

        # audience_id="" -> używa globalnego GET /contacts bez RESEND_AUDIENCE_ID!
        report = rmod.sync_resend_feedback_to_attio(attio, resend, audience_id="", dry_run=False)
        assert resend.last_audience_id == ""
        assert report["stats"]["resend_contacts_fetched"] == 2
        assert report["stats"]["marked_unsubscribed_people"] == 2

        transitions = {t["company_id"]: t["to"] for t in report["status_transitions"]}
        # comp-global przechodzi na Odrzucony / DNC, natomiast 'Były klient' jest chroniony!
        assert transitions == {"comp-global": "Odrzucony / DNC"}

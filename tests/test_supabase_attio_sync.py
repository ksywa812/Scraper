"""Unit tests for supabase_attio_sync.py (Step 6b: Supabase READ-ONLY -> Attio Trial & Client sync)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import supabase_attio_sync as smod
from attio_sync import classify_soundyou_industries


class FakeSupabaseReadClient:
    def __init__(self, *, profiles=None, demo_plays=None):
        self._profiles = list(profiles or [])
        self._demo_plays = list(demo_plays or [])
        self.fetch_profiles_calls = 0
        self.fetch_demos_calls = 0

    def fetch_owner_profiles(self, page_size: int = 500):
        self.fetch_profiles_calls += 1
        return list(self._profiles)

    def fetch_demo_plays(self, page_size: int = 500):
        self.fetch_demos_calls += 1
        return list(self._demo_plays)


class FakeAttioForSupabase:
    def __init__(self, *, people=None, companies=None):
        self.people = list(people or [])
        self.companies = list(companies or [])
        self.calls: list[tuple[str, str, dict | None]] = []
        self.query_counts: dict[str, int] = {}
        self._seq = 0

    def query_all_records(self, obj_slug: str):
        self.query_counts[obj_slug] = self.query_counts.get(obj_slug, 0) + 1
        if obj_slug == "people":
            return list(self.people)
        if obj_slug == "companies":
            return list(self.companies)
        return []

    def request(self, path: str, payload=None, *, method=None):
        m = method or ("POST" if payload is not None else "GET")
        self.calls.append((m, path, payload))
        if m == "POST":
            self._seq += 1
            return {"id": {"record_id": f"new-rec-{self._seq}"}}
        if m == "PATCH":
            return {"id": {"record_id": path.rsplit("/", 1)[-1]}}
        raise AssertionError(f"Unexpected Attio call: {m} {path}")


class TestSupabaseAttioSync:
    def test_all_soundyou_app_business_dropdown_options_map_cleanly(self):
        expected = {
            "Hotel": "Hotele i Pensjonaty",
            "Restauracja": "Restauracje",
            "Siłownia": "Fitness / Siłownia",
            "Spa": "SPA & Wellness",
            "Studio Jogi": "Joga / Pilates / Medytacja",
            "Poczekalnia": "Poczekalnia / Gabinet",
            "Lokal Usługowy": "Lokal Usługowy / Inne",
            "Beauty": "Beauty / Salon Urody",
            "Fryzjer": "Salon Fryzjerski / Barber",
            "Kawiarnia": "Kawiarnie",
            "Sklep": "Sklep / Retail",
        }
        for app_label, canonical_attio in expected.items():
            mapped = classify_soundyou_industries(raw_industry=app_label)
            assert canonical_attio in mapped, f"{app_label} -> {mapped}"

    def test_classify_profile_and_status_precedence_never_downgrades_client(self):
        assert smod.classify_profile_target_status({"plan": "Pro", "valid_until": "2027-05-01"}, today_iso="2026-11-26") == "Klient"
        assert smod.classify_profile_target_status({"plan": "Standard"}, today_iso="2026-11-26") == "Klient"
        assert smod.classify_profile_target_status(
            {"plan": "Business", "subscription_cancelled_at": "2026-11-10T12:00:00Z"},
            today_iso="2026-11-26",
        ) == "Były klient"
        assert smod.classify_profile_target_status(
            {"plan": "Pro", "valid_until": "2026-10-01"},
            today_iso="2026-11-26",
        ) == "Były klient"
        assert smod.classify_profile_target_status({"plan": "Trial"}) == "Trial SoundYou"
        assert smod.classify_profile_target_status({"plan": "", "onboarding_completed": True}) == "Trial SoundYou"
        assert smod.classify_profile_target_status({"plan": "", "marketing_opt_out": True}) == "Odrzucony / DNC"
        assert smod.classify_profile_target_status({"plan": ""}) is None

        # Awans w górę działa + dwukierunkowe przejście Klient <-> Były klient:
        assert smod.compute_status_upgrade("Wysłano kampanię", "Trial SoundYou") == "Trial SoundYou"
        assert smod.compute_status_upgrade("Trial SoundYou", "Klient") == "Klient"
        assert smod.compute_status_upgrade("Klient", "Były klient") == "Były klient"
        assert smod.compute_status_upgrade("Były klient", "Klient") == "Klient"
        # Konwersja w aplikacji przebija nawet wcześniejsze Odrzucony / DNC:
        assert smod.compute_status_upgrade("Odrzucony / DNC", "Trial SoundYou") == "Trial SoundYou"
        assert smod.compute_status_upgrade("Odrzucony / DNC", "Były klient") == "Były klient"
        assert smod.compute_status_upgrade("Odrzucony / DNC", "Klient") == "Klient"
        # NIGDY nie cofa Klienta / Byłego klienta do Triala ani Odrzucony:
        assert smod.compute_status_upgrade("Klient", "Trial SoundYou") is None
        assert smod.compute_status_upgrade("Były klient", "Trial SoundYou") is None
        assert smod.compute_status_upgrade("Klient", "Odrzucony / DNC") is None
        assert smod.compute_status_upgrade("Były klient", "Odrzucony / DNC") is None
        assert smod.compute_status_upgrade("Trial SoundYou", "Odrzucony / DNC") is None

    def test_matches_gmail_trial_via_person_link_and_paid_client_via_corporate_domain(self):
        sb = FakeSupabaseReadClient(
            profiles=[
                # 1. Salon na @gmail.com, który był już w Attio ze statusem 'Wysłano kampanię' -> awans do 'Trial SoundYou'
                {
                    "id": "p-1",
                    "email": "harmonia.kobido@gmail.com",
                    "business_name": "Harmonia Kobido",
                    "business": "Spa",
                    "country": "PL",
                    "plan": "Trial",
                    "onboarding_completed": True,
                    "marketing_opt_out": False,
                },
                # 2. Hotel z własną domeną @hotelbursztyn.pl na płatnym planie + marketing_opt_out -> 'Klient' + plan + valid_until + unsub na People
                {
                    "id": "p-2",
                    "email": "manager@hotelbursztyn.pl",
                    "business_name": "Hotel Bursztyn",
                    "business": "Hotel",
                    "country": "PL",
                    "plan": "Business",
                    "valid_until": "2027-06-30T23:59:59Z",
                    "stripe_customer_id": "cus_123",
                    "onboarding_completed": True,
                    "marketing_opt_out": True,
                },
                # 3. Istniejący 'Klient' w Attio z drugim/starym kontem Trial -> NIE może spaść z 'Klient' na 'Trial SoundYou'
                {
                    "id": "p-3",
                    "email": "recepcja@spalotus.pl",
                    "business_name": "SPA Lotus",
                    "business": "Spa",
                    "country": "PL",
                    "plan": "Trial",
                    "onboarding_completed": True,
                    "marketing_opt_out": False,
                },
                # 4. Anulowana subskrypcja dawnego klienta -> przechodzi z 'Klient' na 'Były klient' z zachowaniem planu i daty wygaśnięcia
                {
                    "id": "p-4",
                    "email": "kontakt@bistrozielone.pl",
                    "business_name": "Bistro Zielone",
                    "business": "Restauracja",
                    "country": "PL",
                    "plan": "Standard",
                    "valid_until": "2026-10-15",
                    "subscription_cancelled_at": "2026-10-10T09:00:00Z",
                    "onboarding_completed": True,
                    "marketing_opt_out": False,
                },
            ]
        )

        attio = FakeAttioForSupabase(
            companies=[
                {
                    "id": {"record_id": "comp-gmail"},
                    "values": {
                        "name": [{"value": "Harmonia Kobido"}],
                        "domains": [],
                        "soundyou_industry": [],  # puste -> powinno uzupełnić z 'Spa'
                        "lead_status": [{"status": {"title": "Wysłano kampanię"}}],
                    },
                },
                {
                    "id": {"record_id": "comp-hotel"},
                    "values": {
                        "name": [{"value": "Hotel Bursztyn"}],
                        "domains": [{"domain": "hotelbursztyn.pl"}],
                        "soundyou_industry": [{"option": {"title": "Hotele i Pensjonaty"}}],
                        "lead_status": [{"status": {"title": "Trial SoundYou"}}],
                    },
                },
                {
                    "id": {"record_id": "comp-client"},
                    "values": {
                        "name": [{"value": "SPA Lotus"}],
                        "domains": [{"domain": "spalotus.pl"}],
                        "soundyou_industry": [{"option": {"title": "SPA & Wellness"}}],
                        "lead_status": [{"status": {"title": "Klient"}}],
                    },
                },
                {
                    "id": {"record_id": "comp-churned"},
                    "values": {
                        "name": [{"value": "Bistro Zielone"}],
                        "domains": [{"domain": "bistrozielone.pl"}],
                        "soundyou_industry": [{"option": {"title": "Restauracje"}}],
                        "lead_status": [{"status": {"title": "Klient"}}],
                    },
                },
            ],
            people=[
                {
                    "id": {"record_id": "pers-gmail"},
                    "values": {
                        "email_addresses": [{"email_address": "harmonia.kobido@gmail.com"}],
                        "company": [{"target_record_id": "comp-gmail"}],
                        "resend_unsubscribed": [{"value": False}],
                    },
                },
                {
                    "id": {"record_id": "pers-client"},
                    "values": {
                        "email_addresses": [{"email_address": "recepcja@spalotus.pl"}],
                        "company": [{"target_record_id": "comp-client"}],
                        "resend_unsubscribed": [{"value": False}],
                    },
                },
            ],
        )

        res = smod.sync_supabase_to_attio(sb, attio, dry_run=False, create_missing=True)
        stats = res["stats"]

        # Zapytania GET do Attio powinny być zbuforowane (po 1 razie na obiekt)
        assert attio.query_counts == {"companies": 1, "people": 1}

        assert stats["matched_existing_companies"] == 4
        assert stats["status_to_Trial SoundYou"] == 1
        assert stats["status_to_Klient"] == 1
        assert stats["status_to_Były klient"] == 1
        assert stats["unchanged_companies"] == 1  # comp-client pozostał 'Klient'
        assert stats["created_people"] == 2  # manager@hotelbursztyn.pl + kontakt@bistrozielone.pl
        assert stats["people_marked_unsubscribed"] == 1

        patch_comp_calls = {
            path: payload["data"]["values"]
            for m, path, payload in attio.calls
            if m == "PATCH" and path.startswith("/objects/companies/records/")
        }
        assert patch_comp_calls["/objects/companies/records/comp-gmail"] == {
            "lead_status": "Trial SoundYou",
            "soundyou_plan": "Trial",
            "soundyou_industry": ["SPA & Wellness", "Kobido & Masaż"],
        }
        assert patch_comp_calls["/objects/companies/records/comp-hotel"] == {
            "lead_status": "Klient",
            "soundyou_plan": "Business",
            "soundyou_valid_until": "2027-06-30",
        }
        assert patch_comp_calls["/objects/companies/records/comp-churned"] == {
            "lead_status": "Były klient",
            "soundyou_plan": "Standard",
            "soundyou_valid_until": "2026-10-15",
        }
        assert "/objects/companies/records/comp-client" not in patch_comp_calls

        post_person_calls = [
            payload["data"]["values"]
            for m, path, payload in attio.calls
            if m == "POST" and path == "/objects/people/records"
        ]
        assert len(post_person_calls) == 2
        assert post_person_calls[0]["email_addresses"] == ["manager@hotelbursztyn.pl"]
        assert post_person_calls[0]["resend_unsubscribed"] is True

    def test_creates_organic_signup_when_enabled_and_respects_dry_run_and_demo_plays(self):
        sb = FakeSupabaseReadClient(
            profiles=[
                {
                    "id": "p-new",
                    "email": "kontakt@jogaprzystan.pl",
                    "business_name": "Przystań Jogi",
                    "business": "Studio Jogi",
                    "country": "PL",
                    "plan": "Trial",
                    "onboarding_completed": True,
                    "marketing_opt_out": False,
                },
            ],
            demo_plays=[
                {
                    "email": "recepcja@kawiarnia-aroma.pl",
                    "industry": "Kawiarnia",
                    "played_at": "2026-11-25T14:00:00Z",
                }
            ],
        )
        attio_base_companies = [
            {
                "id": {"record_id": "comp-cafe"},
                "values": {
                    "name": [{"value": "Kawiarnia Aroma"}],
                    "domains": [{"domain": "kawiarnia-aroma.pl"}],
                    "soundyou_industry": [{"option": {"title": "Kawiarnie"}}],
                    "lead_status": [{"status": {"title": "Zainteresowany"}}],
                },
            }
        ]

        # 1. Dry-run nie wykonuje żadnych zapisów POST/PATCH do Attio
        attio_dry = FakeAttioForSupabase(companies=attio_base_companies, people=[])
        dry_res = smod.sync_supabase_to_attio(
            sb, attio_dry, dry_run=True, create_missing=True, include_demo_plays=True
        )
        assert attio_dry.calls == []
        assert dry_res["stats"]["created_companies"] == 1
        assert dry_res["stats"]["demo_plays_upgraded"] == 1

        # 2. Live tworzy nową firmę z Studio Jogi -> 'Joga / Pilates / Medytacja' oraz awansuje demo_play -> 'Rozmowa / Demo'
        attio_live = FakeAttioForSupabase(companies=attio_base_companies, people=[])
        live_res = smod.sync_supabase_to_attio(
            sb, attio_live, dry_run=False, create_missing=True, include_demo_plays=True
        )
        assert live_res["stats"]["created_companies"] == 1
        assert live_res["stats"]["created_people"] == 1
        assert live_res["stats"]["status_to_Rozmowa / Demo"] == 1

        created_comp = [
            payload["data"]["values"]
            for m, path, payload in attio_live.calls
            if m == "POST" and path == "/objects/companies/records"
        ][0]
        assert created_comp["name"] == "Przystań Jogi"
        assert created_comp["lead_status"] == "Trial SoundYou"
        assert created_comp["lead_batch"] == "SoundYou App (Supabase)"
        assert created_comp["domains"] == ["jogaprzystan.pl"]
        assert "Joga / Pilates / Medytacja" in created_comp["soundyou_industry"]

    def test_cli_main_dry_run_writes_report_and_returns_zero(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "sb_test_key")
        monkeypatch.setenv("ATTIO_API_KEY", "attio_test_key")
        monkeypatch.setattr(smod, "REPORTS_DIR", tmp_path)
        monkeypatch.setattr(
            smod,
            "SupabaseReadClient",
            lambda url, key: FakeSupabaseReadClient(
                profiles=[{"id": "1", "email": "spa@example.pl", "business_name": "Spa Test", "business": "Spa", "plan": "Trial"}]
            ),
        )
        monkeypatch.setattr(smod, "AttioClient", lambda token: FakeAttioForSupabase())

        rc = smod.main(["--dry-run"])
        assert rc == 0
        reports = list(tmp_path.glob("supabase_attio_sync_*.json"))
        assert len(reports) == 1

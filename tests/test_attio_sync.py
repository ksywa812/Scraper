"""Unit tests for attio_sync.py — 12 Attio SoundYou options, EN-PL key, CSV dedup, and mocked Attio sync."""
from __future__ import annotations

import csv
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import attio_sync as mod


class FakeAttioClient:
    """In-memory mock of AttioClient for deterministic sync & schema tests."""

    def __init__(
        self,
        *,
        attributes=None,
        options=None,
        views=None,
        companies=None,
        people=None,
    ):
        self.attributes = list(attributes or [])
        self.options = list(options or [])
        self.views = list(views or [])
        self.companies = list(companies or [])
        self.people = list(people or [])
        self.calls: list[tuple[str, str, dict | None]] = []
        self._counter = 0

    def request(self, path, payload=None, *, method=None):
        m = method or ("POST" if payload is not None else "GET")
        self.calls.append((m, path, payload))

        if m == "GET" and path == "/objects/companies/attributes":
            return self.attributes
        if m == "POST" and path == "/objects/companies/attributes":
            self.attributes.append(payload["data"])
            return payload["data"]
        if m == "GET" and path == f"/objects/companies/attributes/{mod.INDUSTRY_ATTR_SLUG}/options":
            return self.options
        if m == "POST" and path == f"/objects/companies/attributes/{mod.INDUSTRY_ATTR_SLUG}/options":
            opt = {"title": payload["data"]["title"], "is_archived": False}
            self.options.append(opt)
            return opt
        if m == "GET" and path == "/objects/companies/views":
            return self.views
        if m == "POST" and path == "/objects/companies/records":
            self._counter += 1
            rid = f"comp-{self._counter}"
            self.companies.append({"id": {"record_id": rid}, "values": payload["data"]["values"]})
            return {"id": {"record_id": rid}}
        if m == "PATCH" and path.startswith("/objects/companies/records/"):
            return {"id": {"record_id": path.rsplit("/", 1)[-1]}}
        if m == "POST" and path == "/objects/people/records":
            self._counter += 1
            rid = f"pers-{self._counter}"
            self.people.append({"id": {"record_id": rid}, "values": payload["data"]["values"]})
            return {"id": {"record_id": rid}}
        if m == "PATCH" and path.startswith("/objects/people/records/"):
            return {"id": {"record_id": path.rsplit("/", 1)[-1]}}
        raise AssertionError(f"Unexpected request: {m} {path}")

    def query_all_records(self, obj_slug: str):
        if obj_slug == "companies":
            return list(self.companies)
        if obj_slug == "people":
            return list(self.people)
        return []


# ─── Industry classification (12 Attio options + EN/PL key) ─────────────────

class TestClassifySoundYouIndustries:
    def test_exact_12_attio_options_defined(self):
        assert len(mod.SOUNDYOU_INDUSTRIES) == 12
        assert len(mod.EXPECTED_INDUSTRY_VIEWS) == 8

    def test_hotel_spa_becomes_multiselect(self):
        res = mod.classify_soundyou_industries("hotel-spa", name="Hotel Dwór Elizy")
        assert res == ["SPA & Wellness", "Hotele i Pensjonaty"]

    def test_en_and_pl_keys_map_identically(self):
        assert mod.classify_soundyou_industries("beauty salon") == ["Beauty / Salon Urody"]
        assert mod.classify_soundyou_industries("uroda") == ["Beauty / Salon Urody"]

        assert mod.classify_soundyou_industries("coffee shop") == ["Kawiarnie"]
        assert mod.classify_soundyou_industries("kawiarnia") == ["Kawiarnie"]

        assert mod.classify_soundyou_industries("restaurant") == ["Restauracje"]
        assert mod.classify_soundyou_industries("restauracja") == ["Restauracje"]

        assert mod.classify_soundyou_industries("gym") == ["Fitness / Siłownia"]
        assert mod.classify_soundyou_industries("siłownia") == ["Fitness / Siłownia"]

        assert mod.classify_soundyou_industries("yoga") == ["Joga / Pilates / Medytacja"]
        assert mod.classify_soundyou_industries("joga") == ["Joga / Pilates / Medytacja"]

        assert mod.classify_soundyou_industries("barbershop") == ["Salon Fryzjerski / Barber"]
        assert mod.classify_soundyou_industries("fryzjer") == ["Salon Fryzjerski / Barber"]

        assert mod.classify_soundyou_industries("physiotherapy") == ["Poczekalnia / Gabinet"]
        assert mod.classify_soundyou_industries("fizjoterapia") == ["Poczekalnia / Gabinet"]

        assert mod.classify_soundyou_industries("boutique") == ["Sklep / Retail"]
        assert mod.classify_soundyou_industries("sklep") == ["Sklep / Retail"]

        assert mod.classify_soundyou_industries("massage") == ["Kobido & Masaż"]
        assert mod.classify_soundyou_industries("masaż") == ["Kobido & Masaż"]

    def test_spa_with_thai_massage_gets_both_tags(self):
        res = mod.classify_soundyou_industries("spa", name="SĀN SPA & Masaż balijski i tajski")
        assert res == ["SPA & Wellness", "Kobido & Masaż"]

    def test_comma_separated_canonical_preserved(self):
        res = mod.classify_soundyou_industries("Hotele i Pensjonaty, SPA & Wellness")
        assert res == ["SPA & Wellness", "Hotele i Pensjonaty"]

    def test_unknown_falls_back_to_lokal_uslugowy(self):
        assert mod.classify_soundyou_industries("xyz_unknown", name="ABC Sp. z o.o.") == ["Lokal Usługowy / Inne"]


# ─── CSV loading & local deduplication ──────────────────────────────────────

class TestCsvDedup:
    def _write_csv(self, path: Path, rows: list[dict[str, str]]) -> Path:
        headers = [
            "Branża", "Województwo", "Miasto", "Name", "Address", "Phone",
            "Website", "Email 1", "Email 2", "Email 3", "Other Emails", "Sources",
        ]
        with path.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=headers)
            w.writeheader()
            for r in rows:
                w.writerow({h: r.get(h, "") for h in headers})
        return path

    def test_merges_duplicate_by_phone_and_combines_industries(self, tmp_path: Path):
        p = self._write_csv(tmp_path / "leads.csv", [
            {
                "Branża": "spa",
                "Województwo": "Śląskie",
                "Miasto": "Katowice",
                "Name": "Odnova Float & SPA ✨",
                "Address": "Jesionowa 10, 40-158 Katowice",
                "Phone": "797 990 066",
                "Website": "https://www.odnovaspa.pl/",
                "Email 1": "kontakt@odnova.slask.pl",
                "Email 2": "aneksy@booksy.com",
            },
            {
                "Branża": "beauty salon",
                "Województwo": "Śląskie",
                "Miasto": "Katowice",
                "Name": "Odnova Instytut",
                "Address": "",
                "Phone": "+48 797 990 066",
                "Website": "https://booksy.com/pl-pl/odnova",
                "Email 1": "biuro@odnova.slask.pl",
            },
            {
                "Branża": "spa",
                "Miasto": "Katowice",
                "Name": "123/45",  # junk name
                "Phone": "500600700",
            },
        ])
        leads, stats, domain_counts = mod.load_and_dedup_csv([p])
        assert len(leads) == 1
        assert stats["invalid_rows"] == 1
        assert stats["merged_source_rows"] == 1
        lead = leads[0]
        assert lead.name == "Odnova Float & SPA"
        assert lead.ordered_industries == ["SPA & Wellness", "Beauty / Salon Urody"]
        assert lead.emails == {"kontakt@odnova.slask.pl", "biuro@odnova.slask.pl"}
        assert lead.domains == {"odnovaspa.pl"}
        assert domain_counts["odnovaspa.pl"] == 1


# ─── Schema setup & Attio sync ──────────────────────────────────────────────

class TestSchemaAndSync:
    def test_ensure_schema_creates_attribute_and_all_12_options(self):
        client = FakeAttioClient(
            views=[{"title": t} for t in mod.EXPECTED_INDUSTRY_VIEWS]
        )
        summary = mod.ensure_soundyou_schema(client, dry_run=False)
        assert summary["attribute_created"] is True
        assert summary["options_created"] == list(mod.SOUNDYOU_INDUSTRIES)
        assert len(client.options) == 12
        assert summary["missing_industry_views"] == []
        assert len(summary["existing_industry_views"]) == 8

        # Idempotent second run
        summary2 = mod.ensure_soundyou_schema(client, dry_run=False)
        assert summary2["attribute_existed"] is True
        assert summary2["attribute_created"] is False
        assert summary2["options_created"] == []

    def test_sync_creates_new_and_enriches_existing_without_duplicates(self):
        client = FakeAttioClient(
            companies=[
                {
                    "id": {"record_id": "existing-comp-1"},
                    "values": {
                        "name": [{"value": "Jasmine Spa"}],
                        "domains": [{"domain": "jasminespa.pl"}],
                        "primary_location": [{"locality": "Wrocław"}],
                        "description": [{"value": "Existing manual description"}],
                        mod.INDUSTRY_ATTR_SLUG: [{"option": {"title": "SPA & Wellness"}}],
                    },
                }
            ],
            people=[
                {
                    "id": {"record_id": "existing-pers-1"},
                    "values": {
                        "email_addresses": [{"email_address": "recepcja@jasminespa.pl"}],
                        "company": [],
                    },
                }
            ],
        )

        leads = [
            mod.Lead(
                name="Jasmine Spa & Masaż Tajski",
                city="Wrocław",
                region="Dolnośląskie",
                address="Rynek 1, 50-101 Wrocław",
                industries={"SPA & Wellness", "Kobido & Masaż"},
                emails={"recepcja@jasminespa.pl", "nowy@jasminespa.pl"},
                phones={"+48500100200"},
                domains={"jasminespa.pl"},
            ),
            mod.Lead(
                name="Hotel Słoneczny & SPA",
                city="Katowice",
                region="Śląskie",
                address="Słoneczna 5, 40-100 Katowice",
                industries={"Hotele i Pensjonaty", "SPA & Wellness"},
                emails={"biuro@hotelsloneczny.pl"},
                phones={"+48600700800"},
                domains={"hotelsloneczny.pl"},
            ),
        ]

        stats = Counter()
        domain_counts = Counter({"jasminespa.pl": 1, "hotelsloneczny.pl": 1})

        res = mod.sync_leads_to_attio(client, leads, stats, domain_counts, dry_run=False, sync_people=True)
        st = res["stats"]
        assert st["updated_companies"] == 1
        assert st["created_companies"] == 1
        assert st["linked_existing_people"] == 1
        assert st["created_people"] == 2

        patch_calls = [c for c in client.calls if c[0] == "PATCH" and c[1] == "/objects/companies/records/existing-comp-1"]
        assert len(patch_calls) == 1
        assert patch_calls[0][2]["data"]["values"] == {
            mod.INDUSTRY_ATTR_SLUG: ["SPA & Wellness", "Kobido & Masaż"]
        }

    def test_dry_run_makes_no_write_requests(self):
        client = FakeAttioClient()
        leads = [
            mod.Lead(
                name="Cafe Aroma",
                city="Kraków",
                region="Małopolskie",
                address="Floriańska 10, 31-021 Kraków",
                industries={"Kawiarnie"},
                emails={"hi@cafearoma.pl"},
                phones={"+48501502503"},
                domains={"cafearoma.pl"},
            )
        ]
        res = mod.sync_leads_to_attio(client, leads, Counter(), Counter({"cafearoma.pl": 1}), dry_run=True)
        assert res["stats"]["created_companies"] == 1
        assert res["stats"]["created_people"] == 1
        write_calls = [c for c in client.calls if c[0] in ("POST", "PATCH", "PUT", "DELETE")]
        assert write_calls == []

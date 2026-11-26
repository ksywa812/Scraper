"""
supabase_attio_sync.py — Krok 6b: Synchronizacja kont Trial i Klient z bazy SoundYou (Supabase) do Attio CRM.

Bezpieczeństwo (READ-ONLY względem Supabase):
  - Skrypt wykonuje WYŁĄCZNIE zapytania odczytu `GET /rest/v1/profiles` (i opcjonalnie `GET /rest/v1/demo_plays`)
    przez Supabase PostgREST — NIGDY nie modyfikuje bazy produkcyjnej `WEBPlayerPV`.
  - Zapis odbywa się wyłącznie po stronie Attio CRM (i jest całkowicie wyłączony w trybie `--dry-run`).

Funkcje:
  1. Pobiera konta właścicieli (`role = 'owner'`, `parent_profile_id IS NULL`) z `public.profiles`.
  2. Klasyfikuje etap produktu SoundYou na kanoniczny `Status leada` (`lead_status`) w Attio:
     - Aktywny lub historyczny płatny plan (`plan` różny od pustego / `Trial` / `Free` / `Demo`) -> `Klient`
     - Konto w okresie próbnym (`plan = 'Trial'`, ukończony onboarding lub podpięty `stripe_customer_id`) -> `Trial SoundYou`
     - `marketing_opt_out = true` -> ustawia `resend_unsubscribed = true` na `People` w Attio
       (oraz `Odrzucony / DNC` na `Companies`, jeśli konto nie jest `Trial SoundYou` ani `Klient`).
     - Opcjonalnie (`--include-demo-plays`): odtworzenia demo z `public.demo_plays` -> `Rozmowa / Demo`
       dla wczesnych leadów (bez cofania `Trial SoundYou` / `Klient`).
  3. Dopasowuje konta SoundYou do rekordów w Attio w kolejności:
     - istniejący `Person` po adresie e-mail -> jego `company` (działa również dla salonów na `@gmail.com` / `@wp.pl`!),
     - e-mail występujący w opisie firmy (`companies_by_email`),
     - domena firmowa adresu e-mail (`companies_by_domain` — z pominięciem darmowych skrzynek),
     - znormalizowana nazwa firmy `business_name` (`companies_by_name_city` / `companies_by_unique_name`).
  4. Chroni wyższe etapy lejka przed cofnięciem (np. `Klient` NIGDY nie spadnie do `Trial SoundYou`),
     dopisuje `soundyou_industry` tylko gdy w Attio jest puste, a dla nowych rejestracji organicznych
     tworzy brakujące rekordy `Company` + `Person` z partią `SoundYou App (Supabase)` (można wyłączyć `--no-create-missing`).

Użycie:
    python supabase_attio_sync.py --dry-run
    python supabase_attio_sync.py
    python supabase_attio_sync.py --no-create-missing
    python supabase_attio_sync.py --include-demo-plays
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, build_opener

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

from attio_sync import (
    AttioAPIError,
    AttioClient,
    FREE_EMAIL_DOMAINS,
    INDUSTRY_ATTR_SLUG,
    LEAD_BATCH_SLUG,
    LEAD_STATUS_SLUG,
    PLATFORM_DOMAINS,
    REPORTS_DIR,
    SOUNDYOU_PLAN_SLUG,
    SOUNDYOU_VALID_UNTIL_SLUG,
    _NoRedirect,
    _extract_multiselect_titles,
    build_attio_indexes,
    business_domain,
    classify_soundyou_industries,
    normalized,
    resolve_attio_token,
    resolve_company_name,
    validate_email_syntax,
    write_sync_report,
)
from resend_attio_sync import (
    _extract_single_bool,
    _extract_single_text,
    _extract_status_title,
)

logger = logging.getLogger("supabase_attio_sync")

DEFAULT_SUPABASE_BATCH = "SoundYou App (Supabase)"
NON_PAID_PLAN_TOKENS = frozenset({"", "trial", "free", "demo", "none", "test", "brak"})

# Hierarchia statusów w lejku: wyższa liczba = wyższy etap konwersji
STATUS_PRECEDENCE: dict[str, int] = {
    "": 0,
    "Nowy": 10,
    "Gotowy do kampanii": 20,
    "Wysłano kampanię": 30,
    "Zainteresowany": 40,
    "Rozmowa / Demo": 60,
    "Trial SoundYou": 80,
    "Były klient": 95,
    "Klient": 100,
}

PROFILES_FULL_SELECT = (
    "id,email,business_name,business,country,nip,plan,valid_until,"
    "max_locations,stripe_customer_id,payment_issue_code,"
    "subscription_cancelled_at,onboarding_completed,marketing_opt_out"
)
PROFILES_FALLBACK_SELECT = (
    "id,email,business_name,business,country,plan,valid_until,"
    "stripe_customer_id,subscription_cancelled_at,onboarding_completed"
)


# ─── Klient READ-ONLY Supabase PostgREST ────────────────────────────────────

class SupabaseAPIError(Exception):
    def __init__(self, status: int, message: str = ""):
        self.status = status
        self.message = message
        super().__init__(f"Supabase HTTP {status}: {message}")


class SupabaseReadClient:
    """Ściśle READ-ONLY klient Supabase PostgREST (obsługuje wyłącznie `GET`)."""

    def __init__(self, supabase_url: str, service_role_key: str, pace_seconds: float = 0.05):
        clean_url = (supabase_url or "").strip().rstrip("/")
        if not clean_url.startswith(("https://", "http://")):
            raise ValueError(f"Nieprawidłowy SUPABASE_URL: {supabase_url!r}")
        self.base_url = f"{clean_url}/rest/v1"
        self.service_role_key = (service_role_key or "").strip()
        self.pace_seconds = pace_seconds
        self._opener = build_opener(_NoRedirect)

    def get(self, table: str, params: list[tuple[str, str]] | dict[str, str]) -> list[dict[str, Any]]:
        """Wykonuje wyłącznie zapytanie `GET` do `/rest/v1/{table}`."""
        qs = urlencode(params)
        url = f"{self.base_url}/{table.lstrip('/')}?{qs}" if qs else f"{self.base_url}/{table.lstrip('/')}"
        req = Request(
            url,
            method="GET",
            headers={
                "apikey": self.service_role_key,
                "Authorization": f"Bearer {self.service_role_key}",
                "Accept": "application/json",
                "User-Agent": "SoundYou-CRM-Sync/1.0",
            },
        )
        for attempt in range(4):
            if self.pace_seconds > 0:
                time.sleep(self.pace_seconds)
            try:
                with self._opener.open(req, timeout=25) as resp:
                    payload = json.load(resp)
                    return payload if isinstance(payload, list) else []
            except HTTPError as err:
                status = err.code
                msg = ""
                try:
                    msg = err.read().decode("utf-8", errors="replace")[:250]
                except OSError:
                    pass
                err.close()
                if status in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise SupabaseAPIError(status, msg) from None
        raise SupabaseAPIError(429, "Rate limit exceeded")

    def fetch_owner_profiles(self, page_size: int = 500) -> list[dict[str, Any]]:
        """Pobiera wszystkie główne konta właścicieli (`role=owner`, `parent_profile_id IS NULL`)."""
        select_cols = PROFILES_FULL_SELECT
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            params = [
                ("select", select_cols),
                ("role", "eq.owner"),
                ("parent_profile_id", "is.null"),
                ("order", "id.asc"),
                ("limit", str(page_size)),
                ("offset", str(offset)),
            ]
            try:
                batch = self.get("profiles", params)
            except SupabaseAPIError as err:
                if err.status == 400 and select_cols != PROFILES_FALLBACK_SELECT and offset == 0:
                    logger.info("Kolumna opcjonalna niedostępna w profiles — przełączam na zestaw podstawowy.")
                    select_cols = PROFILES_FALLBACK_SELECT
                    continue
                raise
            if not batch:
                break
            rows.extend(batch)
            if len(batch) < page_size:
                break
            offset += len(batch)
        return rows

    def fetch_demo_plays(self, page_size: int = 500) -> list[dict[str, Any]]:
        """Pobiera rekordy odtworzeń demo z `public.demo_plays` (opcjonalnie)."""
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            params = [
                ("select", "email,industry,played_at"),
                ("email", "not.is.null"),
                ("order", "played_at.desc"),
                ("limit", str(page_size)),
                ("offset", str(offset)),
            ]
            batch = self.get("demo_plays", params)
            if not batch:
                break
            rows.extend(batch)
            if len(batch) < page_size:
                break
            offset += len(batch)
        return rows


# ─── Logika domenowa klasyfikacji i progresji statusów ──────────────────────

def extract_corporate_domain(email: str) -> str:
    """Zwraca domenę firmową z e-maila lub `""` dla darmowych/platformowych skrzynek."""
    e = (email or "").strip().lower()
    if "@" not in e:
        return ""
    _, dom = e.rsplit("@", 1)
    if not dom or dom in FREE_EMAIL_DOMAINS or dom in PLATFORM_DOMAINS:
        return ""
    return business_domain(dom)


def extract_iso_date(raw_val: Any) -> str:
    """Wyciąga datę w formacie `YYYY-MM-DD` z pola `valid_until` (lub zwraca `""`)."""
    s = str(raw_val or "").strip()
    if len(s) >= 10 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", s[:10]):
        return s[:10]
    return ""


def _utc_today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def classify_profile_target_status(profile: dict[str, Any], *, today_iso: str | None = None) -> str | None:
    """Mapuje wiersz `public.profiles` na docelowy `Status leada` w Attio (`Klient`, `Były klient`, `Trial SoundYou`, `Odrzucony / DNC` lub `None`)."""
    raw_plan = str(profile.get("plan") or "").strip()
    plan_low = raw_plan.lower()
    has_stripe = bool(str(profile.get("stripe_customer_id") or "").strip())
    onboarded = bool(profile.get("onboarding_completed") is True)
    opt_out = bool(profile.get("marketing_opt_out") is True)
    cancelled = bool(str(profile.get("subscription_cancelled_at") or "").strip())
    valid_date = extract_iso_date(profile.get("valid_until"))
    today = today_iso or _utc_today_iso()

    if plan_low and plan_low not in NON_PAID_PLAN_TOKENS:
        if cancelled or (valid_date and valid_date < today):
            return "Były klient"
        return "Klient"

    if plan_low == "trial" or onboarded or has_stripe:
        return "Trial SoundYou"

    if opt_out:
        return "Odrzucony / DNC"

    return None


def compute_status_upgrade(current_status: str, target_status: str | None) -> str | None:
    """Zwraca nowy `lead_status`: wspiera awans w górę oraz dwukierunkowe przejście `Klient` <-> `Były klient` (nigdy nie cofa klienta do `Trial`)."""
    if not target_status:
        return None
    cur = (current_status or "").strip()
    if cur == target_status:
        return None

    if target_status == "Odrzucony / DNC":
        # Nie oznaczaj aktywnych rozmów/triali/klientów/byłych klientów jako Odrzucony (wypisanie trafia na People.resend_unsubscribed)
        if STATUS_PRECEDENCE.get(cur, 0) >= STATUS_PRECEDENCE["Rozmowa / Demo"]:
            return None
        return "Odrzucony / DNC"

    # Aktywny klient, któremu wygasła lub została anulowana subskrypcja -> 'Były klient'
    if cur == "Klient" and target_status == "Były klient":
        return "Były klient"

    # Realna konwersja w aplikacji (Trial / Były klient / Klient) przebija nawet wcześniejsze Odrzucony / DNC
    if cur == "Odrzucony / DNC":
        if STATUS_PRECEDENCE.get(target_status, 0) >= STATUS_PRECEDENCE["Trial SoundYou"]:
            return target_status
        return None

    if STATUS_PRECEDENCE.get(target_status, 0) > STATUS_PRECEDENCE.get(cur, 0):
        return target_status
    return None


def _normalize_country_code(country_raw: str | None) -> str:
    c = (country_raw or "").strip()
    if not c:
        return "PL"
    up = c.upper()
    if re.fullmatch(r"[A-Z]{2}", up):
        return up
    low = c.lower()
    if low in ("polska", "poland"):
        return "PL"
    return "PL"


def _fallback_company_name(business_name: str, email: str) -> str:
    resolved = resolve_company_name(raw_name=business_name, emails=[email])
    if resolved:
        return resolved
    clean_bn = (business_name or "").strip()
    if clean_bn:
        return clean_bn
    local = email.split("@", 1)[0]
    return local.replace(".", " ").replace("_", " ").replace("-", " ").title() or email


# ─── Odczyt dodatkowych metadanych Attio (`lead_status`, `unsubscribed`) ────

class _CachedQueryAttioProxy:
    """Proxy buforujące `query_all_records` w ramach jednego przebiegu synchronizacji."""

    def __init__(self, inner: Any):
        self._inner = inner
        self._cache: dict[str, list[dict[str, Any]]] = {}

    def query_all_records(self, obj_slug: str) -> list[dict[str, Any]]:
        if obj_slug not in self._cache:
            self._cache[obj_slug] = list(self._inner.query_all_records(obj_slug))
        return self._cache[obj_slug]

    def request(self, path: str, payload: Any = None, *, method: str | None = None) -> Any:
        if method is not None:
            return self._inner.request(path, payload, method=method)
        return self._inner.request(path, payload)


def _extract_single_date(raw_vals: list[dict[str, Any]]) -> str:
    for item in raw_vals or []:
        val = item.get("value")
        if isinstance(val, str) and val.strip():
            return extract_iso_date(val)
    return ""


def load_attio_status_and_unsub(attio: Any) -> tuple[dict[str, dict[str, str]], dict[str, bool]]:
    """Zwraca słowniki `company_id -> {lead_status, plan, valid_until}` oraz `person_id -> resend_unsubscribed`."""
    company_meta: dict[str, dict[str, str]] = {}
    for rec in attio.query_all_records("companies"):
        cid = rec["id"]["record_id"]
        vals = rec.get("values") or {}
        company_meta[cid] = {
            "lead_status": _extract_status_title(vals.get(LEAD_STATUS_SLUG, [])),
            "plan": _extract_single_text(vals.get(SOUNDYOU_PLAN_SLUG, [])),
            "valid_until": _extract_single_date(vals.get(SOUNDYOU_VALID_UNTIL_SLUG, [])),
        }

    person_unsubs: dict[str, bool] = {}
    for rec in attio.query_all_records("people"):
        pid = rec["id"]["record_id"]
        vals = rec.get("values") or {}
        person_unsubs[pid] = _extract_single_bool(vals.get("resend_unsubscribed", []))

    return company_meta, person_unsubs


def _match_profile_to_company(
    email: str,
    corp_domain: str,
    business_name: str,
    indexes: dict[str, Any],
) -> str | None:
    """Dopasowuje profil Supabase do istniejącego `company_id` w Attio."""
    person = indexes["people_by_email"].get(email)
    if person and person.get("company_id"):
        return person["company_id"]

    if email in indexes["companies_by_email"]:
        return indexes["companies_by_email"][email]

    if corp_domain and corp_domain in indexes["companies_by_domain"]:
        return indexes["companies_by_domain"][corp_domain]

    norm_name = normalized(business_name or "")
    if norm_name:
        if (norm_name, "") in indexes["companies_by_name_city"]:
            return indexes["companies_by_name_city"][(norm_name, "")]
        if norm_name in indexes["companies_by_unique_name"]:
            return indexes["companies_by_unique_name"][norm_name]

    return None


# ─── Główna pętla synchronizacji Supabase -> Attio ──────────────────────────

def sync_supabase_to_attio(
    sb_client: SupabaseReadClient,
    attio: AttioClient,
    *,
    dry_run: bool = False,
    create_missing: bool = True,
    include_demo_plays: bool = False,
    lead_batch: str = DEFAULT_SUPABASE_BATCH,
) -> dict[str, Any]:
    stats: Counter[str] = Counter()
    audit: dict[str, list[dict[str, Any]]] = {
        "upgraded_companies": [],
        "created_companies": [],
        "unsubscribed_people": [],
    }

    profiles = sb_client.fetch_owner_profiles()
    stats["supabase_profiles_fetched"] = len(profiles)

    demo_plays: list[dict[str, Any]] = []
    if include_demo_plays:
        demo_plays = sb_client.fetch_demo_plays()
        stats["supabase_demo_plays_fetched"] = len(demo_plays)

    cached_attio = _CachedQueryAttioProxy(attio)
    indexes = build_attio_indexes(cached_attio)  # type: ignore[arg-type]
    company_meta, person_unsubs = load_attio_status_and_unsub(cached_attio)

    profile_emails_seen: set[str] = set()
    active_client_cids: set[str] = set()
    today = _utc_today_iso()

    for prof in profiles:
        email = str(prof.get("email") or "").strip().lower()
        ok_email, _ = validate_email_syntax(email)
        if not ok_email:
            stats["skipped_invalid_email"] += 1
            continue
        profile_emails_seen.add(email)

        target_status = classify_profile_target_status(prof, today_iso=today)
        raw_plan_display = str(prof.get("plan") or "").strip() or (
            "Trial" if target_status == "Trial SoundYou" else ""
        )
        valid_until_iso = extract_iso_date(prof.get("valid_until"))
        opt_out = bool(prof.get("marketing_opt_out") is True)
        business_name = str(prof.get("business_name") or "").strip()
        raw_business = str(prof.get("business") or "").strip()
        corp_domain = extract_corporate_domain(email)

        inferred_industries = classify_soundyou_industries(
            raw_industry=raw_business,
            name=business_name,
            website=corp_domain,
        )
        # Nie nadpisuj pustego pola samym fallbackiem "Lokal Usługowy / Inne", jeśli profil nie podał branży
        has_specific_industry = bool(raw_business) or inferred_industries != ["Lokal Usługowy / Inne"]

        matched_cid = _match_profile_to_company(email, corp_domain, business_name, indexes)

        if matched_cid:
            stats["matched_existing_companies"] += 1
            meta = company_meta.setdefault(matched_cid, {"lead_status": "", "plan": "", "valid_until": ""})
            cur_status = meta["lead_status"]
            # Jeśli w tym samym przebiegu inne konto właściciela tej firmy jest aktywnym 'Klientem', nie obniżaj go do 'Były klient'
            effective_target = (
                None
                if (target_status == "Były klient" and matched_cid in active_client_cids)
                else target_status
            )
            next_status = compute_status_upgrade(cur_status, effective_target)
            if target_status == "Klient" or (cur_status == "Klient" and next_status is None):
                active_client_cids.add(matched_cid)

            state = indexes["company_state"].setdefault(
                matched_cid,
                {"industries": set(), "domains": set(), "facebook": "", "instagram": "", "has_location": False},
            )

            comp_patch: dict[str, Any] = {}
            if next_status:
                comp_patch[LEAD_STATUS_SLUG] = next_status
            # Nie nadpisuj aktywnego planu starym kontem Trial, jeśli firma jest już Klientem / Byłym klientem
            can_update_plan_fields = not (
                STATUS_PRECEDENCE.get(cur_status, 0) >= STATUS_PRECEDENCE["Były klient"]
                and STATUS_PRECEDENCE.get(target_status or "", 0) < STATUS_PRECEDENCE["Były klient"]
            )
            if can_update_plan_fields and raw_plan_display and meta.get("plan") != raw_plan_display:
                comp_patch[SOUNDYOU_PLAN_SLUG] = raw_plan_display
            if can_update_plan_fields and valid_until_iso and meta.get("valid_until") != valid_until_iso:
                comp_patch[SOUNDYOU_VALID_UNTIL_SLUG] = valid_until_iso
            if has_specific_industry and not state.get("industries"):
                comp_patch[INDUSTRY_ATTR_SLUG] = inferred_industries
            if corp_domain and not state.get("domains") and corp_domain not in indexes["companies_by_domain"]:
                comp_patch["domains"] = [corp_domain]

            if comp_patch:
                if not dry_run:
                    try:
                        attio.request(
                            f"/objects/companies/records/{matched_cid}",
                            {"data": {"values": comp_patch}},
                            method="PATCH",
                        )
                    except AttioAPIError as err:
                        logger.warning("PATCH company %s failed: %s", matched_cid, err)
                        stats[f"company_patch_error_{err.status}"] += 1
                        continue
                if next_status:
                    meta["lead_status"] = next_status
                    stats[f"status_to_{next_status}"] += 1
                    audit["upgraded_companies"].append({
                        "company_id": matched_cid,
                        "email": email,
                        "previous_status": cur_status,
                        "new_status": next_status,
                        "plan": raw_plan_display,
                        "valid_until": valid_until_iso,
                    })
                if SOUNDYOU_PLAN_SLUG in comp_patch:
                    meta["plan"] = comp_patch[SOUNDYOU_PLAN_SLUG]
                if SOUNDYOU_VALID_UNTIL_SLUG in comp_patch:
                    meta["valid_until"] = comp_patch[SOUNDYOU_VALID_UNTIL_SLUG]
                if INDUSTRY_ATTR_SLUG in comp_patch:
                    state["industries"].update(comp_patch[INDUSTRY_ATTR_SLUG])
                if "domains" in comp_patch:
                    state["domains"].update(comp_patch["domains"])
                    indexes["companies_by_domain"][corp_domain] = matched_cid
                stats["updated_companies"] += 1
            else:
                stats["unchanged_companies"] += 1

            company_id = matched_cid
        else:
            # Brak firmy w Attio — twórz nowe rekordy tylko dla realnych Triali / Klientów / Byłych klientów, gdy create_missing=True
            if not create_missing or target_status not in ("Trial SoundYou", "Klient", "Były klient"):
                stats["skipped_unmatched_profiles"] += 1
                # Jeśli sam Person już istnieje w Attio i ma opt_out, nadal zsynchronizuj wypisanie
                existing_p = indexes["people_by_email"].get(email)
                if opt_out and existing_p and not person_unsubs.get(existing_p["record_id"], False):
                    if not dry_run:
                        try:
                            attio.request(
                                f"/objects/people/records/{existing_p['record_id']}",
                                {"data": {"values": {"resend_unsubscribed": True}}},
                                method="PATCH",
                            )
                        except AttioAPIError as err:
                            stats[f"person_patch_error_{err.status}"] += 1
                            continue
                    person_unsubs[existing_p["record_id"]] = True
                    stats["people_marked_unsubscribed"] += 1
                continue

            comp_name = _fallback_company_name(business_name, email)
            comp_values: dict[str, Any] = {
                "name": comp_name,
                INDUSTRY_ATTR_SLUG: inferred_industries,
                LEAD_STATUS_SLUG: target_status,
                LEAD_BATCH_SLUG: lead_batch,
                "description": (
                    f"Źródło: SoundYou App (Supabase) | E-mail: {email} | "
                    f"Plan: {raw_plan_display or 'Trial'}"
                ),
                "primary_location": {
                    "country_code": _normalize_country_code(prof.get("country")),
                },
            }
            if raw_plan_display:
                comp_values[SOUNDYOU_PLAN_SLUG] = raw_plan_display
            if valid_until_iso:
                comp_values[SOUNDYOU_VALID_UNTIL_SLUG] = valid_until_iso
            if corp_domain and corp_domain not in indexes["companies_by_domain"]:
                comp_values["domains"] = [corp_domain]

            if dry_run:
                company_id = f"dry-run-sb-comp-{stats['created_companies'] + 1}"
            else:
                try:
                    created_c = attio.request(
                        "/objects/companies/records",
                        {"data": {"values": comp_values}},
                        method="POST",
                    )
                except AttioAPIError as err:
                    logger.warning("POST company %r failed: %s", comp_name, err)
                    stats[f"company_create_error_{err.status}"] += 1
                    continue
                company_id = created_c["id"]["record_id"]

            if target_status == "Klient":
                active_client_cids.add(company_id)
            company_meta[company_id] = {
                "lead_status": target_status,
                "plan": raw_plan_display,
                "valid_until": valid_until_iso,
            }
            norm_cn = normalized(comp_name)
            if norm_cn:
                indexes["companies_by_name_city"][(norm_cn, "")] = company_id
                if len(norm_cn) >= 5:
                    indexes["companies_by_unique_name"].setdefault(norm_cn, company_id)
            if corp_domain and "domains" in comp_values:
                indexes["companies_by_domain"][corp_domain] = company_id
            indexes["companies_by_email"][email] = company_id
            indexes["company_state"][company_id] = {
                "industries": set(inferred_industries),
                "domains": {corp_domain} if "domains" in comp_values else set(),
                "facebook": "",
                "instagram": "",
                "has_location": False,
            }
            stats["created_companies"] += 1
            stats[f"status_to_{target_status}"] += 1
            audit["created_companies"].append({
                "company_id": company_id,
                "name": comp_name,
                "email": email,
                "lead_status": target_status,
                "industries": inferred_industries,
            })

        # Zsynchronizuj Person (podpięcie do Company + ewentualne `resend_unsubscribed`)
        existing_person = indexes["people_by_email"].get(email)
        if existing_person:
            pid = existing_person["record_id"]
            person_patch: dict[str, Any] = {}
            if not existing_person.get("company_id"):
                person_patch["company"] = {
                    "target_object": "companies",
                    "target_record_id": company_id,
                }
            if opt_out and not person_unsubs.get(pid, False):
                person_patch["resend_unsubscribed"] = True

            if person_patch:
                if not dry_run:
                    try:
                        attio.request(
                            f"/objects/people/records/{pid}",
                            {"data": {"values": person_patch}},
                            method="PATCH",
                        )
                    except AttioAPIError as err:
                        logger.warning("PATCH person %s failed: %s", email, err)
                        stats[f"person_patch_error_{err.status}"] += 1
                        continue
                if "company" in person_patch:
                    existing_person["company_id"] = company_id
                    stats["linked_existing_people"] += 1
                if person_patch.get("resend_unsubscribed"):
                    person_unsubs[pid] = True
                    stats["people_marked_unsubscribed"] += 1
                    audit["unsubscribed_people"].append({"person_id": pid, "email": email})
        elif create_missing:
            person_values: dict[str, Any] = {
                "email_addresses": [email],
                "company": {
                    "target_object": "companies",
                    "target_record_id": company_id,
                },
                "description": f"Źródło: SoundYou App (Supabase) | Plan: {prof.get('plan') or 'Trial'}",
            }
            if opt_out:
                person_values["resend_unsubscribed"] = True

            if dry_run:
                pid = f"dry-run-sb-person-{stats['created_people'] + 1}"
            else:
                try:
                    created_p = attio.request(
                        "/objects/people/records",
                        {"data": {"values": person_values}},
                        method="POST",
                    )
                except AttioAPIError as err:
                    logger.warning("POST person %s failed: %s", email, err)
                    stats[f"person_create_error_{err.status}"] += 1
                    continue
                pid = created_p["id"]["record_id"]

            indexes["people_by_email"][email] = {
                "record_id": pid,
                "company_id": company_id,
                "has_phone": False,
            }
            person_unsubs[pid] = opt_out
            stats["created_people"] += 1
            if opt_out:
                stats["people_marked_unsubscribed"] += 1
                audit["unsubscribed_people"].append({"person_id": pid, "email": email})

    # Opcjonalna pętla po `public.demo_plays` -> awans istniejących leadów do `Rozmowa / Demo`
    if include_demo_plays and demo_plays:
        seen_demo_emails: set[str] = set()
        for dp in demo_plays:
            em = str(dp.get("email") or "").strip().lower()
            if not em or em in seen_demo_emails or em in profile_emails_seen:
                continue
            seen_demo_emails.add(em)
            corp_dom = extract_corporate_domain(em)
            cid = _match_profile_to_company(em, corp_dom, "", indexes)
            if not cid:
                continue
            cur_st = company_meta.get(cid, {}).get("lead_status", "")
            next_st = compute_status_upgrade(cur_st, "Rozmowa / Demo")
            if not next_st:
                continue
            if not dry_run:
                try:
                    attio.request(
                        f"/objects/companies/records/{cid}",
                        {"data": {"values": {LEAD_STATUS_SLUG: next_st}}},
                        method="PATCH",
                    )
                except AttioAPIError as err:
                    stats[f"company_patch_error_{err.status}"] += 1
                    continue
            company_meta.setdefault(cid, {"lead_status": "", "plan": "", "valid_until": ""})["lead_status"] = next_st
            stats["demo_plays_upgraded"] += 1
            stats[f"status_to_{next_st}"] += 1

    return {
        "stats": dict(stats),
        "audit": audit,
    }


# ─── CLI ────────────────────────────────────────────────────────────────────

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Synchronizacja statusów Trial SoundYou i Klient z bazy Supabase (READ-ONLY) do Attio CRM."
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Tylko odczyt z Supabase i Attio — zero zapisów do Attio.",
    )
    p.add_argument(
        "--no-create-missing",
        action="store_true",
        help="Aktualizuj wyłącznie firmy już istniejące w Attio (nie twórz nowych z organicznych rejestracji).",
    )
    p.add_argument(
        "--include-demo-plays",
        action="store_true",
        help="Uwzględnij również tabelę demo_plays i awansuj dopasowane firmy do etapu 'Rozmowa / Demo'.",
    )
    p.add_argument(
        "--lead-batch",
        type=str,
        default=DEFAULT_SUPABASE_BATCH,
        help=f"Wartość atrybutu Partia importu dla nowo tworzonych firm (domyślnie: {DEFAULT_SUPABASE_BATCH!r}).",
    )
    p.add_argument(
        "--supabase-url",
        type=str,
        default="",
        help="Adres projektu Supabase (domyślnie SUPABASE_URL z .env).",
    )
    p.add_argument(
        "--no-report",
        action="store_true",
        help="Nie zapisuj raportu JSON w katalogu logs/.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = parse_args(argv)

    sb_url = (args.supabase_url or os.getenv("SUPABASE_URL") or "").strip()
    sb_key = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    if not sb_url or not sb_key:
        logger.error(
            "Brak SUPABASE_URL lub SUPABASE_SERVICE_ROLE_KEY w pliku .env. "
            "Uzupełnij dane dostępowe projektu WEBPlayerPV, aby uruchomić synchronizację."
        )
        return 2

    attio_token = resolve_attio_token(allow_prompt=sys.stdin.isatty())
    if not attio_token:
        logger.error("Brak klucza ATTIO_API_KEY w środowisku / .env.")
        return 2

    sb_client = SupabaseReadClient(sb_url, sb_key)
    attio = AttioClient(attio_token)

    result = sync_supabase_to_attio(
        sb_client,
        attio,
        dry_run=args.dry_run,
        create_missing=not args.no_create_missing,
        include_demo_plays=args.include_demo_plays,
        lead_batch=args.lead_batch,
    )

    stats = result["stats"]
    mode_label = "DRY-RUN (bez zapisu do Attio)" if args.dry_run else "LIVE (zapis do Attio)"
    print("\n" + "=" * 68)
    print(f" PODSUMOWANIE SYNCHRONIZACJI SUPABASE -> ATTIO [{mode_label}]")
    print("=" * 68)
    print(f"  Pobrane profile właścicieli (Supabase) : {stats.get('supabase_profiles_fetched', 0)}")
    if args.include_demo_plays:
        print(f"  Pobrane odtworzenia demo (demo_plays)  : {stats.get('supabase_demo_plays_fetched', 0)}")
    print(f"  Dopasowane istniejące firmy w Attio    : {stats.get('matched_existing_companies', 0)}")
    print(f"  Zaktualizowane firmy w Attio           : {stats.get('updated_companies', 0)}")
    print(f"  Utworzone nowe firmy z aplikacji       : {stats.get('created_companies', 0)}")
    print(f"  Utworzone nowe kontakty (People)       : {stats.get('created_people', 0)}")
    print(f"  Status -> 'Klient' (aktywny)           : {stats.get('status_to_Klient', 0)}")
    print(f"  Status -> 'Były klient' (wygasły/anul.): {stats.get('status_to_Były klient', 0)}")
    print(f"  Status -> 'Trial SoundYou'             : {stats.get('status_to_Trial SoundYou', 0)}")
    if stats.get("status_to_Rozmowa / Demo"):
        print(f"  Awans do 'Rozmowa / Demo'              : {stats.get('status_to_Rozmowa / Demo', 0)}")
    print(f"  Oznaczone wypisania (marketing_opt_out): {stats.get('people_marked_unsubscribed', 0)}")
    print(f"  Firmy bez zmian (już aktualne)         : {stats.get('unchanged_companies', 0)}")
    print("=" * 68)

    if not args.no_report:
        report_payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mode": "dry_run" if args.dry_run else "live",
            "create_missing": not args.no_create_missing,
            "include_demo_plays": args.include_demo_plays,
            "lead_batch": args.lead_batch,
            "stats": stats,
            "audit": result["audit"],
        }
        target_report = REPORTS_DIR / f"supabase_attio_sync_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
        report_path = write_sync_report(report_payload, target_report)
        if report_path:
            print(f"  Raport JSON zapisany w                 : {report_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

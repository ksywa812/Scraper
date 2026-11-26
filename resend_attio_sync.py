"""
resend_attio_sync.py — Krok 3: Automatyczna pętla zwrotna Resend -> Attio CRM.

Funkcje:
  1. Pobiera historię wysłanych e-maili z Resend API (`GET /emails` z paginacją)
     oraz statusy wypisań z Resend Audience (`GET /audiences/{id}/contacts`).
  2. Synchronizuje pojedyncze wysyłki do obiektu `resend_promotional_sends` w Attio
     bez duplikowania istniejących rekordów (sprawdza zarówno `resend_message_key`,
     jak i krotkę `(person_id, kampania, data_wysyłki)` dla historycznych 857 rekordów).
  3. Przelicza i aktualizuje 6 pól podsumowujących `resend_*` na `People` i `Companies`
     wyłącznie tam, gdzie wartości faktycznie uległy zmianie (idempotentność = 0 zbędnych zapisów).
  4. Aktualizuje `resend_unsubscribed` na `People` (wypisania z Audience oraz `complained`).
  5. Automatycznie aktualizuje `Status leada` (`lead_status`) na `Companies`:
     - chroni wyższe etapy lejka (`Rozmowa / Demo`, `Trial SoundYou`, `Klient`) — nigdy ich nie cofa,
     - kliknięcie w mailu (`resend_any_clicked = True`) -> `Zainteresowany`,
     - wysłana kampania (`resend_sends_count >= 1` przy pustym/`Nowy`/`Gotowy do kampanii`) -> `Wysłano kampanię`,
     - wypisanie (`unsubscribed`) lub wyłącznie odbite/zablokowane maile bez otwarć -> `Odrzucony / DNC`.

Użycie:
    python resend_attio_sync.py --dry-run
    python resend_attio_sync.py
    python resend_attio_sync.py --audience-id aud_... --max-emails 1000
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
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
    LEAD_STATUS_SLUG,
    REPORTS_DIR,
    _NoRedirect,
    _extract_multiselect_titles,
    resolve_attio_token,
    write_sync_report,
)

logger = logging.getLogger("resend_attio_sync")

RESEND_API_BASE = "https://api.resend.com"
SENDS_OBJECT_SLUG = "resend_promotional_sends"

# Etapy lejka, których automatyczna pętla Resend NIGDY nie nadpisuje ani nie cofa
PROTECTED_LEAD_STATUSES = frozenset({
    "Rozmowa / Demo",
    "Trial SoundYou",
    "Klient",
})

EARLY_LEAD_STATUSES = frozenset({
    "",
    "Nowy",
    "Gotowy do kampanii",
})


# ─── Klient Resend REST API ─────────────────────────────────────────────────

class ResendAPIError(Exception):
    def __init__(self, status: int, message: str = ""):
        self.status = status
        self.message = message
        super().__init__(f"Resend HTTP {status}: {message}")


class ResendClient:
    """Lekki klient Resend REST API z obsługą paginacji kursorowej i retry na 429."""

    def __init__(self, api_key: str, pace_seconds: float = 0.25):
        self.api_key = api_key
        self.pace_seconds = pace_seconds
        self._opener = build_opener(_NoRedirect)

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        qs = f"?{urlencode({k: v for k, v in (params or {}).items() if v is not None})}" if params else ""
        url = f"{RESEND_API_BASE}{path}{qs}"
        req = Request(
            url,
            method="GET",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
            },
        )
        for attempt in range(4):
            if self.pace_seconds > 0:
                time.sleep(self.pace_seconds)
            try:
                with self._opener.open(req, timeout=25) as resp:
                    return json.load(resp)
            except HTTPError as err:
                status = err.code
                msg = ""
                try:
                    body = err.read().decode("utf-8", errors="replace")
                    msg = body[:200]
                except OSError:
                    pass
                err.close()
                if status in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise ResendAPIError(status, msg) from None
        raise ResendAPIError(429, "Rate limit exceeded")

    def list_recent_emails(self, max_emails: int = 1000) -> list[dict[str, Any]]:
        """Pobiera wysłane e-maile z `GET /emails` (paginacja `after`, do `max_emails`)."""
        items: list[dict[str, Any]] = []
        after_id: str | None = None
        while len(items) < max_emails:
            batch_limit = min(100, max_emails - len(items))
            params: dict[str, Any] = {"limit": batch_limit}
            if after_id:
                params["after"] = after_id
            payload = self.get("/emails", params=params)
            page = payload.get("data", []) if isinstance(payload, dict) else []
            if not page:
                break
            items.extend(page)
            has_more = bool(payload.get("has_more", payload.get("hasMore", False)))
            after_id = page[-1].get("id")
            if not has_more or not after_id:
                break
        return items

    def list_audience_contacts(self, audience_id: str) -> list[dict[str, Any]]:
        """Pobiera wszystkie kontakty z danej Resend Audience (wraz z flagą `unsubscribed`)."""
        if not audience_id:
            return []
        contacts: list[dict[str, Any]] = []
        after_cursor: str | None = None
        while True:
            params: dict[str, Any] = {"limit": 100}
            if after_cursor:
                params["after"] = after_cursor
            payload = self.get(f"/audiences/{audience_id}/contacts", params=params)
            page = payload.get("data", []) if isinstance(payload, dict) else []
            if not page:
                break
            contacts.extend(page)
            has_more = bool(payload.get("has_more", payload.get("hasMore", False)))
            after_cursor = payload.get("cursor") or payload.get("next_cursor") or page[-1].get("id")
            if not has_more or not after_cursor:
                break
        return contacts


# ─── Mapowanie zdarzeń Resend i agregacja ───────────────────────────────────

def normalize_iso_second(ts_raw: str | None) -> str:
    """Normalizuje znacznik czasu ISO 8601 do formatu UTC `YYYY-MM-DDTHH:MM:SSZ`."""
    if not ts_raw:
        return ""
    s = ts_raw.strip()
    if not s:
        return ""
    try:
        # Obsługa '2026-09-28T11:40:47.123456+00:00' lub '...Z' lub spacji zamiast 'T'
        cleaned = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return ""


def make_message_key(provider_email_id: str, recipient_email: str) -> str:
    raw = f"resend:{provider_email_id.strip()}:{recipient_email.strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def map_resend_event(last_event: str | None) -> dict[str, Any]:
    """Mapuje `last_event` z Resend na pola `resend_promotional_sends` w Attio."""
    ev = (last_event or "sent").strip().lower()
    if ev == "clicked":
        return {"delivery_status": "delivered", "opened": True, "clicked": True, "complained": False}
    if ev == "opened":
        return {"delivery_status": "delivered", "opened": True, "clicked": False, "complained": False}
    if ev == "bounced":
        return {"delivery_status": "bounced", "opened": False, "clicked": False, "complained": False}
    if ev in ("suppressed", "canceled", "cancelled"):
        return {"delivery_status": "suppressed", "opened": False, "clicked": False, "complained": False}
    if ev == "complained":
        return {"delivery_status": "delivered", "opened": True, "clicked": False, "complained": True}
    # sent, delivered, delivery_delayed, queued
    return {"delivery_status": "delivered", "opened": False, "clicked": False, "complained": False}


def aggregate_sends(send_list: list[dict[str, Any]]) -> dict[str, Any]:
    """Wylicza 6 pól podsumowujących `resend_*` z listy wysyłek dla osoby lub firmy."""
    def _sort_key(item: dict[str, Any]):
        status_rank = {"delivered": 3, "suppressed": 2, "bounced": 1}.get(item.get("delivery_status", ""), 0)
        return (
            item.get("sent_at") or "",
            int(bool(item.get("clicked"))),
            int(bool(item.get("opened"))),
            status_rank,
        )

    latest = max(send_list, key=_sort_key)
    out: dict[str, Any] = {
        "resend_sends_count": len(send_list),
        "resend_any_opened": any(bool(s.get("opened")) for s in send_list),
        "resend_any_clicked": any(bool(s.get("clicked")) for s in send_list),
    }
    if latest.get("sent_at"):
        out["resend_last_sent_at"] = latest["sent_at"]
    if latest.get("campaign"):
        out["resend_last_campaign"] = latest["campaign"]
    if latest.get("delivery_status") in ("delivered", "bounced", "suppressed"):
        out["resend_last_delivery_status"] = latest["delivery_status"]
    return out


def compute_next_lead_status(
    current_status: str,
    agg: dict[str, Any] | None,
    *,
    all_contacts_unsubscribed: bool = False,
) -> str | None:
    """Wyznacza nowy `lead_status` firmy na podstawie zagregowanych zdarzeń Resend (lub `None` = bez zmian)."""
    cur = (current_status or "").strip()
    if cur in PROTECTED_LEAD_STATUSES:
        return None
    if cur == "Odrzucony / DNC":
        return None

    if all_contacts_unsubscribed:
        return "Odrzucony / DNC"

    if not agg or not agg.get("resend_sends_count"):
        return None

    if agg.get("resend_any_clicked"):
        return "Zainteresowany" if cur != "Zainteresowany" else None

    if (
        agg.get("resend_last_delivery_status") in ("bounced", "suppressed")
        and not agg.get("resend_any_opened")
        and not agg.get("resend_any_clicked")
    ):
        return "Odrzucony / DNC"

    if cur in EARLY_LEAD_STATUSES:
        return "Wysłano kampanię"

    return None


# ─── Ekstrakcja obecnego stanu z Attio ──────────────────────────────────────

def _extract_single_text(raw_vals: list[dict[str, Any]]) -> str:
    for item in raw_vals or []:
        val = item.get("value")
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _extract_single_bool(raw_vals: list[dict[str, Any]]) -> bool:
    for item in raw_vals or []:
        if "value" in item:
            return bool(item.get("value"))
    return False


def _extract_single_number(raw_vals: list[dict[str, Any]]) -> int | None:
    for item in raw_vals or []:
        val = item.get("value")
        if isinstance(val, (int, float)):
            return int(val)
    return None


def _extract_status_title(raw_vals: list[dict[str, Any]]) -> str:
    for item in raw_vals or []:
        st = item.get("status") or item.get("option")
        if isinstance(st, dict) and st.get("title"):
            return str(st["title"]).strip()
        if isinstance(item.get("value"), str):
            return item["value"].strip()
    return ""


def load_attio_resend_state(attio: AttioClient) -> dict[str, Any]:
    """Pobiera obecny stan `people`, `companies` i `resend_promotional_sends` z Attio."""
    people_by_id: dict[str, dict[str, Any]] = {}
    email_to_person_id: dict[str, str] = {}
    company_people: dict[str, list[str]] = defaultdict(list)

    for rec in attio.query_all_records("people"):
        pid = rec["id"]["record_id"]
        vals = rec.get("values") or {}
        comp_refs = vals.get("company") or []
        cid = comp_refs[0].get("target_record_id") if comp_refs else None
        emails = [
            (it.get("email_address") or "").strip().lower()
            for it in vals.get("email_addresses", [])
            if (it.get("email_address") or "").strip()
        ]
        for em in emails:
            email_to_person_id[em] = pid
        if cid:
            company_people[cid].append(pid)

        deliv_opts = _extract_multiselect_titles(vals.get("resend_last_delivery_status", []))
        people_by_id[pid] = {
            "company_id": cid,
            "emails": emails,
            "unsubscribed": _extract_single_bool(vals.get("resend_unsubscribed", [])),
            "summary": {
                "resend_sends_count": _extract_single_number(vals.get("resend_sends_count", [])),
                "resend_any_opened": _extract_single_bool(vals.get("resend_any_opened", [])),
                "resend_any_clicked": _extract_single_bool(vals.get("resend_any_clicked", [])),
                "resend_last_sent_at": normalize_iso_second(_extract_single_text(vals.get("resend_last_sent_at", []))),
                "resend_last_campaign": _extract_single_text(vals.get("resend_last_campaign", [])),
                "resend_last_delivery_status": next(iter(deliv_opts), ""),
            },
        }

    companies_by_id: dict[str, dict[str, Any]] = {}
    for rec in attio.query_all_records("companies"):
        cid = rec["id"]["record_id"]
        vals = rec.get("values") or {}
        deliv_opts = _extract_multiselect_titles(vals.get("resend_last_delivery_status", []))
        companies_by_id[cid] = {
            "name": _extract_single_text(vals.get("name", [])),
            "lead_status": _extract_status_title(vals.get(LEAD_STATUS_SLUG, [])),
            "summary": {
                "resend_sends_count": _extract_single_number(vals.get("resend_sends_count", [])),
                "resend_any_opened": _extract_single_bool(vals.get("resend_any_opened", [])),
                "resend_any_clicked": _extract_single_bool(vals.get("resend_any_clicked", [])),
                "resend_last_sent_at": normalize_iso_second(_extract_single_text(vals.get("resend_last_sent_at", []))),
                "resend_last_campaign": _extract_single_text(vals.get("resend_last_campaign", [])),
                "resend_last_delivery_status": next(iter(deliv_opts), ""),
            },
        }

    sends_by_key: dict[str, dict[str, Any]] = {}
    sends_by_natural: dict[tuple[str, str, str], str] = {}
    all_sends_by_id: dict[str, dict[str, Any]] = {}

    for rec in attio.query_all_records(SENDS_OBJECT_SLUG):
        sid = rec["id"]["record_id"]
        vals = rec.get("values") or {}
        msg_key = _extract_single_text(vals.get("resend_message_key", []))
        campaign = _extract_single_text(vals.get("resend_campaign", []))
        sent_at = normalize_iso_second(_extract_single_text(vals.get("resend_sent_at", [])))
        delivery_status = _extract_single_text(vals.get("resend_delivery_status", []))
        opened = _extract_single_bool(vals.get("resend_opened", []))
        clicked = _extract_single_bool(vals.get("resend_clicked", []))
        person_refs = vals.get("resend_person") or []
        pid = person_refs[0].get("target_record_id") if person_refs else None

        entry = {
            "record_id": sid,
            "message_key": msg_key,
            "person_id": pid,
            "campaign": campaign,
            "sent_at": sent_at,
            "delivery_status": delivery_status,
            "opened": opened,
            "clicked": clicked,
        }
        all_sends_by_id[sid] = entry
        if msg_key:
            sends_by_key[msg_key] = entry
        if pid and campaign and sent_at:
            sends_by_natural[(pid, campaign, sent_at)] = sid

    return {
        "people_by_id": people_by_id,
        "email_to_person_id": email_to_person_id,
        "companies_by_id": companies_by_id,
        "company_people": company_people,
        "all_sends_by_id": all_sends_by_id,
        "sends_by_key": sends_by_key,
        "sends_by_natural": sends_by_natural,
    }


def _summary_differs(current: dict[str, Any], target: dict[str, Any]) -> bool:
    for k, v in target.items():
        if current.get(k) != v:
            return True
    return False


# ─── Główna synchronizacja Resend -> Attio ──────────────────────────────────

def sync_resend_feedback_to_attio(
    attio: AttioClient,
    resend: ResendClient,
    *,
    audience_id: str = "",
    max_emails: int = 1000,
    dry_run: bool = False,
) -> dict[str, Any]:
    stats: Counter[str] = Counter()
    state = load_attio_resend_state(attio)

    people_by_id = state["people_by_id"]
    email_to_pid = state["email_to_person_id"]
    companies_by_id = state["companies_by_id"]
    company_people = state["company_people"]
    all_sends = state["all_sends_by_id"]
    sends_by_key = state["sends_by_key"]
    sends_by_natural = state["sends_by_natural"]

    unsubscribed_emails: set[str] = set()
    if audience_id:
        aud_contacts = resend.list_audience_contacts(audience_id)
        stats["resend_audience_contacts_fetched"] = len(aud_contacts)
        for c in aud_contacts:
            em = (c.get("email") or "").strip().lower()
            if em and bool(c.get("unsubscribed")):
                unsubscribed_emails.add(em)

    recent_emails = resend.list_recent_emails(max_emails=max_emails)
    stats["resend_emails_fetched"] = len(recent_emails)

    for msg in recent_emails:
        provider_id = (msg.get("id") or "").strip()
        if not provider_id:
            continue
        recipients = msg.get("to") or []
        if isinstance(recipients, str):
            recipients = [recipients]
        campaign = (msg.get("subject") or "Kampania Resend").strip()
        sent_at = normalize_iso_second(msg.get("created_at"))
        mapped = map_resend_event(msg.get("last_event"))

        for raw_rcpt in recipients:
            rcpt = (raw_rcpt or "").strip().lower()
            if not rcpt:
                continue
            if mapped["complained"]:
                unsubscribed_emails.add(rcpt)

            pid = email_to_pid.get(rcpt)
            if not pid:
                stats["skipped_unknown_recipient_emails"] += 1
                continue

            msg_key = make_message_key(provider_id, rcpt)
            existing_send = sends_by_key.get(msg_key)
            if existing_send is None and sent_at:
                nat_sid = sends_by_natural.get((pid, campaign, sent_at))
                if nat_sid:
                    existing_send = all_sends[nat_sid]

            if existing_send is not None:
                # Sprawdź, czy pojawiło się nowe otwarcie, kliknięcie lub zmiana statusu dostarczenia
                new_opened = existing_send["opened"] or mapped["opened"]
                new_clicked = existing_send["clicked"] or mapped["clicked"]
                new_deliv = mapped["delivery_status"]
                patch_send: dict[str, Any] = {}
                if new_opened != existing_send["opened"]:
                    patch_send["resend_opened"] = new_opened
                if new_clicked != existing_send["clicked"]:
                    patch_send["resend_clicked"] = new_clicked
                if new_deliv and new_deliv != existing_send["delivery_status"]:
                    # Nie cofaj delivered <- sent, ale zaktualizuj na bounced/suppressed lub z pustego
                    if new_deliv in ("bounced", "suppressed") or not existing_send["delivery_status"]:
                        patch_send["resend_delivery_status"] = new_deliv

                if patch_send:
                    if not dry_run:
                        attio.request(
                            f"/objects/{SENDS_OBJECT_SLUG}/records/{existing_send['record_id']}",
                            {"data": {"values": patch_send}},
                            method="PATCH",
                        )
                    existing_send["opened"] = new_opened
                    existing_send["clicked"] = new_clicked
                    if "resend_delivery_status" in patch_send:
                        existing_send["delivery_status"] = patch_send["resend_delivery_status"]
                    stats["updated_send_records"] += 1
                else:
                    stats["unchanged_send_records"] += 1
            else:
                # Nowy rekord w `resend_promotional_sends`
                send_values: dict[str, Any] = {
                    "resend_message_key": msg_key,
                    "resend_campaign": campaign,
                    "resend_delivery_status": mapped["delivery_status"],
                    "resend_opened": mapped["opened"],
                    "resend_clicked": mapped["clicked"],
                    "resend_person": {
                        "target_object": "people",
                        "target_record_id": pid,
                    },
                }
                if sent_at:
                    send_values["resend_sent_at"] = sent_at

                if dry_run:
                    new_sid = f"dry-send-{stats['created_send_records'] + 1}"
                else:
                    created = attio.request(
                        f"/objects/{SENDS_OBJECT_SLUG}/records",
                        {"data": {"values": send_values}},
                        method="POST",
                    )
                    new_sid = created["id"]["record_id"]

                new_entry = {
                    "record_id": new_sid,
                    "message_key": msg_key,
                    "person_id": pid,
                    "campaign": campaign,
                    "sent_at": sent_at,
                    "delivery_status": mapped["delivery_status"],
                    "opened": mapped["opened"],
                    "clicked": mapped["clicked"],
                }
                all_sends[new_sid] = new_entry
                sends_by_key[msg_key] = new_entry
                if sent_at:
                    sends_by_natural[(pid, campaign, sent_at)] = new_sid
                stats["created_send_records"] += 1

    # 2. Przelicz podsumowania per Person + zaktualizuj wypisania (tylko zmienione rekordy)
    sends_by_person: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for s in all_sends.values():
        if s.get("person_id"):
            sends_by_person[s["person_id"]].append(s)

    for pid, pstate in people_by_id.items():
        person_patch: dict[str, Any] = {}
        should_unsub = pstate["unsubscribed"] or any(em in unsubscribed_emails for em in pstate["emails"])
        if should_unsub and not pstate["unsubscribed"]:
            person_patch["resend_unsubscribed"] = True

        psends = sends_by_person.get(pid)
        if psends:
            target_summary = aggregate_sends(psends)
            if _summary_differs(pstate["summary"], target_summary):
                person_patch.update(target_summary)

        if person_patch:
            if not dry_run:
                attio.request(
                    f"/objects/people/records/{pid}",
                    {"data": {"values": person_patch}},
                    method="PATCH",
                )
            if "resend_unsubscribed" in person_patch:
                pstate["unsubscribed"] = True
                stats["marked_unsubscribed_people"] += 1
            stats["updated_people_summaries"] += 1

    # 3. Przelicz podsumowania i `lead_status` per Company (tylko zmienione rekordy)
    status_transitions: list[dict[str, str]] = []
    for cid, cstate in companies_by_id.items():
        linked_pids = company_people.get(cid, [])
        csends: list[dict[str, Any]] = []
        for pid in linked_pids:
            csends.extend(sends_by_person.get(pid, []))

        all_unsub = bool(linked_pids) and all(people_by_id[pid]["unsubscribed"] for pid in linked_pids)
        target_c_summary = aggregate_sends(csends) if csends else None

        comp_patch: dict[str, Any] = {}
        if target_c_summary and _summary_differs(cstate["summary"], target_c_summary):
            comp_patch.update(target_c_summary)

        next_status = compute_next_lead_status(
            cstate["lead_status"],
            target_c_summary,
            all_contacts_unsubscribed=all_unsub,
        )
        if next_status:
            comp_patch[LEAD_STATUS_SLUG] = next_status

        if comp_patch:
            if not dry_run:
                attio.request(
                    f"/objects/companies/records/{cid}",
                    {"data": {"values": comp_patch}},
                    method="PATCH",
                )
            if next_status:
                status_transitions.append({
                    "company_id": cid,
                    "name": cstate["name"],
                    "from": cstate["lead_status"] or "(brak)",
                    "to": next_status,
                })
                stats[f"status_to_{next_status}"] += 1
            stats["updated_companies"] += 1

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": "dry_run" if dry_run else "apply",
        "stats": dict(stats),
        "status_transitions": status_transitions,
    }


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(
        description="Krok 3: Automatyczna pętla zwrotna Resend -> Attio CRM (wysyłki, podsumowania i Status leada)."
    )
    parser.add_argument("--dry-run", action="store_true", help="Podgląd zmian bez zapisu w Attio")
    parser.add_argument("--audience-id", default=os.getenv("RESEND_AUDIENCE_ID", ""), help="ID Resend Audience do synchronizacji wypisań")
    parser.add_argument("--max-emails", type=int, default=1000, help="Maksymalna liczba ostatnich e-maili z Resend API (domyślnie: 1000)")
    parser.add_argument("--no-report", action="store_true", help="Nie zapisuj raportu JSON w katalogu logs/")
    args = parser.parse_args()

    attio_token = resolve_attio_token(allow_prompt=True)
    resend_key = os.getenv("RESEND_API_KEY", "").strip()
    if not attio_token:
        print("[ERROR] Brak ATTIO_API_KEY w .env.", file=sys.stderr)
        return 2
    if not resend_key or resend_key == "re_YOUR_API_KEY_HERE":
        print("[ERROR] Brak RESEND_API_KEY w .env.", file=sys.stderr)
        return 2

    try:
        attio = AttioClient(attio_token)
        resend = ResendClient(resend_key)
        report = sync_resend_feedback_to_attio(
            attio,
            resend,
            audience_id=args.audience_id,
            max_emails=args.max_emails,
            dry_run=args.dry_run,
        )
        if not args.no_report:
            saved = write_sync_report(
                report,
                REPORTS_DIR / f"resend_attio_sync_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
            )
            if saved:
                report["report_file"] = str(saved)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (AttioAPIError, ResendAPIError, URLError, OSError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

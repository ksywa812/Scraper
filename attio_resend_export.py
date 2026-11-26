"""
attio_resend_export.py — Krok 6a: Eksport odbiorców kampanii z Attio CRM -> Resend.

Uczynia Attio jedynym źródłem prawdy dla wysyłek mailingowych (zamiast surowego CSV):
  1. Pobiera `Companies` i powiązane `People` z Attio.
  2. Filtruje odbiorców według:
     - branży `soundyou_industry` (obsługuje nazwy kanoniczne oraz klucze PL/EN, np. `kawiarnia`, `cafe`),
     - dozwolonego etapu `Status leada` (`lead_status`),
     - twardego wykluczenia `Trial SoundYou`, `Klient`, `Rozmowa / Demo`, `Odrzucony / DNC`,
     - tarczy DNC (`resend_unsubscribed = True`, `bounced`, `suppressed`),
     - okresu karencji (`--cooldown-days`, domyślnie 30 dni od `resend_last_sent_at`),
     - limitu skrzynek na jedną firmę (`--max-per-company`, domyślnie 2),
     - walidacji składni e-maila i filtra anty-ICP.
  3. Zapisuje gotowy plik CSV dla kampanii i/lub zwraca listę kontaktów wprost do `resend_sender.py --from-attio`.

Użycie:
    python attio_resend_export.py --industry "Kawiarnie" --dry-run
    python attio_resend_export.py --industry "SPA & Wellness, Hotele i Pensjonaty" --cooldown-days 30
    python resend_sender.py --from-attio --industry "Kawiarnie" --dry-run
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

from attio_sync import (
    AttioAPIError,
    AttioClient,
    INDUSTRY_ATTR_SLUG,
    INDUSTRY_EN_PL_MAP,
    LEAD_STATUS_SLUG,
    REPORTS_DIR,
    SOUNDYOU_INDUSTRIES,
    _extract_multiselect_titles,
    is_non_icp_business,
    normalized,
    resolve_attio_token,
    validate_email_syntax,
    write_sync_report,
)
from resend_attio_sync import (
    _extract_single_bool,
    _extract_single_text,
    _extract_status_title,
    normalize_iso_second,
)

logger = logging.getLogger("attio_resend_export")

CAMPAIGNS_DIR = Path(__file__).parent / "data" / "Raport" / "campaigns"

# Etapy lejka bezwzględnie wykluczone z zimnych kampanii mailingowych
EXCLUDED_CAMPAIGN_STATUSES = frozenset({
    "Rozmowa / Demo",
    "Trial SoundYou",
    "Klient",
    "Były klient",
    "Odrzucony / DNC",
})


def resolve_target_industries(raw_filters: list[str] | None) -> set[str]:
    """Tłumaczy listę filtrów branżowych (PL/EN/kanonicznych) na zbiór opcji `soundyou_industry`."""
    if not raw_filters:
        return set(SOUNDYOU_INDUSTRIES) - {"Lokal Usługowy / Inne"}

    resolved: set[str] = set()
    for raw in raw_filters:
        for tok in re.split(r"[,;|]+", raw or ""):
            clean = tok.strip()
            if not clean:
                continue
            low = clean.lower()
            if clean in SOUNDYOU_INDUSTRIES:
                resolved.add(clean)
            elif low in INDUSTRY_EN_PL_MAP:
                resolved.update(INDUSTRY_EN_PL_MAP[low])
            else:
                norm_tok = normalized(clean)
                if norm_tok in INDUSTRY_EN_PL_MAP:
                    resolved.update(INDUSTRY_EN_PL_MAP[norm_tok])
    return resolved


def _parse_utc(iso_str: str) -> datetime | None:
    norm = normalize_iso_second(iso_str)
    if not norm:
        return None
    try:
        return datetime.fromisoformat(norm.replace("Z", "+00:00"))
    except ValueError:
        return None


def _email_priority_key(email: str) -> tuple[int, str]:
    """Preferuj skrzynki recepcji/kontakt/biuro/właściciela przed księgowością/fakturami."""
    local = email.split("@", 1)[0].lower()
    if any(k in local for k in ("faktur", "ksiegow", "reklamac", "rodo", "iodo", "tech", "kuchni", "muzeum")):
        return (2, email)
    if any(k in local for k in ("spa", "recepcj", "kontakt", "biuro", "info", "manager", "rezerwac")):
        return (0, email)
    return (1, email)


def select_campaign_recipients_from_attio(
    attio: AttioClient,
    *,
    industries: list[str] | None = None,
    allowed_statuses: set[str] | None = None,
    cooldown_days: int = 30,
    max_per_company: int = 2,
    now_utc: datetime | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Wybiera kwalifikujących się odbiorców kampanii z Attio wraz z pełnymi statystykami odrzuceń."""
    stats: Counter[str] = Counter()
    now = now_utc or datetime.now(timezone.utc)
    cooldown_cutoff = now - timedelta(days=max(0, cooldown_days))
    target_inds = resolve_target_industries(industries)

    companies: dict[str, dict[str, Any]] = {}
    for rec in attio.query_all_records("companies"):
        stats["total_attio_companies"] += 1
        cid = rec["id"]["record_id"]
        vals = rec.get("values") or {}
        name = _extract_single_text(vals.get("name", []))
        doms = [it.get("domain", "") for it in vals.get("domains", []) if it.get("domain")]
        domain = doms[0] if doms else ""
        comp_inds = _extract_multiselect_titles(vals.get(INDUSTRY_ATTR_SLUG, []))
        lead_status = _extract_status_title(vals.get(LEAD_STATUS_SLUG, []))
        comp_deliv = next(iter(_extract_multiselect_titles(vals.get("resend_last_delivery_status", []))), "")
        comp_last_sent = _parse_utc(_extract_single_text(vals.get("resend_last_sent_at", [])))

        locs = vals.get("primary_location", [])
        city = ""
        region = ""
        for loc in locs:
            city = city or (loc.get("locality") or "").strip()
            region = region or (loc.get("region") or "").strip()

        companies[cid] = {
            "name": name,
            "domain": domain,
            "industries": comp_inds,
            "lead_status": lead_status,
            "last_delivery_status": comp_deliv,
            "last_sent_at": comp_last_sent,
            "city": city,
            "region": region,
        }

    # Zgrupuj kwalifikujące się kontakty per firma
    candidates_by_company: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen_emails: set[str] = set()

    for rec in attio.query_all_records("people"):
        stats["total_attio_people"] += 1
        vals = rec.get("values") or {}
        comp_refs = vals.get("company") or []
        cid = comp_refs[0].get("target_record_id") if comp_refs else None
        if not cid or cid not in companies:
            stats["skipped_no_company"] += 1
            continue

        comp = companies[cid]

        # 1. Wyklucz etapy chronione / DNC / Trial / Klient
        if comp["lead_status"] in EXCLUDED_CAMPAIGN_STATUSES:
            stats[f"skipped_status_{comp['lead_status']}"] += 1
            continue
        if allowed_statuses is not None and comp["lead_status"] not in allowed_statuses:
            stats["skipped_not_in_allowed_statuses"] += 1
            continue

        # 2. Filtr branżowy SoundYou
        if not (comp["industries"] & target_inds):
            stats["skipped_industry_mismatch"] += 1
            continue

        # 3. Filtr anty-ICP
        if is_non_icp_business(name=comp["name"], website=comp["domain"])[0]:
            stats["skipped_non_icp"] += 1
            continue

        # 4. Tarcza DNC na poziomie kontaktu
        if _extract_single_bool(vals.get("resend_unsubscribed", [])):
            stats["skipped_unsubscribed"] += 1
            continue
        pers_deliv = next(iter(_extract_multiselect_titles(vals.get("resend_last_delivery_status", []))), "")
        if pers_deliv in ("bounced", "suppressed"):
            stats["skipped_bounced_or_suppressed"] += 1
            continue

        # 5. Cooldown czasowy (zarówno kontakt, jak i cała firma)
        pers_last_sent = _parse_utc(_extract_single_text(vals.get("resend_last_sent_at", [])))
        latest_sent = max(
            [dt for dt in (pers_last_sent, comp["last_sent_at"]) if dt is not None],
            default=None,
        )
        if cooldown_days > 0 and latest_sent is not None and latest_sent > cooldown_cutoff:
            stats["skipped_cooldown"] += 1
            continue

        # 6. Walidacja adresu e-mail
        emails = [
            (it.get("email_address") or "").strip().lower()
            for it in vals.get("email_addresses", [])
            if (it.get("email_address") or "").strip()
        ]
        for em in emails:
            if em in seen_emails:
                continue
            ok_syn, _ = validate_email_syntax(em)
            if not ok_syn:
                stats["skipped_invalid_email"] += 1
                continue
            seen_emails.add(em)
            matched_inds = sorted(comp["industries"] & target_inds)
            candidates_by_company[cid].append({
                "email": em,
                "first_name": comp["name"],
                "last_name": "",
                "unsubscribed": False,
                "city": comp["city"],
                "voivodeship": comp["region"],
                "industry": ", ".join(matched_inds),
                "company_id": cid,
                "domain": comp["domain"],
            })

    # 7. Zastosuj limit kontaktów na jedną firmę (`max_per_company`)
    selected: list[dict[str, Any]] = []
    for cid, clist in candidates_by_company.items():
        clist.sort(key=lambda item: _email_priority_key(item["email"]))
        if max_per_company > 0 and len(clist) > max_per_company:
            stats["skipped_max_per_company_cap"] += len(clist) - max_per_company
            clist = clist[:max_per_company]
        selected.extend(clist)

    stats["selected_companies"] = len(candidates_by_company)
    stats["selected_contacts"] = len(selected)
    meta = {
        "target_industries": sorted(target_inds),
        "cooldown_days": cooldown_days,
        "max_per_company": max_per_company,
        "stats": dict(stats),
    }
    return selected, meta


def write_campaign_csv(contacts: list[dict[str, Any]], out_path: Path) -> Path:
    """Zapisuje wyeksportowanych odbiorców z Attio do pliku CSV zgodnego z `resend_sender.py`."""
    headers = [
        "Branża", "Województwo", "Miasto", "Name", "Website", "Email 1", "Sources",
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        for c in contacts:
            writer.writerow({
                "Branża": c.get("industry", ""),
                "Województwo": c.get("voivodeship", ""),
                "Miasto": c.get("city", ""),
                "Name": c.get("first_name", ""),
                "Website": c.get("domain", ""),
                "Email 1": c.get("email", ""),
                "Sources": "attio_crm",
            })
    return out_path


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(
        description="Krok 6a: Eksport kwalifikujących się odbiorców kampanii z Attio CRM do CSV / Resend."
    )
    parser.add_argument(
        "--industry",
        action="append",
        default=[],
        help="Branża SoundYou (można podać wielokrotnie lub po przecinku, PL/EN, np. 'Kawiarnie, Restauracje')",
    )
    parser.add_argument(
        "--statuses",
        default="",
        help="Opcjonalna lista dozwolonych statusów leada po przecinku (np. 'Nowy, Gotowy do kampanii')",
    )
    parser.add_argument(
        "--cooldown-days",
        type=int,
        default=30,
        help="Minimalna liczba dni od ostatniej wysyłki Resend (domyślnie: 30; 0 wyłącza cooldown)",
    )
    parser.add_argument(
        "--max-per-company",
        type=int,
        default=2,
        help="Maksymalna liczba skrzynek e-mail z jednej firmy (domyślnie: 2)",
    )
    parser.add_argument(
        "--out-csv",
        type=Path,
        default=None,
        help="Ścieżka wyjściowego pliku CSV kampanii",
    )
    parser.add_argument("--dry-run", action="store_true", help="Tylko podgląd statystyk bez zapisu pliku CSV")
    parser.add_argument("--no-report", action="store_true", help="Nie zapisuj raportu JSON w logs/")
    args = parser.parse_args()

    token = resolve_attio_token(allow_prompt=True)
    if not token:
        print("[ERROR] Brak ATTIO_API_KEY w .env.", file=sys.stderr)
        return 2

    allowed_st: set[str] | None = None
    if args.statuses.strip():
        allowed_st = {s.strip() for s in args.statuses.split(",") if s.strip()}

    try:
        attio = AttioClient(token)
        contacts, meta = select_campaign_recipients_from_attio(
            attio,
            industries=args.industry or None,
            allowed_statuses=allowed_st,
            cooldown_days=args.cooldown_days,
            max_per_company=args.max_per_company,
        )
        slug = normalized("_".join(args.industry) if args.industry else "all_icp").replace(" ", "_")[:32]
        csv_target = args.out_csv or (
            CAMPAIGNS_DIR / f"attio_campaign_{slug}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        )
        report: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mode": "dry_run" if args.dry_run else "export_csv",
            **meta,
        }
        if not args.dry_run and contacts:
            written = write_campaign_csv(contacts, csv_target)
            report["exported_csv"] = str(written)
        if not args.no_report:
            saved = write_sync_report(
                report,
                REPORTS_DIR / f"attio_resend_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
            )
            if saved:
                report["report_file"] = str(saved)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (AttioAPIError, OSError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

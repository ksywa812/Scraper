"""
resend_sender.py — Import kontaktów z CSV do Resend Audiences lub wysyłka kampanii

Użycie:
    python resend_sender.py                                          # auto-wybiera najnowszy CSV
    python resend_sender.py --csv data/Raport/x.csv                  # konkretny plik
    python resend_sender.py --dry-run                                # tylko podgląd, bez wysyłki
    python resend_sender.py --mode campaign --from noreply@domena.pl --subject "Temat" --template data/templates/outreach_ideamusic.html

Wymagane zmienne w .env:
    RESEND_API_KEY=re_...
    RESEND_AUDIENCE_ID=aud_...   (tylko tryb audiences)
"""

import argparse
import json
import os
import re
import time
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

# ── Konfiguracja ──────────────────────────────────────────────────────────────

load_dotenv(Path(__file__).parent / ".env")

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_AUDIENCE_ID = os.getenv("RESEND_AUDIENCE_ID", "")
API_BASE = "https://api.resend.com"

USERCHECK_API_KEY = os.getenv("USERCHECK_API_KEY", "")
USERCHECK_API_BASE = "https://api.usercheck.com"

EMAIL_COLUMNS = ["Email 1", "Email 2", "Email 3"]
OTHER_EMAILS_COLUMN = "Other Emails"
NAME_COLUMN = "Name"
DATA_DIR = Path(__file__).parent / "data" / "Raport"
SENT_LOG_PATH = DATA_DIR / "sent_log.json"
VERIFY_CACHE_PATH = DATA_DIR / "email_verify_cache.json"

# Ścisły regex — wymaga TLD ≥ 2 liter (np. odrzuca .coml)
EMAIL_REGEX = re.compile(
    r'^[A-Za-z0-9._%+-]+'
    r'@[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9]'
    r'\.[A-Za-z]{2,}$'
)

_IMAGE_EXTENSIONS = ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.ico')

# E-maile platform (Booksy, Fresha) — nie są kontaktami biznesowymi
_PLATFORM_EMAILS = frozenset({
    "aneksy@booksy.com", "pomoc.pl@booksy.com", "sprzedaz@booksy.com",
    "info.pl@booksy.com", "reklamacje@booksy.com", "info@booksy.com",
    "support@booksy.com", "hello@booksy.com",
    "support@fresha.com", "no-reply@fresha.com", "hello@fresha.com",
})

# ── Sent Log ──────────────────────────────────────────────────────────────────


def load_sent_log() -> dict:
    """
    Wczytuje lokalny log wysłanych kampanii.
    Format: { "email@example.com": {"first_sent": "ISO", "last_sent": "ISO", "count": N, "subjects": [...]} }
    """
    if not SENT_LOG_PATH.exists():
        return {}
    try:
        return json.loads(SENT_LOG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_sent_log(log: dict) -> None:
    SENT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    SENT_LOG_PATH.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")


def mark_emails_sent(emails: list[str], subject: str, log: dict) -> None:
    """Dodaje emaile do logu po udanej wysyłce kampanii."""
    now = datetime.now(timezone.utc).isoformat()
    for email in emails:
        if email not in log:
            log[email] = {"first_sent": now, "last_sent": now, "count": 1, "subjects": [subject]}
        else:
            log[email]["last_sent"] = now
            log[email]["count"] += 1
            if subject not in log[email]["subjects"]:
                log[email]["subjects"].append(subject)

# ── Pomocnicze ────────────────────────────────────────────────────────────────


def validate_config() -> None:
    if not RESEND_API_KEY or RESEND_API_KEY == "re_YOUR_API_KEY_HERE":
        sys.exit("[ERROR] Brak RESEND_API_KEY w .env — uzupełnij klucz API z panelu Resend.")
    if not RESEND_AUDIENCE_ID or RESEND_AUDIENCE_ID == "aud_YOUR_AUDIENCE_ID_HERE":
        sys.exit("[ERROR] Brak RESEND_AUDIENCE_ID w .env — skopiuj ID Audience z URL panelu Resend.")


def find_latest_csv() -> Path:
    csvs = sorted(DATA_DIR.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not csvs:
        sys.exit(f"[ERROR] Brak plików CSV w {DATA_DIR}")
    return csvs[0]


def is_valid_email(email: str) -> bool:
    """Sprawdza format e-mail — odrzuca pliki graficzne i niepoprawne TLD (np. .coml)."""
    if not email or not isinstance(email, str):
        return False
    e = email.strip().lower()
    if e.endswith(_IMAGE_EXTENSIONS):
        return False
    return bool(EMAIL_REGEX.match(e))


def is_sendable_email(email: str) -> bool:
    """Zwraca True jeśli email jest poprawny I nie jest e-mailem platformy (Booksy/Fresha)."""
    return is_valid_email(email) and email.strip().lower() not in _PLATFORM_EMAILS


def load_contacts(csv_path: Path) -> list[dict]:
    """
    Czyta CSV i zwraca listę słowników {email, first_name, city, voivodeship, industry}.
    Każdy unikalny email z kolumn E-mail / E-mail 2 / E-mail 3 = osobny kontakt.
    """
    try:
        df = pd.read_csv(csv_path, dtype=str)
    except Exception as e:
        sys.exit(f"[ERROR] Nie można odczytać CSV: {e}")

    contacts = []
    seen_emails: set[str] = set()
    invalid_count = 0

    for _, row in df.iterrows():
        name = str(row.get(NAME_COLUMN, "")).strip() if NAME_COLUMN in df.columns else ""
        city = str(row.get("Miasto", "")).strip() if "Miasto" in df.columns else ""
        voivodeship = str(row.get("Województwo", "")).strip() if "Województwo" in df.columns else ""
        industry = str(row.get("Branża", "")).strip() if "Branża" in df.columns else ""
        city = "" if city.lower() in ("nan", "none") else city
        voivodeship = "" if voivodeship.lower() in ("nan", "none") else voivodeship
        industry = "" if industry.lower() in ("nan", "none") else industry

        # Zbierz wszystkie e-maile z wiersza: Email 1/2/3 + Other Emails (może być lista)
        raw_emails: list[str] = []
        for col in EMAIL_COLUMNS:
            if col not in df.columns:
                continue
            raw = str(row.get(col, "")).strip()
            if raw and raw.lower() not in ("nan", "none"):
                # Każda komórka może zawierać wiele adresów oddzielonych przecinkiem
                for part in re.split(r'[,;]', raw):
                    raw_emails.append(part.strip())

        if OTHER_EMAILS_COLUMN in df.columns:
            raw_other = str(row.get(OTHER_EMAILS_COLUMN, "")).strip()
            if raw_other and raw_other.lower() not in ("nan", "none"):
                for part in re.split(r'[,;]', raw_other):
                    raw_emails.append(part.strip())

        for raw_email in raw_emails:
            if not raw_email:
                continue
            email = raw_email.lower()
            if not is_sendable_email(email):
                invalid_count += 1
                print(f"  [WARN] Email pominięty ({raw_email!r}): nieprawidłowy format lub e-mail platformy")
                continue
            if email in seen_emails:
                continue

            seen_emails.add(email)
            contacts.append({
                "email": email,
                "first_name": name,
                "last_name": "",
                "unsubscribed": False,
                "city": city,
                "voivodeship": voivodeship,
                "industry": industry,
            })

    print(f"  Wierszy w CSV: {len(df)}")
    print(f"  Nieprawidłowych emaili: {invalid_count}")
    print(f"  Unikalnych kontaktów do importu: {len(contacts)}")
    return contacts


# ── UserCheck — weryfikacja emaili ────────────────────────────────────────────


def load_verify_cache() -> dict:
    if not VERIFY_CACHE_PATH.exists():
        return {}
    try:
        return json.loads(VERIFY_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_verify_cache(cache: dict) -> None:
    VERIFY_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    VERIFY_CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def verify_email(email: str) -> tuple[bool, str]:
    """
    Odpytuje UserCheck API dla pojedynczego emaila.
    Zwraca (True, "") jeśli OK, lub (False, reason) jeśli adres jest zły.
    Sprawdza: brak MX, disposable, blocklisted, spam.
    """
    url = f"{USERCHECK_API_BASE}/email/{email}"
    headers = {"Authorization": f"Bearer {USERCHECK_API_KEY}"}
    delay = 2.0
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=headers, timeout=10)
        except requests.RequestException as e:
            return True, f"network_error: {e}"  # przy błędzie sieci przepuść email

        if resp.status_code == 429:
            time.sleep(delay)
            delay *= 2
            continue
        if resp.status_code != 200:
            return True, ""  # przy nieoczekiwanym błędzie API przepuść email

        data = resp.json()
        if not data.get("mx", True):
            return False, "no_mx"
        if data.get("disposable"):
            return False, "disposable"
        if data.get("blocklisted"):
            return False, "blocklisted"
        if data.get("spam"):
            return False, "spam"
        return True, ""

    return True, ""  # po wyczerpaniu prób przepuść


def filter_verified_contacts(contacts: list[dict]) -> list[dict]:
    """
    Weryfikuje każdy email przez UserCheck API (z lokalnym cache).
    Filtruje adresy bez MX, disposable, blocklisted i spam.
    """
    cache = load_verify_cache()
    good: list[dict] = []
    bad: list[tuple[str, str]] = []
    from_cache = 0

    print(f"  Weryfikacja emaili przez UserCheck ({len(contacts)} adresów)…")

    for i, c in enumerate(contacts, 1):
        email = c["email"]
        if email in cache:
            ok = cache[email]["ok"]
            reason = cache[email]["reason"]
            from_cache += 1
        else:
            ok, reason = verify_email(email)
            cache[email] = {
                "ok": ok,
                "reason": reason,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            time.sleep(1.1)  # 1 req/sek — limit free tier

        if ok:
            good.append(c)
        else:
            bad.append((email, reason))

        if i % 50 == 0:
            print(f"    Postęp: {i}/{len(contacts)} (cache: {from_cache})")

    save_verify_cache(cache)

    print(f"  Wynik: {len(good)} OK, {len(bad)} odfiltrowanych (z cache: {from_cache})")
    for email, reason in bad:
        print(f"    [SKIP] {email}  ({reason})")

    return good


# ── Resend API ────────────────────────────────────────────────────────────────


def fetch_existing_emails(audience_id: str) -> set[str]:
    """
    Pobiera wszystkie emaile już istniejące w Resend Audience.
    Obsługuje paginację (cursor-based).
    """
    url = f"{API_BASE}/audiences/{audience_id}/contacts"
    headers = {"Authorization": f"Bearer {RESEND_API_KEY}"}
    existing: set[str] = set()
    params: dict = {}

    print("  Pobieram istniejące kontakty z Resend Audience…", flush=True)
    page = 0
    while True:
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=15)
        except requests.RequestException as e:
            print(f"  [WARN] Nie udało się pobrać listy kontaktów: {e}")
            return existing

        if resp.status_code == 429:
            time.sleep(2)
            continue
        if resp.status_code != 200:
            print(f"  [WARN] GET contacts → HTTP {resp.status_code}: {resp.text[:120]}")
            return existing

        data = resp.json()
        # Resend zwraca {"data": [...], "cursor": "...", "hasMore": bool}
        # lub {"data": [...]} bez paginacji
        records = data.get("data", [])
        for c in records:
            email = (c.get("email") or "").lower().strip()
            if email:
                existing.add(email)

        page += 1
        cursor = data.get("cursor") or data.get("next_cursor")
        has_more = data.get("hasMore", data.get("has_more", False))
        if not has_more or not cursor:
            break
        params["cursor"] = cursor

    print(f"  Znaleziono {len(existing)} istniejących kontaktów w Audience.")
    return existing


def sync_broadcasts_to_sent_log(audience_id: str) -> int:
    """
    Pobiera wysłane broadcascie z Resend dla danej Audience i zapisuje
    wszystkich jej kontaktów do sent_log.json.
    Zwraca liczbę nowo dodanych wpisów.
    """
    headers = {"Authorization": f"Bearer {RESEND_API_KEY}"}

    # 1. Pobierz listę broadcastów
    print("  Pobieram historię broadcastów z Resend…")
    try:
        resp = requests.get(f"{API_BASE}/broadcasts", headers=headers, timeout=15)
    except requests.RequestException as e:
        print(f"  [WARN] Nie udało się pobrać broadcastów: {e}")
        return 0

    if resp.status_code != 200:
        print(f"  [WARN] GET /broadcasts → HTTP {resp.status_code}: {resp.text[:120]}")
        return 0

    data = resp.json()
    broadcasts = data.get("data", data) if isinstance(data, dict) else data

    # Filtruj: wysłane do tej Audience
    sent_broadcasts = [
        b for b in broadcasts
        if b.get("audience_id") == audience_id and b.get("status") == "sent"
    ]

    if not sent_broadcasts:
        print("  Brak wysłanych broadcastów dla tej Audience.")
        return 0

    print(f"  Znaleziono {len(sent_broadcasts)} wysłanych broadcast(ów):")
    for b in sent_broadcasts:
        sent_at = b.get("sent_at") or b.get("created_at", "?")
        print(f"    • [{sent_at[:10]}] {b.get('subject', b.get('name', '?'))}")

    # 2. Pobierz wszystkich kontaktów z Audience
    existing_emails = fetch_existing_emails(audience_id)
    if not existing_emails:
        print("  [WARN] Brak kontaktów w Audience — nic do zsynchronizowania.")
        return 0

    # 3. Wpisz do sent_log (dla każdego broadcastu osobno)
    log = load_sent_log()
    added = 0
    for b in sent_broadcasts:
        subject = b.get("subject") or b.get("name") or "broadcast"
        sent_at = b.get("sent_at") or b.get("created_at") or datetime.now(timezone.utc).isoformat()
        for email in existing_emails:
            if email not in log:
                log[email] = {
                    "first_sent": sent_at,
                    "last_sent": sent_at,
                    "count": 1,
                    "subjects": [subject],
                }
                added += 1
            else:
                if subject not in log[email]["subjects"]:
                    log[email]["subjects"].append(subject)
                    log[email]["last_sent"] = sent_at
                    log[email]["count"] += 1

    save_sent_log(log)
    print(f"  Zsynchronizowano {added} nowych wpisów do sent_log.json.")
    return added


def _post_contact(contact: dict, dry_run: bool) -> str:
    """
    Wysyła pojedynczy kontakt do Resend.
    Zwraca: 'ok' | 'duplicate' | 'error'
    """
    if dry_run:
        return "ok"

    url = f"{API_BASE}/audiences/{RESEND_AUDIENCE_ID}/contacts"
    headers = {
        "Authorization": f"Bearer {RESEND_API_KEY}",
        "Content-Type": "application/json",
    }
    custom_fields = {}
    if contact.get("city"):
        custom_fields["city"] = contact["city"]
    if contact.get("voivodeship"):
        custom_fields["voivodeship"] = contact["voivodeship"]
    if contact.get("industry"):
        custom_fields["industry"] = contact["industry"]

    payload = {
        "email": contact["email"],
        "first_name": contact["first_name"],
        "last_name": contact["last_name"],
        "unsubscribed": contact["unsubscribed"],
    }
    if custom_fields:
        payload["data"] = custom_fields

    delay = 1.0
    for attempt in range(3):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=15)
        except requests.RequestException as e:
            print(f"  [ERROR] Błąd sieci dla {contact['email']}: {e}")
            return "error"

        if resp.status_code in (200, 201):
            return "ok"
        if resp.status_code == 409:
            return "duplicate"
        if resp.status_code == 429:
            print(f"  [WARN] Rate limit (429) — czekam {delay}s…")
            time.sleep(delay)
            delay *= 2
            continue
        # Inny błąd
        print(f"  [ERROR] {contact['email']} → HTTP {resp.status_code}: {resp.text[:120]}")
        return "error"

    print(f"  [ERROR] {contact['email']} — wyczerpano próby po rate limitach")
    return "error"


def import_contacts(contacts: list[dict], dry_run: bool) -> dict:
    stats = {"ok": 0, "duplicate": 0, "error": 0}

    existing_emails = fetch_existing_emails(RESEND_AUDIENCE_ID) if not dry_run else set()

    new_contacts = []
    for c in contacts:
        if c["email"] in existing_emails:
            stats["duplicate"] += 1
        else:
            new_contacts.append(c)

    sent_log = load_sent_log()
    already_emailed = sum(1 for c in contacts if c["email"] in sent_log)

    print(f"  Nowych do importu: {len(new_contacts)}  |  Już w Audience: {stats['duplicate']}  |  Już otrzymali kampanię: {already_emailed}\n")

    for i, contact in enumerate(new_contacts, 1):
        result = _post_contact(contact, dry_run)
        stats[result] += 1

        if not dry_run:
            time.sleep(0.5)  # ~2 req/s — limit Resend API

        if i % 50 == 0 or i == len(new_contacts):
            print(f"  Postęp: {i}/{len(new_contacts)} — ok:{stats['ok']} dup:{stats['duplicate']} err:{stats['error']}")

    return stats


# ── Tryb kampanii (batch send) ────────────────────────────────────────────────


def send_campaign(contacts: list[dict], from_email: str, subject: str, html_template: str, dry_run: bool) -> dict:
    """
    Wysyła spersonalizowane emaile przez resend.batch.send (do 100/request).
    Zmienne w szablonie: {first_name}, {city}
    Pomija kontakty które już otrzymały kampanię (wg sent_log.json).
    """
    BATCH_SIZE = 100
    stats = {"sent": 0, "skipped": 0, "failed": 0}

    sent_log = load_sent_log()

    # Filtruj już wysłanych
    fresh_contacts = []
    for c in contacts:
        if c["email"] in sent_log:
            stats["skipped"] += 1
        else:
            fresh_contacts.append(c)

    if stats["skipped"]:
        print(f"  Pominięto {stats['skipped']} kontaktów — już otrzymali kampanię wcześniej.")
    print(f"  Do wysyłki: {len(fresh_contacts)} kontaktów.\n")

    url = f"{API_BASE}/emails/batch"
    headers = {
        "Authorization": f"Bearer {RESEND_API_KEY}",
        "Content-Type": "application/json",
    }

    for i in range(0, len(fresh_contacts), BATCH_SIZE):
        batch = fresh_contacts[i:i + BATCH_SIZE]
        payload = [
            {
                "from": from_email,
                "to": [c["email"]],
                "subject": subject,
                "html": html_template.format(
                    first_name=c.get("first_name", ""),
                    city=c.get("city", ""),
                ),
            }
            for c in batch
            if c.get("email")
        ]
        batch_emails = [c["email"] for c in batch if c.get("email")]

        batch_num = i // BATCH_SIZE + 1
        if dry_run:
            print(f"  [DRY RUN] Paczka {batch_num}: {len(payload)} emaili gotowych do wysyłki")
            stats["sent"] += len(payload)
            continue

        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=30)
            if resp.status_code in (200, 201):
                stats["sent"] += len(payload)
                mark_emails_sent(batch_emails, subject, sent_log)
                save_sent_log(sent_log)
                print(f"  Wysłano paczkę {batch_num}: {len(payload)} emaili")
            elif resp.status_code == 429:
                print(f"  [WARN] Rate limit — czekam 5s…")
                time.sleep(5)
                resp = requests.post(url, json=payload, headers=headers, timeout=30)
                if resp.status_code in (200, 201):
                    stats["sent"] += len(payload)
                    mark_emails_sent(batch_emails, subject, sent_log)
                    save_sent_log(sent_log)
                else:
                    print(f"  [ERROR] Paczka {batch_num} → HTTP {resp.status_code}: {resp.text[:120]}")
                    stats["failed"] += len(payload)
            else:
                print(f"  [ERROR] Paczka {batch_num} → HTTP {resp.status_code}: {resp.text[:120]}")
                stats["failed"] += len(payload)
        except requests.RequestException as e:
            print(f"  [ERROR] Paczka {batch_num} błąd sieci: {e}")
            stats["failed"] += len(payload)

    return stats


# ── Główny przepływ ───────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Import kontaktów z CSV do Resend Audiences lub wysyłka kampanii")
    parser.add_argument("--csv", type=Path, help="Ścieżka do pliku CSV (domyślnie: najnowszy z data/Raport/)")
    parser.add_argument("--dry-run", action="store_true", help="Podgląd bez wysyłki do API")
    parser.add_argument("--mode", choices=["audiences", "campaign", "sync-broadcasts"], default="audiences",
                        help="audiences: import do Resend Audiences (domyślnie); campaign: wysyłka emaili; sync-broadcasts: zsynchronizuj historię broadcastów do sent_log")
    parser.add_argument("--audience-id", dest="audience_id", default="",
                        help="Nadpisuje RESEND_AUDIENCE_ID z .env — przydatne przy wielu Audiences (branże)")
    parser.add_argument("--from", dest="from_email", default="",
                        help="Adres nadawcy (wymagany w trybie campaign), np. noreply@twojadomena.pl")
    parser.add_argument("--subject", default="Wiadomość od IdeaMusicPro",
                        help="Temat emaila (tryb campaign)")
    parser.add_argument("--template", type=Path,
                        help="Ścieżka do szablonu HTML (tryb campaign), np. data/templates/outreach_ideamusic.html")
    parser.add_argument("--skip-verify", action="store_true",
                        help="Pomiń weryfikację emaili przez UserCheck API")
    args = parser.parse_args()

    validate_config()

    # --audience-id nadpisuje wartość z .env
    if args.audience_id:
        global RESEND_AUDIENCE_ID
        RESEND_AUDIENCE_ID = args.audience_id

    mode_label = "[DRY RUN] " if args.dry_run else ""
    print(f"\n{'='*50}")
    print(f"{mode_label}Resend Sender — tryb: {args.mode.upper()}")
    if args.mode in ("audiences", "sync-broadcasts", "campaign"):
        print(f"Audience:   {RESEND_AUDIENCE_ID}")
    print("=" * 50)

    # ── Tryb: Sync Broadcasts ─────────────────────────────────────────────────
    if args.mode == "sync-broadcasts":
        print()
        sync_broadcasts_to_sent_log(RESEND_AUDIENCE_ID)
        log = load_sent_log()
        print(f"\n  Łącznie w sent_log: {len(log)} adresów.")
        return

    csv_path: Path = args.csv if args.csv else find_latest_csv()
    if not csv_path.exists():
        sys.exit(f"[ERROR] Plik nie istnieje: {csv_path}")

    print(f"CSV:        {csv_path}")

    contacts = load_contacts(csv_path)

    if not contacts:
        print("\nBrak kontaktów do przetworzenia.")
        return

    # ── Tryb: Audiences ──────────────────────────────────────────────────────
    if args.mode == "audiences":
        if args.dry_run:
            print(f"\n[DRY RUN] Zaimportowano by {len(contacts)} kontaktów. Bez zmian w Resend.")
            return

        print(f"\nRozpoczynam import {len(contacts)} kontaktów do Audiences…\n")
        stats = import_contacts(contacts, dry_run=False)

        print(f"\n{'='*50}")
        print("Resend Import — Podsumowanie")
        print(f"  Zaimportowano:     {stats['ok']}")
        print(f"  Już istniały:      {stats['duplicate']}")
        print(f"  Błędy:             {stats['error']}")
        print("=" * 50)

    # ── Tryb: Campaign ───────────────────────────────────────────────────────
    elif args.mode == "campaign":
        if not args.from_email:
            sys.exit("[ERROR] W trybie campaign wymagany jest --from <adres_nadawcy>")

        template_path = args.template or Path(__file__).parent / "data" / "templates" / "outreach_ideamusic.html"
        if not template_path.exists():
            sys.exit(f"[ERROR] Szablon HTML nie istnieje: {template_path}")

        html_template = template_path.read_text(encoding="utf-8")

        # Dodaj pole city z CSV jeśli dostępne
        try:
            df = pd.read_csv(csv_path, dtype=str)
            city_col = "Miasto" if "Miasto" in df.columns else None
            for c in contacts:
                if city_col:
                    # dopasuj po emailu — uproszczone (pierwsza firma z tym mailem)
                    match = df[df.isin([c["email"]]).any(axis=1)]
                    c["city"] = str(match.iloc[0][city_col]).strip() if not match.empty else ""
                else:
                    c["city"] = ""
        except Exception:
            for c in contacts:
                c["city"] = ""

        if USERCHECK_API_KEY and not args.skip_verify:
            contacts = filter_verified_contacts(contacts)
        elif args.skip_verify:
            print("  [INFO] Weryfikacja UserCheck pominięta (--skip-verify).")
        else:
            print("  [INFO] Brak USERCHECK_API_KEY — pomijam weryfikację emaili.")

        print(f"\nRozpoczynam wysyłkę kampanii do {len(contacts)} odbiorców…")
        print(f"From:     {args.from_email}")
        print(f"Subject:  {args.subject}")
        print(f"Template: {template_path}\n")

        stats = send_campaign(contacts, args.from_email, args.subject, html_template, dry_run=args.dry_run)

        print(f"\n{'='*50}")
        print("Resend Campaign — Podsumowanie")
        print(f"  Wysłano:   {stats['sent']}")
        print(f"  Pominięto: {stats['skipped']}  (już wysłano wcześniej)")
        print(f"  Błędy:     {stats['failed']}")
        print("=" * 50)


if __name__ == "__main__":
    main()

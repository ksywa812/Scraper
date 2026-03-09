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
import os
import re
import time
import sys
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

# ── Konfiguracja ──────────────────────────────────────────────────────────────

load_dotenv(Path(__file__).parent / ".env")

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_AUDIENCE_ID = os.getenv("RESEND_AUDIENCE_ID", "")
API_BASE = "https://api.resend.com"

EMAIL_COLUMNS = ["Email 1", "Email 2", "Email 3"]
NAME_COLUMN = "Name"
DATA_DIR = Path(__file__).parent / "data" / "Raport"

EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

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
    return bool(EMAIL_REGEX.match(email.strip()))


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

        for col in EMAIL_COLUMNS:
            if col not in df.columns:
                continue
            raw = str(row.get(col, "")).strip()
            if not raw or raw.lower() in ("nan", "none", ""):
                continue

            email = raw.lower()
            if not is_valid_email(email):
                invalid_count += 1
                print(f"  [WARN] Nieprawidłowy email pominięty: {raw!r}")
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


# ── Resend API ────────────────────────────────────────────────────────────────


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

    for i, contact in enumerate(contacts, 1):
        result = _post_contact(contact, dry_run)
        stats[result] += 1

        if not dry_run:
            time.sleep(0.5)  # ~2 req/s — limit Resend API

        if i % 50 == 0 or i == len(contacts):
            print(f"  Postęp: {i}/{len(contacts)} — ok:{stats['ok']} dup:{stats['duplicate']} err:{stats['error']}")

    return stats


# ── Tryb kampanii (batch send) ────────────────────────────────────────────────


def send_campaign(contacts: list[dict], from_email: str, subject: str, html_template: str, dry_run: bool) -> dict:
    """
    Wysyła spersonalizowane emaile przez resend.batch.send (do 100/request).
    Zmienne w szablonie: {first_name}, {city}
    """
    BATCH_SIZE = 100
    stats = {"sent": 0, "failed": 0}

    url = f"{API_BASE}/emails/batch"
    headers = {
        "Authorization": f"Bearer {RESEND_API_KEY}",
        "Content-Type": "application/json",
    }

    for i in range(0, len(contacts), BATCH_SIZE):
        batch = contacts[i:i + BATCH_SIZE]
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

        batch_num = i // BATCH_SIZE + 1
        if dry_run:
            print(f"  [DRY RUN] Paczka {batch_num}: {len(payload)} emaili gotowych do wysyłki")
            stats["sent"] += len(payload)
            continue

        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=30)
            if resp.status_code in (200, 201):
                stats["sent"] += len(payload)
                print(f"  Wysłano paczkę {batch_num}: {len(payload)} emaili")
            elif resp.status_code == 429:
                print(f"  [WARN] Rate limit — czekam 5s…")
                time.sleep(5)
                resp = requests.post(url, json=payload, headers=headers, timeout=30)
                if resp.status_code in (200, 201):
                    stats["sent"] += len(payload)
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
    parser.add_argument("--mode", choices=["audiences", "campaign"], default="audiences",
                        help="audiences: import do Resend Audiences (domyślnie); campaign: bezpośrednia wysyłka emaili")
    parser.add_argument("--audience-id", dest="audience_id", default="",
                        help="Nadpisuje RESEND_AUDIENCE_ID z .env — przydatne przy wielu Audiences (branże)")
    parser.add_argument("--from", dest="from_email", default="",
                        help="Adres nadawcy (wymagany w trybie campaign), np. noreply@twojadomena.pl")
    parser.add_argument("--subject", default="Wiadomość od IdeaMusicPro",
                        help="Temat emaila (tryb campaign)")
    parser.add_argument("--template", type=Path,
                        help="Ścieżka do szablonu HTML (tryb campaign), np. data/templates/outreach_ideamusic.html")
    args = parser.parse_args()

    validate_config()

    # --audience-id nadpisuje wartość z .env
    if args.audience_id:
        global RESEND_AUDIENCE_ID
        RESEND_AUDIENCE_ID = args.audience_id

    csv_path: Path = args.csv if args.csv else find_latest_csv()
    if not csv_path.exists():
        sys.exit(f"[ERROR] Plik nie istnieje: {csv_path}")

    mode_label = "[DRY RUN] " if args.dry_run else ""
    print(f"\n{'='*50}")
    print(f"{mode_label}Resend Sender — tryb: {args.mode.upper()}")
    print(f"CSV:        {csv_path}")
    if args.mode == "audiences":
        print(f"Audience:   {RESEND_AUDIENCE_ID}")
    print("=" * 50)

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

        print(f"\nRozpoczynam wysyłkę kampanii do {len(contacts)} odbiorców…")
        print(f"From:     {args.from_email}")
        print(f"Subject:  {args.subject}")
        print(f"Template: {template_path}\n")

        stats = send_campaign(contacts, args.from_email, args.subject, html_template, dry_run=args.dry_run)

        print(f"\n{'='*50}")
        print("Resend Campaign — Podsumowanie")
        print(f"  Wysłano:  {stats['sent']}")
        print(f"  Błędy:    {stats['failed']}")
        print("=" * 50)


if __name__ == "__main__":
    main()

"""
resend_sender.py — Import kontaktów z CSV do Resend Audiences

Użycie:
    python resend_sender.py                          # auto-wybiera najnowszy CSV z data/Raport/
    python resend_sender.py --csv data/Raport/x.csv  # konkretny plik
    python resend_sender.py --dry-run                # tylko podgląd, bez wysyłki do API

Wymagane zmienne w .env:
    RESEND_API_KEY=re_...
    RESEND_AUDIENCE_ID=aud_...
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

EMAIL_COLUMNS = ["Adres E-mail", "Adres E-mail 2", "Adres E-mail 3"]
NAME_COLUMN = "Nazwa Salonu"
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
    Czyta CSV i zwraca listę słowników {email, first_name}.
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
            contacts.append({"email": email, "first_name": name, "last_name": "", "unsubscribed": False})

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
    payload = {
        "email": contact["email"],
        "first_name": contact["first_name"],
        "last_name": contact["last_name"],
        "unsubscribed": contact["unsubscribed"],
    }

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


# ── Główny przepływ ───────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Import kontaktów z CSV do Resend Audiences")
    parser.add_argument("--csv", type=Path, help="Ścieżka do pliku CSV (domyślnie: najnowszy z data/Raport/)")
    parser.add_argument("--dry-run", action="store_true", help="Podgląd bez wysyłki do API")
    args = parser.parse_args()

    validate_config()

    csv_path: Path = args.csv if args.csv else find_latest_csv()
    if not csv_path.exists():
        sys.exit(f"[ERROR] Plik nie istnieje: {csv_path}")

    mode_label = "[DRY RUN] " if args.dry_run else ""
    print(f"\n{'='*50}")
    print(f"{mode_label}Resend Contact Importer")
    print(f"CSV:        {csv_path}")
    print(f"Audience:   {RESEND_AUDIENCE_ID}")
    print("=" * 50)

    contacts = load_contacts(csv_path)

    if not contacts:
        print("\nBrak kontaktów do zaimportowania.")
        return

    if args.dry_run:
        print(f"\n[DRY RUN] Zaimportowano by {len(contacts)} kontaktów. Bez zmian w Resend.")
        return

    print(f"\nRozpoczynam import {len(contacts)} kontaktów…\n")
    stats = import_contacts(contacts, dry_run=False)

    print(f"\n{'='*50}")
    print("Resend Import — Podsumowanie")
    print(f"  Zaimportowano:     {stats['ok']}")
    print(f"  Już istniały:      {stats['duplicate']}")
    print(f"  Błędy:             {stats['error']}")
    print("=" * 50)


if __name__ == "__main__":
    main()

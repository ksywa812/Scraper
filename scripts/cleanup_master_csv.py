"""
cleanup_master_csv.py — Jednorazowe oczyszczenie SoundYouLeads.csv

Operacje:
  1. Usuwa wiersze z pustą lub śmieciową nazwą
  2. Usuwa e-maile platformowe (Booksy, Fresha) z kolumn Email 1/2/3 i Other Emails
  3. Usuwa nazwy plików graficznych z kolumn e-mail
  4. Usuwa emoji z nazw firm
  5. Normalizuje telefony (usuwa prefix +48)
  6. Usuwa duplikaty (ten sam numer telefonu)
  7. Zapisuje do SoundYouLeads_cleaned.csv
  8. Drukuje raport statystyczny

Użycie:
    python scripts/cleanup_master_csv.py                          # pełne czyszczenie
    python scripts/cleanup_master_csv.py --dry-run                # tylko raport, bez zapisu
    python scripts/cleanup_master_csv.py --input custom.csv       # własny plik wejściowy
"""

import argparse
import csv
import re
import sys
import unicodedata
from pathlib import Path

# ─── Stałe (muszą być zsynchronizowane ze scraper.py) ───────────────────────

DATA_DIR = Path(__file__).parent.parent / "data" / "Raport"

EMAIL_RE = re.compile(
    r'^[A-Za-z0-9._%+-]+'
    r'@[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9]'
    r'\.[A-Za-z]{2,}$'
)

_IMAGE_EXTENSIONS = ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.ico', '.bmp')

_PLATFORM_EMAILS = frozenset({
    "aneksy@booksy.com", "pomoc.pl@booksy.com", "sprzedaz@booksy.com",
    "info.pl@booksy.com", "reklamacje@booksy.com", "info@booksy.com",
    "support@booksy.com", "hello@booksy.com",
    "support@fresha.com", "no-reply@fresha.com", "hello@fresha.com",
})

CSV_HEADERS = [
    'Branża', 'Województwo', 'Miasto', 'Name', 'Address', 'Phone',
    'Website', 'Email 1', 'Email 2', 'Email 3', 'Other Emails', 'Sources'
]

# ─── Helpery ────────────────────────────────────────────────────────────────


def is_valid_email(email: str) -> bool:
    if not email or not isinstance(email, str):
        return False
    e = email.strip().lower()
    if e.endswith(_IMAGE_EXTENSIONS):
        return False
    return bool(EMAIL_RE.match(e))


def is_sendable_email(email: str) -> bool:
    return is_valid_email(email) and email.strip().lower() not in _PLATFORM_EMAILS


def strip_emoji(text: str) -> str:
    if not text:
        return text
    return ''.join(
        c for c in text
        if unicodedata.category(c) not in ('So', 'Sk', 'Sm', 'Cs')
    )


def normalize_phone(phone: str) -> str:
    digits = re.sub(r'\D', '', phone or '')
    if digits.startswith('48') and len(digits) == 11:
        digits = digits[2:]
    return digits


def is_junk_name(name: str) -> bool:
    name = name.strip()
    if not name or len(name) < 3:
        return True
    if re.search(r'^[\d,/\\@\s]+$', name):
        return True
    return False


def clean_email_cell(raw: str) -> list[str]:
    """Parsuje komórkę CSV (może zawierać kilka e-maili) i filtruje."""
    result = []
    if not raw or raw.lower() in ('nan', 'none', ''):
        return result
    for part in re.split(r'[,;]', raw):
        part = part.strip()
        if is_sendable_email(part):
            result.append(part.lower())
    return result


# ─── Główna logika ──────────────────────────────────────────────────────────


def cleanup_csv(input_path: Path, output_path: Path, dry_run: bool) -> None:
    stats = {
        'total': 0,
        'junk_name': 0,
        'dedup_removed': 0,
        'platform_emails_removed': 0,
        'invalid_emails_removed': 0,
        'emoji_cleaned': 0,
        'phone_normalized': 0,
        'kept': 0,
    }

    rows = []
    try:
        with open(input_path, newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
    except Exception as e:
        sys.exit(f"[ERROR] Nie można odczytać {input_path}: {e}")

    stats['total'] = len(rows)
    print(f"\n{'='*55}")
    print(f"  CLEANUP: {input_path.name}")
    print(f"  Wierszy wejściowych: {stats['total']}")
    print(f"{'='*55}")

    cleaned_rows = []
    seen_phones: set = set()

    for row in rows:
        name = row.get('Name', '').strip()

        # 1. Pomiń junk records
        if is_junk_name(name):
            stats['junk_name'] += 1
            continue

        # 2. Usuń emoji z nazwy
        clean_name = strip_emoji(name).strip()
        if clean_name != name:
            stats['emoji_cleaned'] += 1
            name = clean_name

        # 3. Normalizuj telefon
        raw_phone = row.get('Phone', '').strip()
        norm_phone = normalize_phone(raw_phone)
        if norm_phone and norm_phone != raw_phone:
            stats['phone_normalized'] += 1

        # 4. Dedup po znormalizowanym telefonie (jeśli nie pusty)
        if norm_phone:
            if norm_phone in seen_phones:
                stats['dedup_removed'] += 1
                continue
            seen_phones.add(norm_phone)

        # 5. Oczyść kolumny e-mail
        email_cols = ['Email 1', 'Email 2', 'Email 3']
        all_emails_before = []
        for col in email_cols:
            val = row.get(col, '').strip()
            if val and val.lower() not in ('nan', 'none'):
                all_emails_before.append(val)

        other_before = row.get('Other Emails', '').strip()
        if other_before and other_before.lower() not in ('nan', 'none'):
            all_emails_before.append(other_before)

        all_emails_after = []
        for raw_e in all_emails_before:
            cleaned = clean_email_cell(raw_e)
            all_emails_after.extend(cleaned)

        # Zlicz usunięte e-maile
        emails_before_count = sum(1 for e in all_emails_before if e)
        removed = emails_before_count - len(all_emails_after)
        if removed > 0:
            stats['invalid_emails_removed'] += removed
            stats['platform_emails_removed'] += removed  # approximate

        # Przepisz do kolumn
        all_emails_after = list(dict.fromkeys(all_emails_after))  # dedup
        row['Name'] = name
        row['Phone'] = norm_phone or raw_phone
        row['Email 1'] = all_emails_after[0] if len(all_emails_after) > 0 else ''
        row['Email 2'] = all_emails_after[1] if len(all_emails_after) > 1 else ''
        row['Email 3'] = all_emails_after[2] if len(all_emails_after) > 2 else ''
        row['Other Emails'] = ', '.join(all_emails_after[3:]) if len(all_emails_after) > 3 else ''

        cleaned_rows.append(row)
        stats['kept'] += 1

    # ─── Raport ──────────────────────────────────────────────────────────────
    print(f"\n  Wierszy-śmieci usuniętych (pusta/junk nazwa): {stats['junk_name']}")
    print(f"  Duplikatów usuniętych (ten sam tel.):         {stats['dedup_removed']}")
    print(f"  E-maili usuniętych (platf. / niepoprawne):   {stats['invalid_emails_removed']}")
    print(f"  Nazw z usuniętymi emoji:                       {stats['emoji_cleaned']}")
    print(f"  Telefonów znormalizowanych (prefix +48):      {stats['phone_normalized']}")
    print(f"  Wierszy zachowanych:                           {stats['kept']}")
    print(f"\n  Redukcja: {stats['total']} -> {stats['kept']} wierszy"
          f" (-{stats['total'] - stats['kept']})")

    if dry_run:
        print(f"\n  [DRY RUN] Brak zapisu. Aby zapisać, uruchom bez --dry-run.")
        return

    try:
        with open(output_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADERS, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(cleaned_rows)
        print(f"\n  Zapisano: {output_path}")
    except Exception as e:
        sys.exit(f"\n[ERROR] Nie można zapisać {output_path}: {e}")


# ─── CLI ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Oczyszczenie SoundYouLeads.csv")
    parser.add_argument(
        '--input', default=None,
        help='Ścieżka do pliku wejściowego CSV (domyślnie: data/Raport/SoundYouLeads.csv)'
    )
    parser.add_argument(
        '--output', default=None,
        help='Ścieżka do pliku wynikowego (domyślnie: data/Raport/SoundYouLeads_cleaned.csv)'
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Tylko raport — bez zapisu pliku'
    )
    args = parser.parse_args()

    input_path = Path(args.input) if args.input else DATA_DIR / 'SoundYouLeads.csv'
    output_path = Path(args.output) if args.output else DATA_DIR / 'SoundYouLeads_cleaned.csv'

    if not input_path.exists():
        sys.exit(f"[ERROR] Plik nie istnieje: {input_path}")

    cleanup_csv(input_path, output_path, dry_run=args.dry_run)


if __name__ == '__main__':
    main()

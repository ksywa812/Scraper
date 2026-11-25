"""
attio_sync.py — Synchronizacja leadów CSV ze scrapera wprost do Attio CRM.

Funkcje:
  1. Kanoniczna klasyfikacja branż SoundYou (multiselect na `Companies`: `soundyou_industry`):
     SPA, Wellness, Beauty, Hotele, Restauracje, Kawiarnie,
     Fitness / Joga, Fizjoterapia, Fryzjer / Barber, Inne.
  2. Automatyczne sprawdzanie/tworzenie schematu w Attio (`--ensure-schema`):
     - tworzy atrybut `soundyou_industry` ("Branża SoundYou", multiselect) na `companies`,
     - dodaje brakujące opcje select dla wszystkich branż SoundYou,
     - raportuje obecne i brakujące widoki (Views) per branża w Attio.
  3. Dwustopniowa deduplikacja:
     - lokalna w CSV (znormalizowana nazwa + miasto, telefon, e-mail + adres),
     - względem Attio (`companies` po domenach, `(name, city)` i e-mailach; `people` po `email_addresses`).
  4. Bezpieczne wzbogacanie istniejących firm (dopisuje brakujące tagi `soundyou_industry`
     i unikalne domeny — nie nadpisuje opisów ani pól Resend).
  5. Tworzenie / podpinanie rekordów `People` dla znalezionych adresów e-mail
     z powiązaniem do firmy (`company`).

Użycie:
    python attio_sync.py --dry-run                       # podgląd bez zapisu (z Attio lub offline)
    python attio_sync.py --offline-dry-run               # tylko analiza i klasyfikacja CSV lokalnie
    python attio_sync.py --ensure-schema                 # utwórz atrybut soundyou_industry + opcje
    python attio_sync.py --csv data/Raport/SoundYouLeads.csv
    python attio_sync.py --companies-only                # bez tworzenia rekordów People
"""

from __future__ import annotations

import argparse
import csv
import getpass
import json
import logging
import os
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger("attio_sync")

# ─── Stałe i kanoniczna taksonomia branż SoundYou ───────────────────────────

ATTIO_API_BASE = "https://api.attio.com/v2"
DEFAULT_CSV_PATH = Path(__file__).parent / "data" / "Raport" / "SoundYouLeads.csv"

INDUSTRY_ATTR_SLUG = "soundyou_industry"
INDUSTRY_ATTR_TITLE = "Branża SoundYou"

SOUNDYOU_INDUSTRIES: tuple[str, ...] = (
    "SPA & Wellness",
    "Kobido & Masaż",
    "Beauty / Salon Urody",
    "Hotele i Pensjonaty",
    "Restauracje",
    "Kawiarnie",
    "Fitness / Siłownia",
    "Joga / Pilates / Medytacja",
    "Salon Fryzjerski / Barber",
    "Poczekalnia / Gabinet",
    "Sklep / Retail",
    "Lokal Usługowy / Inne",
)

# 8 dedykowanych widoków branżowych w Attio (Companies)
EXPECTED_INDUSTRY_VIEWS: tuple[str, ...] = (
    "Branża — SPA & Wellness",
    "Branża — Kobido & Masaż",
    "Branża — Beauty / Salon Urody",
    "Branża — Hotele i Pensjonaty",
    "Branża — Restauracje i Kawiarnie",
    "Branża — Fitness / Joga / Pilates",
    "Branża — Fryzjer / Barber",
    "Branża — Gabinety / Sklepy / Inne",
)

# Klucz mapowania: wersja angielska i polska (zapytania scrapera, CSV, kategorie Attio) -> kanoniczne opcje Attio
INDUSTRY_EN_PL_MAP: dict[str, tuple[str, ...]] = {
    # 1. SPA & Wellness
    "spa": ("SPA & Wellness",),
    "day spa": ("SPA & Wellness",),
    "medical spa": ("SPA & Wellness",),
    "wellness": ("SPA & Wellness",),
    "spa & wellness": ("SPA & Wellness",),
    "spa wellness": ("SPA & Wellness",),
    "health & wellness": ("SPA & Wellness",),
    "odnowa biologiczna": ("SPA & Wellness",),
    "sauna": ("SPA & Wellness",),
    "saunarium": ("SPA & Wellness",),
    "thermal bath": ("SPA & Wellness",),
    "termy": ("SPA & Wellness",),
    "float": ("SPA & Wellness",),
    "floating": ("SPA & Wellness",),
    # 2. Kobido & Masaż
    "kobido": ("Kobido & Masaż",),
    "kobido & masaż": ("Kobido & Masaż",),
    "kobido & masaz": ("Kobido & Masaż",),
    "masaz": ("Kobido & Masaż",),
    "masaż": ("Kobido & Masaż",),
    "masazysta": ("Kobido & Masaż",),
    "masażysta": ("Kobido & Masaż",),
    "massage": ("Kobido & Masaż",),
    "massage parlor": ("Kobido & Masaż",),
    "massage salon": ("Kobido & Masaż",),
    "massage spa": ("SPA & Wellness", "Kobido & Masaż"),
    "thai massage": ("Kobido & Masaż",),
    "balinese massage": ("Kobido & Masaż",),
    "bodywork": ("Kobido & Masaż",),
    # 3. Beauty / Salon Urody
    "beauty": ("Beauty / Salon Urody",),
    "beauty salon": ("Beauty / Salon Urody",),
    "beauty studio": ("Beauty / Salon Urody",),
    "beauty / salon urody": ("Beauty / Salon Urody",),
    "cosmetics": ("Beauty / Salon Urody",),
    "cosmetology": ("Beauty / Salon Urody",),
    "aesthetic medicine": ("Beauty / Salon Urody",),
    "nail salon": ("Beauty / Salon Urody",),
    "manicure": ("Beauty / Salon Urody",),
    "pedicure": ("Beauty / Salon Urody",),
    "brow bar": ("Beauty / Salon Urody",),
    "lash": ("Beauty / Salon Urody",),
    "uroda": ("Beauty / Salon Urody",),
    "salon urody": ("Beauty / Salon Urody",),
    "salon kosmetyczny": ("Beauty / Salon Urody",),
    "kosmetyka": ("Beauty / Salon Urody",),
    "kosmetologia": ("Beauty / Salon Urody",),
    "medycyna estetyczna": ("Beauty / Salon Urody",),
    # 4. Hotele i Pensjonaty
    "hotel": ("Hotele i Pensjonaty",),
    "hotels": ("Hotele i Pensjonaty",),
    "hotele": ("Hotele i Pensjonaty",),
    "hotele i pensjonaty": ("Hotele i Pensjonaty",),
    "resort": ("Hotele i Pensjonaty",),
    "guesthouse": ("Hotele i Pensjonaty",),
    "guest house": ("Hotele i Pensjonaty",),
    "b&b": ("Hotele i Pensjonaty",),
    "bed and breakfast": ("Hotele i Pensjonaty",),
    "pensjonat": ("Hotele i Pensjonaty",),
    "aparthotel": ("Hotele i Pensjonaty",),
    "hostel": ("Hotele i Pensjonaty",),
    "hospitality": ("Hotele i Pensjonaty",),
    "hotel-spa": ("Hotele i Pensjonaty", "SPA & Wellness"),
    "hotel spa": ("Hotele i Pensjonaty", "SPA & Wellness"),
    "hotel & spa": ("Hotele i Pensjonaty", "SPA & Wellness"),
    "spa hotel": ("Hotele i Pensjonaty", "SPA & Wellness"),
    "wellness hotel": ("Hotele i Pensjonaty", "SPA & Wellness"),
    "spa resort": ("Hotele i Pensjonaty", "SPA & Wellness"),
    # 5. Restauracje
    "restaurant": ("Restauracje",),
    "restaurants": ("Restauracje",),
    "restauracja": ("Restauracje",),
    "restauracje": ("Restauracje",),
    "bistro": ("Restauracje",),
    "dining": ("Restauracje",),
    "fine dining": ("Restauracje",),
    "trattoria": ("Restauracje",),
    "pizzeria": ("Restauracje",),
    "sushi": ("Restauracje",),
    "steakhouse": ("Restauracje",),
    "gastro": ("Restauracje",),
    "gastronomy": ("Restauracje",),
    "gastronomia": ("Restauracje",),
    "bar": ("Restauracje",),
    "pub": ("Restauracje",),
    # 6. Kawiarnie
    "cafe": ("Kawiarnie",),
    "café": ("Kawiarnie",),
    "caffe": ("Kawiarnie",),
    "coffee": ("Kawiarnie",),
    "coffee shop": ("Kawiarnie",),
    "coffeeshop": ("Kawiarnie",),
    "espresso bar": ("Kawiarnie",),
    "bakery": ("Kawiarnie",),
    "patisserie": ("Kawiarnie",),
    "tea house": ("Kawiarnie",),
    "kawiarnia": ("Kawiarnie",),
    "kawiarnie": ("Kawiarnie",),
    "cukiernia": ("Kawiarnie",),
    "palarnia kawy": ("Kawiarnie",),
    # 7. Fitness / Siłownia
    "fitness": ("Fitness / Siłownia",),
    "fitness / siłownia": ("Fitness / Siłownia",),
    "fitness / silownia": ("Fitness / Siłownia",),
    "fitness club": ("Fitness / Siłownia",),
    "fitness center": ("Fitness / Siłownia",),
    "gym": ("Fitness / Siłownia",),
    "health club": ("Fitness / Siłownia",),
    "sports & fitness": ("Fitness / Siłownia",),
    "crossfit": ("Fitness / Siłownia",),
    "personal training": ("Fitness / Siłownia",),
    "silownia": ("Fitness / Siłownia",),
    "siłownia": ("Fitness / Siłownia",),
    "klub fitness": ("Fitness / Siłownia",),
    # 8. Joga / Pilates / Medytacja
    "joga": ("Joga / Pilates / Medytacja",),
    "yoga": ("Joga / Pilates / Medytacja",),
    "yoga studio": ("Joga / Pilates / Medytacja",),
    "pilates": ("Joga / Pilates / Medytacja",),
    "meditation": ("Joga / Pilates / Medytacja",),
    "medytacja": ("Joga / Pilates / Medytacja",),
    "mindfulness": ("Joga / Pilates / Medytacja",),
    "joga / pilates / medytacja": ("Joga / Pilates / Medytacja",),
    # 9. Salon Fryzjerski / Barber
    "fryzjer": ("Salon Fryzjerski / Barber",),
    "fryzjerstwo": ("Salon Fryzjerski / Barber",),
    "salon fryzjerski": ("Salon Fryzjerski / Barber",),
    "salon fryzjerski / barber": ("Salon Fryzjerski / Barber",),
    "fryzjer / barber": ("Salon Fryzjerski / Barber",),
    "hair": ("Salon Fryzjerski / Barber",),
    "hair salon": ("Salon Fryzjerski / Barber",),
    "hairdresser": ("Salon Fryzjerski / Barber",),
    "hairdressing": ("Salon Fryzjerski / Barber",),
    "barber": ("Salon Fryzjerski / Barber",),
    "barbershop": ("Salon Fryzjerski / Barber",),
    "barber shop": ("Salon Fryzjerski / Barber",),
    # 10. Poczekalnia / Gabinet
    "poczekalnia": ("Poczekalnia / Gabinet",),
    "gabinet": ("Poczekalnia / Gabinet",),
    "poczekalnia / gabinet": ("Poczekalnia / Gabinet",),
    "waiting room": ("Poczekalnia / Gabinet",),
    "clinic": ("Poczekalnia / Gabinet",),
    "medical clinic": ("Poczekalnia / Gabinet",),
    "dental": ("Poczekalnia / Gabinet",),
    "dentist": ("Poczekalnia / Gabinet",),
    "stomatolog": ("Poczekalnia / Gabinet",),
    "dentysta": ("Poczekalnia / Gabinet",),
    "klinika": ("Poczekalnia / Gabinet",),
    "przychodnia": ("Poczekalnia / Gabinet",),
    "fizjoterapia": ("Poczekalnia / Gabinet",),
    "physiotherapy": ("Poczekalnia / Gabinet",),
    "physical therapy": ("Poczekalnia / Gabinet",),
    "rehabilitacja": ("Poczekalnia / Gabinet",),
    "rehabilitation": ("Poczekalnia / Gabinet",),
    "fizjo": ("Poczekalnia / Gabinet",),
    "osteopatia": ("Poczekalnia / Gabinet",),
    "osteopathy": ("Poczekalnia / Gabinet",),
    "chiropractor": ("Poczekalnia / Gabinet",),
    # 11. Sklep / Retail
    "sklep": ("Sklep / Retail",),
    "sklep / retail": ("Sklep / Retail",),
    "shop": ("Sklep / Retail",),
    "store": ("Sklep / Retail",),
    "retail": ("Sklep / Retail",),
    "boutique": ("Sklep / Retail",),
    "butik": ("Sklep / Retail",),
    "showroom": ("Sklep / Retail",),
    "concept store": ("Sklep / Retail",),
    # 12. Lokal Usługowy / Inne
    "inne": ("Lokal Usługowy / Inne",),
    "other": ("Lokal Usługowy / Inne",),
    "service": ("Lokal Usługowy / Inne",),
    "services": ("Lokal Usługowy / Inne",),
    "local business": ("Lokal Usługowy / Inne",),
    "lokal usługowy": ("Lokal Usługowy / Inne",),
    "lokal uslugowy": ("Lokal Usługowy / Inne",),
    "lokal usługowy / inne": ("Lokal Usługowy / Inne",),
}

# Alias wsteczny
_RAW_INDUSTRY_MAP = INDUSTRY_EN_PL_MAP

# Wzorce wzbogacające multiselect na podstawie nazwy firmy i domeny/strony (PL + EN)
_SIGNAL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "Hotele i Pensjonaty",
        re.compile(
            r"\b(hotel\w*|resort\w*|pensjonat\w*|aparthotel\w*|guesthouse|guest\s+house|b&b|bed\s+and\s+breakfast|palac\w*|pałac\w*|dwor\w*|dwór|zamek|villa|willa|nocleg\w*|hostel\w*)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "SPA & Wellness",
        re.compile(
            r"\b(spa|day\s*spa|wellness|odnow\w*\s+biologiczn\w*|float\w*|saun\w*|term[ay]|thermal|balneolog\w*|hammam|aromaterap\w*|aromatherapy)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Kobido & Masaż",
        re.compile(
            r"\b(kobido|masaz\w*|masaż\w*|massage\w*|tajsk\w*|thai\s+massage|balijsk\w*|balinese|lomi|bodywork|shiatsu|refleksologi\w*|reflexology)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Beauty / Salon Urody",
        re.compile(
            r"\b(beauty|urod\w*|kosmetolog\w*|kosmetycz\w*|cosmet\w*|estetic\w*|aesthetic\w*|medycyn\w*\s+estetyczn\w*|manicure|pedicure|nail\w*|paznokci\w*|depilacj\w*|brow\w*|lash\w*|rzęs\w*|rzes\w*|makija[żz]\w*|makeup)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Restauracje",
        re.compile(
            r"\b(restaurac\w*|restaurant\w*|bistro|trattoria|pizzeri\w*|sushi|karczm\w*|ober[żz]\w*|gastronomi\w*|gastronomy|steakhouse|tapas|fine\s+dining)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Kawiarnie",
        re.compile(
            r"\b(kawiarni\w*|coffee\w*|caf[eé]|caffe|espresso|palarni\w*\s+kaw\w*|cukierni\w*|piekarni\w*|bakery|patisserie|tea\s+house)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Fitness / Siłownia",
        re.compile(
            r"\b(fitness|gym|siłowni\w*|silowni\w*|crossfit|trening\w*\s+personaln\w*|personal\s+training|health\s+club|workout)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Joga / Pilates / Medytacja",
        re.compile(
            r"\b(jog[ai]|yoga|pilates|medytacj\w*|meditation|mindfulness|ashtanga|vinyasa|hatha)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Salon Fryzjerski / Barber",
        re.compile(
            r"\b(fryzjer\w*|barber\w*|hair\s*(salon|studio|bar|dresser)?|hairdress\w*|strzy[żz]eni\w*|koloryzacj\w*)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Poczekalnia / Gabinet",
        re.compile(
            r"\b(fizjoterap\w*|physiotherap\w*|physical\s+therapy|fizjo\w*|rehabilitac\w*|rehab\w*|osteopat\w*|kinezyterap\w*|terapi\w*\s+manualn\w*|stomatolog\w*|dentyst\w*|dentist\w*|dental|klinik\w*|clinic|przychodni\w*|poczekalni\w*|waiting\s+room|chiropract\w*)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Sklep / Retail",
        re.compile(
            r"\b(sklep\w*|butik\w*|boutique|showroom|concept\s+store|retail|store)\b",
            re.IGNORECASE,
        ),
    ),
]

EMAIL_RE = re.compile(r"[\w.!#$%&'*+/=?^`{|}~-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+", re.I | re.ASCII)

PLATFORM_DOMAINS = frozenset({
    "booksy.com", "fresha.com", "facebook.com", "instagram.com", "tiktok.com",
    "google.com", "maps.app.goo.gl", "linktr.ee", "wixsite.com", "wordpress.com",
    "business.site", "goo.gl", "youtube.com", "linkedin.com", "znanylekarz.pl",
    "panoramafirm.pl", "pkt.pl", "fixly.pl", "oferteo.pl", "firmy.net",
})

PLATFORM_EMAILS = frozenset({
    "aneksy@booksy.com", "pomoc.pl@booksy.com", "sprzedaz@booksy.com",
    "info.pl@booksy.com", "reklamacje@booksy.com", "info@booksy.com",
    "support@booksy.com", "hello@booksy.com",
    "support@fresha.com", "no-reply@fresha.com", "hello@fresha.com",
})

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".bmp")


# ─── Funkcje pomocnicze i klasyfikacja ──────────────────────────────────────

def normalized(text: str) -> str:
    """Normalizuje tekst do porównań (bez diakrytyków, lowercase, same słowa)."""
    decomposed = unicodedata.normalize("NFKD", (text or "").casefold().replace("ł", "l"))
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(re.findall(r"[^\W_]+", stripped))


def strip_emoji(text: str) -> str:
    if not text:
        return ""
    return "".join(c for c in text if unicodedata.category(c) not in {"So", "Sk", "Cs"}).strip()


def business_domain(raw: str) -> str:
    """Wyciąga czystą domenę biznesową z URL lub hosta; odrzuca katalogi i social media."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        host = urlsplit(raw if "://" in raw else f"https://{raw}").hostname or ""
        host = host.removeprefix("www.").rstrip(".").lower().encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return ""
    if not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z][a-z0-9-]+", host):
        return ""
    if any(host == p or host.endswith(f".{p}") for p in PLATFORM_DOMAINS):
        return ""
    return host


def extract_emails(text: str) -> set[str]:
    """Wyciąga poprawne, wysyłalne adresy e-mail z komórki CSV lub opisu."""
    found: set[str] = set()
    for email in EMAIL_RE.findall(text or ""):
        e = email.strip().lower()
        if len(e) > 254 or e in PLATFORM_EMAILS or e.endswith(IMAGE_EXTENSIONS):
            continue
        domain_part = e.rsplit("@", 1)[1]
        if any(domain_part == p or domain_part.endswith(f".{p}") for p in PLATFORM_DOMAINS):
            continue
        found.add(e)
    return found


def normalize_phone_e164(raw: str) -> str:
    """Normalizuje polski numer telefonu do formatu +48XXXXXXXXX (lub pusty string)."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("48"):
        digits = digits[2:]
    if len(digits) == 9:
        return f"+48{digits}"
    return ""


def classify_soundyou_industries(
    raw_industry: str = "",
    name: str = "",
    website: str = "",
) -> list[str]:
    """Klasyfikuje lead do kanonicznych branż SoundYou (multiselect).

    Łączy deklarowaną kolumnę `Branża` z sygnałami z nazwy i domeny/URL,
    zwracając listę unikalnych tagów w kanonicznej kolejności `SOUNDYOU_INDUSTRIES`.
    """
    matched: set[str] = set()

    # 1. Parsowanie wartości z kolumny Branża (obsługuje też listy po przecinku/średniku)
    if raw_industry:
        for token in re.split(r"[,;|]+", raw_industry):
            clean_tok = token.strip()
            if not clean_tok:
                continue
            low = clean_tok.lower()
            if clean_tok in SOUNDYOU_INDUSTRIES:
                matched.add(clean_tok)
            elif low in _RAW_INDUSTRY_MAP:
                matched.update(_RAW_INDUSTRY_MAP[low])
            else:
                norm_tok = normalized(clean_tok)
                if norm_tok in _RAW_INDUSTRY_MAP:
                    matched.update(_RAW_INDUSTRY_MAP[norm_tok])

    # 2. Wzbogacenie z nazwy firmy i domeny (wykorzystuje zalety multiselect)
    signal_text = f"{name or ''} {website or ''}".strip()
    if signal_text:
        for industry, pattern in _SIGNAL_PATTERNS:
            if pattern.search(signal_text):
                matched.add(industry)

    # Jeśli wykryto konkretne branże, usuń ewentualne "Lokal Usługowy / Inne"
    if len(matched) > 1 and "Lokal Usługowy / Inne" in matched:
        matched.discard("Lokal Usługowy / Inne")

    if not matched:
        return ["Lokal Usługowy / Inne"]

    return [ind for ind in SOUNDYOU_INDUSTRIES if ind in matched]


# ─── Model leada i lokalna deduplikacja CSV ─────────────────────────────────

@dataclass
class Lead:
    name: str
    city: str
    region: str
    address: str
    industries: set[str] = field(default_factory=set)
    emails: set[str] = field(default_factory=set)
    phones: set[str] = field(default_factory=set)
    domains: set[str] = field(default_factory=set)

    @property
    def identity(self) -> tuple[str, str]:
        return normalized(self.name), normalized(self.city)

    @property
    def ordered_industries(self) -> list[str]:
        ordered = [i for i in SOUNDYOU_INDUSTRIES if i in self.industries]
        return ordered or ["Lokal Usługowy / Inne"]

    def company_values(self, safe_domains: list[str] | None = None) -> dict[str, Any]:
        industries = self.ordered_industries
        facts = ["Źródło: Web-Crunchy-Scrap", f"Branża: {', '.join(industries)}"]
        if self.phones:
            facts.append(f"Telefony: {', '.join(sorted(self.phones))}")
        if self.emails:
            facts.append(f"E-maile: {', '.join(sorted(self.emails))}")

        postcode_m = re.search(r"\b\d{2}-\d{3}\b", self.address)
        location = {
            "line_1": self.address or None,
            "line_2": None,
            "line_3": None,
            "line_4": None,
            "locality": self.city or None,
            "region": self.region or None,
            "postcode": postcode_m.group() if postcode_m else None,
            "country_code": "PL",
            "latitude": None,
            "longitude": None,
        }
        values: dict[str, Any] = {
            "name": self.name,
            "description": "\n".join(facts),
            "primary_location": location,
            INDUSTRY_ATTR_SLUG: industries,
        }
        doms = safe_domains if safe_domains is not None else sorted(self.domains)
        if doms:
            values["domains"] = doms
        return values


def read_csv_leads(csv_path: Path, stats: Counter[str]) -> Iterable[Lead]:
    """Czyta plik CSV ze scrapera i zwraca strumień oczyszczonych obiektów Lead."""
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.reader(stream)
        headers = next(reader, None)
        if not headers:
            return
        email_cols = [i for i, h in enumerate(headers) if "email" in h.lower().replace("-", "")]
        for row in reader:
            stats["source_rows"] += 1
            if len(row) < len(headers):
                row = row + [""] * (len(headers) - len(row))
            data = {k.strip(): v.strip() for k, v in zip(headers, row) if v and v.strip()}
            raw_name = data.get("Name") or data.get("Nazwa Salonu", "")
            name = strip_emoji(raw_name)
            city = data.get("Miasto", "")
            if len(name) < 3 or not any(c.isalpha() for c in name):
                stats["invalid_rows"] += 1
                continue
            phone = normalize_phone_e164(data.get("Phone") or data.get("Numer Telefonu", ""))
            website = data.get("Website", "")
            domain = business_domain(website)
            emails: set[str] = set()
            for idx in email_cols:
                if idx < len(row):
                    emails.update(extract_emails(row[idx]))
            raw_industry = data.get("Branża", "")
            industries = set(classify_soundyou_industries(raw_industry, name=name, website=website))
            yield Lead(
                name=name,
                city=city,
                region=data.get("Województwo", ""),
                address=data.get("Address") or data.get("Adres", ""),
                industries=industries,
                emails=emails,
                phones={phone} if phone else set(),
                domains={domain} if domain else set(),
            )


def load_and_dedup_csv(csv_paths: list[Path]) -> tuple[list[Lead], Counter[str], Counter[str]]:
    """Ładuje jeden lub więcej plików CSV i lokalnie deduplikuje leady."""
    stats: Counter[str] = Counter()
    leads: list[Lead] = []

    for csv_path in csv_paths:
        for lead in read_csv_leads(csv_path, stats):
            name_norm, city_norm = lead.identity
            candidates = [
                item for item in leads
                if (item.identity[1] == city_norm or not city_norm or not item.identity[1]) and (
                    item.identity[0] == name_norm
                    or bool(item.phones & lead.phones)
                    or bool(
                        item.emails & lead.emails
                        and lead.address
                        and normalized(item.address) == normalized(lead.address)
                    )
                )
            ]
            if len(candidates) > 1:
                stats["ambiguous_source_rows"] += 1
                continue
            if candidates:
                existing = candidates[0]
                existing.industries.update(lead.industries)
                if len(existing.industries) > 1:
                    existing.industries.discard("Lokal Usługowy / Inne")
                existing.emails.update(lead.emails)
                existing.phones.update(lead.phones)
                existing.domains.update(lead.domains)
                existing.address = existing.address or lead.address
                existing.region = existing.region or lead.region
                existing.city = existing.city or lead.city
                stats["merged_source_rows"] += 1
            else:
                leads.append(lead)

    domain_counts = Counter(d for lead in leads for d in lead.domains)
    stats["candidate_companies"] = len(leads)
    stats["candidates_with_email"] = sum(bool(lead.emails) for lead in leads)
    return leads, stats, domain_counts


# ─── Klient Attio REST API v2 ───────────────────────────────────────────────

class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class AttioAPIError(Exception):
    def __init__(self, status: int, message: str = ""):
        self.status = status
        self.message = message
        super().__init__(f"Attio HTTP {status}: {message}")


class AttioClient:
    """Lekki klient Attio REST API v2 z obsługą retry/backoff na 429/5xx."""

    def __init__(self, token: str, pace_seconds: float = 0.15):
        self.token = token
        self.pace_seconds = pace_seconds
        self._opener = build_opener(_NoRedirect)

    def request(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        method: str | None = None,
    ) -> Any:
        url = f"{ATTIO_API_BASE}{path}"
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        req_method = method or ("POST" if payload is not None else "GET")
        req = Request(
            url,
            data=body,
            method=req_method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        for attempt in range(4):
            if self.pace_seconds > 0:
                time.sleep(self.pace_seconds)
            try:
                with self._opener.open(req, timeout=30) as resp:
                    parsed = json.load(resp)
                    return parsed.get("data", parsed)
            except HTTPError as err:
                status = err.code
                msg = ""
                try:
                    info = json.load(err)
                    if isinstance(info, dict):
                        msg = str(info.get("message") or info)
                except (ValueError, OSError):
                    pass
                err.close()
                if status in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise AttioAPIError(status, msg) from None
        raise AttioAPIError(429, "Rate limit exceeded after retries")

    def query_all_records(self, obj_slug: str) -> Iterable[dict[str, Any]]:
        offset = 0
        while True:
            page = self.request(f"/objects/{obj_slug}/records/query", {"limit": 250, "offset": offset})
            if not isinstance(page, list):
                raise ValueError(f"Nieoczekiwana odpowiedź paginacji dla {obj_slug}")
            yield from page
            if len(page) < 250:
                return
            offset += len(page)


# ─── Konfiguracja schematu (soundyou_industry + opcje + raport widoków) ─────

def ensure_soundyou_schema(client: AttioClient, dry_run: bool = False) -> dict[str, Any]:
    """Zapewnia istnienie multiselect `soundyou_industry` i wszystkich opcji na `companies`."""
    summary: dict[str, Any] = {
        "attribute_existed": False,
        "attribute_created": False,
        "options_created": [],
        "existing_industry_views": [],
        "missing_industry_views": [],
    }

    attrs = client.request("/objects/companies/attributes", method="GET")
    existing_attr = next(
        (a for a in (attrs or []) if a.get("api_slug") == INDUSTRY_ATTR_SLUG),
        None,
    )

    if existing_attr:
        summary["attribute_existed"] = True
    elif not dry_run:
        client.request(
            "/objects/companies/attributes",
            {
                "data": {
                    "title": INDUSTRY_ATTR_TITLE,
                    "api_slug": INDUSTRY_ATTR_SLUG,
                    "description": "Kanoniczna branża SoundYou (multiselect) — źródło prawdy dla widoków per branża.",
                    "type": "select",
                    "is_multiselect": True,
                    "is_required": False,
                    "is_unique": False,
                    "config": {},
                }
            },
            method="POST",
        )
        summary["attribute_created"] = True

    existing_option_titles: set[str] = set()
    if summary["attribute_existed"] or summary["attribute_created"]:
        opts = client.request(f"/objects/companies/attributes/{INDUSTRY_ATTR_SLUG}/options", method="GET")
        existing_option_titles = {
            o.get("title", "") for o in (opts or []) if not o.get("is_archived")
        }

    for title in SOUNDYOU_INDUSTRIES:
        if title not in existing_option_titles:
            if not dry_run:
                client.request(
                    f"/objects/companies/attributes/{INDUSTRY_ATTR_SLUG}/options",
                    {"data": {"title": title}},
                    method="POST",
                )
            summary["options_created"].append(title)

    # Sprawdź 8 dedykowanych widoków branżowych na Companies
    try:
        views = client.request("/objects/companies/views", method="GET") or []
        view_titles = {v.get("title", "").lower() for v in views}
        for expected_title in EXPECTED_INDUSTRY_VIEWS:
            if expected_title.lower() in view_titles:
                summary["existing_industry_views"].append(expected_title)
            else:
                summary["missing_industry_views"].append(expected_title)
    except AttioAPIError:
        pass

    return summary


# ─── Indeksowanie Attio i synchronizacja (Companies + People) ───────────────

def _extract_multiselect_titles(raw_values: list[dict[str, Any]]) -> set[str]:
    titles: set[str] = set()
    for item in raw_values or []:
        opt = item.get("option")
        if isinstance(opt, dict) and opt.get("title"):
            titles.add(opt["title"])
        elif isinstance(item.get("value"), str):
            titles.add(item["value"])
    return titles


def build_attio_indexes(client: AttioClient) -> dict[str, Any]:
    """Buduje indeksy deduplikacji z aktualnego stanu `companies` i `people` w Attio."""
    companies_by_domain: dict[str, str] = {}
    companies_by_name_city: dict[tuple[str, str], str] = {}
    companies_by_email: dict[str, str] = {}
    company_state: dict[str, dict[str, Any]] = {}
    people_by_email: dict[str, dict[str, Any]] = {}

    for rec in client.query_all_records("companies"):
        rid = rec["id"]["record_id"]
        vals = rec.get("values") or {}

        doms = {
            business_domain(item.get("domain") or "")
            for item in vals.get("domains", [])
            if item.get("domain")
        } - {""}
        for d in doms:
            companies_by_domain[d] = rid

        cities = {
            normalized(item.get("locality") or "")
            for item in vals.get("primary_location", [])
        } or {""}
        for item in vals.get("name", []):
            norm_name = normalized(item.get("value") or "")
            if not norm_name:
                continue
            for c in cities:
                companies_by_name_city[(norm_name, c)] = rid

        for item in vals.get("description", []):
            for em in extract_emails(item.get("value") or ""):
                companies_by_email.setdefault(em, rid)

        company_state[rid] = {
            "industries": _extract_multiselect_titles(vals.get(INDUSTRY_ATTR_SLUG, [])),
            "domains": doms,
        }

    for rec in client.query_all_records("people"):
        pid = rec["id"]["record_id"]
        vals = rec.get("values") or {}
        comp_refs = vals.get("company") or []
        linked_cid = comp_refs[0].get("target_record_id") if comp_refs else None

        for item in vals.get("email_addresses", []):
            em = (item.get("email_address") or "").strip().lower()
            if not em:
                continue
            people_by_email[em] = {
                "record_id": pid,
                "company_id": linked_cid,
            }
            if linked_cid:
                companies_by_email.setdefault(em, linked_cid)

    return {
        "companies_by_domain": companies_by_domain,
        "companies_by_name_city": companies_by_name_city,
        "companies_by_email": companies_by_email,
        "company_state": company_state,
        "people_by_email": people_by_email,
    }


def _find_matching_company(lead: Lead, indexes: dict[str, Any]) -> str | None:
    by_dom = indexes["companies_by_domain"]
    for d in sorted(lead.domains):
        if d in by_dom:
            return by_dom[d]

    name_norm, city_norm = lead.identity
    by_nc = indexes["companies_by_name_city"]
    if (name_norm, city_norm) in by_nc:
        return by_nc[(name_norm, city_norm)]
    if (name_norm, "") in by_nc:
        return by_nc[(name_norm, "")]

    by_em = indexes["companies_by_email"]
    for em in sorted(lead.emails):
        if em in by_em:
            return by_em[em]

    return None


def sync_leads_to_attio(
    client: AttioClient,
    leads: list[Lead],
    stats: Counter[str],
    domain_counts: Counter[str],
    *,
    dry_run: bool = False,
    sync_people: bool = True,
) -> dict[str, Any]:
    """Synchronizuje zdeduplikowane leady do `Companies` i `People` w Attio."""
    indexes = build_attio_indexes(client)
    industry_counts: Counter[str] = Counter()

    for lead in leads:
        for ind in lead.ordered_industries:
            industry_counts[ind] += 1

        safe_domains = sorted(
            d for d in lead.domains
            if domain_counts[d] == 1 and d not in indexes["companies_by_domain"]
        )
        matched_cid = _find_matching_company(lead, indexes)

        if matched_cid:
            # Firma już istnieje — bezpiecznie dopisz brakujące branże / unikalne domeny
            state = indexes["company_state"].setdefault(
                matched_cid, {"industries": set(), "domains": set()}
            )
            missing_industries = [
                ind for ind in lead.ordered_industries
                if ind not in state["industries"]
            ]
            patch_values: dict[str, Any] = {}
            if missing_industries:
                merged_inds = [
                    ind for ind in SOUNDYOU_INDUSTRIES
                    if ind in (state["industries"] | set(missing_industries))
                ]
                patch_values[INDUSTRY_ATTR_SLUG] = merged_inds
            if safe_domains and not state["domains"]:
                patch_values["domains"] = safe_domains

            if patch_values:
                if not dry_run:
                    try:
                        client.request(
                            f"/objects/companies/records/{matched_cid}",
                            {"data": {"values": patch_values}},
                            method="PATCH",
                        )
                    except AttioAPIError as err:
                        logger.warning("PATCH company %s failed: %s", matched_cid, err)
                        stats[f"company_patch_error_{err.status}"] += 1
                        continue
                state["industries"].update(patch_values.get(INDUSTRY_ATTR_SLUG, []))
                state["domains"].update(patch_values.get("domains", []))
                for d in patch_values.get("domains", []):
                    indexes["companies_by_domain"][d] = matched_cid
                stats["updated_companies"] += 1
            else:
                stats["skipped_existing_companies"] += 1
            company_id = matched_cid
        else:
            # Nowa firma — wymagaj co najmniej 1 sygnału kontaktowego/tożsamościowego
            if not (lead.emails or lead.phones or safe_domains):
                stats["skipped_weak_identity"] += 1
                continue

            values = lead.company_values(safe_domains=safe_domains)
            if dry_run:
                company_id = f"dry-run-{stats['created_companies'] + 1}"
            else:
                try:
                    created = client.request(
                        "/objects/companies/records",
                        {"data": {"values": values}},
                        method="POST",
                    )
                except AttioAPIError as err:
                    logger.warning("POST company %r failed: %s", lead.name, err)
                    stats[f"company_create_error_{err.status}"] += 1
                    continue
                company_id = created["id"]["record_id"]

            # Zaktualizuj indeksy w pamięci, by uniknąć duplikatów w ramach tego samego przebiegu
            indexes["companies_by_name_city"][lead.identity] = company_id
            for d in safe_domains:
                indexes["companies_by_domain"][d] = company_id
            for em in lead.emails:
                indexes["companies_by_email"].setdefault(em, company_id)
            indexes["company_state"][company_id] = {
                "industries": set(lead.ordered_industries),
                "domains": set(safe_domains),
            }
            stats["created_companies"] += 1

        # Synchronizacja People dla każdego e-maila na leadzie
        if not sync_people or not lead.emails:
            continue

        primary_phone = sorted(lead.phones)[0] if lead.phones else ""
        for email in sorted(lead.emails):
            existing_person = indexes["people_by_email"].get(email)
            if existing_person:
                if not existing_person.get("company_id"):
                    if not dry_run:
                        try:
                            client.request(
                                f"/objects/people/records/{existing_person['record_id']}",
                                {
                                    "data": {
                                        "values": {
                                            "company": {
                                                "target_object": "companies",
                                                "target_record_id": company_id,
                                            }
                                        }
                                    }
                                },
                                method="PATCH",
                            )
                        except AttioAPIError as err:
                            logger.warning("PATCH person %s failed: %s", email, err)
                            stats[f"person_patch_error_{err.status}"] += 1
                            continue
                    existing_person["company_id"] = company_id
                    stats["linked_existing_people"] += 1
                else:
                    stats["skipped_existing_people"] += 1
                continue

            person_values: dict[str, Any] = {
                "email_addresses": [email],
                "company": {
                    "target_object": "companies",
                    "target_record_id": company_id,
                },
                "description": (
                    f"Źródło: Web-Crunchy-Scrap | Firma: {lead.name} | "
                    f"Branża: {', '.join(lead.ordered_industries)}"
                ),
            }
            if primary_phone:
                person_values["phone_numbers"] = [primary_phone]
            if lead.city or lead.region:
                person_values["primary_location"] = {
                    "locality": lead.city or None,
                    "region": lead.region or None,
                    "country_code": "PL",
                }

            if dry_run:
                person_id = f"dry-run-person-{stats['created_people'] + 1}"
            else:
                try:
                    created_p = client.request(
                        "/objects/people/records",
                        {"data": {"values": person_values}},
                        method="POST",
                    )
                except AttioAPIError as err:
                    logger.warning("POST person %s failed: %s", email, err)
                    stats[f"person_create_error_{err.status}"] += 1
                    continue
                person_id = created_p["id"]["record_id"]

            indexes["people_by_email"][email] = {
                "record_id": person_id,
                "company_id": company_id,
            }
            stats["created_people"] += 1

    return {
        "stats": dict(stats),
        "industry_counts": dict(industry_counts),
    }


def resolve_attio_token(explicit_token: str | None = None, allow_prompt: bool = True) -> str:
    token = (explicit_token or os.getenv("ATTIO_API_KEY") or "").strip()
    if token and token != "your_attio_api_key_here":
        return token
    if allow_prompt and sys.stdin.isatty():
        return getpass.getpass("Attio API token (ATTIO_API_KEY): ").strip()
    return ""


def sync_csv_to_attio(
    csv_path: str | Path = DEFAULT_CSV_PATH,
    *,
    token: str | None = None,
    dry_run: bool = False,
    offline_dry_run: bool = False,
    ensure_schema: bool = True,
    sync_people: bool = True,
) -> dict[str, Any]:
    """Główny punkt wejścia (wywoływany z CLI lub z `scraper.py --sync-attio`)."""
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"Plik CSV nie istnieje: {path}")

    leads, stats, domain_counts = load_and_dedup_csv([path])

    if offline_dry_run:
        industry_counts = Counter(ind for lead in leads for ind in lead.ordered_industries)
        return {
            "mode": "offline_dry_run",
            "csv": str(path),
            "stats": dict(stats),
            "industry_counts": dict(industry_counts),
        }

    api_token = resolve_attio_token(token, allow_prompt=True)
    if not api_token:
        raise RuntimeError(
            "Brak ATTIO_API_KEY (ustaw w pliku .env lub przekaż --token). "
            "Do testu bez połączenia użyj --offline-dry-run."
        )

    client = AttioClient(api_token)
    schema_summary = None
    if ensure_schema:
        schema_summary = ensure_soundyou_schema(client, dry_run=dry_run)

    result = sync_leads_to_attio(
        client,
        leads,
        stats,
        domain_counts,
        dry_run=dry_run,
        sync_people=sync_people,
    )
    return {
        "mode": "dry_run" if dry_run else "apply",
        "csv": str(path),
        "schema": schema_summary,
        **result,
    }


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(
        description="Synchronizacja leadów CSV ze scrapera wprost do Attio (Companies + People, branże SoundYou, deduplikacja)."
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=DEFAULT_CSV_PATH,
        help=f"Ścieżka do pliku CSV (domyślnie: {DEFAULT_CSV_PATH})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Połącz z Attio do odczytu/deduplikacji, ale nic nie zapisuj",
    )
    parser.add_argument(
        "--offline-dry-run",
        action="store_true",
        help="Tylko lokalna deduplikacja i klasyfikacja branż bez połączenia z Attio",
    )
    parser.add_argument(
        "--ensure-schema",
        action="store_true",
        help="Tylko sprawdź/utwórz atrybut `soundyou_industry` i opcje branż w Attio (bez importu CSV)",
    )
    parser.add_argument(
        "--no-ensure-schema",
        action="store_true",
        help="Pomiń krok sprawdzania schematu przed synchronizacją",
    )
    parser.add_argument(
        "--companies-only",
        action="store_true",
        help="Twórz/aktualizuj tylko `Companies`, bez rekordów `People`",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Token Attio API (nadpisuje ATTIO_API_KEY z .env)",
    )
    args = parser.parse_args()

    try:
        if args.ensure_schema:
            token = resolve_attio_token(args.token, allow_prompt=True)
            if not token:
                print("[ERROR] Brak ATTIO_API_KEY w .env lub --token.", file=sys.stderr)
                return 2
            client = AttioClient(token)
            summary = ensure_soundyou_schema(client, dry_run=args.dry_run)
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0

        report = sync_csv_to_attio(
            args.csv,
            token=args.token,
            dry_run=args.dry_run,
            offline_dry_run=args.offline_dry_run,
            ensure_schema=not args.no_ensure_schema,
            sync_people=not args.companies_only,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (FileNotFoundError, RuntimeError, AttioAPIError, URLError, OSError, csv.Error) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

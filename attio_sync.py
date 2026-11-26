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
from datetime import datetime, timezone
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
USERCHECK_API_BASE = "https://api.usercheck.com"
DEFAULT_CSV_PATH = Path(__file__).parent / "data" / "Raport" / "SoundYouLeads.csv"
VERIFY_CACHE_PATH = Path(__file__).parent / "data" / "Raport" / "email_verify_cache.json"
REPORTS_DIR = Path(__file__).parent / "logs"

INDUSTRY_ATTR_SLUG = "soundyou_industry"
INDUSTRY_ATTR_TITLE = "Branża SoundYou"

LEAD_STATUS_SLUG = "lead_status"
LEAD_STATUS_TITLE = "Status leada"
DEFAULT_NEW_LEAD_STATUS = "Nowy"
LEAD_STATUS_OPTIONS: tuple[str, ...] = (
    "Nowy",
    "Gotowy do kampanii",
    "Wysłano kampanię",
    "Zainteresowany",
    "Rozmowa / Demo",
    "Trial SoundYou",
    "Klient",
    "Były klient",
    "Odrzucony / DNC",
)

LEAD_BATCH_SLUG = "lead_batch"
LEAD_BATCH_TITLE = "Partia importu"

SOUNDYOU_PLAN_SLUG = "soundyou_plan"
SOUNDYOU_PLAN_TITLE = "Plan SoundYou"
SOUNDYOU_VALID_UNTIL_SLUG = "soundyou_valid_until"
SOUNDYOU_VALID_UNTIL_TITLE = "Subskrypcja do"

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
    "grota solna": ("SPA & Wellness",),
    "haloterapia": ("SPA & Wellness",),
    "normobaria": ("SPA & Wellness",),
    "komora hiperbaryczna": ("SPA & Wellness",),
    "biohacking": ("SPA & Wellness",),
    "longevity": ("SPA & Wellness",),
    "head spa": ("SPA & Wellness", "Kobido & Masaż"),
    "sanatorium": ("SPA & Wellness", "Hotele i Pensjonaty"),
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
    "ajurweda": ("Kobido & Masaż",),
    "ayurveda": ("Kobido & Masaż",),
    "refleksologia": ("Kobido & Masaż",),
    "akupunktura": ("Kobido & Masaż",),
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
    "podologia": ("Beauty / Salon Urody", "Poczekalnia / Gabinet"),
    "trychologia": ("Beauty / Salon Urody",),
    "depilacja laserowa": ("Beauty / Salon Urody",),
    "makijaz permanentny": ("Beauty / Salon Urody",),
    "makijaż permanentny": ("Beauty / Salon Urody",),
    "endermologia": ("Beauty / Salon Urody",),
    "tattoo": ("Beauty / Salon Urody",),
    "studio tatuazu": ("Beauty / Salon Urody",),
    "studio tatuażu": ("Beauty / Salon Urody",),
    "tatuaz": ("Beauty / Salon Urody",),
    "tatuaż": ("Beauty / Salon Urody",),
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
    "glamping": ("Hotele i Pensjonaty",),
    "agroturystyka": ("Hotele i Pensjonaty",),
    "dom weselny": ("Hotele i Pensjonaty",),
    "sala bankietowa": ("Hotele i Pensjonaty",),
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
    "wine bar": ("Restauracje",),
    "winiarnia": ("Restauracje",),
    "cocktail bar": ("Restauracje",),
    "ramen": ("Restauracje",),
    "brunch": ("Restauracje",),
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
    "lodziarnia": ("Kawiarnie",),
    "gelateria": ("Kawiarnie",),
    "herbaciarnia": ("Kawiarnie",),
    "matcha": ("Kawiarnie",),
    "boulangerie": ("Kawiarnie",),
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
    "trening ems": ("Fitness / Siłownia",),
    "studio ems": ("Fitness / Siłownia",),
    "squash": ("Fitness / Siłownia",),
    "padel": ("Fitness / Siłownia",),
    "boulderownia": ("Fitness / Siłownia",),
    "scianka wspinaczkowa": ("Fitness / Siłownia",),
    "ścianka wspinaczkowa": ("Fitness / Siłownia",),
    "szkola tanca": ("Fitness / Siłownia",),
    "szkoła tańca": ("Fitness / Siłownia",),
    "pole dance": ("Fitness / Siłownia",),
    "sztuki walki": ("Fitness / Siłownia",),
    # 8. Joga / Pilates / Medytacja
    "joga": ("Joga / Pilates / Medytacja",),
    "studio jogi": ("Joga / Pilates / Medytacja",),
    "yoga": ("Joga / Pilates / Medytacja",),
    "yoga studio": ("Joga / Pilates / Medytacja",),
    "pilates": ("Joga / Pilates / Medytacja",),
    "reformer pilates": ("Joga / Pilates / Medytacja",),
    "meditation": ("Joga / Pilates / Medytacja",),
    "medytacja": ("Joga / Pilates / Medytacja",),
    "mindfulness": ("Joga / Pilates / Medytacja",),
    "stretching": ("Joga / Pilates / Medytacja",),
    "barre": ("Joga / Pilates / Medytacja",),
    "sound healing": ("Joga / Pilates / Medytacja",),
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
    "weterynarz": ("Poczekalnia / Gabinet",),
    "klinika weterynaryjna": ("Poczekalnia / Gabinet",),
    "przychodnia weterynaryjna": ("Poczekalnia / Gabinet",),
    "veterinary": ("Poczekalnia / Gabinet",),
    "psychoterapia": ("Poczekalnia / Gabinet",),
    "psycholog": ("Poczekalnia / Gabinet",),
    "dietetyk": ("Poczekalnia / Gabinet",),
    "logopeda": ("Poczekalnia / Gabinet",),
    "kancelaria": ("Poczekalnia / Gabinet",),
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
    "salon sukien slubnych": ("Sklep / Retail",),
    "salon sukien ślubnych": ("Sklep / Retail",),
    "salon optyczny": ("Sklep / Retail", "Poczekalnia / Gabinet"),
    "optyk": ("Sklep / Retail", "Poczekalnia / Gabinet"),
    "perfumeria": ("Sklep / Retail",),
    "jubiler": ("Sklep / Retail",),
    "kwiaciarnia": ("Sklep / Retail",),
    # 12. Lokal Usługowy / Inne
    "inne": ("Lokal Usługowy / Inne",),
    "other": ("Lokal Usługowy / Inne",),
    "service": ("Lokal Usługowy / Inne",),
    "services": ("Lokal Usługowy / Inne",),
    "local business": ("Lokal Usługowy / Inne",),
    "lokal usługowy": ("Lokal Usługowy / Inne",),
    "lokal uslugowy": ("Lokal Usługowy / Inne",),
    "lokal usługowy / inne": ("Lokal Usługowy / Inne",),
    "coworking": ("Lokal Usługowy / Inne",),
    "biuro serwisowane": ("Lokal Usługowy / Inne",),
    "bawialnia": ("Lokal Usługowy / Inne",),
    "sala zabaw": ("Lokal Usługowy / Inne",),
    "kregielnia": ("Lokal Usługowy / Inne",),
    "kręgielnia": ("Lokal Usługowy / Inne",),
    "galeria sztuki": ("Lokal Usługowy / Inne",),
    "groomer": ("Lokal Usługowy / Inne",),
}

# Alias wsteczny
_RAW_INDUSTRY_MAP = INDUSTRY_EN_PL_MAP

# Wzorce wzbogacające multiselect na podstawie nazwy firmy i domeny/strony (PL + EN + branże pokrewne)
_SIGNAL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "Hotele i Pensjonaty",
        re.compile(
            r"\b(hotel\w*|resort\w*|pensjonat\w*|aparthotel\w*|guesthouse|guest\s+house|b&b|bed\s+and\s+breakfast|palac\w*|pałac\w*|dwor\w*|dwór|zamek|villa|willa|nocleg\w*|hostel\w*|glamping\w*|agroturystyk\w*|dom\s+weseln\w*|sal[ai]\s+bankietow\w*)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "SPA & Wellness",
        re.compile(
            r"\b(spa|day\s*spa|head\s*spa|wellness|odnow\w*\s+biologiczn\w*|float\w*|saun\w*|term[ay]|thermal|balneolog\w*|hammam|aromaterap\w*|aromatherapy|grot\w*\s+soln\w*|haloterapi\w*|normobari\w*|hiperbaryczn\w*|biohacking|longevity|łaźni\w*|lazni\w*|bania\s+ruska|kriokomor\w*|sanatori\w*)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Kobido & Masaż",
        re.compile(
            r"\b(kobido|masaz\w*|masaż\w*|massage\w*|tajsk\w*|thai\s+massage|balijsk\w*|balinese|lomi|bodywork|shiatsu|refleksologi\w*|reflexology|ajurwed\w*|ayurved\w*|akupunktur\w*|head\s*spa)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Beauty / Salon Urody",
        re.compile(
            r"\b(beauty|urod\w*|kosmetolog\w*|kosmetycz\w*|cosmet\w*|estetic\w*|aesthetic\w*|medycyn\w*\s+estetyczn\w*|manicure|pedicure|nail\w*|paznokci\w*|depilacj\w*|brow\w*|lash\w*|rzęs\w*|rzes\w*|makija[żz]\w*|makeup|podolog\w*|trycholog\w*|endermologi\w*|tatua[żz]\w*|tattoo\w*|piercing\w*)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Restauracje",
        re.compile(
            r"\b(restaurac\w*|restaurant\w*|bistro|trattoria|pizzeri\w*|sushi|karczm\w*|ober[żz]\w*|gastronomi\w*|gastronomy|steakhouse|tapas|fine\s+dining|wine\s*bar\w*|winiarni\w*|cocktail\s*bar\w*|multitap\w*|ramen|brunch|shisha\s+lounge)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Kawiarnie",
        re.compile(
            r"\b(kawiarni\w*|coffee\w*|caf[eé]|caffe|espresso|palarni\w*\s+kaw\w*|cukierni\w*|piekarni\w*|bakery|patisserie|boulangerie|tea\s+house|herbaciarni\w*|lodziarni\w*|gelateri\w*|matcha\s+bar)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Fitness / Siłownia",
        re.compile(
            r"\b(fitness|gym|siłowni\w*|silowni\w*|crossfit|trening\w*\s+(?:personaln\w*|ems)|studio\s+ems|personal\s+training|health\s+club|workout|squash\w*|padel\w*|boulderowni\w*|wspinaczk\w*|szkoł\w*\s+tańc\w*|szkol\w*\s+tanc\w*|pole\s+dance|sztuk\w*\s+walki)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Joga / Pilates / Medytacja",
        re.compile(
            r"\b(jog[ai]|yoga|pilates|reformer|medytacj\w*|meditation|mindfulness|ashtanga|vinyasa|hatha|barre|stretching|sound\s+healing|breathwork)\b",
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
            r"\b(fizjoterap\w*|physiotherap\w*|physical\s+therapy|fizjo\w*|rehabilitac\w*|rehab\w*|osteopat\w*|kinezyterap\w*|terapi\w*\s+manualn\w*|stomatolog\w*|dentyst\w*|dentist\w*|dental|klinik\w*|clinic|przychodni\w*|poczekalni\w*|waiting\s+room|chiropract\w*|weterynar\w*|veterinary|psychoterap\w*|psycholog\w*|logoped\w*|dietetyk\w*|optometryst\w*|protetyk\w*\s+słuchu)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Sklep / Retail",
        re.compile(
            r"\b(sklep\w*|butik\w*|boutique|showroom|concept\s+store|retail|store|sukien\s+[śs]lubn\w*|salon\s+optyczn\w*|optyk\w*|perfumeri\w*|jubiler\w*|kwiaciarni\w*|delikates\w*|ksi[ęe]garni\w*)\b",
            re.IGNORECASE,
        ),
    ),
]

_PET_GROOMING_RE = re.compile(
    r"\b(psi\w*\s+(?:spa|fryzjer\w*|salon\w*)|salon\s+(?:dla\s+)?ps[óo]w|groomer\w*|grooming\s+ps[óo]w|strzy[żz]enie\s+ps[óo]w)\b",
    re.IGNORECASE,
)

EMAIL_RE = re.compile(r"[\w.!#$%&'*+/=?^`{|}~-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+", re.I | re.ASCII)

PLATFORM_DOMAINS = frozenset({
    "booksy.com", "booksy.net", "fresha.com", "freshastatus.com", "facebook.com", "fb.com", "instagram.com", "tiktok.com",
    "google.com", "maps.app.goo.gl", "linktr.ee", "wixsite.com", "wordpress.com",
    "business.site", "goo.gl", "youtube.com", "linkedin.com", "znanylekarz.pl",
    "panoramafirm.pl", "pkt.pl", "fixly.pl", "oferteo.pl", "firmy.net", "aleo.com",
})

FREE_EMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "wp.pl", "o2.pl", "onet.pl", "op.pl",
    "poczta.onet.pl", "onet.eu", "vp.pl", "interia.pl", "interia.eu",
    "poczta.fm", "tlen.pl", "gazeta.pl", "yahoo.com", "yahoo.pl",
    "hotmail.com", "outlook.com", "live.com", "msn.com", "icloud.com",
    "me.com", "proton.me", "protonmail.com", "aol.com", "gmx.com",
    "home.pl", "nazwa.pl", "lh.pl", "cyberfolks.pl", "ovh.pl",
})

GENERIC_EMAIL_PREFIXES = frozenset({
    "kontakt", "biuro", "recepcja", "info", "rezerwacja", "rezerwacje",
    "booking", "office", "hello", "hej", "halo", "mail", "poczta",
    "administracja", "sekretariat", "bok", "obsługa", "obsluga", "sklep",
    "zamowienia", "zamówienia", "marketing", "manager", "kontaktowy",
})

UNCLEAR_EXACT_NAMES = frozenset({
    "", "brak", "brak nazwy", "bez nazwy", "unknown", "unnamed", "untitled",
    "none", "null", "n a", "na", "test",
    "strona glowna", "strona główna", "home", "homepage", "home page",
    "index", "welcome", "witamy", "start", "oferta", "kontakt", "cennik",
    "o nas", "rezerwacja", "rezerwacje", "kalendarz", "uslugi", "usługi",
    "moja strona", "twoja strona", "nowa strona", "wordpress", "wixsite",
    "mysite", "website", "strona internetowa", "wizytowka", "wizytówka",
    "403 forbidden", "404 not found", "attention required", "just a moment",
    # Samotne nazwy kategorii bez marki własnej
    "spa", "day spa", "wellness", "spa wellness", "masaz", "masaż",
    "gabinet masazu", "gabinet masażu", "salon masazu", "salon masażu",
    "kobido", "uroda", "beauty", "salon urody", "salon kosmetyczny",
    "gabinet kosmetyczny", "kosmetyczka", "kosmetologia",
    "fryzjer", "salon fryzjerski", "barber", "barbershop",
    "hotel", "pensjonat", "restauracja", "kawiarnia", "bistro",
    "fitness", "silownia", "siłownia", "joga", "yoga", "pilates",
    "fizjoterapia", "rehabilitacja", "gabinet", "klinika", "przychodnia",
    "poczekalnia", "sklep", "lokal uslugowy", "lokal usługowy",
})

DIRECTORY_SOCIAL_HANDLES = frozenset({
    "booksy", "booksypl", "booksybiz", "booksy.polska", "booksy_polska",
    "fresha", "freshacom", "panoramafirm", "pktpl", "znanylekarz",
    "fixlypl", "oferteo", "firmynet", "aleopl", "wix", "wordpress",
    "elementor", "shoper", "idosell", "woocommerce", "prestashop",
    "squarespace", "webflow", "canva", "meta", "instagram", "facebook",
})

IG_SKIP_SEGMENTS = frozenset({
    "p", "reel", "reels", "stories", "explore", "accounts", "direct",
    "tv", "tags", "about", "legal", "developer", "directory", "ar",
})

FB_SKIP_SEGMENTS = frozenset({
    "sharer", "sharer.php", "share", "share.php", "dialog", "plugins",
    "events", "groups", "watch", "photo", "photo.php", "photos", "videos",
    "login", "login.php", "policies", "privacy", "help", "tr", "ads",
    "marketplace", "gaming", "reel", "reels", "stories", "hashtag",
    "permalink.php", "story.php", "l.php",
})

# Słowa branżowe do inteligentnego rozklejania sklejonych nazw z domen/uchwytów (np. "enklawaspa" -> "Enklawa Spa")
_BRAND_SPLIT_TOKENS = (
    "wellness", "massage", "masaze", "masaż", "masaz", "kobido", "beauty",
    "estetica", "estetyka", "kosmetyka", "kosmetologia", "medispa", "dayspa",
    "studiospa", "thaispa", "balispa", "orientspa", "floatspa", "greenspa",
    "hotel", "resort", "palac", "zamek", "dwor", "villa", "willa", "pensjonat",
    "restauracja", "restaurant", "bistro", "kawiarnia", "coffee", "cafe",
    "fitness", "silownia", "crossfit", "pilates", "yoga", "joga",
    "barbershop", "barber", "fryzjer", "hair", "klinika", "clinic",
    "fizjo", "rehab", "studio", "salon", "instytut", "centrum", "strefa",
    "ogrod", "kraina", "kopalnia", "fabryka", "akademia", "dom", "przystan",
    "zakatek", "oaza", "harmonia", "relaksu", "relaks", "relax", "odnowa",
    "urody", "uroda", "ciala", "twarzy", "zdrowia", "termy", "sauna", "spa",
    "wroclaw", "krakow", "warszawa", "poznan", "gdansk", "gdynia", "sopot",
    "katowice", "lodz", "szczecin", "lublin", "bialystok", "rzeszow", "opole",
    "slask", "polska",
)

PLATFORM_EMAILS = frozenset({
    "aneksy@booksy.com", "pomoc.pl@booksy.com", "sprzedaz@booksy.com",
    "info.pl@booksy.com", "reklamacje@booksy.com", "info@booksy.com",
    "support@booksy.com", "hello@booksy.com",
    "support@fresha.com", "no-reply@fresha.com", "hello@fresha.com",
})

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".bmp")

# Literówki w końcówkach TLD oraz techniczne rozszerzenia plików mylone z e-mailami
TYPO_OR_FILE_TLDS = frozenset({
    "coml", "con", "cmo", "comm", "coom", "cm", "om",
    "pll", "plll", "p", "euu", "nett", "orgg", "inf",
    "local", "localhost", "invalid", "test", "example",
    "html", "htm", "php", "asp", "aspx", "js", "css", "json", "xml", "pdf", "txt",
})

# Częste literówki popularnych domen pocztowych + domeny testowe / jednorazowe (disposable)
BLOCKED_EMAIL_DOMAINS = frozenset({
    "gmai.com", "gmial.com", "gamil.com", "gnail.com", "gmal.com", "gmail.con", "gmail.coml", "gmail.co",
    "hotmial.com", "outlok.com", "yaho.com", "wp.pll", "o2.pll", "onet.pll", "interia.pll",
    "example.com", "example.org", "example.net", "example.pl",
    "test.com", "test.pl", "domain.com", "domena.pl", "twojadomena.pl", "yourdomain.com",
    "sentry.io", "wixpress.com",
    "mailinator.com", "tempmail.com", "10minutemail.com", "guerrillamail.com",
    "yopmail.com", "sharklasers.com", "trashmail.com", "dispostable.com",
})

# Filtr anty-ICP: wyklucza firmy ewidentnie spoza docelowych lokali usługowych SoundYou
NON_ICP_PATTERNS = re.compile(
    r"\b("
    r"auto[\s\-]*spa|car[\s\-]*spa|auto[\s\-]*detailing|car[\s\-]*detailing|"
    r"myjni\w*\s+samochod\w*|warsztat\w*\s+samochod\w*|wulkanizacj\w*|mechanik\w*\s+pojazd\w*|"
    r"pralni\w*\s+chemiczn\w*|pralni\w*\s+przemyslow\w*|multiwash|"
    r"software\s+house|agencj\w*\s+(?:interaktywn\w*|marketingow\w*|seo|reklamow\w*)|"
    r"tworzeni\w*\s+stron|website\s+builder|hosting\w*|rejestracj\w*\s+domen|"
    r"spedycj\w*|transport\s+miedzynarodow\w*|hurtowni\w*\s+stali|"
    r"erotic\s+massage|masa[żz]\w*\s+erotyczn\w*|nuru\s+massage|masa[żz]\s+nuru|body\s+to\s+body|"
    r"producent\w*\s+saun\w*|budow\w*\s+saun\w*|sprzeda[żz]\s+(?:saun|wanien\s+spa)|"
    r"hydromasa[żz]e|sauny,\s*[łl]a[źz]nie,\s*kominki|kominki"
    r")\b",
    re.IGNORECASE,
)

NON_ICP_DOMAINS = frozenset({
    "wenet.pl", "bowwe.com", "webador.com", "domena.pl", "multiwash.pl",
    "home.pl", "nazwa.pl", "cyberfolks.pl", "ovh.pl", "unionalpha.com",
    "hydromasaze.com",
})

CORE_VENUE_GUARD_RE = re.compile(
    r"\b(hotel\w*|pensjonat\w*|restaurac\w*|kawiarni\w*|bistro|fryzjer\w*|barber\w*|"
    r"kobido|masaż\w*|masaz\w*|jog[ai]|yoga|pilates|siłowni\w*|silowni\w*|"
    r"salon\s+urody|kosmetycz\w*|kosmetolog\w*|odnow\w*\s+biologiczn\w*|termy)\b",
    re.IGNORECASE,
)

AUTO_WASH_RE = re.compile(r"\b(auto[\s\-]*spa|car[\s\-]*spa|detailing|myjni\w*)\b", re.IGNORECASE)
HARD_NON_ICP_RE = re.compile(
    r"\b(erotic\s+massage|masa[żz]\w*\s+erotyczn\w*|nuru\s+massage|masa[żz]\s+nuru|body\s+to\s+body|"
    r"producent\w*\s+saun\w*|budow\w*\s+saun\w*|hydromasa[żz]e|kominki)\b",
    re.IGNORECASE,
)


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


def validate_email_syntax(raw: str) -> tuple[bool, str]:
    """Sprawdza składnię e-maila, odrzuca obrazy, konta katalogów, literówki TLD (.coml) i disposable."""
    e = (raw or "").strip().lower()
    if not e or len(e) > 254 or "@" not in e:
        return False, "invalid_syntax"
    if not EMAIL_RE.fullmatch(e):
        return False, "invalid_syntax"
    if e.endswith(IMAGE_EXTENSIONS):
        return False, "image_filename"
    if e in PLATFORM_EMAILS:
        return False, "platform_email"
    local_part, domain_part = e.rsplit("@", 1)
    if not local_part or local_part.startswith(".") or local_part.endswith(".") or ".." in e:
        return False, "invalid_syntax"
    if any(domain_part == p or domain_part.endswith(f".{p}") for p in PLATFORM_DOMAINS):
        return False, "platform_domain"
    if domain_part in BLOCKED_EMAIL_DOMAINS:
        return False, "blocked_or_typo_domain"
    tld = domain_part.rsplit(".", 1)[-1]
    if len(tld) < 2 or not tld.isalpha() or tld in TYPO_OR_FILE_TLDS:
        return False, f"typo_tld:{tld}"
    return True, ""


def extract_emails(text: str, rejected_log: list[dict[str, str]] | None = None) -> set[str]:
    """Wyciąga poprawne, wysyłalne adresy e-mail z komórki CSV lub opisu."""
    found: set[str] = set()
    for email in EMAIL_RE.findall(text or ""):
        e = email.strip().lower()
        ok, reason = validate_email_syntax(e)
        if not ok:
            if rejected_log is not None and reason not in ("platform_email", "platform_domain", "image_filename"):
                rejected_log.append({"email": e, "reason": reason})
            continue
        found.add(e)
    return found


def is_non_icp_business(name: str = "", website: str = "", raw_industry: str = "") -> tuple[bool, str]:
    """Wykrywa firmy spoza docelowego ICP SoundYou (np. Auto SPA / detailing, erotyka, producenci saun, agencje IT/SEO, pralnie)."""
    dom = business_domain(website)
    if dom in NON_ICP_DOMAINS:
        return True, f"non_icp_domain:{dom}"
    blob = f"{name or ''} {dom} {raw_industry or ''}".strip()
    if AUTO_WASH_RE.search(blob):
        return True, "auto_wash_or_detailing"
    m_hard = HARD_NON_ICP_RE.search(blob)
    if m_hard:
        return True, f"hard_non_icp:{m_hard.group(0).lower()}"
    m = NON_ICP_PATTERNS.search(blob)
    if m and not CORE_VENUE_GUARD_RE.search(name or ""):
        return True, f"non_icp_keyword:{m.group(0).lower()}"
    return False, ""


def load_email_verify_cache(cache_path: Path | None = None) -> dict[str, dict[str, Any]]:
    target = cache_path or VERIFY_CACHE_PATH
    if not target.exists():
        return {}
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_email_verify_cache(cache: dict[str, dict[str, Any]], cache_path: Path | None = None) -> None:
    target = cache_path or VERIFY_CACHE_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("Nie udało się zapisać cache UserCheck %s: %s", target, exc)


def verify_email_usercheck(
    email: str,
    api_key: str,
    *,
    cache: dict[str, dict[str, Any]] | None = None,
    pace_seconds: float = 1.05,
) -> tuple[bool, str]:
    """Weryfikuje dostarczalność e-maila przez UserCheck API (MX, disposable, blocklisted, spam) z lokalnym cache."""
    e = email.strip().lower()
    ok_syn, syn_reason = validate_email_syntax(e)
    if not ok_syn:
        return False, syn_reason

    if cache is not None and e in cache:
        cached = cache[e]
        return bool(cached.get("ok", True)), str(cached.get("reason", ""))

    if not api_key or api_key == "your_usercheck_api_key_here":
        return True, ""

    req = Request(
        f"{USERCHECK_API_BASE}/email/{e}",
        method="GET",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "SoundYou-CRM-Sync/1.0",
        },
    )
    opener = build_opener(_NoRedirect)
    for attempt in range(3):
        if pace_seconds > 0:
            time.sleep(pace_seconds)
        try:
            with opener.open(req, timeout=10) as resp:
                data = json.load(resp)
        except HTTPError as err:
            code = err.code
            err.close()
            if code == 429 and attempt < 2:
                time.sleep(2 ** (attempt + 1))
                continue
            return True, ""
        except (URLError, OSError, ValueError):
            return True, ""

        ok = True
        reason = ""
        if not data.get("mx", True):
            ok, reason = False, "no_mx"
        elif data.get("disposable"):
            ok, reason = False, "disposable"
        elif data.get("blocklisted"):
            ok, reason = False, "blocklisted"
        elif data.get("spam"):
            ok, reason = False, "spam"

        if cache is not None:
            cache[e] = {
                "ok": ok,
                "reason": reason,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
        return ok, reason

    return True, ""


def normalize_phone_e164(raw: str) -> str:
    """Normalizuje polski numer telefonu do formatu +48XXXXXXXXX (lub pusty string)."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("48"):
        digits = digits[2:]
    if len(digits) == 9:
        return f"+48{digits}"
    return ""


PHONE_EXTRACT_RE = re.compile(r"(?:\+?48[\s.\-]?)?(?:\d[\s.\-]?){8}\d")
POSTCODE_RE = re.compile(r"\b\d{2}-\d{3}\b")


def extract_phones_e164(text: str) -> set[str]:
    """Wyciąga wszystkie polskie numery telefonów (+48XXXXXXXXX) z tekstu/opisu."""
    found: set[str] = set()
    for m in PHONE_EXTRACT_RE.findall(text or ""):
        norm = normalize_phone_e164(m)
        if norm:
            found.add(norm)
    return found


def extract_postcode(text: str) -> str:
    m = POSTCODE_RE.search(text or "")
    return m.group(0) if m else ""


SOCIAL_URL_RE = re.compile(
    r"https?://(?:www\.|m\.|pl-pl\.)?(?:instagram\.com|facebook\.com|fb\.com)/[^\s\"'<>)\]]+",
    re.IGNORECASE,
)


def normalize_instagram_url(raw: str) -> str:
    """Zwraca kanoniczny URL profilu Instagram (`https://www.instagram.com/<handle>`) lub pusty string."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    if "instagram.com" not in raw.lower():
        return ""
    try:
        u = urlsplit(raw if "://" in raw else f"https://{raw}")
    except ValueError:
        return ""
    host = (u.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    if host != "instagram.com":
        return ""
    parts = [p for p in (u.path or "").split("/") if p]
    if not parts:
        return ""
    handle = parts[0].lstrip("@").strip()
    low = handle.lower()
    if low in IG_SKIP_SEGMENTS or low in DIRECTORY_SOCIAL_HANDLES:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9._]{2,40}", handle):
        return ""
    return f"https://www.instagram.com/{handle}"


def normalize_facebook_url(raw: str) -> str:
    """Zwraca kanoniczny URL strony/profilu Facebook lub pusty string."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    if not any(d in raw.lower() for d in ("facebook.com", "fb.com")):
        return ""
    try:
        u = urlsplit(raw if "://" in raw else f"https://{raw}")
    except ValueError:
        return ""
    host = (u.hostname or "").lower()
    for prefix in ("www.", "m.", "web.", "pl-pl.", "touch."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    if host not in ("facebook.com", "fb.com"):
        return ""
    parts = [p for p in (u.path or "").split("/") if p]
    if not parts:
        return ""
    first_low = parts[0].lower()
    if first_low in FB_SKIP_SEGMENTS or first_low in DIRECTORY_SOCIAL_HANDLES:
        return ""
    if first_low == "profile.php":
        m_id = re.search(r"(?:^|&)id=(\d{5,})", u.query or "")
        return f"https://www.facebook.com/profile.php?id={m_id.group(1)}" if m_id else ""
    if first_low in ("people", "pages", "p") and len(parts) >= 2:
        slug_part = "/".join(parts[:3]) if len(parts) >= 3 and parts[2].isdigit() else "/".join(parts[:2])
        if parts[1].lower() in DIRECTORY_SOCIAL_HANDLES:
            return ""
        return f"https://www.facebook.com/{slug_part}"
    slug = parts[0].strip()
    if len(slug) < 2 or slug.lower() in DIRECTORY_SOCIAL_HANDLES:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9._\-]{2,80}", slug):
        return ""
    return f"https://www.facebook.com/{slug}"


def extract_social_links(*texts: str) -> dict[str, str]:
    """Wyciąga pierwsze poprawne linki `facebook` i `instagram` z przekazanych URL-i lub tekstu HTML."""
    fb = ""
    ig = ""
    for text in texts:
        if not text:
            continue
        candidates = [text.strip()] + SOCIAL_URL_RE.findall(text)
        for cand in candidates:
            if not ig:
                ig = normalize_instagram_url(cand)
            if not fb:
                fb = normalize_facebook_url(cand)
            if fb and ig:
                return {"facebook": fb, "instagram": ig}
    return {"facebook": fb, "instagram": ig}


def _humanize_slug(slug: str) -> str:
    """Zamienia identyfikator z domeny, uchwytu IG/FB lub local-part e-maila na czytelną nazwę własną."""
    s = (slug or "").strip().lstrip("@")
    if not s:
        return ""
    # Usuń końcowe numeryczne ID stron z Facebooka (np. "salon-urody-102938475610293")
    s = re.sub(r"[-_]\d{6,}$", "", s)
    # Rozdziel CamelCase (np. "SalonUrodyMagnolia" -> "Salon Urody Magnolia")
    s = re.sub(r"([a-z0-9ąćęłńóśźż])([A-ZĄĆĘŁŃÓŚŹŻ])", r"\1 \2", s)
    # Zamień kropki, myśliwki, podkreślenia i plusy na spacje
    s = re.sub(r"[._\-+/]+", " ", s)
    # Usuń wiszące 1-4 cyfrowe sufiksy na końcu tokenu (np. "ladna ja2024" -> "ladna ja", "spa4" -> "spa")
    tokens: list[str] = []
    for raw_tok in s.split():
        tok = re.sub(r"(?<=[A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż])\d{1,4}$", "", raw_tok)
        if tok.isdigit() and len(tok) >= 4:
            continue
        # Rozklej długie sklejone słowa wokół znanych rdzeni branżowych (np. "enklawaspa" -> "enklawa spa")
        low_tok = tok.lower()
        if len(low_tok) >= 7 and low_tok.isalpha():
            for kw in _BRAND_SPLIT_TOKENS:
                if low_tok != kw and len(kw) >= 3:
                    if low_tok.endswith(kw) and len(low_tok) - len(kw) >= 3:
                        prefix = tok[: len(tok) - len(kw)]
                        suffix = tok[len(tok) - len(kw) :]
                        tok = f"{prefix} {suffix}"
                        break
                    if low_tok.startswith(kw) and len(low_tok) - len(kw) >= 3:
                        prefix = tok[: len(kw)]
                        suffix = tok[len(kw) :]
                        tok = f"{prefix} {suffix}"
                        break
        tokens.extend(tok.split())

    cleaned: list[str] = []
    UPPER_WORDS = {"spa", "vip", "med", "fit", "pro", "art", "bio", "eco"}
    LOWER_CONNECTORS = {"i", "w", "z", "na", "nad", "pod", "przy", "and", "of", "by", "the"}
    for idx, t in enumerate(tokens):
        tl = t.lower()
        if not any(c.isalpha() for c in t):
            continue
        if tl in UPPER_WORDS:
            cleaned.append(tl.upper())
        elif idx > 0 and tl in LOWER_CONNECTORS:
            cleaned.append(tl)
        else:
            cleaned.append(t[0].upper() + t[1:].lower() if t.islower() or t.isupper() else t)

    result = " ".join(cleaned).strip()
    if len(result) < 3 or normalized(result) in UNCLEAR_EXACT_NAMES:
        return ""
    return result


def _name_from_social_url(social_url: str) -> str:
    if not social_url:
        return ""
    try:
        u = urlsplit(social_url if "://" in social_url else f"https://{social_url}")
    except ValueError:
        return ""
    parts = [p for p in (u.path or "").split("/") if p]
    if not parts or parts[0].lower() == "profile.php":
        return ""
    if parts[0].lower() in ("people", "pages", "p") and len(parts) >= 2:
        candidate = parts[1]
    else:
        candidate = parts[0]
    from urllib.parse import unquote
    return _humanize_slug(unquote(candidate))


def _name_from_domain(domain_or_url: str) -> str:
    dom = business_domain(domain_or_url)
    if not dom:
        return ""
    # Usuń popularne polskie i globalne końcówki TLD/regionalne
    stem = re.sub(
        r"\.(?:com\.pl|net\.pl|org\.pl|info\.pl|biz\.pl|waw\.pl|wroclaw\.pl|krakow\.pl|"
        r"poznan\.pl|gda\.pl|gdansk\.pl|slask\.pl|katowice\.pl|lodz\.pl|szczecin\.pl|"
        r"lublin\.pl|rzeszow\.pl|opole\.pl|zakopane\.pl|karpacz\.pl|kolobrzeg\.pl|"
        r"pl|com|eu|net|org|info|biz|online|site|website|co)$",
        "",
        dom,
        flags=re.IGNORECASE,
    )
    # Jeśli TLD samo w sobie niesie branżę (np. harmonia.spa, lotus.studio), zachowaj je!
    return _humanize_slug(stem)


def _name_from_emails(emails: Iterable[str]) -> str:
    email_list = sorted({(e or "").strip().lower() for e in emails if e and "@" in e})
    # 1. Najpierw szukaj własnej domeny firmowej w adresie e-mail (np. recepcja@hotelmarta.com.pl -> Hotel Marta)
    for em in email_list:
        _, dom = em.rsplit("@", 1)
        if dom not in FREE_EMAIL_DOMAINS and dom not in PLATFORM_DOMAINS:
            from_dom = _name_from_domain(dom)
            if from_dom:
                return from_dom
    # 2. Dla darmowych skrzynek (@gmail.com, @wp.pl itd.) wyciągnij nazwę z części przed '@'
    for em in email_list:
        local, _ = em.rsplit("@", 1)
        parts = [
            p for p in re.split(r"[._\-+]+", local)
            if p and p.lower() not in GENERIC_EMAIL_PREFIXES and not p.isdigit()
        ]
        if not parts:
            continue
        candidate = _humanize_slug(" ".join(parts))
        if candidate and normalized(candidate) not in GENERIC_EMAIL_PREFIXES:
            return candidate
    return ""


def is_unclear_company_name(name: str) -> bool:
    """Zwraca True, gdy nazwa firmy ze scrapera jest pusta, generyczna, techniczna lub nieczytelna."""
    clean = strip_emoji(name or "").strip()
    if len(clean) < 3 or not any(c.isalpha() for c in clean):
        return True
    low = clean.lower()
    if low.startswith(("http://", "https://", "www.")) or "@" in low:
        return True
    # Tymczasowe inicjały typu "R.", "L. · 7958970a"
    if re.fullmatch(r"[A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż]{1,2}\.?(?:\s*[·\-]\s*[0-9a-f]{6,})?", clean):
        return True
    norm = normalized(clean)
    if norm in UNCLEAR_EXACT_NAMES:
        return True
    return False


def resolve_company_name(
    raw_name: str = "",
    *,
    site_title: str = "",
    website: str = "",
    facebook: str = "",
    instagram: str = "",
    emails: Iterable[str] = (),
    city: str = "",
) -> str:
    """Automatycznie ustala najlepszą nazwę firmy z dostępnych danych leada.

    Jeśli `raw_name` jest podana jasno, zwraca oczyszczoną `raw_name`.
    Gdy `raw_name` jest pusta lub niejasna (np. 'Strona główna', 'Salon', URL, brak),
    kolejno próbuje wyznaczyć nazwę z:
      1) tytułu strony WWW (`og:site_name` / `<title>`),
      2) linku Facebook / Instagram (nazwa profilu / handle),
      3) domeny strony WWW,
      4) adresu e-mail (domena własna lub część przed `@` dla gmail/wp/o2).
    """
    clean_raw = strip_emoji(raw_name or "").strip()
    if not is_unclear_company_name(clean_raw):
        return clean_raw

    # 1. Tytuł witryny WWW (np. og:site_name lub oczyszczony <title>)
    clean_title = strip_emoji(site_title or "").strip()
    if clean_title and not is_unclear_company_name(clean_title) and len(clean_title) <= 80:
        return clean_title

    # 2. Social media (Facebook często ma pełniejszy slug nazwy niż IG; sprawdzamy oba)
    socials = extract_social_links(facebook, instagram, website)
    for social_key in ("facebook", "instagram"):
        inferred = _name_from_social_url(socials.get(social_key, ""))
        if inferred and not is_unclear_company_name(inferred):
            return inferred

    # 3. Domena strony WWW
    from_web = _name_from_domain(website)
    if from_web and not is_unclear_company_name(from_web):
        return from_web

    # 4. Adres e-mail (domena firmowa lub local-part na darmowej poczcie)
    from_email = _name_from_emails(emails)
    if from_email and not is_unclear_company_name(from_email):
        return from_email

    # 5. Fallback: jeśli była ogólna kategoria (np. "Gabinet masażu") + miasto, dopisz miasto dla rozróżnienia
    if clean_raw and len(clean_raw) >= 3 and any(c.isalpha() for c in clean_raw):
        if city and city.lower() not in clean_raw.lower():
            return f"{clean_raw} ({city.strip()})"
        return clean_raw
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
        # Ochrona przed pomyleniem psich salonów/groomerów z ludzkim SPA/fryzjerem
        if _PET_GROOMING_RE.search(signal_text):
            return ["Lokal Usługowy / Inne"]
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
    facebook: str = ""
    instagram: str = ""

    @property
    def identity(self) -> tuple[str, str]:
        return normalized(self.name), normalized(self.city)

    @property
    def ordered_industries(self) -> list[str]:
        ordered = [i for i in SOUNDYOU_INDUSTRIES if i in self.industries]
        return ordered or ["Lokal Usługowy / Inne"]

    def company_values(
        self,
        safe_domains: list[str] | None = None,
        *,
        lead_status: str = DEFAULT_NEW_LEAD_STATUS,
        lead_batch: str = "",
    ) -> dict[str, Any]:
        industries = self.ordered_industries
        facts = ["Źródło: Web-Crunchy-Scrap", f"Branża: {', '.join(industries)}"]
        if lead_batch:
            facts.append(f"Partia: {lead_batch}")
        if self.phones:
            facts.append(f"Telefony: {', '.join(sorted(self.phones))}")
        if self.emails:
            facts.append(f"E-maile: {', '.join(sorted(self.emails))}")
        if self.facebook:
            facts.append(f"Facebook: {self.facebook}")
        if self.instagram:
            facts.append(f"Instagram: {self.instagram}")

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
        if lead_status:
            values[LEAD_STATUS_SLUG] = lead_status
        if lead_batch:
            values[LEAD_BATCH_SLUG] = lead_batch
        if self.facebook:
            values["facebook"] = self.facebook
        if self.instagram:
            values["instagram"] = self.instagram
        doms = safe_domains if safe_domains is not None else sorted(self.domains)
        if doms:
            values["domains"] = doms
        return values


def read_csv_leads(
    csv_path: Path,
    stats: Counter[str],
    *,
    filter_non_icp: bool = True,
    audit: dict[str, list[dict[str, str]]] | None = None,
) -> Iterable[Lead]:
    """Czyta plik CSV ze scrapera i zwraca strumień oczyszczonych obiektów Lead."""
    rejected_emails_log = audit.setdefault("rejected_emails", []) if audit is not None else None
    inferred_names_log = audit.setdefault("inferred_names", []) if audit is not None else None
    non_icp_log = audit.setdefault("non_icp_skipped", []) if audit is not None else None

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
            city = data.get("Miasto", "")
            phone = normalize_phone_e164(data.get("Phone") or data.get("Numer Telefonu", ""))
            website = data.get("Website", "")
            socials = extract_social_links(
                data.get("Facebook", ""),
                data.get("Instagram", ""),
                data.get("Social Media", ""),
                website,
            )
            fb = socials["facebook"]
            ig = socials["instagram"]
            domain = business_domain(website)
            emails: set[str] = set()
            for idx in email_cols:
                if idx < len(row):
                    emails.update(extract_emails(row[idx], rejected_log=rejected_emails_log))

            name = resolve_company_name(
                raw_name,
                website=website,
                facebook=fb,
                instagram=ig,
                emails=emails,
                city=city,
            )
            if len(name) < 3 or not any(c.isalpha() for c in name):
                stats["invalid_rows"] += 1
                continue

            raw_industry = data.get("Branża", "")
            if filter_non_icp:
                non_icp, non_icp_reason = is_non_icp_business(name=name, website=website, raw_industry=raw_industry)
                if non_icp:
                    stats["skipped_non_icp"] += 1
                    if non_icp_log is not None:
                        non_icp_log.append({"name": name, "domain": domain, "reason": non_icp_reason})
                    continue

            if strip_emoji(raw_name).strip() != name:
                stats["inferred_company_names"] += 1
                if inferred_names_log is not None:
                    inferred_names_log.append({
                        "raw_name": raw_name,
                        "resolved_name": name,
                        "city": city,
                        "website": website,
                        "facebook": fb,
                        "instagram": ig,
                    })

            signal_urls = " ".join(x for x in (website, fb, ig) if x)
            industries = set(classify_soundyou_industries(raw_industry, name=name, website=signal_urls))
            yield Lead(
                name=name,
                city=city,
                region=data.get("Województwo", ""),
                address=data.get("Address") or data.get("Adres", ""),
                industries=industries,
                emails=emails,
                phones={phone} if phone else set(),
                domains={domain} if domain else set(),
                facebook=fb,
                instagram=ig,
            )


def load_and_dedup_csv(
    csv_paths: list[Path],
    *,
    filter_non_icp: bool = True,
    audit: dict[str, list[dict[str, str]]] | None = None,
) -> tuple[list[Lead], Counter[str], Counter[str]]:
    """Ładuje jeden lub więcej plików CSV i lokalnie deduplikuje leady."""
    stats: Counter[str] = Counter()
    leads: list[Lead] = []

    for csv_path in csv_paths:
        for lead in read_csv_leads(csv_path, stats, filter_non_icp=filter_non_icp, audit=audit):
            name_norm, city_norm = lead.identity
            lead_pc = extract_postcode(lead.address)
            candidates = [
                item for item in leads
                if bool(item.phones & lead.phones)
                or bool(item.emails & lead.emails)
                or (lead.facebook and item.facebook == lead.facebook)
                or (lead.instagram and item.instagram == lead.instagram)
                or (
                    bool(item.domains & lead.domains)
                    and (item.identity[0] == name_norm or item.identity[1] == city_norm or not city_norm or not item.identity[1])
                )
                or (
                    item.identity[0] == name_norm
                    and (
                        item.identity[1] == city_norm
                        or not city_norm
                        or not item.identity[1]
                        or (lead_pc and lead_pc == extract_postcode(item.address))
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
                existing.facebook = existing.facebook or lead.facebook
                existing.instagram = existing.instagram or lead.instagram
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
                "Accept": "application/json",
                "User-Agent": "SoundYou-CRM-Sync/1.0",
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
    """Zapewnia istnienie `soundyou_industry`, `lead_status` i `lead_batch` na `companies`."""
    summary: dict[str, Any] = {
        "attribute_existed": False,
        "attribute_created": False,
        "options_created": [],
        "lead_status_ready": False,
        "lead_batch_ready": False,
        "existing_industry_views": [],
        "missing_industry_views": [],
    }

    attrs = client.request("/objects/companies/attributes", method="GET") or []
    by_slug = {a.get("api_slug"): a for a in attrs if isinstance(a, dict)}

    if INDUSTRY_ATTR_SLUG in by_slug:
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

    # Sprawdź / utwórz `lead_status` i `lead_batch`
    if LEAD_STATUS_SLUG in by_slug:
        summary["lead_status_ready"] = True
    elif not dry_run:
        try:
            client.request(
                "/objects/companies/attributes",
                {
                    "data": {
                        "title": LEAD_STATUS_TITLE,
                        "api_slug": LEAD_STATUS_SLUG,
                        "description": "Etap leada w lejku sprzedażowym SoundYou",
                        "type": "status",
                        "is_multiselect": False,
                        "is_required": False,
                        "is_unique": False,
                        "config": {},
                    }
                },
                method="POST",
            )
            for st_title in LEAD_STATUS_OPTIONS:
                client.request(
                    f"/objects/companies/attributes/{LEAD_STATUS_SLUG}/statuses",
                    {"data": {"title": st_title}},
                    method="POST",
                )
            summary["lead_status_ready"] = True
        except AttioAPIError as err:
            logger.warning("Nie udało się utworzyć %s: %s", LEAD_STATUS_SLUG, err)

    if LEAD_BATCH_SLUG in by_slug:
        summary["lead_batch_ready"] = True
    elif not dry_run:
        try:
            client.request(
                "/objects/companies/attributes",
                {
                    "data": {
                        "title": LEAD_BATCH_TITLE,
                        "api_slug": LEAD_BATCH_SLUG,
                        "description": "Źródło i partia importu leada",
                        "type": "text",
                        "is_multiselect": False,
                        "is_required": False,
                        "is_unique": False,
                        "config": {},
                    }
                },
                method="POST",
            )
            summary["lead_batch_ready"] = True
        except AttioAPIError as err:
            logger.warning("Nie udało się utworzyć %s: %s", LEAD_BATCH_SLUG, err)

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
    companies_by_name_postcode: dict[tuple[str, str], str] = {}
    companies_by_email: dict[str, str] = {}
    companies_by_phone: dict[str, str] = {}
    companies_by_social: dict[str, str] = {}
    name_to_cids: dict[str, set[str]] = defaultdict(set)
    company_state: dict[str, dict[str, Any]] = {}
    people_by_email: dict[str, dict[str, Any]] = {}
    people_by_phone: dict[str, dict[str, Any]] = {}
    dnc_emails: set[str] = set()

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

        locs = vals.get("primary_location", [])
        cities = {
            normalized(item.get("locality") or "")
            for item in locs
        } or {""}
        postcodes = {
            (item.get("postcode") or extract_postcode(item.get("line_1") or "")).strip()
            for item in locs
        } - {""}

        for item in vals.get("name", []):
            norm_name = normalized(item.get("value") or "")
            if not norm_name:
                continue
            name_to_cids[norm_name].add(rid)
            for c in cities:
                companies_by_name_city[(norm_name, c)] = rid
            for pc in postcodes:
                companies_by_name_postcode[(norm_name, pc)] = rid

        for item in vals.get("description", []):
            desc_text = item.get("value") or ""
            for em in extract_emails(desc_text):
                companies_by_email.setdefault(em, rid)
            for ph in extract_phones_e164(desc_text):
                companies_by_phone.setdefault(ph, rid)
            desc_socials = extract_social_links(desc_text)
            for s_url in desc_socials.values():
                if s_url:
                    companies_by_social.setdefault(s_url.lower(), rid)

        fb_existing = ""
        for fb_item in vals.get("facebook", []):
            fb_val = (fb_item.get("value") or "").strip()
            if fb_val and not fb_existing:
                fb_existing = fb_val
            fb_norm = normalize_facebook_url(fb_val) or (f"https://www.facebook.com/{fb_val.lstrip('@/')}" if fb_val and " " not in fb_val else "")
            if fb_norm:
                companies_by_social.setdefault(fb_norm.lower(), rid)
        ig_existing = ""
        for ig_item in vals.get("instagram", []):
            ig_val = (ig_item.get("value") or "").strip()
            if ig_val and not ig_existing:
                ig_existing = ig_val
            ig_norm = normalize_instagram_url(ig_val) or (f"https://www.instagram.com/{ig_val.lstrip('@/')}" if ig_val and " " not in ig_val else "")
            if ig_norm:
                companies_by_social.setdefault(ig_norm.lower(), rid)

        has_loc = any(
            bool((item.get("line_1") or "").strip() or (item.get("locality") or "").strip())
            for item in locs
        )
        company_state[rid] = {
            "industries": _extract_multiselect_titles(vals.get(INDUSTRY_ATTR_SLUG, [])),
            "domains": doms,
            "facebook": fb_existing,
            "instagram": ig_existing,
            "has_location": has_loc,
        }

    for rec in client.query_all_records("people"):
        pid = rec["id"]["record_id"]
        vals = rec.get("values") or {}
        comp_refs = vals.get("company") or []
        linked_cid = comp_refs[0].get("target_record_id") if comp_refs else None

        unsub_vals = vals.get("resend_unsubscribed") or []
        is_unsub = any(bool(v.get("value")) for v in unsub_vals)
        deliv_titles = _extract_multiselect_titles(vals.get("resend_last_delivery_status", []))
        is_bounced = bool(deliv_titles & {"bounced", "suppressed"})

        person_phones: list[str] = []
        for item in vals.get("phone_numbers", []):
            raw_ph = item.get("phone_number") or item.get("original_phone_number") or ""
            ph = normalize_phone_e164(raw_ph)
            if not ph:
                continue
            person_phones.append(ph)
            people_by_phone[ph] = {
                "record_id": pid,
                "company_id": linked_cid,
            }
            if linked_cid:
                companies_by_phone.setdefault(ph, linked_cid)

        for item in vals.get("email_addresses", []):
            em = (item.get("email_address") or "").strip().lower()
            if not em:
                continue
            if is_unsub or is_bounced:
                dnc_emails.add(em)
            people_by_email[em] = {
                "record_id": pid,
                "company_id": linked_cid,
                "has_phone": bool(person_phones),
            }
            if linked_cid:
                companies_by_email.setdefault(em, linked_cid)

    companies_by_unique_name = {
        nm: next(iter(cids))
        for nm, cids in name_to_cids.items()
        if len(nm) >= 5 and len(cids) == 1
    }

    return {
        "companies_by_domain": companies_by_domain,
        "companies_by_name_city": companies_by_name_city,
        "companies_by_name_postcode": companies_by_name_postcode,
        "companies_by_unique_name": companies_by_unique_name,
        "companies_by_email": companies_by_email,
        "companies_by_phone": companies_by_phone,
        "companies_by_social": companies_by_social,
        "company_state": company_state,
        "people_by_email": people_by_email,
        "people_by_phone": people_by_phone,
        "dnc_emails": dnc_emails,
    }


def _find_matching_company(lead: Lead, indexes: dict[str, Any]) -> str | None:
    by_dom = indexes["companies_by_domain"]
    for d in sorted(lead.domains):
        if d in by_dom:
            return by_dom[d]

    by_soc = indexes["companies_by_social"]
    for soc in (lead.facebook, lead.instagram):
        if soc and soc.lower() in by_soc:
            return by_soc[soc.lower()]

    by_em = indexes["companies_by_email"]
    people_em = indexes["people_by_email"]
    for em in sorted(lead.emails):
        if em in by_em:
            return by_em[em]
        if em in people_em:
            return people_em[em].get("company_id") or f"person:{people_em[em]['record_id']}"

    by_ph = indexes["companies_by_phone"]
    people_ph = indexes["people_by_phone"]
    for ph in sorted(lead.phones):
        if ph in by_ph:
            return by_ph[ph]
        if ph in people_ph:
            return people_ph[ph].get("company_id") or f"person:{people_ph[ph]['record_id']}"

    name_norm, city_norm = lead.identity
    by_nc = indexes["companies_by_name_city"]
    if (name_norm, city_norm) in by_nc:
        return by_nc[(name_norm, city_norm)]
    if (name_norm, "") in by_nc:
        return by_nc[(name_norm, "")]

    pc = extract_postcode(lead.address)
    by_npc = indexes["companies_by_name_postcode"]
    if pc and (name_norm, pc) in by_npc:
        return by_npc[(name_norm, pc)]

    by_un = indexes["companies_by_unique_name"]
    if name_norm in by_un:
        return by_un[name_norm]

    return None


def sync_leads_to_attio(
    client: AttioClient,
    leads: list[Lead],
    stats: Counter[str],
    domain_counts: Counter[str],
    *,
    dry_run: bool = False,
    sync_people: bool = True,
    fill_missing: bool = False,
    enrich_existing: bool = False,
    lead_batch: str = "",
    lead_status: str = DEFAULT_NEW_LEAD_STATUS,
    verify_emails: bool = True,
    usercheck_api_key: str | None = None,
    audit: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Synchronizuje zdeduplikowane leady do `Companies` i `People` w Attio.

    Domyślnie (`fill_missing=False`, `enrich_existing=False`) działa w trybie ścisłej deduplikacji:
    jeśli lead już istnieje w Attio, NIE jest dodawany ani modyfikowany.
    W trybie `--fill-missing` (`fill_missing=True`) uzupełnia wyłącznie PUSTE pola na istniejących
    rekordach (nigdy nie nadpisuje istniejących wartości) oraz dodaje nowo odkryte e-maile jako `People`.
    """
    indexes = build_attio_indexes(client)
    industry_counts: Counter[str] = Counter()
    uc_key = (usercheck_api_key if usercheck_api_key is not None else os.getenv("USERCHECK_API_KEY", "")).strip()
    uc_cache = load_email_verify_cache() if (verify_emails and uc_key) else None
    uc_dirty = False
    rejected_emails_log = audit.setdefault("rejected_emails", []) if audit is not None else []
    filled_companies_log = audit.setdefault("filled_missing_companies", []) if audit is not None else []

    def _filter_deliverable_emails(candidate_emails: set[str], *, skip_already_indexed: bool = False) -> set[str]:
        nonlocal uc_dirty
        out_emails: set[str] = set()
        for em in sorted(candidate_emails):
            if em in indexes["dnc_emails"]:
                stats["skipped_dnc_emails"] += 1
                rejected_emails_log.append({"email": em, "reason": "attio_dnc"})
                continue
            if skip_already_indexed and em in indexes["people_by_email"]:
                out_emails.add(em)
                continue
            if verify_emails and uc_key:
                was_cached = uc_cache is not None and em in uc_cache
                ok_uc, uc_reason = verify_email_usercheck(em, uc_key, cache=uc_cache)
                if not was_cached:
                    uc_dirty = True
                if not ok_uc:
                    stats["rejected_bad_emails"] += 1
                    rejected_emails_log.append({"email": em, "reason": f"usercheck:{uc_reason}"})
                    continue
            out_emails.add(em)
        return out_emails

    for lead in leads:
        for ind in lead.ordered_industries:
            industry_counts[ind] += 1

        safe_domains = sorted(
            d for d in lead.domains
            if domain_counts[d] == 1 and d not in indexes["companies_by_domain"]
        )
        matched_cid = _find_matching_company(lead, indexes)
        is_existing_company = bool(matched_cid)

        if matched_cid:
            if (not fill_missing and not enrich_existing) or matched_cid.startswith("person:"):
                stats["skipped_existing_companies"] += 1
                if sync_people and lead.emails:
                    stats["skipped_existing_people"] += len(lead.emails)
                continue

            state = indexes["company_state"].setdefault(
                matched_cid,
                {
                    "industries": set(),
                    "domains": set(),
                    "facebook": "",
                    "instagram": "",
                    "has_location": False,
                },
            )
            patch_values: dict[str, Any] = {}

            if fill_missing:
                # Fill-blanks-only: dopisz WYŁĄCZNIE pola, które w Attio są obecnie puste
                if safe_domains and not state.get("domains"):
                    patch_values["domains"] = safe_domains
                if lead.instagram and not state.get("instagram"):
                    patch_values["instagram"] = lead.instagram
                if lead.facebook and not state.get("facebook"):
                    patch_values["facebook"] = lead.facebook
                if lead.ordered_industries and not state.get("industries"):
                    patch_values[INDUSTRY_ATTR_SLUG] = lead.ordered_industries
                if (lead.address or lead.city) and not state.get("has_location"):
                    patch_values["primary_location"] = lead.company_values()["primary_location"]

            if enrich_existing:
                missing_industries = [
                    ind for ind in lead.ordered_industries
                    if ind not in state["industries"]
                ]
                if missing_industries:
                    merged_inds = [
                        ind for ind in SOUNDYOU_INDUSTRIES
                        if ind in (state["industries"] | set(missing_industries))
                    ]
                    patch_values[INDUSTRY_ATTR_SLUG] = merged_inds
                if safe_domains and not state.get("domains"):
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
                if "facebook" in patch_values:
                    state["facebook"] = patch_values["facebook"]
                    indexes["companies_by_social"].setdefault(patch_values["facebook"].lower(), matched_cid)
                if "instagram" in patch_values:
                    state["instagram"] = patch_values["instagram"]
                    indexes["companies_by_social"].setdefault(patch_values["instagram"].lower(), matched_cid)
                if "primary_location" in patch_values:
                    state["has_location"] = True
                for d in patch_values.get("domains", []):
                    indexes["companies_by_domain"][d] = matched_cid

                if fill_missing and not enrich_existing:
                    stats["filled_missing_companies"] += 1
                    filled_companies_log.append({
                        "record_id": matched_cid,
                        "name": lead.name,
                        "filled_fields": sorted(patch_values.keys()),
                    })
                else:
                    stats["updated_companies"] += 1
            else:
                stats["skipped_existing_companies"] += 1

            if sync_people and lead.emails:
                lead.emails = _filter_deliverable_emails(lead.emails, skip_already_indexed=True)
            company_id = matched_cid
        else:
            # Brama jakości e-maili (DNC Attio + UserCheck MX/disposable) TYLKO dla nowych leadów
            if lead.emails:
                lead.emails = _filter_deliverable_emails(lead.emails)

            # Nowa firma — wymagaj co najmniej 1 sygnału kontaktowego/tożsamościowego
            if not (lead.emails or lead.phones or safe_domains or lead.facebook or lead.instagram):
                stats["skipped_weak_identity"] += 1
                continue

            values = lead.company_values(
                safe_domains=safe_domains,
                lead_status=lead_status,
                lead_batch=lead_batch,
            )
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

            # Zaktualizuj wszystkie indeksy w pamięci, by uniknąć duplikatów w tym samym batchu
            name_norm, city_norm = lead.identity
            indexes["companies_by_name_city"][(name_norm, city_norm)] = company_id
            pc = extract_postcode(lead.address)
            if pc:
                indexes["companies_by_name_postcode"][(name_norm, pc)] = company_id
            if len(name_norm) >= 5:
                indexes["companies_by_unique_name"].setdefault(name_norm, company_id)
            for d in safe_domains:
                indexes["companies_by_domain"][d] = company_id
            for em in lead.emails:
                indexes["companies_by_email"].setdefault(em, company_id)
            for ph in lead.phones:
                indexes["companies_by_phone"].setdefault(ph, company_id)
            for soc in (lead.facebook, lead.instagram):
                if soc:
                    indexes["companies_by_social"].setdefault(soc.lower(), company_id)
            indexes["company_state"][company_id] = {
                "industries": set(lead.ordered_industries),
                "domains": set(safe_domains),
                "facebook": lead.facebook,
                "instagram": lead.instagram,
                "has_location": bool(lead.address or lead.city),
            }
            stats["created_companies"] += 1

        # Synchronizacja People dla każdego e-maila na leadzie
        if not sync_people or not lead.emails:
            continue

        primary_phone = sorted(lead.phones)[0] if lead.phones else ""
        # Najpierw obsłuż kontakty już istniejące w Attio, potem twórz nowo odkryte adresy
        ordered_emails = sorted(lead.emails, key=lambda e: (e not in indexes["people_by_email"], e))
        for email in ordered_emails:
            existing_person = indexes["people_by_email"].get(email)
            if existing_person:
                person_patch: dict[str, Any] = {}
                if (fill_missing or enrich_existing) and not existing_person.get("company_id"):
                    person_patch["company"] = {
                        "target_object": "companies",
                        "target_record_id": company_id,
                    }
                ph_owner_cid = (indexes["people_by_phone"].get(primary_phone) or {}).get("company_id")
                if (
                    fill_missing
                    and primary_phone
                    and not existing_person.get("has_phone")
                    and ph_owner_cid in (None, company_id)
                ):
                    person_patch["phone_numbers"] = [primary_phone]

                if person_patch:
                    if not dry_run:
                        try:
                            client.request(
                                f"/objects/people/records/{existing_person['record_id']}",
                                {"data": {"values": person_patch}},
                                method="PATCH",
                            )
                        except AttioAPIError as err:
                            logger.warning("PATCH person %s failed: %s", email, err)
                            stats[f"person_patch_error_{err.status}"] += 1
                            continue
                    if "company" in person_patch:
                        existing_person["company_id"] = company_id
                    if "phone_numbers" in person_patch:
                        existing_person["has_phone"] = True
                        indexes["people_by_phone"][primary_phone] = {
                            "record_id": existing_person["record_id"],
                            "company_id": company_id,
                        }
                    if fill_missing and not enrich_existing:
                        stats["filled_missing_people"] += 1
                    else:
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
            if primary_phone and primary_phone not in indexes["people_by_phone"]:
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
                "has_phone": "phone_numbers" in person_values,
            }
            if "phone_numbers" in person_values:
                indexes["people_by_phone"][primary_phone] = {
                    "record_id": person_id,
                    "company_id": company_id,
                }
            stats["created_people"] += 1
            if is_existing_company and fill_missing:
                stats["created_people_for_existing_companies"] += 1

    if uc_dirty and uc_cache is not None:
        save_email_verify_cache(uc_cache)

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


def write_sync_report(report: dict[str, Any], report_path: Path | None = None) -> Path | None:
    """Zapisuje ustrukturyzowany raport JSON z przebiegu do `logs/attio_sync_<ts>.json`."""
    target = report_path or (
        REPORTS_DIR / f"attio_sync_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return target
    except OSError as exc:
        logger.warning("Nie udało się zapisać raportu przebiegu %s: %s", target, exc)
        return None


def sync_csv_to_attio(
    csv_path: str | Path = DEFAULT_CSV_PATH,
    *,
    token: str | None = None,
    dry_run: bool = False,
    offline_dry_run: bool = False,
    ensure_schema: bool = True,
    sync_people: bool = True,
    fill_missing: bool = False,
    enrich_existing: bool = False,
    lead_batch: str | None = None,
    verify_emails: bool = True,
    filter_non_icp: bool = True,
    save_report: bool = True,
    report_path: Path | None = None,
) -> dict[str, Any]:
    """Główny punkt wejścia (wywoływany z CLI lub z `scraper.py --sync-attio`)."""
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"Plik CSV nie istnieje: {path}")

    effective_batch = (
        lead_batch
        if lead_batch is not None
        else f"{datetime.now().strftime('%Y-%m-%d')} | {path.stem}"
    )
    audit: dict[str, list[dict[str, Any]]] = {
        "inferred_names": [],
        "rejected_emails": [],
        "non_icp_skipped": [],
        "filled_missing_companies": [],
    }
    leads, stats, domain_counts = load_and_dedup_csv(
        [path],
        filter_non_icp=filter_non_icp,
        audit=audit,
    )
    if audit["rejected_emails"]:
        stats["rejected_syntax_or_typo_emails"] = len(audit["rejected_emails"])

    if offline_dry_run:
        industry_counts = Counter(ind for lead in leads for ind in lead.ordered_industries)
        out = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mode": "offline_dry_run",
            "csv": str(path),
            "lead_batch": effective_batch,
            "stats": dict(stats),
            "industry_counts": dict(industry_counts),
            "audit": audit,
        }
        if save_report:
            saved = write_sync_report(out, report_path)
            if saved:
                out["report_file"] = str(saved)
        return out

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
        fill_missing=fill_missing,
        enrich_existing=enrich_existing,
        lead_batch=effective_batch,
        verify_emails=verify_emails,
        audit=audit,
    )
    out = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": "dry_run" if dry_run else "apply",
        "csv": str(path),
        "lead_batch": effective_batch,
        "schema": schema_summary,
        **result,
        "audit": audit,
    }
    if save_report:
        saved = write_sync_report(out, report_path)
        if saved:
            out["report_file"] = str(saved)
    return out


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(
        description="Synchronizacja leadów CSV ze scrapera wprost do Attio (Companies + People, branże SoundYou, ścisła deduplikacja i brama jakości)."
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
        help="Tylko lokalna deduplikacja, filtr anty-ICP i klasyfikacja branż bez połączenia z Attio",
    )
    parser.add_argument(
        "--ensure-schema",
        action="store_true",
        help="Tylko sprawdź/utwórz atrybuty SoundYou (`soundyou_industry`, `lead_status`, `lead_batch`) w Attio",
    )
    parser.add_argument(
        "--no-ensure-schema",
        action="store_true",
        help="Pomiń krok sprawdzania schematu przed synchronizacją",
    )
    parser.add_argument(
        "--companies-only",
        action="store_true",
        help="Twórz tylko nowe `Companies`, bez rekordów `People`",
    )
    parser.add_argument(
        "--fill-missing",
        action="store_true",
        help="Tryb fill-blanks-only: dla firm już istniejących w Attio uzupełnij WYŁĄCZNIE puste pola (np. brakujące WWW, IG, FB, branżę, lokalizację) i dodaj nowo znalezione e-maile jako People — nigdy nie nadpisuj istniejących wartości",
    )
    parser.add_argument(
        "--enrich-existing",
        action="store_true",
        help="Opcjonalnie dopisuj brakujące tagi multiselect do `soundyou_industry` na istniejących firmach",
    )
    parser.add_argument(
        "--batch",
        default=None,
        help="Etykieta partii importu zapisywana w polu `lead_batch` (domyślnie: YYYY-MM-DD | nazwa_csv)",
    )
    parser.add_argument(
        "--no-verify-emails",
        action="store_true",
        help="Pomiń zdalną weryfikację MX/disposable w UserCheck API dla nowych leadów",
    )
    parser.add_argument(
        "--include-non-icp",
        action="store_true",
        help="Wyłącz filtr anty-ICP (domyślnie odrzuca Auto SPA, agencje IT/SEO, pralnie itp.)",
    )
    parser.add_argument(
        "--no-report",
        action="store_true",
        help="Nie zapisuj pliku raportu JSON w katalogu logs/",
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
            fill_missing=args.fill_missing,
            enrich_existing=args.enrich_existing,
            lead_batch=args.batch,
            verify_emails=not args.no_verify_emails,
            filter_non_icp=not args.include_non_icp,
            save_report=not args.no_report,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (FileNotFoundError, RuntimeError, AttioAPIError, URLError, OSError, csv.Error) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

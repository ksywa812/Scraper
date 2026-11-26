# --- START OF FILE scraper.py ---

import argparse
import csv
import json
import logging
import os
import random
import re
import sqlite3
import time
import unicodedata
from urllib.parse import quote_plus, urlparse, urljoin
from urllib.robotparser import RobotFileParser

import openpyxl
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Load API key from .env file
load_dotenv()
API_KEY = os.getenv('GOOGLE_MAPS_API_KEY')

OUTPUT_FILE = 'results.xlsx'  # Changed from 'wyniki.xlsx'
OUTPUT_DIR = os.path.join('Data', 'Raport')
BOOKSY_BASE = "https://booksy.com/pl-pl"
SPAEDEN_RANKING_URL = "https://www.spaeden.pl/spa-wellness/rankingi-spa/2909-najlepsze-hotele-spa-ranking-100-best-spa-hotels"
FRESHA_BASE = "https://www.fresha.com"
ZNANYLEKARZ_BASE = "https://www.znanylekarz.pl"
FIXLY_BASE = "https://fixly.pl"
CYLEX_BASE = "https://www.cylex-polska.pl"
OFERTEO_BASE = "https://www.oferteo.pl"
MOMENT_BASE = "https://www.moment.pl"
FIRMYNET_BASE = "https://www.firmy.net"
BIZNESFINDER_BASE = "https://www.biznesfinder.pl"
KRS_API_BASE = "https://api-rs.ms.gov.pl"       # search/query API (unofficial)
KRS_LOOKUP_BASE = "https://api-krs.ms.gov.pl"  # official lookup-by-KRS-number API
ALEO_BASE = "https://aleo.com"

LOG_FORMAT = "%(asctime)s | %(levelname)s | %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger(__name__)


def is_probably_path(value):
    if not value:
        return False
    lowered = value.strip().lower()
    if ":\\" in lowered or ":/" in lowered:
        return True
    if "/" in lowered or "\\" in lowered:
        return True
    if lowered.endswith((".txt", ".log", ".csv", ".xlsx", ".json")):
        return True
    return False


def setup_run_logging(output_path, logs_dir="logs"):
    os.makedirs(logs_dir, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    base_name = os.path.splitext(os.path.basename(output_path))[0] or "results"
    log_filename = f"{base_name}_{timestamp}.log"
    log_path = os.path.join(logs_dir, log_filename)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logger.addHandler(file_handler)

    logger.info("Log file: %s", log_path)
    return log_path

# List of User-Agents for rotation to avoid blocking
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
]

def get_random_user_agent():
    """Returns a random User-Agent string."""
    return random.choice(USER_AGENTS)


# Polish characters that unicodedata.normalize('NFKD') does NOT decompose
POLISH_CHAR_MAP = {
    'ł': 'l', 'Ł': 'L',
    'ą': 'a', 'Ą': 'A',
    'ć': 'c', 'Ć': 'C',
    'ę': 'e', 'Ę': 'E',
    'ń': 'n', 'Ń': 'N',
    'ó': 'o', 'Ó': 'O',
    'ś': 's', 'Ś': 'S',
    'ź': 'z', 'Ź': 'Z',
    'ż': 'z', 'Ż': 'Z',
}

# Centralny, poprawny wzorzec e-mail (zamiast trzech różnych błędnych wersji w pliku)
# Poprawka: [A-Z|a-z] → [A-Za-z], niedozwolone domeny z podwójną kropką, png/jpg w końcówce
EMAIL_RE = re.compile(
    r'\b[A-Za-z0-9._%+-]+'
    r'@'
    r'[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9]'
    r'\.'
    r'[A-Za-z]{2,}\b'
)

_IMAGE_EXTENSIONS = ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.ico', '.bmp')


def is_valid_email(email: str) -> bool:
    """Zwraca True jeśli email jest poprawny (format + nie jest plikiem graficznym)."""
    if not email or not isinstance(email, str):
        return False
    e = email.strip().lower()
    if e.endswith(_IMAGE_EXTENSIONS):
        return False
    return bool(EMAIL_RE.match(email.strip()))


# E-maile platform (Booksy, Fresha) — nie są kontaktami biznesowymi
_PLATFORM_EMAILS = frozenset({
    "aneksy@booksy.com", "pomoc.pl@booksy.com", "sprzedaz@booksy.com",
    "info.pl@booksy.com", "reklamacje@booksy.com", "info@booksy.com",
    "support@booksy.com", "hello@booksy.com",
    "support@fresha.com", "no-reply@fresha.com", "hello@fresha.com",
})


def filter_platform_emails(emails):
    """Usuwa e-maile platform (Booksy, Fresha) z listy kontaktów."""
    return [e for e in emails if e.lower() not in _PLATFORM_EMAILS]


def strip_emoji(text: str) -> str:
    """Usuwa emoji i znaki specjalne z tekstu (zachowuje litery, cyfry, interpunkcję)."""
    if not text:
        return text
    return ''.join(
        c for c in text
        if unicodedata.category(c) not in ('So', 'Sk', 'Sm', 'Cs')
    )


def strip_accents(text):
    # First, manually replace Polish chars that NFKD misses (especially ł)
    for pl_char, ascii_char in POLISH_CHAR_MAP.items():
        text = text.replace(pl_char, ascii_char)
    # Then handle remaining diacritics via standard Unicode decomposition
    return ''.join(c for c in unicodedata.normalize('NFKD', text) if not unicodedata.combining(c))


def slugify(text):
    text = strip_accents(text or "")
    text = re.sub(r"[^a-zA-Z0-9\s-]", "", text)
    text = re.sub(r"\s+", "-", text.strip().lower())
    text = re.sub(r"-+", "-", text)
    return text


def is_catalog_url(url):
    if not url:
        return False
    lower = url.lower()
    return any(d in lower for d in [
        "booksy.com",
        "booksy.net",
        "firmy.net",
        "oferteo.pl",
        "cylex-polska.pl",
        "fresha.com",
        "freshastatus.com",
        "fixly.pl",
        "panoramafirm.pl",
        "pkt.pl",
        "biznesfinder.pl",
        "znanylekarz.pl",
        "moment.pl",
        "aleo.com",
        "treatwell.",
        "versum.com",
        "itunes.apple.com",
        "apps.apple.com",
        "play.google.com",
        "apple.com/app",
        "maps.google.",
        "goo.gl/maps",
        "maps.app.goo.gl",
    ])


_EXTERNAL_PROFILE_SKIP_DEFAULTS = (
    "freshastatus.com", "fresha.com", "booksy.com", "booksy.net",
    "cookiebot.com", "onetrust.com", "trustpilot.com", "appsflyer.com",
    "cloudflare.com", "itunes.apple.com", "apps.apple.com", "play.google.com",
    "apple.com/app", "google.com/maps", "maps.google.", "doubleclick.net",
    "googletagmanager.com", "googleapis.com", "gstatic.com", "onelink.me",
)


def extract_external_website_from_profile(profile_url, session=None, skip_domains=None):
    if not profile_url:
        return ""
    session = session or create_session()
    merged_skip = list(_EXTERNAL_PROFILE_SKIP_DEFAULTS) + list(skip_domains or [])

    def is_valid_external(url):
        if not url or not url.startswith('http'):
            return False
        lower = url.lower()
        return not any(sd in lower for sd in merged_skip)

    try:
        resp = session.get(profile_url, headers={'User-Agent': get_random_user_agent()}, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')

        # First try: obvious website links
        for a in soup.find_all('a', href=True):
            text = " ".join(a.stripped_strings).lower()
            href = a.get('href')
            if any(k in text for k in ['www', 'strona', 'website', 'witryna', 'odwiedz', 'odwiedź']):
                if is_valid_external(href):
                    return href

        # JSON-LD
        for script in soup.find_all('script', type='application/ld+json'):
            try:
                data = json.loads(script.get_text(strip=True))
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if isinstance(item, dict):
                        url = item.get('url') or item.get('sameAs')
                        if isinstance(url, list):
                            for u in url:
                                if is_valid_external(u):
                                    return u
                        elif is_valid_external(url):
                            return url
            except Exception:
                continue

        # Fallback: first external link
        for a in soup.find_all('a', href=True):
            href = a.get('href')
            if is_valid_external(href):
                return href
    except Exception:
        return ""
    return ""


_HOTEL_KEYWORDS = [
    "hotel", "resort", "pensjonat", "aparthotel", "motel", "hostel",
    "lodge", "inn", "manor", "dworek", "palace",
    # Znane sieci hotelowe (Bristol, Sheraton itp. bez słowa "hotel" w nazwie)
    "sheraton", "marriott", "hilton", "radisson", "novotel", "ibis",
    "bristol", "manor house", "health resort", "wellness resort", "spa resort",
]

_HOTEL_KEYWORD_RE = re.compile(
    r'\b(' + '|'.join(re.escape(k) for k in _HOTEL_KEYWORDS) + r')\b',
    re.IGNORECASE,
)


def _is_hotel_record(record):
    """Zwraca True jeśli rekord to hotel/resort mimo że trafił do kategorii spa/beauty/masaż."""
    name = record.get('name') or ''
    website = record.get('website') or ''
    return bool(_HOTEL_KEYWORD_RE.search(name) or _HOTEL_KEYWORD_RE.search(website))


CATEGORY_KEYWORDS = {
    "spa": [
        "spa", "day spa", "spa & wellness", "rytual spa", "rytuał spa", "salon spa",
        "wellness", "odnowa biologiczna",
        "masaz", "masaź", "masaż", "massage", "masazysta", "masażysta",
        "masaz relaksacyjny", "masaz klasyczny", "masaz leczniczy",
        "masaz sportowy", "masaz balijski", "masaz tajski", "masaz kobido",
        "masaz lomi", "masaz tkanek", "masaz goracymi kamieniami",
        "masaz aroma", "aromaterapia", "bodywork", "kobido"
    ],
    "joga": [
        "joga", "yoga", "pilates", "medytacja", "meditation", "mindfulness",
        "hatha", "vinyasa", "ashtanga", "yin", "kundalini", "joga nidra",
    ],
    "fitness": [
        "fitness", "siłownia", "silownia", "gym", "klub fitness", "fitness club",
        "crossfit", "trening personalny", "personal training",
    ],
    "fizjoterapia": [
        "fizjoterapia", "rehabilitacja", "fizjo", "terapia manualna", "kinezyterapia",
        "osteopatia", "poczekalnia", "gabinet", "klinika", "przychodnia",
        "stomatolog", "dentysta", "dental",
    ],
    "uroda": [
        "uroda", "beauty", "beauty salon", "salon urody", "salon kosmetyczny",
        "kosmetyczka", "kosmetologia", "makijaz", "makijaż", "depilacja",
        "paznokcie", "manicure", "pedicure", "brwi", "rzesy", "rzęsy",
        "lifting", "mezoterapia", "medycyna estetyczna",
    ],
    "fryzjer": [
        "fryzjer", "fryzjerstwo", "salon fryzjerski", "barber", "barbershop",
        "hair salon", "hairdresser", "strzyżenie", "koloryzacja", "keratyna", "farbowanie wlosow",
    ],
    "hotel": [
        "hotel", "hotel spa", "hotel & spa", "resort", "pensjonat", "aparthotel",
        "spa hotel", "wellness hotel",
    ],
    "restaurant": [
        "restaurant", "restauracja", "restauracje", "bistro", "bar",
        "trattoria", "pizzeria", "sushi", "grill", "gastro", "gastronomia",
        "jadłodajnia", "bar mleczny", "food", "kuchnia",
    ],
    "kawiarnia": [
        "kawiarnia", "kawiarnie", "cafe", "caffe", "coffee", "coffee shop",
        "cukiernia", "palarnia kawy", "espresso", "bakery",
    ],
    "sklep": [
        "sklep", "butik", "boutique", "retail", "shop", "store", "showroom",
    ],
}

CATEGORY_PRIORITY = [
    "spa", "wellness", "joga", "fitness", "fizjoterapia",
    "uroda", "fryzjer", "hotel", "kawiarnia", "restaurant", "sklep",
]


def map_query_to_category(query):
    query_lower = (query or "").lower()
    for category in CATEGORY_PRIORITY:
        keywords = CATEGORY_KEYWORDS.get(category, [])
        if any(k in query_lower for k in keywords):
            return category
    return None


def normalize_query_for_sources(query):
    category = map_query_to_category(query)
    return category or query


def fresha_business_type_for_query(query):
    category = map_query_to_category(query)
    if category == "masaz":
        return "massage"
    if category in ("spa", "wellness"):
        return "spas"
    if category == "fizjoterapia":
        return "therapy-centers"
    if category == "joga":
        return "yoga"
    if category == "uroda":
        return "beauty-salons"
    if category == "fryzjer":
        return "hair-salons"
    if category == "restaurant":
        return "restaurants"
    return "spas"


def category_keywords_for_query(query):
    category = map_query_to_category(query)
    return CATEGORY_KEYWORDS.get(category, []) if category else []


def znanylekarz_path_for_query(query):
    category = map_query_to_category(query)
    _ZNANY_PATHS = {
        "masaz": "uslugi-zabiegi/masaz",
        "fizjoterapia": "fizjoterapeuta",
        "spa": "uslugi-zabiegi/kosmetyka",
        "uroda": "uslugi-zabiegi/kosmetyka",
        "wellness": "uslugi-zabiegi/masaz",
        "fryzjer": "fryzjer",
        "joga": "uslugi-zabiegi/masaz",
    }
    return _ZNANY_PATHS.get(category)


def fixly_path_for_query(query):
    category = map_query_to_category(query)
    if category == "masaz":
        return "kategoria/masaz"
    if category in ("spa", "wellness"):
        return "kategoria/spa"
    if category == "fizjoterapia":
        return "kategoria/fizjoterapia"
    if category == "joga":
        return "kategoria/joga"
    if category == "uroda":
        return "kategoria/uroda"
    if category == "fryzjer":
        return "kategoria/fryzjer"
    if category == "restaurant":
        return "kategoria/gastronomia"
    return None


def oferteo_paths_for_query(query, location):
    city_slug = slugify(location)
    if not city_slug:
        return []

    # Use only the generic search listing to avoid frequent 404s on category paths
    return [f"{OFERTEO_BASE}/firmy/{city_slug}?q={quote_plus(query)}"]


def cylex_search_urls(query, location):
    city_slug = slugify(location)
    search_terms = query.strip()
    if not search_terms:
        return []

    encoded = quote_plus(f"{search_terms} {location}".strip())
    candidates = [
        f"{CYLEX_BASE}/firmy/?q={encoded}",
        f"{CYLEX_BASE}/firmy?q={encoded}",
        f"{CYLEX_BASE}/{quote_plus(search_terms)}.html",
    ]

    if city_slug:
        candidates.extend([
            f"{CYLEX_BASE}/{city_slug}/?q={encoded}",
            f"{CYLEX_BASE}/{city_slug}/",  # fallback to city page
        ])
    return candidates


def firmynet_search_url(query, location):
    return f"{FIRMYNET_BASE}/szukaj.html?co={quote_plus(query)}&gdzie={quote_plus(location)}"


def biznesfinder_search_url(query, location):
    return f"{BIZNESFINDER_BASE}/?what={quote_plus(query)}&where={quote_plus(location)}"


def fresha_category_keywords(category):
    if category == "masaz":
        return ["masaz", "masaź", "masaż", "massage", "spa", "thai", "tajski", "kobido", "bodywork"]
    if category in ("spa", "wellness"):
        return ["spa", "wellness", "sauna", "sauny", "saunarium", "rytuał", "rytual"]
    if category == "joga":
        return ["joga", "yoga", "vinyasa", "ashtanga", "hatha", "kundalini", "yin"]
    if category == "fizjoterapia":
        return ["fizjo", "fizjoterapia", "rehabilitacja", "physio", "therapy", "terapia"]
    if category == "uroda":
        return ["uroda", "kosmetyczka", "salon kosmetyczny", "makijaz", "depilacja", "paznokcie"]
    if category == "fryzjer":
        return ["fryzjer", "barber", "salon fryzjerski", "strzyżenie", "koloryzacja"]
    if category == "hotel":
        return ["hotel", "resort", "pensjonat", "hotel spa"]
    if category == "restaurant":
        return ["restauracja", "restaurant", "bistro", "bar", "kawiarnia", "cafe", "pizzeria", "grill"]
    return []


def create_session():
    """Create a requests session with retries and backoff."""
    session = requests.Session()
    retry = Retry(
        total=3,
        read=3,
        connect=3,
        backoff_factor=0.6,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"]
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def create_cffi_session():
    """Create a curl_cffi session that impersonates Chrome TLS fingerprint.

    Falls back to a standard requests session if curl_cffi is not installed.
    Use this for sources protected by Cloudflare or TLS fingerprint analysis
    (e.g. Cylex, PKT.pl, Panorama Firm).
    """
    try:
        from curl_cffi import requests as cffi_requests
        session = cffi_requests.Session(impersonate="chrome124")
        logger.debug("curl_cffi session created (Chrome124 TLS impersonation)")
        return session
    except ImportError:
        logger.warning("curl_cffi not installed — falling back to standard requests session. "
                       "Install with: pip install curl_cffi")
        return create_session()


def _detect_chrome_major_version():
    """Detect installed Chrome major version number."""
    import subprocess
    try:
        # Windows: query registry for Chrome version
        result = subprocess.run(
            ['reg', 'query', r'HKEY_CURRENT_USER\Software\Google\Chrome\BLBeacon', '/v', 'version'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if 'version' in line.lower():
                    ver = line.strip().split()[-1]
                    return int(ver.split('.')[0])
    except Exception:
        pass
    return None


def get_rendered_html(url, wait_seconds=3, timeout=25):
    """Fetch JS-rendered HTML using nodriver (direct Chrome connection, no ChromeDriver).

    Falls back to undetected_chromedriver if nodriver is not installed.
    nodriver provides better anti-detect stealth as it communicates directly with
    the browser binary without the ChromeDriver intermediary (2025+ standard).
    Install with: pip install nodriver
    """
    # Primary: nodriver (better stealth, no ChromeDriver layer)
    try:
        import nodriver as nd
        import asyncio

        async def _fetch():
            browser = await nd.start(headless=True)
            try:
                page = await browser.get(url)
                await asyncio.sleep(wait_seconds)
                html = await page.get_content()
                return html
            finally:
                browser.stop()

        try:
            return asyncio.run(_fetch())
        except RuntimeError:
            # Already inside an event loop (e.g. Jupyter) — use nest_asyncio or thread
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, _fetch())
                return future.result(timeout=timeout + 5)
    except ImportError:
        pass
    except Exception as e:
        logger.warning("nodriver fetch failed for %s: %s", url, e)

    # Fallback: undetected_chromedriver
    try:
        import undetected_chromedriver as uc
    except Exception as e:
        logger.warning("Headless unavailable (nodriver and undetected_chromedriver both missing): %s", e)
        return None

    options = uc.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1280,720")

    chrome_ver = _detect_chrome_major_version()
    if chrome_ver:
        logger.debug("Detected Chrome major version: %s", chrome_ver)

    driver = None
    try:
        driver = uc.Chrome(options=options, version_main=chrome_ver)
        driver.set_page_load_timeout(timeout)
        driver.get(url)
        time.sleep(wait_seconds)
        return driver.page_source
    except Exception as e:
        logger.warning("Headless fetch failed for %s: %s", url, e)
        return None
    finally:
        try:
            if driver:
                driver.quit()
        except Exception:
            pass


def render_with_playwright(url, timeout_ms=25000):
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        logger.warning("Playwright unavailable: %s", e)
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            html = page.content()
            browser.close()
            return html
    except Exception as e:
        logger.warning("Playwright render failed for %s: %s", url, e)
        return None


def _playwright_reveal_email(profile_url, timeout_ms=20000):
    """Open a business profile page with Playwright, click the 'Pokaż e-mail' button,
    and return any email addresses found after the reveal.

    Works for PKT.pl and Panorama Firm where emails are loaded via XHR on button click.
    Returns an empty list if Playwright is unavailable or no emails are found.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return []

    email_pattern = EMAIL_RE
    # Selectors for the "show email" button across PKT and Panorama Firm
    reveal_selectors = [
        "span.call-text",          # PKT.pl
        "a.show-email",            # Panorama Firm
        "[data-action='show-email']",
        "button.show-email",
        "a[href*='mailto:']",      # direct mailto links (fallback)
        ".email-reveal",
        "[class*='show-email']",
        "[class*='reveal-email']",
    ]
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                           '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
            )
            page.goto(profile_url, wait_until="domcontentloaded", timeout=timeout_ms)

            clicked = False
            for selector in reveal_selectors:
                try:
                    btn = page.query_selector(selector)
                    if btn and btn.is_visible():
                        btn.click()
                        page.wait_for_timeout(1500)  # wait for XHR
                        clicked = True
                        break
                except Exception:
                    continue

            html = page.content()
            browser.close()

            # Search in deobfuscated HTML
            emails = email_pattern.findall(_deobfuscate_emails(html))
            # Filter out generic / noisy domains
            emails = [e for e in emails if not any(
                d in e.lower() for d in ['example.com', 'domain.com', 'sentry.io', 'wixpress.com']
            )]
            if emails and clicked:
                logger.info("Playwright email reveal found %s email(s) on %s", len(emails), profile_url)
            return list(dict.fromkeys(emails))
    except Exception as e:
        logger.debug("Playwright email reveal failed for %s: %s", profile_url, e)
        return []


def fetch_html(url, session=None, use_headless=False, headless_wait=3, timeout=20):
    session = session or create_session()
    try:
        resp = session.get(url, headers={'User-Agent': get_random_user_agent()}, timeout=timeout)
        resp.raise_for_status()
        html = resp.text
        if use_headless and ("cloudflare" in html.lower() or "attention required" in html.lower()):
            rendered = get_rendered_html(url, wait_seconds=headless_wait, timeout=timeout + 5)
            return rendered or html
        return html
    except Exception as e:
        if use_headless:
            rendered = get_rendered_html(url, wait_seconds=headless_wait, timeout=timeout + 5)
            if rendered:
                return rendered
        logger.warning("Fetch error %s: %s", url, e)
        return ""


def get_booksy_category_slug(query, session=None):
    session = session or create_session()
    query_lower = (query or "").lower()
    mapped = map_query_to_category(query_lower)
    if mapped:
        # Booksy has its own category slugs (e.g., spa/massage -> "masaz")
        booksy_category_map = {
            "spa": "masaz",
            "wellness": "masaz",
            "fizjoterapia": "fizjoterapia",
            "joga": "trening-i-dieta",
            "fitness": "trening-i-dieta",
            "uroda": "kosmetyczka",
            "fryzjer": "fryzjerstwo",
            "hotel": None,         # hotels aren't on Booksy
            "restaurant": None,    # restaurants aren't on Booksy
            "kawiarnia": None,     # cafes aren't on Booksy
            "sklep": None,         # retail shops aren't on Booksy
        }
        return booksy_category_map.get(mapped)

    # Fallback: try slugified query
    slug = slugify(query_lower)
    if not slug:
        return None

    try:
        url = f"{BOOKSY_BASE}/s/{slug}"
        resp = session.get(url, headers={'User-Agent': get_random_user_agent()}, timeout=15)
        if resp.status_code == 200 and f"/s/{slug}" in resp.text:
            return slug
    except Exception:
        pass

    return None


def get_booksy_category_slugs(query, session=None):
    """Zwraca listę kategorii Booksy odpowiadających branży (wiele podkategorii = więcej leadów)."""
    query_lower = (query or "").lower()
    if any(k in query_lower for k in ("kobido", "masaz", "masaż", "massage")):
        return ["masaz"]
    if any(k in query_lower for k in ("barber", "barbershop")):
        return ["barber-shop", "fryzjerstwo"]
    if any(k in query_lower for k in ("tatuaż", "tatuaz", "tattoo", "piercing")):
        return ["tatuaz-i-piercing"]

    mapped = map_query_to_category(query_lower)
    multi_map = {
        "spa": ["masaz", "spa"],
        "wellness": ["masaz", "spa"],
        "uroda": ["kosmetyczka", "paznokcie", "brwi-i-rzesy", "medycyna-estetyczna"],
        "fryzjer": ["fryzjerstwo", "barber-shop"],
        "fizjoterapia": ["fizjoterapia"],
        "joga": ["trening-i-dieta"],
        "fitness": ["trening-i-dieta"],
        "hotel": [],
        "restaurant": [],
        "kawiarnia": [],
        "sklep": [],
    }
    if mapped in multi_map:
        return list(multi_map[mapped])

    single = get_booksy_category_slug(query, session=session)
    return [single] if single else []


BOOKSY_CITY_SLUG_OVERRIDES = {
    "wrocław": "wroclaw",
    "kraków": "krakow",
    "warszawa": "warszawa",
    "łódź": "lodz",
    "gdańsk": "gdansk",
    "poznań": "poznan",
    "szczecin": "szczecin",
    "lublin": "lublin",
    "katowice": "katowice",
    "gorzów wielkopolski": "gorzow-wielkopolski",
    "zielona góra": "zielona-gora",
    "rzeszów": "rzeszow",
    "płock": "plock",
}


def get_booksy_city_slug(category_slug, location, session=None):
    session = session or create_session()
    location_lower = (location or "").strip().lower()
    if not location_lower:
        return None

    candidates = []
    override = BOOKSY_CITY_SLUG_OVERRIDES.get(location_lower)
    if override:
        candidates.append(override)
    slug = slugify(location_lower)
    if slug:
        candidates.append(slug)

    # Try direct city slug by probing the city page
    for candidate in list(dict.fromkeys([c for c in candidates if c])):
        try:
            city_url = f"{BOOKSY_BASE}/s/{category_slug}/{candidate}"
            resp = session.get(city_url, headers={'User-Agent': get_random_user_agent()}, timeout=15)
            if resp.status_code == 200 and f"/s/{category_slug}/" in resp.url:
                return candidate
        except Exception:
            pass

    # Fallback: parse category page for matching city link
    url = f"{BOOKSY_BASE}/s/{category_slug}"
    try:
        resp = session.get(url, headers={'User-Agent': get_random_user_agent()}, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        for a in soup.find_all('a', href=True):
            href = a['href']
            if f"/s/{category_slug}/" not in href:
                continue
            text = " ".join(a.stripped_strings).lower()
            if location_lower in text or (slug and f"_{slug}" in href):
                part = href.split(f"/s/{category_slug}/", 1)[1]
                return part.strip("/")
    except Exception as e:
        logger.warning("Could not resolve Booksy city slug: %s", e)

    return None


def _is_booksy_noise_name(text):
    """Wykrywa fałszywe nazwy z kart Booksy (np. sama ocena '5.0', '4.9', 'Nowy', 'Umów')."""
    if not text:
        return True
    s = re.sub(r"\s+", " ", text).strip()
    if len(s) < 2:
        return True
    # Sama ocena gwiazdkowa typu "5.0", "4,9", "5.0 (123)" lub liczby/symbole
    if re.fullmatch(r"[\d.,\s()★☆⭐•·+\-/%]+", s):
        return True
    low = s.lower()
    if low in {
        "nowy", "new", "promowany", "polecany", "zobacz", "umów", "umow",
        "rezerwuj", "więcej", "wiecej", "karty podarunkowe booksy",
        "karty podarunkowe", "pokaż więcej", "pokaz wiecej",
    }:
        return True
    return False


def _humanize_booksy_slug_from_url(url):
    """Wyciąga czytelną nazwę salonu z URL Booksy (`/pl-pl/<id>_<salon-slug>_<cat>_<city_id>_<city>`)."""
    if not url:
        return ""
    path = urlparse(url).path or ""
    m = re.search(r"/pl-pl/\d+_([^_/]+)_", path, re.IGNORECASE)
    if not m:
        return ""
    slug = m.group(1).strip("-")
    if not slug:
        return ""
    words = [w for w in slug.split("-") if w]
    if not words:
        return ""
    upper_words = {"spa", "vip", "med", "fit", "pro", "art", "bio", "eco", "ems"}
    return " ".join(w.upper() if w.lower() in upper_words else w.capitalize() for w in words)


def clean_booksy_name(text):
    if not text:
        return ""
    text = text.replace("Promowany", "").replace("Karty podarunkowe Booksy", "").strip()
    if " - " in text:
        text = text.split(" - ")[0]
    text = re.sub(r"\b\d+(?:\.\d+)?\s*km\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\b\d+[.,]\d+\b", "", text)
    text = re.sub(r"\b\d+\s*opin\w*\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_booksy_profile_data(profile_url, session=None):
    """Extract name, website, social links (Facebook/Instagram), email, phone, and address from a Booksy profile."""
    result = {'name': '', 'address': '', 'website': '', 'facebook': '', 'instagram': '', 'emails': [], 'phone': ''}
    if not profile_url:
        return result
    session = session or create_session()
    try:
        from attio_sync import extract_social_links
    except ImportError:
        extract_social_links = lambda *a: {'facebook': '', 'instagram': ''}
    try:
        resp = session.get(profile_url, headers={'User-Agent': get_random_user_agent()}, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')

        skip_domains = [
            'booksy.com', 'booksy.net', 'facebook.com', 'instagram.com', 'youtube.com',
            'tiktok.com', 'linkedin.com', 'google.', 'maps.google.',
            'itunes.apple.com', 'apps.apple.com', 'play.google.com',
            'apple.com/app', 'microsoft.com', 'apps.microsoft.com',
            'appsflyer.com', 'doubleclick.net', 'cloudfront.net',
            'googleapis.com', 'googletagmanager.com', 'feroot.com',
            'onelink.me', 'apple-mapkit.com', 'app.link', 'freshastatus.com',
            'cookiebot.com', 'onetrust.com', 'cloudflare.com', 'sentry.io',
        ]

        def is_valid_external(url):
            if not url or not isinstance(url, str) or not url.startswith('http'):
                return False
            lower = url.lower()
            return not any(sd in lower for sd in skip_domains)

        def _collect_emails_from_text(blob):
            if not blob:
                return
            for e in EMAIL_RE.findall(blob):
                if is_valid_email(e) and e.lower() not in _PLATFORM_EMAILS:
                    if e not in result['emails']:
                        result['emails'].append(e)

        # 0. Nazwa z <h1> (najbardziej wiarygodna na stronie profilu Booksy)
        h1 = soup.find('h1')
        if h1:
            h1_text = clean_booksy_name(h1.get_text(' ', strip=True))
            if h1_text and not _is_booksy_noise_name(h1_text):
                result['name'] = h1_text

        # 1. JSON-LD (LocalBusiness / BeautySalon / HealthAndBeautyBusiness — stabilny także w Nuxt 3)
        for script in soup.find_all('script', type='application/ld+json'):
            try:
                data = json.loads(script.get_text(strip=True))
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    ld_name = item.get('name')
                    if isinstance(ld_name, str) and not result['name']:
                        cn = clean_booksy_name(ld_name)
                        if cn and not _is_booksy_noise_name(cn):
                            result['name'] = cn
                    ld_tel = item.get('telephone')
                    if isinstance(ld_tel, str) and ld_tel.strip() and not result['phone']:
                        result['phone'] = ld_tel.strip()
                    ld_email = item.get('email')
                    if isinstance(ld_email, str):
                        _collect_emails_from_text(ld_email)
                    ld_addr = item.get('address')
                    if isinstance(ld_addr, dict) and not result['address']:
                        parts = [
                            str(ld_addr.get('streetAddress') or '').strip(),
                            str(ld_addr.get('postalCode') or '').strip(),
                            str(ld_addr.get('addressLocality') or '').strip(),
                        ]
                        addr_str = ', '.join(p for p in parts if p)
                        if addr_str:
                            result['address'] = addr_str
                    for field in ('url', 'sameAs'):
                        val = item.get(field)
                        urls = val if isinstance(val, list) else ([val] if isinstance(val, str) else [])
                        for u in urls:
                            if not isinstance(u, str):
                                continue
                            soc = extract_social_links(u)
                            result['facebook'] = result['facebook'] or soc['facebook']
                            result['instagram'] = result['instagram'] or soc['instagram']
                            if not result['website'] and is_valid_external(u):
                                result['website'] = u
            except Exception:
                continue

        # 2. Parse window.__NUXT__ (Nuxt 2) oraz <script id="__NUXT_DATA__"> (Nuxt 3 hydration JSON)
        for script in soup.find_all('script'):
            script_id = (script.get('id') or '').strip()
            text = script.get_text(' ', strip=True)
            if not text:
                continue
            is_nuxt2 = 'window.__NUXT__' in text
            is_nuxt3 = script_id == '__NUXT_DATA__' or '"website"' in text
            if not (is_nuxt2 or is_nuxt3):
                continue
            decoded = text.replace('\\u002F', '/').replace('\\/', '/')

            # Extract website (Nuxt 2: website:"...", Nuxt 3 JSON: "website":"...")
            if not result['website']:
                for pat in (r'website:"(https?://[^"]+)"', r'"website"\s*:\s*"(https?://[^"]+)"'):
                    m_web = re.search(pat, decoded)
                    if m_web:
                        w = m_web.group(1)
                        if is_valid_external(w):
                            result['website'] = w
                            break
                        else:
                            soc = extract_social_links(w)
                            result['facebook'] = result['facebook'] or soc['facebook']
                            result['instagram'] = result['instagram'] or soc['instagram']

            soc_all = extract_social_links(decoded)
            result['facebook'] = result['facebook'] or soc_all['facebook']
            result['instagram'] = result['instagram'] or soc_all['instagram']

            if not result['phone']:
                for pat in (r'phone:"([^"]+)"', r'"phone"\s*:\s*"([^"]+)"'):
                    m_ph = re.search(pat, decoded)
                    if m_ph and len(re.sub(r'\D', '', m_ph.group(1))) >= 9:
                        result['phone'] = m_ph.group(1)
                        break

            _collect_emails_from_text(decoded)
            if is_nuxt2:
                break

        # 3. HTML fallbacks: mailto:, tel:, social links i linki do strony WWW
        for a in soup.find_all('a', href=True):
            href = (a.get('href') or '').strip()
            low_href = href.lower()
            if low_href.startswith('mailto:'):
                _collect_emails_from_text(href[7:].split('?')[0])
                continue
            if low_href.startswith('tel:') and not result['phone']:
                cand_ph = href[4:].strip()
                if len(re.sub(r'\D', '', cand_ph)) >= 9:
                    result['phone'] = cand_ph
                continue
            soc = extract_social_links(href)
            result['facebook'] = result['facebook'] or soc['facebook']
            result['instagram'] = result['instagram'] or soc['instagram']
            if not result['website']:
                anchor_text = " ".join(a.stripped_strings).lower()
                if any(k in anchor_text for k in ['www', 'strona', 'website', 'witryna', 'odwiedz', 'odwiedź']):
                    if is_valid_external(href):
                        result['website'] = href
    except Exception:
        pass
    return result


def parse_booksy_listings(html, category_slug):
    soup = BeautifulSoup(html, 'html.parser')
    entries = {}

    href_pattern = re.compile(rf"^/pl-pl/\d+_.+_{re.escape(category_slug)}_", re.IGNORECASE)

    for a in soup.find_all('a', href=True):
        href = a['href']
        if not href_pattern.match(href):
            continue

        clean_href = href.split('#', 1)[0].split('?', 1)[0]
        if clean_href.startswith("http"):
            full_url = clean_href
        elif clean_href.startswith("/pl-pl/"):
            full_url = f"https://booksy.com{clean_href}"
        else:
            full_url = f"{BOOKSY_BASE}{clean_href}"

        entry = entries.setdefault(full_url, {"name": "", "address": "", "profile_url": full_url})

        # Preferuj nagłówek (h2/h3/h4) wewnątrz linku — omija odznaki ocen typu "5.0"
        heading = a.find(['h2', 'h3', 'h4'])
        if heading:
            h_name = clean_booksy_name(" ".join(heading.stripped_strings))
            if h_name and not _is_booksy_noise_name(h_name):
                entry["name"] = h_name

        text = " ".join(a.stripped_strings)
        if not text:
            continue

        if "•" in text:
            left, right = text.split("•", 1)
            name = clean_booksy_name(left)
            address = right.replace("Karty podarunkowe Booksy", "").strip()
            if name and not _is_booksy_noise_name(name) and not entry["name"]:
                entry["name"] = name
            if address and not entry["address"]:
                entry["address"] = address
        else:
            name = clean_booksy_name(text)
            if name and not _is_booksy_noise_name(name) and not entry["name"]:
                entry["name"] = name

    results = []
    for url, data in entries.items():
        final_name = data["name"]
        if not final_name or _is_booksy_noise_name(final_name):
            final_name = _humanize_booksy_slug_from_url(url)
        if not final_name:
            continue
        results.append({
            'name': final_name,
            'formatted_address': data["address"],
            'formatted_phone_number': "",
            'website': url,
            'profile_url': data.get("profile_url", url),
            'emails': []
        })

    return results


def scrape_booksy(query, location, max_pages=3, session=None):
    results = []
    session = session or create_session()

    category_slugs = get_booksy_category_slugs(query, session=session)
    if not category_slugs:
        logger.info("Booksy: no matching category for query '%s'.", query)
        return results

    seen_urls: set = set()
    for category_slug in category_slugs:
        city_slug = get_booksy_city_slug(category_slug, location, session=session)
        if not city_slug:
            logger.info("Booksy: could not resolve city slug for '%s' in category '%s'.", location, category_slug)
            continue

        logger.info("Scraping Booksy category '%s' for %s", category_slug, location)
        cat_seen: set = set()
        for page in range(1, max_pages + 1):
            page_url = f"{BOOKSY_BASE}/s/{category_slug}/{city_slug}"
            if page > 1:
                page_url = f"{page_url}?page={page}"

            try:
                resp = session.get(page_url, headers={'User-Agent': get_random_user_agent()}, timeout=15)
                resp.raise_for_status()
                page_results = parse_booksy_listings(resp.text, category_slug)
                if not page_results:
                    if page == 1:
                        logger.info("Booksy: no results found for %s", page_url)
                    break
                page_urls = {r['profile_url'] for r in page_results}
                if page > 1 and page_urls.issubset(cat_seen):
                    logger.info("Booksy: page %s returned only already-seen listings — stopping pagination", page)
                    break
                cat_seen.update(page_urls)
                for r in page_results:
                    if r['profile_url'] not in seen_urls:
                        seen_urls.add(r['profile_url'])
                        results.append(r)
                logger.info("Booksy: found %s listings on page %s (%s)", len(page_results), page, category_slug)
                time.sleep(random.uniform(0.8, 1.5))
            except Exception as e:
                logger.warning("Booksy: error fetching %s: %s", page_url, e)
                break

    # Deep scrape Booksy profiles to get real external website URLs, socials, emails, and phones
    if results:
        logger.info("Booksy: deep scraping %s profiles for external websites, socials & emails", len(results))
    for idx, item in enumerate(results, start=1):
        profile_url = item.get('profile_url') or item.get('website')
        if not profile_url:
            continue
        logger.info("Booksy: profile %s/%s  %s", idx, len(results), item.get('name', ''))
        profile_data = extract_booksy_profile_data(profile_url, session=session)
        # Upgrade noisy listing name z <h1>/JSON-LD profilu
        if profile_data.get('name') and (
            _is_booksy_noise_name(item.get('name', '')) or len(item.get('name', '')) > 90
        ):
            item['name'] = profile_data['name']
        if profile_data.get('address') and not item.get('formatted_address'):
            item['formatted_address'] = profile_data['address']
        # Zachowaj linki Facebook / Instagram z profilu Booksy
        if profile_data.get('facebook') and not item.get('facebook'):
            item['facebook'] = profile_data['facebook']
        if profile_data.get('instagram') and not item.get('instagram'):
            item['instagram'] = profile_data['instagram']
        # Website — zachowaj profile_url jako fallback jeśli nie ma realnej strony
        if profile_data['website'] and not is_catalog_url(profile_data['website']):
            logger.info("Booksy:   → website: %s", profile_data['website'])
            item['website'] = profile_data['website']
        else:
            item['website'] = item.get('profile_url', '')
        # Emails
        if profile_data['emails']:
            logger.info("Booksy:   → emails: %s", ', '.join(profile_data['emails']))
            existing = item.get('emails') or []
            item['emails'] = list(dict.fromkeys(existing + profile_data['emails']))
        # Phone
        if profile_data['phone'] and not item.get('formatted_phone_number'):
            item['formatted_phone_number'] = profile_data['phone']
        time.sleep(random.uniform(0.4, 0.9))

    return results


_SPAEDEN_NOISE_RE = re.compile(
    r"\b(edycja|awards|nagrod\w*|ranking\w*|konkurs\w*|laureaci|zwyci[ęe]zc\w*)\b",
    re.IGNORECASE,
)


def parse_spaeden_rankings(text):
    results = []
    seen = set()

    # Numbered list entries: "1. Hotel Name – City"
    numbered = re.findall(r"\b\d+\.\s*([^\n–-]+?)\s*[–-]\s*([^\n]+)", text)
    # Bullet entries: "• Hotel Name – City"
    bullets = re.findall(r"•\s*([^\n–-]+?)\s*[–-]\s*([^\n]+)", text)

    for name, place in numbered + bullets:
        name = re.sub(r"\s+", " ", name).strip()
        place = re.sub(r"\s+", " ", place).strip()
        if not name or _SPAEDEN_NOISE_RE.search(name):
            continue
        key = f"{normalize_name(name)}|{normalize_address(place)}"
        if key in seen:
            continue
        seen.add(key)
        results.append({
            'name': name,
            'formatted_address': place,
            'formatted_phone_number': "",
            'website': "",
            'emails': []
        })

    return results


def scrape_spaeden_rankings(session=None, location=None):
    results = []
    session = session or create_session()
    try:
        resp = session.get(SPAEDEN_RANKING_URL, headers={'User-Agent': get_random_user_agent()}, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        text = soup.get_text("\n")
        results = parse_spaeden_rankings(text)
        if location:
            loc_norm = strip_accents(location).lower().strip()
            results = [
                r for r in results
                if loc_norm in strip_accents(f"{r.get('formatted_address', '')} {r.get('name', '')}").lower()
            ]
        logger.info("SPAeden: extracted %s ranking entries%s", len(results), f" (filtered for {location})" if location else "")
    except Exception as e:
        logger.warning("SPAeden: error fetching rankings: %s", e)
    return results


def extract_fresha_name_address(container):
    lines = [ln.strip() for ln in container.get_text("\n").splitlines() if ln.strip()]
    if not lines:
        return "", ""

    skip_tokens = {"Show number", "Call to book"}
    cleaned = [ln for ln in lines if ln not in skip_tokens]

    address = ""
    name = ""
    for i, ln in enumerate(cleaned):
        if ", Poland" in ln:
            address = ln
            # find previous meaningful line as name
            for j in range(i - 1, -1, -1):
                if cleaned[j] and ", Poland" not in cleaned[j]:
                    name = cleaned[j]
                    break
            break

    if not name and cleaned:
        name = cleaned[0]

    return name, address


# Cache robots parser per host
_ROBOTS_CACHE = {}

FRESHA_SITEMAPS = [
    "https://www.fresha.com/sitemap.xml",
    "https://www.fresha.com/lp/en/sitemap-landing-pages.xml",
    "https://www.fresha.com/lp/en/sitemap-lite-venue-pages.xml",
    "https://sitemaps.fresha.com/www/sitemap-salons.xml",
]


def _get_robots_parser(base_url, session=None):
    # Return a RobotFileParser for base_url, cached
    if base_url in _ROBOTS_CACHE:
        return _ROBOTS_CACHE[base_url]
    parser = RobotFileParser()
    robots_url = f"{base_url.rstrip('/')}/robots.txt"
    try:
        if session is None:
            import requests
            r = requests.get(robots_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
            if r.status_code == 200:
                parser.parse(r.text.splitlines())
            else:
                parser.read()  # fallback to default (may raise)
        else:
            r = session.get(robots_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
            if r.status_code == 200:
                parser.parse(r.text.splitlines())
            else:
                parser.read()
    except Exception:
        try:
            parser.read()
        except Exception:
            pass
    _ROBOTS_CACHE[base_url] = parser
    return parser


def _fetch_sitemap_urls(session, location=None):
    """Fetch Fresha sitemaps and return a list of candidate URLs. Filters by location if provided."""
    urls = []
    for sitemap in FRESHA_SITEMAPS:
        try:
            r = session.get(sitemap, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
            if r.status_code != 200:
                logger.debug("Fresha sitemap fetch returned %s for %s", r.status_code, sitemap)
                continue
            # naive XML parse for <loc>
            import re
            found = re.findall(r"<loc>(.*?)</loc>", r.text)
            for loc in found:
                loc = loc.strip()
                if not loc:
                    continue
                if location:
                    if location.lower() in loc.lower() or f"/pl-" in loc or "/pl/" in loc:
                        urls.append(loc)
                else:
                    urls.append(loc)
        except Exception as e:
            logger.debug("Fresha: error fetching sitemap %s: %s", sitemap, e)
    # dedupe preserving order
    seen = set(); out = []
    for u in urls:
        if u not in seen:
            seen.add(u); out.append(u)
    return out


def _fetch_with_retries(session, url, max_retries=3, backoff=1.0):
    """Fetch url with retries for 5xx responses; returns (response or None, status, allowed)"""
    parsed = requests.utils.urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    robots = _get_robots_parser(base, session=session)
    allowed = robots.can_fetch("*", url)
    if not allowed:
        logger.info("Fresha: disallowed by robots.txt: %s", url)
        return None, None, False

    attempt = 0
    while attempt < max_retries:
        try:
            r = session.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
            logger.debug("Fresha fetch %s -> %s", url, r.status_code)
            if r.status_code == 200:
                return r, 200, True
            if r.status_code in (403, 404):
                logger.warning("Fresha: %s for %s", r.status_code, url)
                return r, r.status_code, True
            if 500 <= r.status_code < 600:
                # retry with backoff
                attempt += 1
                time.sleep(backoff * (2 ** (attempt - 1)))
                continue
            # other statuses: return as-is
            return r, r.status_code, True
        except requests.RequestException as e:
            logger.warning("Fresha: request error for %s: %s", url, e)
            attempt += 1
            time.sleep(backoff * (2 ** (attempt - 1)))
    logger.warning("Fresha: failed to fetch after retries: %s", url)
    return None, None, True


def scrape_fresha(query, location, session=None, max_pages=3, use_headless=False, headless_wait=3):
    results = []
    session = session or create_session()

    category = map_query_to_category(query) or "spa"
    keyword_filter = fresha_category_keywords(category)
    business_type = fresha_business_type_for_query(query)
    location_slug = slugify(location)
    base_urls = []
    if location_slug:
        base_urls.append(f"{FRESHA_BASE}/lp/en/bt/{business_type}/in/pl-{location_slug}")
        base_urls.append(f"{FRESHA_BASE}/lp/en/bt/{business_type}/in/{location_slug}")
    base_urls.append(f"{FRESHA_BASE}/lp/en/bt/{business_type}")
    if location:
        base_urls.append(f"{FRESHA_BASE}/search?q={quote_plus(location)}")
        base_urls.append(f"{FRESHA_BASE}/search?q={quote_plus(f'{query} {location}'.strip())}")

    def parse_fresha_html(html, seen_keys):
        soup = BeautifulSoup(html, 'html.parser')
        page_added = 0
        for a in soup.find_all('a', href=True):
            href = a['href']
            if "/lvp/" not in href:
                continue
            full_url = href if href.startswith("http") else f"{FRESHA_BASE}{href}"
            container = a.find_parent()
            if not container:
                continue
            container_text = container.get_text(" ").lower()
            if keyword_filter and not any(k in container_text for k in keyword_filter):
                continue
            name, address = extract_fresha_name_address(container)
            if not name:
                continue
            key = f"{normalize_name(name)}|{normalize_address(address)}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            page_added += 1
            results.append({
                'name': name,
                'formatted_address': address,
                'formatted_phone_number': "",
                'website': full_url,
                'emails': []
            })
        return page_added

    seen = set()

    # Prefer sitemap-driven discovery
    sitemap_urls = _fetch_sitemap_urls(session, location=location)
    if sitemap_urls:
        logger.info("Fresha: discovered %s candidate URLs from sitemaps", len(sitemap_urls))
        for url in sitemap_urls:
            r, status, allowed = _fetch_with_retries(session, url)
            logger.info("Fresha fetch: url=%s status=%s allowed=%s", url, status, allowed)
            if not allowed:
                continue
            if r is None or status in (403, 404):
                continue
            html = r.text
            page_added = parse_fresha_html(html, seen)
            # if nothing added and headless is allowed, try rendering
            if page_added == 0 and use_headless:
                rendered = get_rendered_html(url, wait_seconds=headless_wait, timeout=25)
                if rendered:
                    page_added = parse_fresha_html(rendered, seen)
            logger.info("Fresha: extracted %s listings from %s", page_added, url)
            time.sleep(random.uniform(0.5, 1.5))
    else:
        # Fallback to previous search-based approach, but respect robots and headers
        logger.info("Fresha: no sitemaps found or empty; falling back to search pages")
        for base_url in base_urls:
            total_added_for_base = 0
            for page in range(1, max_pages + 1):
                url = base_url if page == 1 else f"{base_url}?page={page}"
                r, status, allowed = _fetch_with_retries(session, url)
                logger.info("Fresha fetch: url=%s status=%s allowed=%s", url, status, allowed)
                if not allowed:
                    break
                if r is None:
                    break
                if status in (403, 404):
                    break
                page_added = parse_fresha_html(r.text, seen)
                if page_added == 0 and use_headless:
                    rendered = get_rendered_html(url, wait_seconds=headless_wait, timeout=25)
                    if rendered:
                        page_added = parse_fresha_html(rendered, seen)
                logger.info("Fresha: extracted %s listings from %s", page_added, url)
                total_added_for_base += page_added
                if page_added == 0:
                    break
                time.sleep(random.uniform(1.0, 2.0))

            # If we added results for this base_url, stop checking other base URLs
            if total_added_for_base > 0:
                break

    return results


def scrape_moment(query, location, session=None, max_pages=3):
    logger.info("Moment.pl redirects to Booksy; skipping (results already collected via Booksy).")
    return []  # Booksy results are already collected, no need to deep scrape again


def extract_cylex_profile(profile_url, session=None, use_headless=False, headless_wait=3):
    session = session or create_session()
    try:
        html = fetch_html(profile_url, session=session, use_headless=use_headless, headless_wait=headless_wait)
        if not html:
            return None
        if "cloudflare" in html.lower() or "attention required" in html.lower():
            logger.warning("Cylex: Cloudflare block detected for %s", profile_url)
            return None
        soup = BeautifulSoup(html, 'lxml')

        name_tag = soup.find(attrs={'itemprop': 'name'}) or soup.find('h1')
        name = name_tag.get_text(' ', strip=True) if name_tag else ""

        street = soup.find(attrs={'itemprop': 'streetAddress'})
        postal = soup.find(attrs={'itemprop': 'postalCode'})
        locality = soup.find(attrs={'itemprop': 'addressLocality'})
        address_parts = []
        if street:
            address_parts.append(street.get_text(' ', strip=True))
        if postal:
            address_parts.append(postal.get_text(' ', strip=True))
        if locality:
            address_parts.append(locality.get_text(' ', strip=True))
        address = ', '.join([p for p in address_parts if p])

        phone_tag = soup.find(attrs={'itemprop': 'telephone'})
        phone = phone_tag.get_text(' ', strip=True) if phone_tag else ""

        website = ""
        website_tag = soup.find(attrs={'itemprop': 'url'})
        if website_tag and website_tag.get('href'):
            website = website_tag['href']
        else:
            for a in soup.find_all('a', href=True):
                href = a['href']
                if href.startswith('http') and CYLEX_BASE not in href:
                    website = href
                    break

        return {
            'name': name,
            'formatted_address': address,
            'formatted_phone_number': phone,
            'website': website or profile_url,
            'profile_url': profile_url,
            'emails': []
        }
    except Exception as e:
        logger.warning("Cylex: profile fetch error %s: %s", profile_url, e)
        return None


def scrape_cylex(query, location, session=None, max_pages=3, use_headless=False, headless_wait=3):
    results = []
    session = session or create_cffi_session()
    keywords = category_keywords_for_query(query)

    seen_profiles = set()
    max_profiles = max_pages * 20

    for url in cylex_search_urls(query, location):
        try:
            html = fetch_html(url, session=session, use_headless=use_headless, headless_wait=headless_wait)
            if not html:
                continue
            if "cloudflare" in html.lower() or "attention required" in html.lower():
                logger.warning("Cylex: Cloudflare block detected for %s", url)
                break
            soup = BeautifulSoup(html, 'lxml')

            profile_links = []
            for a in soup.find_all('a', href=True):
                href = a['href']
                if href.startswith('/'):
                    href = urljoin(CYLEX_BASE, href)
                if CYLEX_BASE not in href:
                    continue
                if "/firmy/" not in href or not href.endswith('.html'):
                    continue
                profile_links.append(href)

            profile_links = list(dict.fromkeys(profile_links))
            if not profile_links:
                logger.info("Cylex: no profiles found on %s", url)
                continue

            for profile_url in profile_links:
                if profile_url in seen_profiles:
                    continue
                seen_profiles.add(profile_url)
                profile = extract_cylex_profile(profile_url, session=session, use_headless=use_headless, headless_wait=headless_wait)
                if profile:
                    text_blob = f"{profile.get('name', '')} {profile.get('formatted_address', '')}".lower()
                    if keywords and not any(k in text_blob for k in keywords):
                        continue
                    results.append(profile)
                time.sleep(random.uniform(0.3, 0.8))
                if len(seen_profiles) >= max_profiles:
                    break

            logger.info("Cylex: collected %s profiles from %s", len(results), url)
            if results:
                break
        except Exception as e:
            logger.warning("Cylex: error fetching %s: %s", url, e)

    return results


def extract_oferteo_profile(profile_url, session=None):
    session = session or create_session()
    try:
        resp = session.get(profile_url, headers={'User-Agent': get_random_user_agent()}, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'lxml')
        name = ""
        address = ""
        phone = ""
        website = ""

        for script in soup.find_all('script', type='application/ld+json'):
            try:
                data = json.loads(script.get_text(strip=True))
            except Exception:
                continue
            if isinstance(data, list):
                items = data
            else:
                items = [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get('@type') in ("LocalBusiness", "Organization"):
                    name = item.get('name', '') or name
                    address_obj = item.get('address', {}) if isinstance(item.get('address'), dict) else {}
                    street = address_obj.get('streetAddress', '')
                    postal = address_obj.get('postalCode', '')
                    locality = address_obj.get('addressLocality', '')
                    address_parts = [p for p in [street, postal, locality] if p]
                    address = ', '.join(address_parts) or address
                    phone = item.get('telephone', '') or phone
                    website = item.get('url', '') or website

        if not name:
            name_tag = soup.find('h1') or soup.find(attrs={'itemprop': 'name'})
            name = name_tag.get_text(' ', strip=True) if name_tag else ""
        if phone == "-":
            phone = ""

        if not website:
            for a in soup.find_all('a', href=True):
                href = a['href']
                if href.startswith('http') and OFERTEO_BASE not in href and 'oferteo.pl' not in href:
                    website = href
                    break

        return {
            'name': name,
            'formatted_address': address,
            'formatted_phone_number': phone,
            'website': website or profile_url,
            'profile_url': profile_url,
            'emails': []
        }
    except Exception as e:
        logger.warning("Oferteo: profile fetch error %s: %s", profile_url, e)
        return None


def scrape_oferteo(query, location, session=None, max_pages=3):
    results = []
    session = session or create_session()
    keywords = category_keywords_for_query(query)
    seen_profiles = set()
    max_profiles = max_pages * 20

    for base_url in oferteo_paths_for_query(query, location):
        for page in range(1, max_pages + 1):
            if page == 1:
                url = base_url
            else:
                sep = '&' if '?' in base_url else '?'
                url = f"{base_url}{sep}page={page}"
            try:
                resp = session.get(url, headers={'User-Agent': get_random_user_agent()}, timeout=20)
                resp.raise_for_status()
                soup = BeautifulSoup(resp.text, 'lxml')

                profile_links = []
                for a in soup.find_all('a', href=True):
                    href = a['href']
                    if href.startswith('/'):
                        href = urljoin(OFERTEO_BASE, href)
                    if OFERTEO_BASE not in href:
                        continue
                    if "/firma/" in href:
                        profile_links.append(href.split('#')[0])

                profile_links = list(dict.fromkeys(profile_links))
                if not profile_links:
                    if page == 1:
                        logger.info("Oferteo: no profiles found on %s", url)
                    break

                page_added = 0
                for profile_url in profile_links:
                    if profile_url in seen_profiles:
                        continue
                    seen_profiles.add(profile_url)
                    profile = extract_oferteo_profile(profile_url, session=session)
                    if profile:
                        text_blob = f"{profile.get('name', '')} {profile.get('formatted_address', '')}".lower()
                        if keywords and not any(k in text_blob for k in keywords):
                            continue
                        results.append(profile)
                        page_added += 1
                    if len(seen_profiles) >= max_profiles:
                        break

                logger.info("Oferteo: collected %s profiles from %s", len(results), url)
                if page_added == 0 or len(seen_profiles) >= max_profiles:
                    break
                time.sleep(random.uniform(1.0, 2.0))
            except Exception as e:
                logger.warning("Oferteo: error fetching %s: %s", url, e)
                break

    return results


def extract_znanylekarz_profile(profile_url, session=None):
    session = session or create_session()
    try:
        resp = session.get(profile_url, headers={'User-Agent': get_random_user_agent()}, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'lxml')

        name = ""
        h1 = soup.find('h1')
        if h1:
            name = h1.get_text(' ', strip=True)

        street = soup.find(attrs={'itemprop': 'streetAddress'})
        postal = soup.find(attrs={'itemprop': 'postalCode'})
        locality = soup.find(attrs={'itemprop': 'addressLocality'})

        address_parts = []
        if street:
            address_parts.append(street.get_text(' ', strip=True))
        if postal:
            address_parts.append(postal.get_text(' ', strip=True))
        if locality:
            address_parts.append(locality.get_text(' ', strip=True))
        address = ', '.join([p for p in address_parts if p])

        phone_tag = soup.find(attrs={'itemprop': 'telephone'})
        phone = phone_tag.get_text(' ', strip=True) if phone_tag else ""

        return {
            'name': name,
            'formatted_address': address,
            'formatted_phone_number': phone,
            'website': profile_url,
            'emails': []
        }
    except Exception as e:
        logger.warning("ZnanyLekarz: profile fetch error %s: %s", profile_url, e)
        return None


def scrape_znanylekarz(query, location, session=None, max_pages=3):
    results = []
    session = session or create_session()

    path = znanylekarz_path_for_query(query)
    if not path:
        logger.info("ZnanyLekarz: no matching category for '%s'.", query)
        return results

    city_slug = slugify(location)
    if not city_slug:
        return results

    base_url = f"{ZNANYLEKARZ_BASE}/{path}/{city_slug}"
    seen_profiles = set()
    max_profiles = max_pages * 20

    for page in range(1, max_pages + 1):
        url = base_url if page == 1 else f"{base_url}?page={page}"
        try:
            resp = session.get(url, headers={'User-Agent': get_random_user_agent()}, timeout=20)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, 'lxml')

            page_profiles = []
            for a in soup.find_all('a', href=True):
                href = a['href']
                if href.startswith('/'):
                    href = f"{ZNANYLEKARZ_BASE}{href}"
                elif not href.startswith('http'):
                    continue
                if "znanylekarz.pl" not in href:
                    continue
                if "/uslugi-zabiegi/" in href:
                    continue
                # profile links look like /{slug}/{specialty}/{city}
                parts = href.replace(ZNANYLEKARZ_BASE, "").split('?')[0].strip('/').split('/')
                if len(parts) < 3:
                    continue
                if parts[-1] != city_slug:
                    continue
                page_profiles.append(href)

            page_profiles = list(dict.fromkeys(page_profiles))
            if not page_profiles:
                if page == 1:
                    logger.info("ZnanyLekarz: no profiles found for %s", url)
                break

            for profile_url in page_profiles:
                if profile_url in seen_profiles:
                    continue
                seen_profiles.add(profile_url)
                profile = extract_znanylekarz_profile(profile_url, session=session)
                if profile:
                    results.append(profile)
                if len(seen_profiles) >= max_profiles:
                    break

            logger.info("ZnanyLekarz: page %s profiles %s", page, len(page_profiles))
            if len(seen_profiles) >= max_profiles:
                break
            time.sleep(random.uniform(1.0, 2.0))
        except Exception as e:
            logger.warning("ZnanyLekarz: error fetching %s: %s", url, e)
            break

    return results


def scrape_fixly(query, location, session=None, max_pages=3, use_headless=False, headless_wait=3):
    results = []
    session = session or create_session()

    path = fixly_path_for_query(query)
    if not path:
        logger.info("Fixly: no matching category for '%s'.", query)
        return results

    city_slug = slugify(location)
    if not city_slug:
        return results

    base_url = f"{FIXLY_BASE}/{path}/{city_slug}"
    for page in range(1, max_pages + 1):
        url = base_url if page == 1 else f"{base_url}?page={page}"
        try:
            html = fetch_html(url, session=session, use_headless=use_headless, headless_wait=headless_wait)
            if not html:
                break
            soup = BeautifulSoup(html, 'lxml')

            # Best-effort: Fixly content is often JS-rendered. Try to capture any visible listings.
            cards = soup.find_all(attrs={'data-testid': re.compile('card|listing|offer', re.I)})
            if not cards and use_headless:
                rendered = get_rendered_html(url, wait_seconds=headless_wait, timeout=25)
                if rendered:
                    soup = BeautifulSoup(rendered, 'lxml')
                    cards = soup.find_all(attrs={'data-testid': re.compile('card|listing|offer', re.I)})
            if not cards:
                logger.info("Fixly: no listings found on %s", url)
                break

            for card in cards:
                name = ""
                address = ""
                link = ""
                a = card.find('a', href=True)
                if a:
                    link = a['href'] if a['href'].startswith('http') else f"{FIXLY_BASE}{a['href']}"
                name = card.get_text(' ', strip=True).split('  ')[0]
                if name:
                    results.append({
                        'name': name,
                        'formatted_address': address,
                        'formatted_phone_number': "",
                        'website': link,
                        'emails': []
                    })

            if not cards:
                break
            time.sleep(random.uniform(1.0, 2.0))
        except Exception as e:
            logger.warning("Fixly: error fetching %s: %s", url, e)
            break

    return results


def extract_firmynet_profile(profile_url, session=None, use_headless=False, headless_wait=3):
    session = session or create_session()
    try:
        html = fetch_html(profile_url, session=session, use_headless=use_headless, headless_wait=headless_wait)
        if not html:
            return None
        soup = BeautifulSoup(html, 'lxml')

        name_tag = soup.find('h1')
        name = name_tag.get_text(' ', strip=True) if name_tag else ""

        phone = ""
        phone_link = soup.find('a', href=re.compile(r'^tel:', re.I))
        if phone_link:
            phone = phone_link.get('href', '').replace('tel:', '').strip()

        emails = []
        for mail in soup.find_all('a', href=re.compile(r'^mailto:', re.I)):
            addr = mail.get('href', '').replace('mailto:', '').strip()
            if addr and '@' in addr:
                emails.append(addr)

        website = ""
        # Domains to skip (ads, trackers, social media, internal links)
        skip_domains = [
            'firmy.net', 'st-firmy.net', 'google.', 'facebook.com',
            'instagram.com', 'twitter.com', 'youtube.com', 'linkedin.com',
            'tiktok.com', 'pinterest.com', 'bing.com', 'yahoo.com',
            'doubleclick.net', 'googlesyndication.com', 'googletagmanager.com',
            'googleadservices.com', 'gstatic.com', 'googleapis.com',
            'cloudflare.com', 'cdn.', 'wp.pl', 'onet.pl',
        ]

        def is_valid_external(url):
            if not url or not url.startswith('http'):
                return False
            lower = url.lower()
            return not any(sd in lower for sd in skip_domains)

        # First try: look for a link with text like "strona www", "witryna", "odwiedź"
        www_link = soup.find('a', href=True, string=re.compile(
            r'(strona|www|witryna|odwied|website|homepage)', re.I
        ))
        if www_link:
            href = www_link.get('href')
            if is_valid_external(href):
                website = href

        # Second try: data-href/data-url (often used in buttons)
        if not website:
            for tag in soup.find_all(attrs={"data-href": True}):
                href = tag.get("data-href")
                if is_valid_external(href):
                    website = href
                    break
        if not website:
            for tag in soup.find_all(attrs={"data-url": True}):
                href = tag.get("data-url")
                if is_valid_external(href):
                    website = href
                    break

        # Third try: unwrap redirect links containing ?url= or ?u=
        if not website:
            for a in soup.find_all('a', href=True):
                href = a.get('href', '')
                if 'url=' in href or 'u=' in href:
                    try:
                        from urllib.parse import urlparse, parse_qs
                        parsed = urlparse(href)
                        params = parse_qs(parsed.query)
                        for key in ('url', 'u'):
                            if key in params:
                                candidate = params[key][0]
                                if is_valid_external(candidate):
                                    website = candidate
                                    break
                    except Exception:
                        pass
                if website:
                    break

        # Fallback: find first external link that isn't a skip domain
        if not website:
            for a in soup.find_all('a', href=True, rel=lambda r: r != 'nofollow' if r else True):
                href = a['href']
                if is_valid_external(href):
                    website = href
                    break

        address = ""
        # Try structured data first for address
        for script in soup.find_all('script', type='application/ld+json'):
            try:
                data = json.loads(script.get_text(strip=True))
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if isinstance(item, dict) and item.get('@type') == 'LocalBusiness':
                        addr_obj = item.get('address', {})
                        if isinstance(addr_obj, dict):
                            parts = [addr_obj.get('streetAddress', ''),
                                     addr_obj.get('postalCode', ''),
                                     addr_obj.get('addressLocality', '')]
                            address = ', '.join(p for p in parts if p)
                        if not website:
                            website = item.get('url', '') or ''
            except Exception:
                continue

        # Try to extract website from embedded JS/JSON blobs
        if not website:
            for script in soup.find_all('script'):
                text = script.get_text(" ", strip=True)
                if not text:
                    continue
                for url in re.findall(r"https?://[^\s'\"<>]+", text):
                    if is_valid_external(url):
                        website = url
                        break
                if website:
                    break
        # Fallback to meta description
        if not address:
            meta_desc = soup.find('meta', attrs={'name': 'description'})
            if meta_desc and meta_desc.get('content'):
                address = meta_desc['content']

        return {
            'name': name,
            'formatted_address': address,
            'formatted_phone_number': phone,
            'website': website,  # Don't fallback to profile_url — it's a catalog URL
            'profile_url': profile_url,
            'emails': list(dict.fromkeys([e for e in emails if '@' in e]))
        }
    except Exception as e:
        logger.warning("Firmy.net: profile fetch error %s: %s", profile_url, e)
        return None


def scrape_firmynet(query, location, session=None, max_pages=3, use_headless=False, headless_wait=3):
    results = []
    session = session or create_session()
    keywords = category_keywords_for_query(query)
    seen_profiles = set()
    max_profiles = max_pages * 20

    url = firmynet_search_url(query, location)
    html = fetch_html(url, session=session, use_headless=use_headless, headless_wait=headless_wait)
    if not html:
        return results
    soup = BeautifulSoup(html, 'lxml')

    profile_links = []
    # Try to scope extraction to actual result containers first
    result_containers = soup.select(
        'div.company-item, div.company-list-item, div.result-item, '
        'li.company, div.business-card, article.company, '
        'div[class*="company"], div[class*="result"], div[class*="listing"]'
    )
    if result_containers:
        for container in result_containers:
            first_link = container.find('a', href=True)
            if not first_link:
                continue
            href = first_link['href']
            if href.startswith('/'):
                href = urljoin(FIRMYNET_BASE, href)
            if not href.startswith(FIRMYNET_BASE):
                continue
            if ',' in href and href.endswith('.html'):
                profile_links.append(href)
    else:
        # Fallback: search all <a> but only within the main content area
        main_content = soup.find('main') or soup.find('div', id='content') or soup.find('div', class_='content') or soup
        for a in main_content.find_all('a', href=True):
            href = a['href']
            if href.startswith('/'):
                href = urljoin(FIRMYNET_BASE, href)
            if not href.startswith(FIRMYNET_BASE):
                continue
            # Skip sidebar/footer links: only accept links that look like direct profiles
            if ',' in href and href.endswith('.html'):
                # Exclude common non-profile patterns
                if any(x in href.lower() for x in ['/kategorie/', '/miasta/', '/regulamin', '/polityka', '/mapa-strony']):
                    continue
                profile_links.append(href)

    profile_links = list(dict.fromkeys(profile_links))
    logger.info("Firmy.net: found %s profile links on search page", len(profile_links))
    for profile_url in profile_links:
        if profile_url in seen_profiles:
            continue
        seen_profiles.add(profile_url)
        profile = extract_firmynet_profile(profile_url, session=session, use_headless=use_headless, headless_wait=headless_wait)
        if profile:
            text_blob = f"{profile.get('name', '')} {profile.get('formatted_address', '')}".lower()
            if keywords and not any(k in text_blob for k in keywords):
                continue
            results.append(profile)
        time.sleep(random.uniform(0.3, 0.8))
        if len(seen_profiles) >= max_profiles:
            break

    logger.info("Firmy.net: collected %s profiles", len(results))
    return results


def extract_biznesfinder_profile(profile_url, session=None, use_headless=False, headless_wait=3):
    session = session or create_session()
    try:
        html = fetch_html(profile_url, session=session, use_headless=use_headless, headless_wait=headless_wait)
        if not html:
            return None
        soup = BeautifulSoup(html, 'lxml')

        name = ""
        address = ""
        phone = ""
        website = ""
        emails = []

        for script in soup.find_all('script', type='application/ld+json'):
            try:
                data = json.loads(script.get_text(strip=True))
            except Exception:
                continue
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get('@type') == "LocalBusiness":
                    name = item.get('name', '') or name
                    phone = item.get('telephone', '') or phone
                    website = item.get('url', '') or website
                    email = item.get('email', '')
                    if email:
                        emails.append(email)
                    address_obj = item.get('address', {}) if isinstance(item.get('address'), dict) else {}
                    street = address_obj.get('streetAddress', '')
                    postal = address_obj.get('postalCode', '')
                    locality = address_obj.get('addressLocality', '')
                    address_parts = [p for p in [street, postal, locality] if p]
                    if address_parts:
                        address = ', '.join(address_parts)

        if not name:
            name_tag = soup.find('h1')
            name = name_tag.get_text(' ', strip=True) if name_tag else ""

        return {
            'name': name,
            'formatted_address': address,
            'formatted_phone_number': phone,
            'website': website or profile_url,
            'profile_url': profile_url,
            'emails': list(dict.fromkeys([e for e in emails if '@' in e]))
        }
    except Exception as e:
        logger.warning("BiznesFinder: profile fetch error %s: %s", profile_url, e)
        return None


def scrape_biznesfinder(query, location, session=None, max_pages=3, use_headless=False, headless_wait=3):
    results = []
    session = session or create_session()
    keywords = category_keywords_for_query(query)
    seen_profiles = set()
    max_profiles = max_pages * 20

    url = biznesfinder_search_url(query, location)
    html = fetch_html(url, session=session, use_headless=use_headless, headless_wait=headless_wait)
    if not html:
        return results
    soup = BeautifulSoup(html, 'lxml')

    profile_links = []
    for a in soup.find_all('a', href=True):
        href = a['href']
        if href.startswith('/'):
            href = urljoin(BIZNESFINDER_BASE, href)
        if not href.startswith(BIZNESFINDER_BASE):
            continue
        if any(seg in href for seg in ['/poradnik/', '/firma/', '/polska/', '/mapa', '/o-nas', '/kontakt']):
            continue
        if href.endswith('.html'):
            profile_links.append(href)

    profile_links = list(dict.fromkeys(profile_links))
    if not profile_links and use_headless:
        rendered = get_rendered_html(url, wait_seconds=headless_wait, timeout=25)
        if rendered:
            soup = BeautifulSoup(rendered, 'lxml')
            for a in soup.find_all('a', href=True):
                href = a['href']
                if href.startswith('/'):
                    href = urljoin(BIZNESFINDER_BASE, href)
                if href.startswith(BIZNESFINDER_BASE) and href.endswith('.html'):
                    if not any(seg in href for seg in ['/poradnik/', '/firma/', '/polska/', '/mapa']):
                        profile_links.append(href)
            profile_links = list(dict.fromkeys(profile_links))

    for profile_url in profile_links:
        if profile_url in seen_profiles:
            continue
        seen_profiles.add(profile_url)
        profile = extract_biznesfinder_profile(profile_url, session=session, use_headless=use_headless, headless_wait=headless_wait)
        if profile:
            text_blob = f"{profile.get('name', '')} {profile.get('formatted_address', '')}".lower()
            if keywords and not any(k in text_blob for k in keywords):
                continue
            results.append(profile)
        time.sleep(random.uniform(0.3, 0.8))
        if len(seen_profiles) >= max_profiles:
            break

    logger.info("BiznesFinder: collected %s profiles", len(results))
    return results


def setup_cache(db_path="cache.sqlite"):
    """Create a SQLite cache for website emails."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS email_cache (
            url TEXT PRIMARY KEY,
            emails TEXT,
            created_at INTEGER
        )
        """
    )
    conn.commit()
    return conn


def cache_get_emails(conn, url):
    cursor = conn.execute("SELECT emails FROM email_cache WHERE url = ?", (url,))
    row = cursor.fetchone()
    if row and row[0]:
        return row[0].split("|") if row[0] else []
    return None


def cache_set_emails(conn, url, emails):
    emails_text = "|".join(emails)
    conn.execute(
        "INSERT OR REPLACE INTO email_cache (url, emails, created_at) VALUES (?, ?, ?)",
        (url, emails_text, int(time.time()))
    )
    conn.commit()


def normalize_phone(phone):
    digits = re.sub(r'\D', '', phone or '')
    # Normalizuj prefix +48 → usuń, żeby ten sam numer zawsze dawał ten sam klucz dedup
    if digits.startswith('48') and len(digits) == 11:
        digits = digits[2:]
    return digits


def normalize_name(name):
    return re.sub(r"[^\w\s]", "", (name or "").lower()).strip()


def normalize_address(address):
    address = (address or "").lower()
    address = re.sub(r"\s+", " ", address)
    return address.strip()

def scrape_panorama_firm(query, location, max_pages=3, session=None, use_headless=False):
    """Scrapes data from Panorama Firm website."""
    results = []
    session = session or create_cffi_session()
    
    try:
        logger.info("Scraping data from Panorama Firm for: %s in %s", query, location)
        
        # Build the search URL
        search_query = f"{query} {location}"
        encoded_query = quote_plus(search_query)
        base_url = "https://panoramafirm.pl"
        
        for page in range(1, max_pages + 1):
            url = f"{base_url}/szukaj?k={encoded_query}&o={page}"
            
            headers = {
                'User-Agent': get_random_user_agent(),
                'Accept': 'text/html,application/xhtml+xml,application/xml',
                'Accept-Language': 'en-US,en;q=0.9,pl;q=0.8', # Prioritize English
                'Referer': 'https://panoramafirm.pl/',
            }
            
            logger.info("Fetching page %s from Panorama Firm...", page)
            response = session.get(url, headers=headers, timeout=15)
            response.raise_for_status() # Raise an exception for HTTP errors
            
            soup = BeautifulSoup(response.text, 'html.parser')
            businesses = (
                soup.select('div.card.company-item')
                or soup.select('article.company-card')
                or soup.select('[data-company-id]')
                or soup.select('div.company-item')
            )

            if not businesses:
                logger.info("No more results found on page %s", page)
                break

            for business in businesses:
                try:
                    # Basic data
                    name_elem = business.select_one('h2.company-name a') or business.select_one('h2.company-name')
                    name = name_elem.text.strip() if name_elem else "Unknown Name"

                    # Profile URL on panoramafirm.pl
                    profile_url = ""
                    if name_elem and name_elem.get('href'):
                        href = name_elem['href']
                        profile_url = href if href.startswith('http') else base_url + href

                    # Address
                    address_elem = business.select_one('div.address')
                    address = address_elem.text.strip() if address_elem else ""

                    # Phone
                    phone_elem = business.select_one('a[data-company-phone]')
                    phone = phone_elem.get('data-company-phone', "") if phone_elem else ""

                    # Website
                    website_elem = business.select_one('a.icon-website')
                    website = website_elem.get('href', "") if website_elem else ""

                    # Check if it's not an internal Panorama Firm link
                    if website and not website.startswith(('http://', 'https://')):
                        website = ""

                    # Try to reveal hidden email via Playwright click
                    emails = []
                    if use_headless and profile_url:
                        emails = _playwright_reveal_email(profile_url)

                    result = {
                        'name': name,
                        'formatted_address': address,
                        'formatted_phone_number': phone,
                        'website': website,
                        'profile_url': profile_url,
                        'emails': emails,
                    }

                    results.append(result)

                except Exception as e:
                    logger.warning("Error processing a business entry: %s", e)
            
            logger.info("Found %s businesses on page %s", len(businesses), page)
            time.sleep(random.uniform(1.5, 3.0))  # Random delay between pages
            
        return results
    
    except Exception as e:
        logger.error("Error during Panorama Firm scraping: %s", e)
        return results

def scrape_pkt_pl(query, location, max_pages=3, session=None, use_headless=False):
    """Scrapes data from PKT.pl website."""
    results = []
    session = session or create_cffi_session()
    
    try:
        logger.info("Scraping data from PKT.pl for: %s in %s", query, location)
        
        # Build the search URL
        search_query = f"{query} {location}"
        encoded_query = quote_plus(search_query)
        base_url = "https://www.pkt.pl"
        
        for page in range(1, max_pages + 1):
            url = f"{base_url}/szukaj/{encoded_query}/{page}"
            
            headers = {
                'User-Agent': get_random_user_agent(),
                'Accept': 'text/html,application/xhtml+xml,application/xml',
                'Accept-Language': 'en-US,en;q=0.9,pl;q=0.8',
                'Referer': 'https://www.pkt.pl/',
            }
            
            logger.info("Fetching page %s from PKT.pl...", page)
            response = session.get(url, headers=headers, timeout=15)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            businesses = (
                soup.select('li.list-items')
                or soup.select('div.result-item')
                or soup.select('li[data-id]')
                or soup.select('div.company-result')
            )

            if not businesses:
                logger.info("No more results found on page %s", page)
                break

            for business in businesses:
                try:
                    # Basic data
                    name_elem = business.select_one('h2.company-name a')
                    name = name_elem.text.strip() if name_elem else "Unknown Name"

                    # Profile URL on PKT.pl
                    profile_url = ""
                    if name_elem and name_elem.get('href'):
                        href = name_elem['href']
                        profile_url = href if href.startswith('http') else base_url + href

                    # Address
                    address_elem = business.select_one('address.rest-address')
                    address = address_elem.text.strip() if address_elem else ""

                    # Phone
                    phone_elem = business.select_one('a.icon-telephone')
                    phone = phone_elem.text.strip() if phone_elem else ""

                    # Website
                    website_elem = business.select_one('a.company-url')
                    website = website_elem.get('href', "") if website_elem else ""

                    # Check if it's not an internal PKT.pl link
                    if website and base_url in website:
                        website = ""

                    # Try to reveal hidden email via Playwright click
                    emails = []
                    if use_headless and profile_url:
                        emails = _playwright_reveal_email(profile_url)

                    result = {
                        'name': name,
                        'formatted_address': address,
                        'formatted_phone_number': phone,
                        'website': website,
                        'profile_url': profile_url,
                        'emails': emails,
                    }

                    results.append(result)

                except Exception as e:
                    logger.warning("Error processing a business entry: %s", e)
            
            logger.info("Found %s businesses on page %s", len(businesses), page)
            time.sleep(random.uniform(1.5, 3.0))  # Random delay between pages
            
        return results
    
    except Exception as e:
        logger.error("Error during PKT.pl scraping: %s", e)
        return results

# ---------------------------------------------------------------------------
# KRS API (Krajowy Rejestr Sądowy) — free REST API from Ministry of Justice
#
# Dwa API o różnych rolach:
#   KRS_API_BASE    = api-rs.ms.gov.pl  → wyszukiwarka (search by PKD/name/city)
#   KRS_LOOKUP_BASE = api-krs.ms.gov.pl → lookup po numerze KRS (OdpisAktualny)
# ---------------------------------------------------------------------------

# Mapping: internal category -> list of PKD codes to query
KRS_PKD_MAP = {
    "spa":         ["96.04.Z", "93.13.Z", "93.11.Z"],  # wellness, siłownie, obiekty sportowe
    "wellness":    ["96.04.Z"],
    "masaz":       ["96.04.Z"],
    "fizjoterapia":["86.90.A", "86.90.B", "86.10.Z"],  # fizjoterapia, inne usługi zdrowotne
    "joga":        ["93.13.Z", "85.51.Z"],              # siłownie, pozaszkolne formy edukacji
    "uroda":       ["96.02.Z", "96.09.Z"],              # fryzjerstwo, pozostałe usługi
    "fryzjer":     ["96.02.Z"],
    "hotel":       ["55.10.Z", "55.20.Z", "93.21.Z"],  # hotele, obiekty noclegowe, parki rozrywki
    "restaurant":  ["56.10.A", "56.10.B", "56.30.Z"],  # restauracje, ruchome placówki, bary
}


def _extract_krs_number(url):
    """Wyciąga numer KRS z URL profilu ekrs.ms.gov.pl lub z dowolnego tekstu.

    Obsługiwane formaty:
      https://ekrs.ms.gov.pl/web/wyszukiwarka-krs/strona-glowna?numer=0000123456
      /krs/0000123456
      numer KRS: 0000123456
    """
    if not url:
        return ''
    m = re.search(r'numer=(\d{10})', url)
    if m:
        return m.group(1)
    m = re.search(r'/krs/(\d{10})', url)
    if m:
        return m.group(1)
    m = re.search(r'\b(\d{10})\b', url)
    if m:
        return m.group(1)
    return ''


def fetch_krs_aktualny(krs_num, session=None, rejestr='P'):
    """Pobiera OdpisAktualny z oficjalnego API api-krs.ms.gov.pl dla podanego numeru KRS.

    Endpoint: GET /api/krs/OdpisAktualny/{krs}?rejestr={rejestr}&format=json
    Zwraca dict z polami: name, formatted_address, formatted_phone_number,
    website, emails, profile_url  — lub None gdy błąd/404.
    """
    if not krs_num:
        return None
    session = session or create_session()
    url = f"{KRS_LOOKUP_BASE}/api/krs/OdpisAktualny/{krs_num}"
    try:
        resp = session.get(
            url,
            params={'rejestr': rejestr, 'format': 'json'},
            headers={'Accept': 'application/json'},
            timeout=20,
        )
        if resp.status_code == 404:
            logger.debug("KRS OdpisAktualny: podmiot %s nie znaleziony", krs_num)
            return None
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.debug("KRS OdpisAktualny: błąd dla %s: %s", krs_num, e)
        return None

    # Odpowiedź ma strukturę zależną od formy prawnej; próbujemy kilka ścieżek
    try:
        odpis = data.get('odpis') or data
        dane = odpis.get('dane', {}) or {}

        # Nazwa
        name = (
            dane.get('nazwa') or
            dane.get('nazwaSkrocona') or
            (odpis.get('naglowekA', {}) or {}).get('firmaNazwa') or
            ''
        ).strip()

        # Adres siedziby
        adres_dict = (
            dane.get('adresSiedziby') or
            dane.get('adres') or
            (odpis.get('dzial1', {}) or {}).get('siedzibaIAdres', {}) or
            {}
        )
        street  = adres_dict.get('ulica') or adres_dict.get('adresUlica') or ''
        building = adres_dict.get('nrDomu') or adres_dict.get('nrLokalu') or ''
        postcode = adres_dict.get('kodPocztowy') or ''
        city    = adres_dict.get('miejscowosc') or adres_dict.get('miasto') or ''
        address = ' '.join(filter(None, [street, building, postcode, city])).strip()

        # Kontakt
        kontakt = dane.get('kontakt') or {}
        email   = (kontakt.get('email') or dane.get('email') or dane.get('adresEmail') or '').strip()
        phone   = (kontakt.get('telefon') or dane.get('telefon') or '').strip()
        website = (kontakt.get('stronaWWW') or dane.get('stronaInternetowa') or dane.get('www') or '').strip()
        if website and not website.startswith('http'):
            website = 'https://' + website

        profile_url = f"https://ekrs.ms.gov.pl/web/wyszukiwarka-krs/strona-glowna?numer={krs_num}"

        return {
            'name': name,
            'formatted_address': address,
            'formatted_phone_number': phone,
            'website': website,
            'profile_url': profile_url,
            'emails': [email] if email else [],
        }
    except Exception as e:
        logger.debug("KRS OdpisAktualny: błąd parsowania dla %s: %s", krs_num, e)
        return None


def _parse_krs_subject(subject):
    """Extract fields from a KRS search result JSON object (api-rs.ms.gov.pl)."""
    dane = subject.get('dane', {}) or subject
    name = (dane.get('nazwa') or dane.get('nazwaSkrocona') or '').strip()
    if not name:
        return None

    adres = dane.get('adres', {}) or {}
    street   = adres.get('ulica', '') or ''
    building = adres.get('nrDomu', '') or ''
    city     = adres.get('miejscowosc', '') or ''
    postcode = adres.get('kodPocztowy', '') or ''
    address  = ' '.join(filter(None, [street, building, postcode, city])).strip()

    email   = (dane.get('email') or dane.get('adresEmail') or '').strip()
    website = (dane.get('stronaInternetowa') or dane.get('www') or '').strip()
    if website and not website.startswith('http'):
        website = 'https://' + website

    krs_num = dane.get('numerKRS') or dane.get('krs') or ''
    profile_url = (
        f"https://ekrs.ms.gov.pl/web/wyszukiwarka-krs/strona-glowna?numer={krs_num}"
        if krs_num else ''
    )
    return {
        'name': name,
        'formatted_address': address,
        'formatted_phone_number': '',
        'website': website,
        'profile_url': profile_url,
        'emails': [email] if email else [],
        '_krs_num': krs_num,  # tymczasowe pole — używane do enrichmentu, usuwane po merge
    }


def scrape_krs_api(query, location, session=None, max_results=100):
    """Wyszukuje firmy przez api-rs.ms.gov.pl (search), a następnie wzbogaca
    każdy wynik przez api-krs.ms.gov.pl/OdpisAktualny (lookup) — oficjalny endpoint
    Ministerstwa Sprawiedliwości zwracający pełne dane: email, stronę, adres.

    Strategia:
      1. Wyszukaj po kodach PKD + miejscowość (api-rs.ms.gov.pl)
      2. Fallback: wyszukaj po nazwie + miejscowość
      3. Dla każdego wyniku z numerem KRS — pobierz OdpisAktualny i uzupełnij pola
    """
    session = session or create_session()
    results = []
    seen_krs = set()

    category = map_query_to_category(query) or query.lower()
    pkd_codes = KRS_PKD_MAP.get(category, [])
    pkd_search_list = pkd_codes[:3]

    def _fetch_search_page(params):
        try:
            resp = session.get(
                f"{KRS_API_BASE}/v1/odpis",
                params=params,
                headers={'Accept': 'application/json'},
                timeout=20,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.debug("KRS search request failed: %s", e)
            return None

    def _collect_subjects(data):
        if not data:
            return []
        if isinstance(data, list):
            return data
        return (data.get('lista') or data.get('wyniki') or
                data.get('podmioty') or data.get('odpisy') or [])

    def _add_result(parsed):
        key = parsed['name'].lower()
        if key in seen_krs:
            return
        seen_krs.add(key)

        # Enrichment: jeśli mamy numer KRS, pobierz OdpisAktualny
        krs_num = parsed.pop('_krs_num', '')
        if krs_num:
            aktualny = fetch_krs_aktualny(krs_num, session=session)
            if aktualny:
                # Uzupełnij puste pola pełniejszymi danymi z OdpisAktualny
                if aktualny.get('name') and not parsed.get('name'):
                    parsed['name'] = aktualny['name']
                if aktualny.get('formatted_address') and not parsed.get('formatted_address'):
                    parsed['formatted_address'] = aktualny['formatted_address']
                if aktualny.get('formatted_phone_number'):
                    parsed['formatted_phone_number'] = aktualny['formatted_phone_number']
                if aktualny.get('website') and not parsed.get('website'):
                    parsed['website'] = aktualny['website']
                # Merge emails (OdpisAktualny jako pierwsze — oficjalne źródło)
                new_emails = aktualny.get('emails') or []
                existing  = parsed.get('emails') or []
                merged_emails = new_emails + [e for e in existing if e not in new_emails]
                parsed['emails'] = list(dict.fromkeys(merged_emails))
                time.sleep(0.3)

        results.append(parsed)

    # 1. Wyszukaj po kodach PKD
    for pkd in pkd_search_list:
        if len(results) >= max_results:
            break
        data = _fetch_search_page({
            'rejestr': 'P',
            'pkd': pkd,
            'miejscowosc': location,
            'maxWynikow': min(50, max_results - len(results)),
            'strona': 1,
        })
        for subj in _collect_subjects(data):
            if len(results) >= max_results:
                break
            parsed = _parse_krs_subject(subj)
            if parsed:
                _add_result(parsed)
        time.sleep(0.5)

    # 2. Fallback: wyszukaj po nazwie firmy
    if len(results) < 10:
        data = _fetch_search_page({
            'rejestr': 'P',
            'nazwa': query,
            'miejscowosc': location,
            'maxWynikow': min(50, max_results - len(results)),
            'strona': 1,
        })
        for subj in _collect_subjects(data):
            if len(results) >= max_results:
                break
            parsed = _parse_krs_subject(subj)
            if parsed:
                _add_result(parsed)

    logger.info("KRS API: found %s records for '%s' in %s", len(results), query, location)
    return results


def enrich_with_krs(results, session=None):
    """Post-processing: dla każdego rekordu z numerem KRS w profile_url — pobierz
    OdpisAktualny i uzupełnij brakujące pola (email, strona, telefon, adres).

    Wywołuj po merge_results(), przed zapisem do pliku.
    Modyfikuje listę in-place, zwraca liczbę uzupełnionych rekordów.
    """
    if not results:
        return 0
    session = session or create_session()
    enriched = 0

    for r in results:
        profile_url = r.get('profile_url', '')
        krs_num = _extract_krs_number(profile_url)
        if not krs_num:
            continue

        # Pomiń jeśli rekord ma już kompletne dane
        has_email   = bool(r.get('emails'))
        has_website = bool(r.get('website'))
        has_phone   = bool(r.get('formatted_phone_number'))
        if has_email and has_website and has_phone:
            continue

        aktualny = fetch_krs_aktualny(krs_num, session=session)
        if not aktualny:
            continue

        changed = False
        if not has_email and aktualny.get('emails'):
            r['emails'] = aktualny['emails']
            changed = True
        if not has_website and aktualny.get('website'):
            r['website'] = aktualny['website']
            changed = True
        if not has_phone and aktualny.get('formatted_phone_number'):
            r['formatted_phone_number'] = aktualny['formatted_phone_number']
            changed = True
        if not r.get('formatted_address') and aktualny.get('formatted_address'):
            r['formatted_address'] = aktualny['formatted_address']
            changed = True

        if changed:
            enriched += 1
            logger.info("KRS enrich: uzupełniono dane dla '%s' (KRS %s)", r.get('name', ''), krs_num)

        time.sleep(0.3)

    logger.info("KRS enrich: uzupełniono %s/%s rekordów", enriched, len(results))
    return enriched


# ---------------------------------------------------------------------------
# Aleo.com — Polish B2B catalog with company profiles
# ---------------------------------------------------------------------------

def _aleo_category_slug(query):
    """Map internal query/category to Aleo.com URL category segment."""
    category = map_query_to_category(query) or query.lower()
    mapping = {
        "spa":         "uslugi-kosmetyczne-i-spa",
        "wellness":    "uslugi-kosmetyczne-i-spa",
        "masaz":       "uslugi-kosmetyczne-i-spa",
        "fizjoterapia":"fizjoterapia-i-rehabilitacja",
        "joga":        "sport-i-rekreacja",
        "uroda":       "uslugi-kosmetyczne-i-spa",
        "fryzjer":     "fryzjerstwo",
        "hotel":       "hotele-i-noclegi",
        "restaurant":  "gastronomia",
    }
    return mapping.get(category, slugify(query))


def scrape_aleo(query, location, session=None, max_pages=3):
    """Scrape business listings from aleo.com — a Polish B2B catalog.

    Aleo profiles often include direct email addresses visible in static HTML.
    Install: no extra dependencies.
    """
    session = session or create_cffi_session()
    results = []
    seen = set()
    category_slug = _aleo_category_slug(query)
    city_slug = slugify(location)

    for page in range(1, max_pages + 1):
        if city_slug:
            url = f"{ALEO_BASE}/pl/firmy/{category_slug}/{city_slug}/?strona={page}"
        else:
            url = f"{ALEO_BASE}/pl/firmy/{category_slug}/?strona={page}"

        try:
            resp = session.get(url, headers={'User-Agent': get_random_user_agent()}, timeout=20)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, 'lxml')

            # Aleo company cards — selector may vary; try several
            cards = (soup.select('div.company-item') or
                     soup.select('article.company-card') or
                     soup.select('div[class*="company"]') or
                     soup.select('li.search-result'))
            if not cards:
                logger.info("Aleo: no cards on page %s (url=%s)", page, url)
                break

            for card in cards:
                try:
                    name_el = (card.select_one('h2 a') or card.select_one('h3 a') or
                               card.select_one('a.company-name') or card.select_one('a[class*="name"]'))
                    if not name_el:
                        continue
                    name = name_el.get_text(strip=True)
                    if not name:
                        continue

                    profile_href = name_el.get('href', '')
                    profile_url = profile_href if profile_href.startswith('http') else (ALEO_BASE + profile_href)

                    address_el = card.select_one('address') or card.select_one('[class*="address"]')
                    address = address_el.get_text(separator=' ', strip=True) if address_el else ''

                    phone_el = card.select_one('a[href^="tel:"]') or card.select_one('[class*="phone"]')
                    phone = ''
                    if phone_el:
                        phone = phone_el.get('href', '').replace('tel:', '').strip() or phone_el.get_text(strip=True)

                    website_el = card.select_one('a[href^="http"]:not([href*="aleo.com"])')
                    website = website_el.get('href', '') if website_el else ''

                    # Direct email in card (sometimes visible)
                    email_el = card.select_one('a[href^="mailto:"]')
                    emails = []
                    if email_el:
                        emails = [email_el.get('href', '').replace('mailto:', '').strip()]

                    key = name.lower()
                    if key in seen:
                        continue
                    seen.add(key)

                    results.append({
                        'name': name,
                        'formatted_address': address,
                        'formatted_phone_number': phone,
                        'website': website,
                        'profile_url': profile_url,
                        'emails': emails,
                    })
                except Exception as e:
                    logger.debug("Aleo: error parsing card: %s", e)

            logger.info("Aleo: page %s — %s results so far", page, len(results))
            time.sleep(random.uniform(1.5, 3.0))

        except Exception as e:
            logger.warning("Aleo: error fetching %s: %s", url, e)
            break

    return results


def search_places(query, location, session=None):
    """Searches for places using Google Places API."""
    if not API_KEY:
        logger.warning("Google Maps API key not found. Skipping Google Places search.")
        return [], None
    session = session or create_session()
    
    try:
        url = 'https://maps.googleapis.com/maps/api/place/textsearch/json'
        params = {'query': f'{query} in {location}', 'key': API_KEY}
        resp = session.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        
        if data.get('status') not in ['OK', 'ZERO_RESULTS']: # Check for OK or ZERO_RESULTS
            logger.error("API Error: %s - %s", data.get('status'), data.get('error_message', ''))
            return [], None
            
        return data.get('results', []), data.get('next_page_token')
    except requests.exceptions.RequestException as e:
        logger.error("Error during place search: %s", e)
        return [], None

def search_next_page(next_page_token, session=None):
    """Fetches the next page of results from Google Places API."""
    if not API_KEY:
        return [], None
    session = session or create_session()
        
    try:
        url = 'https://maps.googleapis.com/maps/api/place/textsearch/json'
        params = {'pagetoken': next_page_token, 'key': API_KEY}
        resp = session.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        
        if data.get('status') not in ['OK', 'ZERO_RESULTS']:
            logger.error("API Error: %s - %s", data.get('status'), data.get('error_message', ''))
            return [], None
            
        return data.get('results', []), data.get('next_page_token')
    except requests.exceptions.RequestException as e:
        logger.error("Error fetching next page: %s", e)
        return [], None

_CITY_COORDS = {
    "wrocław": (51.1079, 17.0385), "warszawa": (52.2297, 21.0122),
    "kraków": (50.0647, 19.9450), "poznań": (52.4064, 16.9252),
    "gdańsk": (54.3520, 18.6466), "gdynia": (54.5189, 18.5305),
    "sopot": (54.4416, 18.5601), "łódź": (51.7592, 19.4560),
    "katowice": (50.2649, 19.0238), "bielsko-biała": (49.8225, 19.0444),
    "częstochowa": (50.8118, 19.1203), "gliwice": (50.2945, 18.6714),
    "zabrze": (50.3249, 18.7857), "bytom": (50.3481, 18.9320),
    "rybnik": (50.0971, 18.5430), "tychy": (50.1357, 18.9968),
    "sosnowiec": (50.2863, 19.1041), "chorzów": (50.2976, 18.9540),
    "dąbrowa górnicza": (50.3248, 19.1993), "szczecin": (53.4285, 14.5528),
    "bydgoszcz": (53.1235, 18.0084), "lublin": (51.2465, 22.5684),
    "białystok": (53.1325, 23.1688), "rzeszów": (50.0412, 21.9991),
    "kielce": (50.8661, 20.6286), "olsztyn": (53.7784, 20.4801),
    "toruń": (53.0138, 18.5981), "radom": (51.4027, 21.1471),
    "opole": (50.6751, 17.9213), "zielona góra": (51.9356, 15.5062),
    "tarnów": (50.0121, 20.9858), "koszalin": (54.1943, 16.1716),
    "legnica": (51.2070, 16.1619), "wałbrzych": (50.7714, 16.2843),
    "jelenia góra": (50.9044, 15.7197), "lubin": (51.4010, 16.1996),
    "głogów": (51.6656, 16.0843), "świdnica": (50.8448, 16.4875),
    "włocławek": (52.6484, 19.0677), "grudziądz": (53.4837, 18.7536),
    "inowrocław": (52.7980, 18.2585), "zamość": (50.7230, 23.2520),
    "chełm": (51.1432, 23.4716), "biała podlaska": (52.0318, 23.1160),
    "gorzów wielkopolski": (52.7325, 15.2369), "nowa sól": (51.8020, 15.7201),
    "piotrków trybunalski": (51.4058, 19.7030), "pabianice": (51.6640, 19.3540),
    "tomaszów mazowiecki": (51.5253, 20.0111), "bełchatów": (51.3618, 19.3596),
    "nowy sącz": (49.6237, 20.6926), "oświęcim": (50.0344, 19.2044),
    "chrzanów": (50.1332, 19.4027), "płock": (52.5463, 19.7065),
    "siedlce": (52.1676, 22.2903), "pruszków": (52.1701, 20.7990),
    "legionowo": (52.4046, 20.9385), "kędzierzyn-koźle": (50.3481, 18.2198),
    "nysa": (50.4748, 17.3328), "przemyśl": (49.7838, 22.7677),
    "stalowa wola": (50.5825, 22.0524), "mielec": (50.2887, 21.4215),
    "suwałki": (54.1115, 22.9306), "łomża": (53.1782, 22.0591),
    "słupsk": (54.4641, 17.0286), "tczew": (53.7771, 18.7794),
    "wejherowo": (54.6059, 18.2344), "kalisz": (51.7611, 18.0910),
    "konin": (52.2230, 18.2511), "piła": (53.1513, 16.7383),
    "ostrów wielkopolski": (51.6500, 17.8167), "gniezno": (52.5354, 17.5986),
    "stargard": (53.3353, 15.0491), "kołobrzeg": (54.1758, 15.5753),
    "świnoujście": (53.9108, 14.2439), "elbląg": (54.1560, 19.4044),
    "ełk": (53.8284, 22.3571), "ostrowiec świętokrzyski": (50.9300, 21.3842),
    "starachowice": (51.0384, 21.0692),
}


def get_city_coords(city, cache_conn=None, session=None):
    """Return (lat, lng) for a city. Checks hardcoded dict first, then sqlite cache,
    then falls back to Places findplacefromtext (no Geocoding API needed)."""
    key = city.lower().strip()

    # 1. Hardcoded dict — instant, no API
    if key in _CITY_COORDS:
        return _CITY_COORDS[key]

    # 2. SQLite cache
    if cache_conn:
        try:
            cache_conn.execute(
                "CREATE TABLE IF NOT EXISTS geocode_cache "
                "(city TEXT PRIMARY KEY, lat REAL, lng REAL)"
            )
            row = cache_conn.execute(
                "SELECT lat, lng FROM geocode_cache WHERE city=?", (key,)
            ).fetchone()
            if row:
                return (row[0], row[1])
        except Exception:
            pass

    # 3. Places findplacefromtext as geocoder (uses already-enabled Places API)
    if not API_KEY:
        return None
    session = session or create_session()
    try:
        url = 'https://maps.googleapis.com/maps/api/place/findplacefromtext/json'
        params = {
            'input': city,
            'inputtype': 'textquery',
            'fields': 'geometry',
            'key': API_KEY,
            'language': 'pl',
        }
        resp = session.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get('status') == 'OK' and data.get('candidates'):
            loc = data['candidates'][0].get('geometry', {}).get('location', {})
            lat, lng = loc.get('lat'), loc.get('lng')
            if lat and lng:
                if cache_conn:
                    try:
                        cache_conn.execute(
                            "INSERT OR REPLACE INTO geocode_cache (city, lat, lng) VALUES (?,?,?)",
                            (key, lat, lng)
                        )
                        cache_conn.commit()
                    except Exception:
                        pass
                return (lat, lng)
    except Exception as e:
        logger.warning("get_city_coords: failed for '%s': %s", city, e)

    return None


def get_place_details(place_id, session=None):
    """Fetches place details from Google Places API."""
    if not API_KEY:
        return {}
    session = session or create_session()
        
    try:
        url = 'https://maps.googleapis.com/maps/api/place/details/json'
        params = {
            'place_id': place_id,
            'fields': 'name,formatted_address,formatted_phone_number,website,business_status',
            'language': 'pl',
            'key': API_KEY
        }
        resp = session.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        
        if data.get('status') != 'OK':
            logger.error("Error fetching details for place %s: %s", place_id, data.get('status'))
            return {}
            
        return data.get('result', {})
    except requests.exceptions.RequestException as e:
        logger.error("Error fetching details for place %s: %s", place_id, e)
        return {}


_GOOGLE_MATCH_STOPWORDS = frozenset({
    "spa", "day", "wellness", "salon", "studio", "gabinet", "centrum", "instytut",
    "klinika", "masaz", "masazu", "masaze", "massage", "urody", "uroda", "beauty",
    "kosmetyczny", "kosmetologia", "fryzjer", "fryzjerski", "barber", "hotel",
    "resort", "pensjonat", "restauracja", "restaurant", "kawiarnia", "cafe",
    "bistro", "fitness", "silownia", "gym", "joga", "yoga", "pilates", "sklep",
    "butik", "fizjoterapia", "rehabilitacja", "stomatologia", "dentysta",
    "odnowy", "biologicznej", "ciala", "twarzy", "zdrowia", "polska", "poland",
    "sp", "zoo", "o", "z", "i", "w", "na", "dla", "oraz", "the", "and", "by", "club",
})


def _distinctive_name_tokens(text, location=""):
    norm = strip_accents(strip_emoji(text or "")).lower()
    loc_tokens = set(re.findall(r"[a-z0-9]{2,}", strip_accents(location or "").lower()))
    tokens = re.findall(r"[a-z0-9]{2,}", norm)
    return {t for t in tokens if t not in _GOOGLE_MATCH_STOPWORDS and t not in loc_tokens and not t.isdigit()}


def _is_confident_google_match(source_name, candidate_name, source_address="", candidate_address="", location=""):
    """Sprawdza, czy wynik Google Places naprawdę odpowiada tej samej firmie (eliminuje fałszywe dopasowania)."""
    if not source_name or not candidate_name:
        return False
    src_norm = strip_accents(strip_emoji(source_name)).lower().strip()
    cand_norm = strip_accents(strip_emoji(candidate_name)).lower().strip()
    if not src_norm or not cand_norm:
        return False

    # Odrzuć oczywisty mismatch miasta, gdy adres kandydata nie zawiera docelowej lokalizacji (ani adresu źródła)
    if location and candidate_address:
        loc_norm = strip_accents(location).lower().strip()
        cand_addr_norm = strip_accents(candidate_address).lower()
        src_addr_norm = strip_accents(source_address or "").lower()
        if loc_norm and loc_norm not in cand_addr_norm:
            # Zezwól tylko, jeśli źródło samo miało inny adres i oba mają wspólny kod pocztowy/token miasta
            src_pc = re.search(r"\b\d{2}-\d{3}\b", src_addr_norm)
            cand_pc = re.search(r"\b\d{2}-\d{3}\b", cand_addr_norm)
            if not (src_pc and cand_pc and src_pc.group(0) == cand_pc.group(0)):
                return False

    # 1. Dokładne zawieranie się pełnych oczyszczonych nazw (>= 5 znaków)
    if len(src_norm) >= 5 and (src_norm in cand_norm or cand_norm in src_norm):
        return True

    # 2. Porównanie tokenów wyróżniających (bez słów branżowych typu "spa", "salon", miasta)
    src_toks = _distinctive_name_tokens(source_name, location=location)
    cand_toks = _distinctive_name_tokens(candidate_name, location=location)
    if not src_toks or not cand_toks:
        return False

    overlap = src_toks & cand_toks
    if not overlap:
        # Pozwól na dopasowanie prefiksowe długiego tokenu marki (np. "enklawa" vs "enklawaspa")
        prefix_hit = any(
            (len(a) >= 5 and len(b) >= 5 and (a in b or b in a))
            for a in src_toks for b in cand_toks
        )
        if not prefix_hit:
            return False
        return True

    min_len = min(len(src_toks), len(cand_toks))
    containment = len(overlap) / max(1, min_len)
    # Co najmniej 1 mocny token (>= 4 znaki) i pokrycie >= 50% mniejszego zbioru
    has_strong_tok = any(len(t) >= 4 for t in overlap)
    return has_strong_tok and containment >= 0.5


def _needs_website_enrichment(record):
    """Zwraca True, gdy rekord nie ma prawdziwej strony WWW (brak, katalog typu Booksy/Fresha, lub samo FB/IG)."""
    w = (record.get('website') or '').strip()
    if not w:
        return True
    if is_catalog_url(w):
        return True
    wl = w.lower()
    if any(soc in wl for soc in ('facebook.com', 'fb.com', 'instagram.com')):
        return True
    return False


def enrich_with_google_places(results, location, session=None, cache_conn=None):
    """For records missing a real company website (empty, catalog-only, or social-only),
    look them up via Google Places findplacefromtext → place/details with strict name+city verification.
    """
    if not API_KEY:
        return

    session = session or create_session()
    try:
        from attio_sync import extract_social_links
    except ImportError:
        extract_social_links = lambda *a: {'facebook': '', 'instagram': ''}

    candidates = [r for r in results if _needs_website_enrichment(r)]
    if not candidates:
        logger.info("Google enrichment: all records already have a company website — skipping")
        return

    logger.info("=== Google Places enrichment: %d records missing real website (seeking emails) ===", len(candidates))

    coords = get_city_coords(location, cache_conn=cache_conn, session=session)
    if coords:
        location_bias = f'circle:30000@{coords[0]},{coords[1]}'
        logger.info("Google enrichment: locationbias set to %s", location_bias)
    else:
        location_bias = None
        logger.warning("Google enrichment: could not resolve coords for '%s', locationbias disabled", location)

    enriched = 0
    rejected_mismatch = 0
    for item in candidates:
        name = item.get('name', '').strip()
        if not name or _is_booksy_noise_name(name):
            continue
        query_text = f"{name} {location}"
        try:
            url = 'https://maps.googleapis.com/maps/api/place/findplacefromtext/json'
            params = {
                'input': query_text,
                'inputtype': 'textquery',
                'fields': 'place_id,name,formatted_address,business_status',
                'key': API_KEY,
                'language': 'pl',
            }
            if location_bias:
                params['locationbias'] = location_bias
            resp = session.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            if data.get('status') != 'OK' or not data.get('candidates'):
                continue

            cand = data['candidates'][0]
            if cand.get('business_status') == 'CLOSED_PERMANENTLY':
                continue

            cand_name = cand.get('name', '')
            cand_addr = cand.get('formatted_address', '')
            if not _is_confident_google_match(
                name, cand_name,
                source_address=item.get('formatted_address', ''),
                candidate_address=cand_addr,
                location=location,
            ):
                rejected_mismatch += 1
                logger.debug("Google enrichment: rejected mismatch '%s' != '%s' (%s)", name, cand_name, cand_addr)
                continue

            place_id = cand.get('place_id')
            if not place_id:
                continue

            details = get_place_details(place_id, session=session)
            if not details or details.get('business_status') == 'CLOSED_PERMANENTLY':
                continue

            phone = details.get('formatted_phone_number', '')
            website = details.get('website', '')

            # Jeśli obecna strona to FB/IG, zachowaj ją w dedykowanym polu przed nadpisaniem
            old_web = (item.get('website') or '').strip()
            if old_web:
                soc_old = extract_social_links(old_web)
                if soc_old['facebook'] and not item.get('facebook'):
                    item['facebook'] = soc_old['facebook']
                if soc_old['instagram'] and not item.get('instagram'):
                    item['instagram'] = soc_old['instagram']

            updated = False
            if phone and not item.get('formatted_phone_number'):
                item['formatted_phone_number'] = phone
                updated = True
            if website and not is_catalog_url(website):
                soc_new = extract_social_links(website)
                if soc_new['facebook'] and not item.get('facebook'):
                    item['facebook'] = soc_new['facebook']
                if soc_new['instagram'] and not item.get('instagram'):
                    item['instagram'] = soc_new['instagram']
                is_new_social_only = any(s in website.lower() for s in ('facebook.com', 'fb.com', 'instagram.com'))
                if not old_web or is_catalog_url(old_web) or (not is_new_social_only and any(s in old_web.lower() for s in ('facebook.com', 'fb.com', 'instagram.com'))):
                    item['website'] = website
                    updated = True
            if cand_addr and not item.get('formatted_address'):
                item['formatted_address'] = cand_addr
            if updated:
                sources = item.get('sources') or []
                if 'google' not in sources:
                    item['sources'] = sources + ['google']
                enriched += 1
                logger.info("Google enrichment: ✓ %s → phone=%s website=%s", name, phone or '—', website or '—')

            time.sleep(random.uniform(0.2, 0.45))

        except Exception as e:
            logger.warning("Google enrichment: error for '%s': %s", name, e)

    logger.info(
        "Google enrichment: filled %d/%d records (rejected %d low-confidence matches)",
        enriched, len(candidates), rejected_mismatch,
    )


def can_fetch_url(url, respect_robots=False):
    if not respect_robots:
        return True
    try:
        parsed = urlparse(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        rp = RobotFileParser()
        rp.set_url(robots_url)
        rp.read()
        return rp.can_fetch("*", url)
    except Exception:
        return True


def get_sitemap_urls(base_url, session=None, limit=10):
    session = session or create_session()
    try:
        sitemap_url = base_url.rstrip('/') + '/sitemap.xml'
        resp = session.get(sitemap_url, headers={'User-Agent': get_random_user_agent()}, timeout=15)
        if resp.status_code != 200:
            return []
        soup = BeautifulSoup(resp.text, 'xml')
        urls = [loc.get_text(strip=True) for loc in soup.find_all('loc')]
        urls = [u for u in urls if u.startswith(base_url)]
        # prioritize contact-like pages
        priority_keywords = ['kontakt', 'contact', 'o-nas', 'about', 'oferta', 'cennik', 'services']
        prioritized = [u for u in urls if any(k in u.lower() for k in priority_keywords)]
        others = [u for u in urls if u not in prioritized]
        return (prioritized + others)[:limit]
    except Exception:
        return []


def _deobfuscate_emails(text):
    """Convert obfuscated email formats to standard @ notation."""
    # kontakt [at] firma [dot] pl  ->  kontakt@firma.pl
    text = re.sub(r'\s*\[at\]\s*', '@', text, flags=re.IGNORECASE)
    text = re.sub(r'\s*\(at\)\s*', '@', text, flags=re.IGNORECASE)
    text = re.sub(r'\s*\[dot\]\s*', '.', text, flags=re.IGNORECASE)
    text = re.sub(r'\s*\(dot\)\s*', '.', text, flags=re.IGNORECASE)
    # kontakt AT firma DOT pl  (standalone words only)
    text = re.sub(r'(?<=[A-Za-z0-9])\s+AT\s+(?=[A-Za-z0-9])', '@', text)
    text = re.sub(r'(?<=[A-Za-z0-9])\s+DOT\s+(?=[A-Za-z0-9])', '.', text)
    return text


_WEBSITE_META_CACHE: dict = {}


def _extract_social_and_title_from_soup(soup):
    """Wyciąga linki Facebook/Instagram oraz nazwę marki (og:site_name / JSON-LD / <title>) ze sparsowanego HTML."""
    meta = {'facebook': '', 'instagram': '', 'site_title': ''}
    if soup is None:
        return meta
    try:
        from attio_sync import extract_social_links, is_unclear_company_name
    except ImportError:
        return meta

    hrefs = [a.get('href', '').strip() for a in soup.find_all('a', href=True)]
    soc = extract_social_links(*hrefs)
    meta['facebook'] = soc['facebook']
    meta['instagram'] = soc['instagram']

    # 1. meta og:site_name
    og = soup.find('meta', attrs={'property': 'og:site_name'}) or soup.find('meta', attrs={'name': 'application-name'})
    if og and og.get('content'):
        cand = og['content'].strip()
        if not is_unclear_company_name(cand):
            meta['site_title'] = cand

    # 2. JSON-LD Organization / LocalBusiness (nazwa + sameAs social media)
    for script in soup.find_all('script', type='application/ld+json'):
        try:
            ld = json.loads(script.get_text(strip=True))
            items = ld if isinstance(ld, list) else [ld]
            for it in items:
                if not isinstance(it, dict):
                    continue
                if not meta['site_title'] and isinstance(it.get('name'), str):
                    cand = it['name'].strip()
                    if not is_unclear_company_name(cand):
                        meta['site_title'] = cand
                same_as = it.get('sameAs')
                if isinstance(same_as, list):
                    s2 = extract_social_links(*[str(x) for x in same_as])
                    meta['facebook'] = meta['facebook'] or s2['facebook']
                    meta['instagram'] = meta['instagram'] or s2['instagram']
                elif isinstance(same_as, str):
                    s2 = extract_social_links(same_as)
                    meta['facebook'] = meta['facebook'] or s2['facebook']
                    meta['instagram'] = meta['instagram'] or s2['instagram']
        except Exception:
            continue

    # 3. Fallback: pierwszy niegeneryczny człon <title>
    if not meta['site_title'] and soup.title and soup.title.string:
        raw_t = soup.title.string.strip()
        for part in re.split(r'\s*[\|–—•·]\s*|\s+-\s+', raw_t):
            p_clean = part.strip()
            if p_clean and not is_unclear_company_name(p_clean) and len(p_clean) <= 70:
                meta['site_title'] = p_clean
                break

    return meta


def extract_emails_from_website(url, session=None, cache_conn=None, respect_robots=False, use_headless=False, headless_wait=3):
    """Extracts email addresses from a website."""
    if not url:
        return []

    session = session or create_session()
    
    try:
        logger.info("Fetching emails from website: %s", url)
        # Add protocol if missing
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url

        if not can_fetch_url(url, respect_robots=respect_robots):
            logger.info("Blocked by robots.txt: %s", url)
            return []

        if cache_conn is not None:
            cached = cache_get_emails(cache_conn, url)
            if cached is not None:
                return cached
            
        headers = {
            'User-Agent': get_random_user_agent(),
            'Accept': 'text/html,application/xhtml+xml,application/xml',
            'Accept-Language': 'en-US,en;q=0.9,pl;q=0.8',
        }
        
        response = session.get(url, headers=headers, timeout=15, allow_redirects=True)
        response.raise_for_status()
        
        # Skip non-HTML responses (e.g. PDFs, images)
        content_type = response.headers.get('Content-Type', '')
        if 'text/html' not in content_type and 'text/plain' not in content_type:
            logger.info("Skipping non-HTML content: %s", content_type)
            return []
        
        # Decode obfuscated emails before regex matching
        raw_text = _deobfuscate_emails(response.text)

        # Use regex to find email addresses
        email_pattern = EMAIL_RE
        emails = email_pattern.findall(raw_text)
        
        # Also, get emails from contact pages if available
        soup = BeautifulSoup(response.text, 'html.parser')
        _WEBSITE_META_CACHE[url] = _extract_social_and_title_from_soup(soup)
        
        # Check subpages like "kontakt" or "contact"
        contact_links = []
        base_domain = '{uri.scheme}://{uri.netloc}'.format(uri=urlparse(url))
        keyword_hints = ['kontakt', 'contact', 'about', 'o-nas', 'oferta', 'cennik', 'price', 'services']
        for link in soup.find_all('a', href=True):
            href = link['href'].strip()
            # Skip mailto:, tel:, javascript: and anchor-only links
            if href.startswith(('mailto:', 'tel:', 'javascript:', '#', 'data:')):
                continue
            if any(keyword in href.lower() for keyword in keyword_hints):
                # Add full URL if it's relative
                if href.startswith('/'):
                    contact_links.append(base_domain + href)
                elif href.startswith('http'):
                    contact_links.append(href)
                elif '/' in href or '.' in href: # Only append if it looks like a path
                    contact_links.append(base_domain + '/' + href)
        
        # Remove duplicate contact links
        contact_links = list(set(contact_links))
        
        # Visit found contact pages (limit to 5 to avoid excessive requests)
        for contact_url in contact_links[:5]:
            try:
                if not can_fetch_url(contact_url, respect_robots=respect_robots):
                    continue
                contact_response = session.get(contact_url, headers={'User-Agent': get_random_user_agent()}, timeout=10, allow_redirects=True)
                contact_response.raise_for_status()
                contact_emails = re.findall(email_pattern, _deobfuscate_emails(contact_response.text))
                emails.extend(contact_emails)
            except Exception as e:
                logger.warning("Could not fetch contact page %s: %s", contact_url, e)

        # Sitemap crawl fallback (limit additional pages)
        if not emails:
            sitemap_urls = get_sitemap_urls(base_domain, session=session, limit=8)
            for sitemap_url in sitemap_urls:
                try:
                    if not can_fetch_url(sitemap_url, respect_robots=respect_robots):
                        continue
                    resp = session.get(sitemap_url, headers={'User-Agent': get_random_user_agent()}, timeout=10, allow_redirects=True)
                    resp.raise_for_status()
                    emails.extend(re.findall(email_pattern, _deobfuscate_emails(resp.text)))
                except Exception:
                    continue

        # Headless fallback for JS-rendered pages (Playwright first, then Chromium)
        if not emails and use_headless:
            rendered = render_with_playwright(url, timeout_ms=20000)
            if not rendered:
                rendered = get_rendered_html(url, wait_seconds=headless_wait, timeout=20)
            if rendered:
                emails.extend(re.findall(email_pattern, _deobfuscate_emails(rendered)))
                rendered_soup = BeautifulSoup(rendered, 'html.parser')
                rendered_links = []
                base_domain = '{uri.scheme}://{uri.netloc}'.format(uri=urlparse(url))
                for link in rendered_soup.find_all('a', href=True):
                    href = link['href'].strip()
                    if href.startswith(('mailto:', 'tel:', 'javascript:', '#', 'data:')):
                        continue
                    if any(keyword in href.lower() for keyword in ['kontakt', 'contact', 'about', 'o-nas']):
                        if href.startswith('/'):
                            rendered_links.append(base_domain + href)
                        elif href.startswith('http'):
                            rendered_links.append(href)
                        elif '/' in href or '.' in href:
                            rendered_links.append(base_domain + '/' + href)
                rendered_links = list(set(rendered_links))
                for contact_url in rendered_links[:3]:
                    try:
                        if not can_fetch_url(contact_url, respect_robots=respect_robots):
                            continue
                        contact_response = session.get(contact_url, headers={'User-Agent': get_random_user_agent()}, timeout=10, allow_redirects=True)
                        contact_response.raise_for_status()
                        contact_emails = re.findall(email_pattern, _deobfuscate_emails(contact_response.text))
                        emails.extend(contact_emails)
                    except Exception:
                        pass
        
        # Check 'data-email' attribute often used for hidden emails
        for element in soup.find_all(attrs={"data-email": True}):
            data_email = element.get('data-email')
            if data_email and '@' in data_email: # Ensure data_email is not None
                emails.append(data_email)
        
        # Remove duplicates and filter emails
        unique_emails = list(dict.fromkeys(emails))

        # Filtruj: błędny format, pliki graficzne, e-maile platform, domeny testowe
        filtered_emails = [
            e for e in unique_emails
            if is_valid_email(e)
            and e.lower() not in _PLATFORM_EMAILS
            and not any(
                domain in e.lower()
                for domain in ['example.com', 'domain.com', 'yourmail.com', 'wixpress.com', 'sentry.io']
            )
        ]

        logger.info("Found %s unique email addresses.", len(filtered_emails))
        if cache_conn is not None:
            cache_set_emails(cache_conn, url, filtered_emails)
        return filtered_emails
    
    except Exception as e:
        logger.error("Error fetching emails from %s: %s", url, e)
        return []

def annotate_sources(results_list, source_name):
    """Ensure each result has a 'sources' list and add the given source name."""
    if not results_list:
        return
    for r in results_list:
        r.setdefault('sources', [])
        if source_name not in r['sources']:
            r['sources'].append(source_name)


def merge_results(*lists):
    """Merge multiple result lists into unique records, merging fields and prioritizing Booksy emails.

    Matching signals:
    - Exact normalized phone (9 digits), OR
    - Exact business domain (non-catalog, non-social), OR
    - Exact Facebook / Instagram URL, OR
    - Overlapping valid non-platform email, OR
    - Normalized name with compatible address (same street prefix or one address empty).

    Merging rules:
    - Union emails (unique); if a source labelled 'booksy' provides emails, place them first.
    - Prefer real business websites over catalog/social URLs; preserve Facebook, Instagram, site_title.
    - Collect 'sources' for each merged record.
    """
    all_results = []
    for lst in lists:
        if not lst:
            continue
        all_results.extend(lst)

    try:
        from attio_sync import business_domain as _biz_domain, extract_social_links as _extract_socials
    except ImportError:
        _biz_domain = lambda u: ""
        _extract_socials = lambda *a: {"facebook": "", "instagram": ""}

    records = []
    by_phone = {}
    by_domain = {}
    by_social = {}
    by_email = {}
    by_name = {}

    def _simple_addr(raw_addr):
        parts = normalize_address(raw_addr or "").split(",")
        return parts[0].strip() if parts else ""

    def _is_real_website(url):
        if not url or is_catalog_url(url):
            return False
        low = url.lower()
        return not any(s in low for s in ("facebook.com", "fb.com", "instagram.com"))

    def _index_record(idx, rec):
        ph = normalize_phone(rec.get("formatted_phone_number", ""))
        if len(ph) >= 9:
            by_phone.setdefault(ph, idx)
        dom = _biz_domain(rec.get("website", ""))
        if dom:
            by_domain.setdefault(dom, idx)
        soc = _extract_socials(rec.get("facebook", ""), rec.get("instagram", ""), rec.get("website", ""))
        for s_url in soc.values():
            if s_url:
                by_social.setdefault(s_url.lower(), idx)
        for em in rec.get("emails") or []:
            em_l = (em or "").strip().lower()
            if em_l and "@" in em_l and em_l not in _PLATFORM_EMAILS:
                by_email.setdefault(em_l, idx)
        nm = normalize_name(rec.get("name", ""))
        if nm:
            by_name.setdefault(nm, []).append(idx)

    for r in all_results:
        name = (r.get("name") or "").strip()
        normalized_name = normalize_name(name)
        if not normalized_name:
            continue

        simple_addr = _simple_addr(r.get("formatted_address", ""))
        phone = normalize_phone(r.get("formatted_phone_number", ""))
        dom = _biz_domain(r.get("website", ""))
        soc = _extract_socials(r.get("facebook", ""), r.get("instagram", ""), r.get("website", ""))
        rec_emails = [
            (e or "").strip().lower()
            for e in (r.get("emails") or [])
            if e and "@" in e and (e or "").strip().lower() not in _PLATFORM_EMAILS
        ]

        match_idx = None
        if len(phone) >= 9 and phone in by_phone:
            match_idx = by_phone[phone]
        elif dom and dom in by_domain:
            # Wymagaj zgodności nazwy lub pustego/zgodnego adresu, żeby nie skleić 2 oddziałów sieci pod tą samą domeną
            cand_idx = by_domain[dom]
            cand = records[cand_idx]
            cand_addr = _simple_addr(cand.get("formatted_address", ""))
            if (
                normalize_name(cand.get("name", "")) == normalized_name
                or not simple_addr
                or not cand_addr
                or simple_addr == cand_addr
            ):
                match_idx = cand_idx
        if match_idx is None:
            for s_url in soc.values():
                if s_url and s_url.lower() in by_social:
                    match_idx = by_social[s_url.lower()]
                    break
        if match_idx is None:
            for em_l in rec_emails:
                if em_l in by_email:
                    match_idx = by_email[em_l]
                    break
        if match_idx is None and normalized_name in by_name:
            for cand_idx in by_name[normalized_name]:
                cand = records[cand_idx]
                cand_addr = _simple_addr(cand.get("formatted_address", ""))
                cand_phone = normalize_phone(cand.get("formatted_phone_number", ""))
                if (
                    simple_addr == cand_addr
                    or (not simple_addr and not cand_addr)
                    or (not simple_addr or not cand_addr)
                ) and (not phone or not cand_phone or phone == cand_phone):
                    match_idx = cand_idx
                    break

        if match_idx is not None:
            existing = records[match_idx]
            for s in r.get("sources", []):
                if s not in existing["sources"]:
                    existing["sources"].append(s)

            existing_emails = existing.get("emails") or []
            new_emails = r.get("emails") or []
            if "booksy" in r.get("sources", []):
                combined = new_emails + [e for e in existing_emails if e not in new_emails]
            else:
                combined = existing_emails + [e for e in new_emails if e not in existing_emails]

            seen = set()
            final_emails = []
            for e in combined:
                if e and e not in seen:
                    seen.add(e)
                    final_emails.append(e)
            existing["emails"] = final_emails

            # Preferuj prawdziwą stronę WWW nad linkiem katalogu/social media
            ex_web = (existing.get("website") or "").strip()
            new_web = (r.get("website") or "").strip()
            if new_web and (not ex_web or (not _is_real_website(ex_web) and _is_real_website(new_web))):
                existing["website"] = new_web

            if not existing.get("profile_url") and r.get("profile_url"):
                existing["profile_url"] = r.get("profile_url")
            if not existing.get("formatted_phone_number") and r.get("formatted_phone_number"):
                existing["formatted_phone_number"] = r.get("formatted_phone_number")
            if not existing.get("formatted_address") and r.get("formatted_address"):
                existing["formatted_address"] = r.get("formatted_address")
            if not existing.get("facebook") and (r.get("facebook") or soc["facebook"]):
                existing["facebook"] = r.get("facebook") or soc["facebook"]
            if not existing.get("instagram") and (r.get("instagram") or soc["instagram"]):
                existing["instagram"] = r.get("instagram") or soc["instagram"]
            if not existing.get("site_title") and r.get("site_title"):
                existing["site_title"] = r.get("site_title")
            if not existing.get("name") and r.get("name"):
                existing["name"] = r.get("name")
            _index_record(match_idx, existing)
        else:
            new = dict(r)
            new.setdefault("sources", list(r.get("sources", [])))
            new["emails"] = list(dict.fromkeys(new.get("emails") or []))
            if soc["facebook"] and not new.get("facebook"):
                new["facebook"] = soc["facebook"]
            if soc["instagram"] and not new.get("instagram"):
                new["instagram"] = soc["instagram"]
            idx = len(records)
            records.append(new)
            _index_record(idx, new)

    return records

def _rebuild_branza_sheets(wb, template_path):
    """Sync per-branża sheets from the 'Wszystkie' master sheet.

    Reads all data rows from 'Wszystkie', groups them by Branża, then
    recreates each branża sheet with city-header rows (dark blue) and data rows.
    Called after every save to SoundYouLeads.xlsx.
    """
    from collections import defaultdict
    from openpyxl.styles import PatternFill, Font, Alignment

    KNOWN_BRANZE = {
        # Kanoniczne 12 branż SoundYou (Attio) + klucze EN/PL -> bezpieczne nazwy arkuszy Excel (bez '/')
        "spa": "SPA & Wellness",
        "wellness": "SPA & Wellness",
        "spa & wellness": "SPA & Wellness",
        "health & wellness": "SPA & Wellness",
        "masaz": "Kobido & Masaż",
        "masaż": "Kobido & Masaż",
        "massage": "Kobido & Masaż",
        "kobido": "Kobido & Masaż",
        "kobido & masaż": "Kobido & Masaż",
        "uroda": "Beauty - Salon Urody",
        "beauty": "Beauty - Salon Urody",
        "beauty salon": "Beauty - Salon Urody",
        "beauty / salon urody": "Beauty - Salon Urody",
        "hotel": "Hotele i Pensjonaty",
        "hotels": "Hotele i Pensjonaty",
        "hotele": "Hotele i Pensjonaty",
        "hotele i pensjonaty": "Hotele i Pensjonaty",
        "resort": "Hotele i Pensjonaty",
        "restaurant": "Restauracje",
        "restaurants": "Restauracje",
        "restauracja": "Restauracje",
        "restauracje": "Restauracje",
        "kawiarnia": "Kawiarnie",
        "kawiarnie": "Kawiarnie",
        "cafe": "Kawiarnie",
        "coffee": "Kawiarnie",
        "coffee shop": "Kawiarnie",
        "fitness": "Fitness - Siłownia",
        "gym": "Fitness - Siłownia",
        "siłownia": "Fitness - Siłownia",
        "fitness / siłownia": "Fitness - Siłownia",
        "joga": "Joga - Pilates - Medytacja",
        "yoga": "Joga - Pilates - Medytacja",
        "pilates": "Joga - Pilates - Medytacja",
        "meditation": "Joga - Pilates - Medytacja",
        "joga / pilates / medytacja": "Joga - Pilates - Medytacja",
        "fryzjer": "Salon Fryzjerski - Barber",
        "hairdresser": "Salon Fryzjerski - Barber",
        "hair salon": "Salon Fryzjerski - Barber",
        "barber": "Salon Fryzjerski - Barber",
        "salon fryzjerski / barber": "Salon Fryzjerski - Barber",
        "fizjoterapia": "Poczekalnia - Gabinet",
        "physiotherapy": "Poczekalnia - Gabinet",
        "clinic": "Poczekalnia - Gabinet",
        "gabinet": "Poczekalnia - Gabinet",
        "poczekalnia / gabinet": "Poczekalnia - Gabinet",
        "sklep": "Sklep - Retail",
        "shop": "Sklep - Retail",
        "retail": "Sklep - Retail",
        "sklep / retail": "Sklep - Retail",
        "inne": "Lokal Usługowy - Inne",
        "other": "Lokal Usługowy - Inne",
        "lokal usługowy / inne": "Lokal Usługowy - Inne",
    }

    if 'Wszystkie' not in wb.sheetnames:
        return

    ws_all = wb['Wszystkie']
    headers = [cell.value for cell in next(ws_all.iter_rows(min_row=1, max_row=1))]
    while headers and headers[-1] is None:
        headers.pop()

    try:
        branza_col_idx = headers.index('Branża')
        miasto_col_idx = headers.index('Miasto')
    except ValueError:
        return  # columns not present yet

    # Read all data rows; skip city-header rows (merged rows with ≤1 non-None cell)
    data_rows = []
    for row in ws_all.iter_rows(min_row=2, values_only=True):
        row_list = list(row)[:len(headers)]
        # Pad if shorter
        while len(row_list) < len(headers):
            row_list.append(None)
        non_none = sum(1 for v in row_list if v is not None)
        if non_none <= 1:
            continue  # city header row
        data_rows.append(row_list)

    # Group by sheet name (supports comma-separated multiselect values like "SPA & Wellness, Hotele i Pensjonaty")
    branza_groups = defaultdict(list)
    for row in data_rows:
        raw_cell = str(row[branza_col_idx] or '').strip()
        tokens = [t.strip().lower() for t in re.split(r'[,;|]+', raw_cell) if t.strip()]
        target_sheets = []
        for tok in tokens:
            if tok in ('hotel-spa', 'hotel spa', 'hotel & spa', 'spa hotel'):
                target_sheets.extend(['Hotele i Pensjonaty', 'SPA & Wellness'])
            else:
                target_sheets.append(KNOWN_BRANZE.get(tok, 'Lokal Usługowy - Inne'))
        if not target_sheets:
            target_sheets = ['Lokal Usługowy - Inne']
        for sheet_name in dict.fromkeys(target_sheets):
            branza_groups[sheet_name].append(row)

    header_fill = PatternFill(start_color='1F4E78', end_color='1F4E78', fill_type='solid')
    header_font = Font(color='FFFFFF', bold=True)
    header_align = Alignment(horizontal='center')

    sheet_order = [
        'SPA & Wellness', 'Kobido & Masaż', 'Beauty - Salon Urody',
        'Hotele i Pensjonaty', 'Restauracje', 'Kawiarnie',
        'Fitness - Siłownia', 'Joga - Pilates - Medytacja',
        'Salon Fryzjerski - Barber', 'Poczekalnia - Gabinet',
        'Sklep - Retail', 'Lokal Usługowy - Inne',
    ]

    for sheet_name, rows in branza_groups.items():
        # Remove existing sheet and recreate
        if sheet_name in wb.sheetnames:
            del wb[sheet_name]
        ws = wb.create_sheet(sheet_name)

        # Header row
        ws.append(headers)

        # Group rows by miasto, sort alphabetically
        miasto_groups = defaultdict(list)
        for row in rows:
            miasto_val = str(row[miasto_col_idx] or '').upper().strip()
            miasto_groups[miasto_val].append(row)

        for miasto_name in sorted(miasto_groups):
            # City header row
            ws.append([f'!{miasto_name}'] + [None] * (len(headers) - 1))
            city_row_idx = ws.max_row
            ws.merge_cells(
                start_row=city_row_idx, start_column=1,
                end_row=city_row_idx, end_column=len(headers)
            )
            cell = ws.cell(row=city_row_idx, column=1)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = header_align

            for row in miasto_groups[miasto_name]:
                ws.append(row)

    # Reorder sheets: Wszystkie first, then known branże in order, then Inne
    desired = ['Wszystkie'] + [s for s in sheet_order if s in wb.sheetnames]
    remaining = [s for s in wb.sheetnames if s not in desired]
    final_order = desired + remaining
    for target_idx, name in enumerate(final_order):
        if name not in wb.sheetnames:
            continue
        current_idx = wb.sheetnames.index(name)
        if current_idx != target_idx:
            wb.move_sheet(name, offset=target_idx - current_idx)

    wb.save(template_path)


def save_to_excel(data, filename=OUTPUT_FILE, city=None, append=False):
    """Saves data to an Excel file. If target is the SoundYouLeads template, append into it
    and preserve layout/style, insert a colored city header row, set Status='to call', and
    ensure email2/email3 columns exist (hidden).
    """
    try:
        template_path = os.path.join(OUTPUT_DIR, 'SoundYouLeads.xlsx')
        target_basename = os.path.basename(filename or '')

        # Validate provided filename. If invalid, fallback to template (if exists) or
        # a safe default in the output folder to avoid writing to root like "\\.xlsx".
        try:
            bad_name = False
            if not filename or not isinstance(filename, str):
                bad_name = True
            else:
                base = os.path.basename(filename)
                root, ext = os.path.splitext(base)
                # Treat empty root (eg. ".xlsx"), only extension, or '.' as invalid
                if not root or root.strip() == '' or root in ('.', '..'):
                    bad_name = True
            if bad_name:
                if os.path.exists(template_path):
                    filename = template_path
                else:
                    os.makedirs(OUTPUT_DIR, exist_ok=True)
                    filename = os.path.join(OUTPUT_DIR, 'results.xlsx')
                logger.warning("Invalid output filename provided; using fallback: %s", filename)
        except Exception:
            # If validation itself fails for any reason, ensure a safe fallback
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            filename = os.path.join(OUTPUT_DIR, 'results.xlsx')
            logger.warning("Error validating output filename; using fallback: %s", filename)

        # If we're updating the live SoundYouLeads.xlsx, open and append
        if os.path.exists(template_path) and target_basename.lower() == os.path.basename(template_path).lower():
            wb = openpyxl.load_workbook(template_path)

            # Ensure main sheet is named "Wszystkie"
            first_sheet = wb[wb.sheetnames[0]]
            _branża_sheet_names = {'Spa', 'Joga', 'Fizjoterapia', 'Uroda', 'Fryzjer', 'Masaż', 'Hotel', 'Hotel-Spa', 'Restaurant', 'Inne'}
            if first_sheet.title not in ({'Wszystkie'} | _branża_sheet_names):
                first_sheet.title = 'Wszystkie'
            if 'Wszystkie' not in wb.sheetnames:
                ws = wb.create_sheet('Wszystkie', 0)
            else:
                ws = wb['Wszystkie']

            # Read header row (assume row 1)
            headers = [cell.value for cell in next(ws.iter_rows(min_row=1, max_row=1))]
            # Normalize headers list
            while headers and headers[-1] is None:
                headers.pop()

            # Ensure email 2/3 columns exist immediately after 'Adres E-mail'
            try:
                if 'Adres E-mail' in headers:
                    idx = headers.index('Adres E-mail') + 1
                    if idx >= len(headers) or headers[idx] != 'Adres E-mail 2':
                        headers.insert(idx, 'Adres E-mail 2')
                    if idx + 1 >= len(headers) or headers[idx + 1] != 'Adres E-mail 3':
                        headers.insert(idx + 1, 'Adres E-mail 3')
                else:
                    # fallback: append
                    headers.extend(['Adres E-mail', 'Adres E-mail 2', 'Adres E-mail 3'])
            except Exception:
                headers = headers + ['Adres E-mail', 'Adres E-mail 2', 'Adres E-mail 3']

            # Ensure 'Status' and 'Data kontaktu' columns close to emails
            if 'Status' not in headers:
                # Insert Status after email columns
                try:
                    status_insert_idx = headers.index('Adres E-mail 3') + 1
                except Exception:
                    status_insert_idx = len(headers)
                headers.insert(status_insert_idx, 'Status')
            if 'Data kontaktu' not in headers:
                headers.insert(headers.index('Status') + 1, 'Data kontaktu')

            # Decide whether to move existing 'Status' column to be right after email 3
            try:
                current_status_idx = headers.index('Status')
                desired_idx = headers.index('Adres E-mail 3') + 1
                if current_status_idx != desired_idx:
                    move_status_column = (current_status_idx + 1, desired_idx + 1)  # store 1-based indices
                else:
                    move_status_column = None
            except Exception:
                move_status_column = None

            # Ensure 'Other Emails' and 'Sources' exist at the end
            if 'Other Emails' not in headers:
                headers.append('Other Emails')
            if 'Sources' not in headers:
                headers.append('Sources')

            # Ensure metadata columns exist (appended after Sources for backward compat)
            for _meta_col in ['Branża', 'Województwo', 'Miasto']:
                if _meta_col not in headers:
                    headers.append(_meta_col)

            # If actual sheet headings in file differ, ensure columns exist in sheet by adding empty cells
            existing_header_cells = list(ws.iter_rows(min_row=1, max_row=1, values_only=False))[0]
            current_col_count = len(existing_header_cells)
            needed_cols = len(headers)
            if needed_cols > current_col_count:
                for c in range(current_col_count + 1, needed_cols + 1):
                    ws.cell(row=1, column=c, value=headers[c - 1])
            else:
                # Update header names to reflect our canonical names where applicable
                for i, h in enumerate(headers, start=1):
                    ws.cell(row=1, column=i, value=h)

            # If requested, move existing 'Status' column to be right after email 3
            try:
                if move_status_column:
                    old_idx_1based, desired_idx_1based = move_status_column
                    # Copy old column values
                    rows_count = ws.max_row
                    temp_values = [ws.cell(row=r, column=old_idx_1based).value for r in range(1, rows_count + 1)]
                    # Insert new empty column at desired index
                    ws.insert_cols(desired_idx_1based)
                    # Set values into the newly inserted column
                    for r, v in enumerate(temp_values, start=1):
                        ws.cell(row=r, column=desired_idx_1based, value=v)
                    # Adjust old column index (it may have shifted if old<desired)
                    if old_idx_1based > desired_idx_1based:
                        old_to_delete = old_idx_1based + 1
                    else:
                        old_to_delete = old_idx_1based
                    ws.delete_cols(old_to_delete)
                    # Re-read headers after move
                    headers = [cell.value for cell in next(ws.iter_rows(min_row=1, max_row=1))]
            except Exception:
                pass

            # If append was requested, merge new data into existing rows and append only new records
            if append:
                # Build header->col index map
                header_index = {h: i+1 for i, h in enumerate(headers)}

                # Build existing records map: key -> row index
                existing_map = {}
                for r in range(2, ws.max_row + 1):
                    name_cell = ws.cell(row=r, column=header_index.get('Nazwa Salonu', 1)).value
                    addr_cell = ws.cell(row=r, column=header_index.get('Adres', 2)).value
                    phone_cell = ws.cell(row=r, column=header_index.get('Numer Telefonu', 3)).value
                    if not name_cell:
                        continue
                    key = f"{normalize_name(name_cell)}|{normalize_address(addr_cell)}|{normalize_phone(str(phone_cell) if phone_cell else '')}"
                    existing_map[key] = r

                to_append = []
                updated_count = 0
                for item in data:
                    name_val = item.get('name', '')
                    addr_val = item.get('formatted_address', '')
                    phone_val = item.get('formatted_phone_number', '')
                    key = f"{normalize_name(name_val)}|{normalize_address(addr_val)}|{normalize_phone(phone_val)}"

                    new_emails = item.get('emails') or []
                    new_sources = item.get('sources') or []

                    if key in existing_map:
                        rownum = existing_map[key]
                        # Read existing emails
                        existing_emails = []
                        main_e = ws.cell(row=rownum, column=header_index.get('Adres E-mail')).value or ''
                        e2 = ws.cell(row=rownum, column=header_index.get('Adres E-mail 2')).value or ''
                        e3 = ws.cell(row=rownum, column=header_index.get('Adres E-mail 3')).value or ''
                        other = ws.cell(row=rownum, column=header_index.get('Other Emails')).value or ''
                        import re
                        m = re.match(r"^([^\(]+)\s*(?:\(([^)]+)\))?", str(main_e))
                        if m:
                            first = m.group(1).strip()
                            if first:
                                existing_emails.append(first)
                            if m.group(2):
                                for part in m.group(2).split(','):
                                    p = part.strip()
                                    if p:
                                        existing_emails.append(p)
                        if e2:
                            existing_emails.append(e2)
                        if e3:
                            existing_emails.append(e3)
                        if other:
                            for p in str(other).split(','):
                                p = p.strip()
                                if p:
                                    existing_emails.append(p)

                        # Merge emails with Booksy priority
                        if 'booksy' in new_sources:
                            combined = new_emails + [e for e in existing_emails if e not in new_emails]
                        else:
                            combined = existing_emails + [e for e in new_emails if e not in existing_emails]

                        # Deduplicate preserving order
                        seen = set()
                        final = []
                        for e in combined:
                            if e and e not in seen:
                                seen.add(e)
                                final.append(e)

                        # Write back: main, e2, e3, other
                        ws.cell(row=rownum, column=header_index.get('Adres E-mail')).value = final[0] if final else ''
                        ws.cell(row=rownum, column=header_index.get('Adres E-mail 2')).value = final[1] if len(final) > 1 else ''
                        ws.cell(row=rownum, column=header_index.get('Adres E-mail 3')).value = final[2] if len(final) > 2 else ''
                        ws.cell(row=rownum, column=header_index.get('Other Emails')).value = ', '.join(final[3:]) if len(final) > 3 else ''

                        # Update phone if missing
                        if header_index.get('Numer Telefonu'):
                            if not (ws.cell(row=rownum, column=header_index['Numer Telefonu']).value) and phone_val:
                                ws.cell(row=rownum, column=header_index['Numer Telefonu']).value = phone_val

                        # Update Sources column
                        old_src = ws.cell(row=rownum, column=header_index.get('Sources')).value or ''
                        old_src_set = set(s.strip() for s in str(old_src).split(',') if s.strip())
                        new_src_set = set(new_sources)
                        all_src = ', '.join(sorted(old_src_set.union(new_src_set)))
                        ws.cell(row=rownum, column=header_index.get('Sources')).value = all_src

                        # Set Status to 'to call' and clear contact date
                        ws.cell(row=rownum, column=header_index.get('Status')).value = 'to call'
                        if header_index.get('Data kontaktu'):
                            ws.cell(row=rownum, column=header_index.get('Data kontaktu')).value = ''

                        updated_count += 1

                    else:
                        to_append.append(item)

                # Append new rows under the appropriate city block (insert under existing city header or add header after last data row)
                if to_append:
                    city_name = (city or '').upper().strip()
                    city_header_row = None
                    # Find the last occurrence of this city header (search column 1)
                    for r in range(1, ws.max_row + 1):
                        v = ws.cell(row=r, column=1).value
                        if not v:
                            continue
                        try:
                            vs = str(v).strip().upper()
                        except Exception:
                            continue
                        if vs == city_name or vs == f'!{city_name}':
                            city_header_row = r

                    if city_header_row:
                        # Ensure header is marked with '!'
                        header_cell = ws.cell(row=city_header_row, column=1)
                        if not str(header_cell.value).strip().startswith('!'):
                            header_cell.value = f"!{str(header_cell.value).strip()}"

                        # Find next merged header row (if any) to determine end of this city's block
                        next_header_row = None
                        for merged in ws.merged_cells.ranges:
                            try:
                                min_row = merged.min_row
                                min_col = merged.min_col
                            except Exception:
                                continue
                            if min_col == 1 and min_row > city_header_row:
                                if next_header_row is None or min_row < next_header_row:
                                    next_header_row = min_row

                        if next_header_row:
                            insert_row = next_header_row
                        else:
                            # Find the end of this city's block by scanning downward from the header
                            def _is_non_empty(cell):
                                v = cell.value
                                if v is None:
                                    return False
                                if isinstance(v, str) and v.strip() == '':
                                    return False
                                return True

                            def _looks_like_header(val):
                                if not val:
                                    return False
                                s = str(val).strip()
                                if s.startswith('!'):
                                    s2 = s[1:]
                                else:
                                    s2 = s
                                # heuristic: short, all-caps, no commas, no digits
                                if len(s2.split()) <= 6 and s2 == s2.upper() and (',' not in s2) and not any(c.isdigit() for c in s2):
                                    return True
                                return False

                            # Walk forward from header until we find the first empty row (end of this city's contiguous block)
                            last_seen = city_header_row
                            for r in range(city_header_row + 1, ws.max_row + 1):
                                has_any = False
                                for c in range(1, len(headers) + 1):
                                    if _is_non_empty(ws.cell(row=r, column=c)):
                                        has_any = True
                                        break
                                if has_any:
                                    last_seen = r
                                    continue
                                # first fully-empty row after the block -> insert here
                                insert_row = r
                                break
                            else:
                                # no empty row found, append after last seen
                                insert_row = last_seen + 1

                    else:
                        # No existing city header: find the first visible gap near the top (3 consecutive empty rows) and insert there; fallback to last non-empty row
                        def _is_non_empty(cell):
                            v = cell.value
                            if v is None:
                                return False
                            if isinstance(v, str) and v.strip() == '':
                                return False
                            return True

                        def _row_has_data(r):
                            for c in range(1, len(headers) + 1):
                                if _is_non_empty(ws.cell(row=r, column=c)):
                                    return True
                            return False

                        gap_row = None
                        consec_needed = 1  # treat 1 empty row as end-of-block
                        for r in range(2, max(2, ws.max_row - consec_needed)):
                            empty_run = True
                            for k in range(consec_needed):
                                if r + k > ws.max_row:
                                    empty_run = False
                                    break
                                if _row_has_data(r + k):
                                    empty_run = False
                                    break
                            if empty_run:
                                gap_row = r
                                break

                        if gap_row:
                            insert_row = gap_row
                            logger.info("Fresha: inserting city header '%s' at first visible gap row %s", city_name, insert_row)
                        else:
                            last_non_empty = 1
                            for r in range(ws.max_row, 1, -1):
                                if _row_has_data(r):
                                    last_non_empty = r
                                    break
                            insert_row = last_non_empty + 1
                            logger.info("Fresha: inserting city header '%s' at end row %s (no early gap found)", city_name, insert_row)

                        # Insert city header with '!' prefix
                        ws.insert_rows(insert_row)
                        ws.merge_cells(start_row=insert_row, start_column=1, end_row=insert_row, end_column=len(headers))
                        cell = ws.cell(row=insert_row, column=1)
                        cell.value = f"!{city_name}" if city_name else ''
                        from openpyxl.styles import PatternFill, Font, Alignment
                        cell.fill = PatternFill(start_color='1F4E78', end_color='1F4E78', fill_type='solid')
                        cell.font = Font(color='FFFFFF', bold=True)
                        cell.alignment = Alignment(horizontal='center')
                        insert_row += 1

                    # Insert new rows starting at insert_row
                    for idx, item in enumerate(to_append):
                        emails = item.get('emails', []) or []
                        sources = item.get('sources') or []
                        main_email = ''
                        if emails:
                            main_email = emails[0]
                            if len(emails) > 1:
                                additional = ', '.join(emails[1:3])
                                main_email = f"{main_email} ({additional})"
                        other_emails = ', '.join(emails[3:]) if len(emails) > 3 else ''

                        row_vals = []
                        for h in headers:
                            key = (h or '').strip().lower()
                            if key in ('nazwa salonu', 'name'):
                                row_vals.append(item.get('name', ''))
                            elif key in ('adres', 'address'):
                                row_vals.append(item.get('formatted_address', ''))
                            elif key in ('numer telefonu', 'phone'):
                                row_vals.append(item.get('formatted_phone_number', ''))
                            elif key in ('adres e-mail', 'adres e-mail 1', 'email 1'):
                                row_vals.append(main_email)
                            elif key == 'adres e-mail 2':
                                row_vals.append(emails[1] if len(emails) > 1 else '')
                            elif key == 'adres e-mail 3':
                                row_vals.append(emails[2] if len(emails) > 2 else '')
                            elif key == 'status':
                                row_vals.append('to call')
                            elif key == 'data kontaktu':
                                row_vals.append('')
                            elif key == 'other emails':
                                row_vals.append(other_emails)
                            elif key == 'sources':
                                row_vals.append(', '.join(sources))
                            elif key == 'branża':
                                row_vals.append(item.get('branża', ''))
                            elif key == 'województwo':
                                row_vals.append(item.get('województwo', ''))
                            elif key == 'miasto':
                                row_vals.append(item.get('miasto', ''))
                            else:
                                row_vals.append('')

                        ws.insert_rows(insert_row + idx)
                        for ci, v in enumerate(row_vals, start=1):
                            ws.cell(row=insert_row + idx, column=ci).value = v

                    # Ensure email 2/3 columns (E and F) are visible
                    try:
                        col_idx_email2 = headers.index('Adres E-mail 2') + 1
                        col_idx_email3 = headers.index('Adres E-mail 3') + 1
                        col_letter_2 = openpyxl.utils.get_column_letter(col_idx_email2)
                        col_letter_3 = openpyxl.utils.get_column_letter(col_idx_email3)
                        # Unhide columns (in case template had them hidden) and ensure sensible width
                        ws.column_dimensions[col_letter_2].hidden = False
                        ws.column_dimensions[col_letter_3].hidden = False
                        if getattr(ws.column_dimensions[col_letter_2], 'width', None) == 0:
                            ws.column_dimensions[col_letter_2].width = 20
                        if getattr(ws.column_dimensions[col_letter_3], 'width', None) == 0:
                            ws.column_dimensions[col_letter_3].width = 20
                    except Exception:
                        pass

                # Save and finish merge/append (avoid appending all items again)
                appended_count = len(to_append)
                if appended_count:
                    logger.info("Inserted %s rows starting at row %s for city %s", appended_count, insert_row, city_name)
                else:
                    logger.info("No rows appended for city %s", city_name)
                wb.save(template_path)
                logger.info("Updated %s existing rows and appended %s new records to %s", updated_count, appended_count, template_path)
                _rebuild_branza_sheets(wb, template_path)
                return
            else:
                # Insert city header row (one long colored row with white font)
                insert_row = ws.max_row + 1
                last_col_letter = openpyxl.utils.get_column_letter(len(headers))
                ws.merge_cells(start_row=insert_row, start_column=1, end_row=insert_row, end_column=len(headers))
                cell = ws.cell(row=insert_row, column=1)
                cell.value = (city or '').upper() if city else ''
                from openpyxl.styles import PatternFill, Font, Alignment
                cell.fill = PatternFill(start_color='1F4E78', end_color='1F4E78', fill_type='solid')
                cell.font = Font(color='FFFFFF', bold=True)
                cell.alignment = Alignment(horizontal='center')

                # Ensure email 2/3 columns (E and F) are visible
                try:
                    col_idx_email2 = headers.index('Adres E-mail 2') + 1
                    col_idx_email3 = headers.index('Adres E-mail 3') + 1
                    col_letter_2 = openpyxl.utils.get_column_letter(col_idx_email2)
                    col_letter_3 = openpyxl.utils.get_column_letter(col_idx_email3)
                    # Unhide columns (in case template had them hidden) and ensure sensible width
                    ws.column_dimensions[col_letter_2].hidden = False
                    ws.column_dimensions[col_letter_3].hidden = False
                    if getattr(ws.column_dimensions[col_letter_2], 'width', None) == 0:
                        ws.column_dimensions[col_letter_2].width = 20
                    if getattr(ws.column_dimensions[col_letter_3], 'width', None) == 0:
                        ws.column_dimensions[col_letter_3].width = 20
                except Exception:
                    pass

# Now append each record with mapping to headers
            for item in data:
                emails = item.get('emails', []) or []
                sources = item.get('sources') or []
                name_val = item.get('name', '')
                addr_val = item.get('formatted_address', '')
                phone_val = item.get('formatted_phone_number', '')

                # Main email cell: include primary and additional emails concatenated so they are visible in one cell
                main_email = ''
                if emails:
                    main_email = emails[0]
                    if len(emails) > 1:
                        additional = ', '.join(emails[1:3])
                        main_email = f"{main_email} ({additional})"
                # Other Emails column will contain any remaining beyond 3
                other_emails = ', '.join(emails[3:]) if len(emails) > 3 else ''

                row_vals = []
                for h in headers:
                    key = (h or '').strip().lower()
                    if key in ('nazwa salonu', 'name'):
                        row_vals.append(name_val)
                    elif key in ('adres', 'address'):
                        row_vals.append(addr_val)
                    elif key in ('numer telefonu', 'phone', 'numer telefonu (bez spacji)'):
                        row_vals.append(phone_val)
                    elif key in ('adres e-mail', 'adres e-mail 1', 'email 1'):
                        row_vals.append(main_email)
                    elif key == 'adres e-mail 2':
                        row_vals.append(emails[1] if len(emails) > 1 else '')
                    elif key == 'adres e-mail 3':
                        row_vals.append(emails[2] if len(emails) > 2 else '')
                    elif key == 'status':
                        row_vals.append('to call')
                    elif key == 'data kontaktu':
                        row_vals.append('')
                    elif key == 'other emails':
                        row_vals.append(other_emails)
                    elif key == 'sources':
                        row_vals.append(', '.join(sources))
                    elif key == 'branża':
                        row_vals.append(item.get('branża', ''))
                    elif key == 'województwo':
                        row_vals.append(item.get('województwo', ''))
                    elif key == 'miasto':
                        row_vals.append(item.get('miasto', ''))
                    else:
                        # Unknown column from template: leave empty
                        row_vals.append('')

                ws.append(row_vals)

            # Save workbook back to template path
            wb.save(template_path)
            logger.info("Appended %s records to %s", len(data), template_path)
            _rebuild_branza_sheets(wb, template_path)
            return

        # Fallback behaviour: create a new workbook (previous behaviour)
        wb = openpyxl.Workbook()
        ws = wb.active

        # Fallback headers (Polish names matching template intent)
        headers = ['Branża', 'Województwo', 'Miasto', 'Nazwa Salonu', 'Adres', 'Numer Telefonu', 'Adres E-mail', 'Adres E-mail 2', 'Adres E-mail 3', 'Other Emails', 'Sources']
        ws.append(headers)

        for item in data:
            emails = item.get('emails', []) or []
            sources = item.get('sources') or []
            row = [
                item.get('branża', ''),
                item.get('województwo', ''),
                item.get('miasto', ''),
                item.get('name', ''),
                item.get('formatted_address', ''),
                item.get('formatted_phone_number', ''),
                emails[0] if len(emails) > 0 else '',
                emails[1] if len(emails) > 1 else '',
                emails[2] if len(emails) > 2 else '',
                ', '.join(emails[3:]) if len(emails) > 3 else '',
                ', '.join(sources)
            ]
            ws.append(row)

        wb.save(filename)
        logger.info("Saved %s records to %s", len(data), filename)
            
        # Adjust column widths
        for column_cells in ws.columns: # Changed 'column' to 'column_cells' for clarity
            max_length = 0
            column_letter = openpyxl.utils.get_column_letter(column_cells[0].column)
            for cell in column_cells:
                try:
                    if cell.value is not None and len(str(cell.value)) > max_length: # Check for None
                        max_length = len(str(cell.value))
                except: # Broad except, consider specific exceptions if known
                    pass
            adjusted_width = (max_length + 2)
            ws.column_dimensions[column_letter].width = min(adjusted_width, 60)  # Limit max width slightly more
        
        wb.save(filename)
        logger.info("Saved %s records to %s", len(data), filename)
    except Exception as e:
        try:
            logger.error("Error saving to Excel (attempted file: %s): %s", filename, e)
        except Exception:
            logger.error("Error saving to Excel: %s", e)
        # Try to save to a safe fallback (template if available, otherwise Data/Raport/results.xlsx)
        try:
            fallback = template_path if os.path.exists(template_path) else os.path.join(OUTPUT_DIR, 'results.xlsx')
            os.makedirs(os.path.dirname(fallback), exist_ok=True)
            if 'wb' in locals():
                wb.save(fallback)
                logger.info("Workbook saved to fallback location: %s", fallback)
            else:
                logger.error("No workbook object available to save to fallback location: %s", fallback)
        except Exception as e2:
            logger.error("Fallback save also failed: %s", e2)


def save_to_csv(data, filename):
    if os.path.exists(filename):
        confirm = input(f"File {filename} already exists. Overwrite? (y/n): ").lower()
        if confirm != 'y':
            new_name = input("Enter a new filename: ")
            if new_name:
                filename = new_name if new_name.endswith('.csv') else f"{new_name}.csv"
    try:
        with open(filename, mode='w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow([
                'Branża', 'Województwo', 'Miasto', 'Name', 'Address', 'Phone',
                'Website', 'Facebook', 'Instagram',
                'Email 1', 'Email 2', 'Email 3', 'Other Emails', 'Sources',
            ])
            for item in data:
                emails = item.get('emails', [])
                row_data = [
                    item.get('branża', ''),
                    item.get('województwo', ''),
                    item.get('miasto', ''),
                    item.get('name', ''),
                    item.get('formatted_address', ''),
                    item.get('formatted_phone_number', ''),
                    item.get('website', ''),
                    item.get('facebook', ''),
                    item.get('instagram', ''),
                ]
                for i in range(3):
                    row_data.append(emails[i] if i < len(emails) else '')
                row_data.append(', '.join(emails[3:]) if len(emails) > 3 else '')
                sources = item.get('sources') or []
                row_data.append(', '.join(sources))
                writer.writerow(row_data)
        logger.info("Saved %s records to %s", len(data), filename)
    except Exception as e:
        logger.error("Error saving to CSV: %s", e)


def append_to_csv_master(data, csv_path):
    """Append new records to the cumulative CSV master file and fill blank fields on existing rows.

    Reads existing rows (if any) and matches by normalized phone (+48 stripped),
    business domain, Facebook/Instagram URL, email address, and normalized (name, city).
    - New leads are appended.
    - Existing leads receive fill-blanks-only enrichment (missing Website/Facebook/Instagram/
      Phone/Address/new unique emails) without overwriting existing values.
    """
    CSV_HEADERS = [
        'Branża', 'Województwo', 'Miasto', 'Name', 'Address', 'Phone',
        'Website', 'Facebook', 'Instagram',
        'Email 1', 'Email 2', 'Email 3', 'Other Emails', 'Sources',
    ]

    try:
        from attio_sync import (
            business_domain as _biz_domain,
            extract_social_links as _extract_socials,
            is_non_icp_business as _is_non_icp,
            resolve_company_name as _resolve_name,
            validate_email_syntax as _val_email_syn,
        )
    except ImportError:
        _biz_domain = lambda u: ''
        _extract_socials = lambda *a: {'facebook': '', 'instagram': ''}
        _is_non_icp = lambda **kw: (False, '')
        _resolve_name = lambda raw, **kw: strip_emoji(raw or '').strip()
        _val_email_syn = lambda e: (True, '')

    idx_phones: dict = {}
    idx_domains: dict = {}
    idx_socials: dict = {}
    idx_emails: dict = {}
    idx_name_city: dict = {}
    existing_rows: list = []

    def _row_emails(row_dict):
        found = []
        for col in ('Email 1', 'Email 2', 'Email 3', 'Other Emails'):
            for em in re.split(r'[,;\s]+', row_dict.get(col, '') or ''):
                em_s = em.strip()
                if em_s and '@' in em_s and em_s.lower() not in _PLATFORM_EMAILS:
                    if em_s.lower() not in {x.lower() for x in found}:
                        found.append(em_s)
        return found

    def _index_row(row_idx, row_dict):
        ph = normalize_phone(row_dict.get('Phone', ''))
        if ph:
            idx_phones.setdefault(ph, row_idx)
        dom = _biz_domain(row_dict.get('Website', ''))
        if dom:
            idx_domains.setdefault(dom, row_idx)
        soc_ex = _extract_socials(row_dict.get('Facebook', ''), row_dict.get('Instagram', ''), row_dict.get('Website', ''))
        for s_url in soc_ex.values():
            if s_url:
                idx_socials.setdefault(s_url.lower(), row_idx)
        nm = normalize_name(strip_emoji(row_dict.get('Name', '')))
        ct = normalize_name(row_dict.get('Miasto', ''))
        if nm:
            idx_name_city.setdefault((nm, ct), row_idx)
            if not ct:
                idx_name_city.setdefault((nm, ''), row_idx)
        for em in _row_emails(row_dict):
            idx_emails.setdefault(em.lower(), row_idx)

    os.makedirs(os.path.dirname(csv_path) or '.', exist_ok=True)

    if os.path.exists(csv_path):
        try:
            with open(csv_path, newline='', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    row.setdefault('Facebook', '')
                    row.setdefault('Instagram', '')
                    row_idx = len(existing_rows)
                    existing_rows.append(row)
                    _index_row(row_idx, row)
        except Exception as e:
            logger.warning("Could not read existing CSV master %s: %s", csv_path, e)

    new_rows = []
    updated_existing = 0

    for item in data:
        raw_name = item.get('name', '').strip()
        website = item.get('website', '')
        socials = _extract_socials(item.get('facebook', ''), item.get('instagram', ''), website)
        fb = socials['facebook']
        ig = socials['instagram']
        emails = [
            e for e in item.get('emails', [])
            if is_valid_email(e) and e.lower() not in _PLATFORM_EMAILS and _val_email_syn(e)[0]
        ]
        emails_low = {e.lower() for e in emails}

        clean_name = _resolve_name(
            raw_name,
            site_title=item.get('site_title', ''),
            website=website,
            facebook=fb,
            instagram=ig,
            emails=emails,
            city=item.get('miasto', ''),
        )
        if not clean_name or len(clean_name) < 3 or re.search(r'^[\d,/\\@\s]+$', clean_name):
            logger.debug("Pominięto rekord bez czytelnej nazwy: %r", raw_name)
            continue
        if _is_non_icp(name=clean_name, website=website, raw_industry=item.get('branża', ''))[0]:
            logger.info("Pominięto rekord spoza ICP: %s (%s)", clean_name, website)
            continue

        nm = normalize_name(clean_name)
        ct = normalize_name(item.get('miasto', ''))
        ph = normalize_phone(item.get('formatted_phone_number', ''))
        dom = _biz_domain(website)
        item_socials = {s.lower() for s in (fb, ig) if s}

        if not nm and not ph and not dom and not emails_low and not item_socials:
            continue

        match_idx = None
        if ph and ph in idx_phones:
            match_idx = idx_phones[ph]
        elif dom and dom in idx_domains:
            match_idx = idx_domains[dom]
        elif any(s in idx_socials for s in item_socials):
            match_idx = next(idx_socials[s] for s in item_socials if s in idx_socials)
        elif any(em in idx_emails for em in emails_low):
            match_idx = next(idx_emails[em] for em in emails_low if em in idx_emails)
        elif nm and (nm, ct) in idx_name_city:
            match_idx = idx_name_city[(nm, ct)]
        elif nm and (nm, '') in idx_name_city:
            match_idx = idx_name_city[(nm, '')]

        if match_idx is not None:
            # Fill-blanks-only na istniejącym wierszu (nigdy nie nadpisuje istniejących wartości!)
            target_row = existing_rows[match_idx] if match_idx < len(existing_rows) else new_rows[match_idx - len(existing_rows)]
            changed = False
            if not (target_row.get('Phone') or '').strip() and item.get('formatted_phone_number'):
                target_row['Phone'] = item['formatted_phone_number']
                changed = True
            if not (target_row.get('Address') or '').strip() and item.get('formatted_address'):
                target_row['Address'] = item['formatted_address']
                changed = True
            old_web = (target_row.get('Website') or '').strip()
            if website and (not old_web or (is_catalog_url(old_web) and not is_catalog_url(website))):
                target_row['Website'] = website
                changed = True
            if not (target_row.get('Facebook') or '').strip() and fb:
                target_row['Facebook'] = fb
                changed = True
            if not (target_row.get('Instagram') or '').strip() and ig:
                target_row['Instagram'] = ig
                changed = True
            if emails:
                cur_emails = _row_emails(target_row)
                cur_low = {e.lower() for e in cur_emails}
                added_emails = [e for e in emails if e.lower() not in cur_low]
                if added_emails:
                    combined = cur_emails + added_emails
                    target_row['Email 1'] = combined[0] if len(combined) > 0 else ''
                    target_row['Email 2'] = combined[1] if len(combined) > 1 else ''
                    target_row['Email 3'] = combined[2] if len(combined) > 2 else ''
                    target_row['Other Emails'] = ', '.join(combined[3:]) if len(combined) > 3 else ''
                    changed = True
            if changed:
                _index_row(match_idx, target_row)
                if match_idx < len(existing_rows):
                    updated_existing += 1
            continue

        row_dict = {
            'Branża': item.get('branża', ''),
            'Województwo': item.get('województwo', ''),
            'Miasto': item.get('miasto', ''),
            'Name': clean_name,
            'Address': item.get('formatted_address', ''),
            'Phone': item.get('formatted_phone_number', ''),
            'Website': website,
            'Facebook': fb,
            'Instagram': ig,
            'Email 1': emails[0] if len(emails) > 0 else '',
            'Email 2': emails[1] if len(emails) > 1 else '',
            'Email 3': emails[2] if len(emails) > 2 else '',
            'Other Emails': ', '.join(emails[3:]) if len(emails) > 3 else '',
            'Sources': ', '.join(item.get('sources') or []),
        }
        new_idx = len(existing_rows) + len(new_rows)
        new_rows.append(row_dict)
        _index_row(new_idx, row_dict)

    if not new_rows and updated_existing == 0:
        logger.info("CSV master: no new records or blank-field enrichments (all %d already present)", len(data))
        return

    try:
        with open(csv_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADERS, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(existing_rows)
            writer.writerows(new_rows)
        logger.info(
            "CSV master: appended %d new, enriched %d existing → %s (%d total)",
            len(new_rows), updated_existing, csv_path, len(existing_rows) + len(new_rows),
        )
    except Exception as e:
        logger.error("Error writing CSV master %s: %s", csv_path, e)


def save_to_json(data, filename):
    try:
        with open(filename, mode='w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info("Saved %s records to %s", len(data), filename)
    except Exception as e:
        logger.error("Error saving to JSON: %s", e)


def save_results(data, output_file, output_format, city=None, append_to_template=False):
    output_format = (output_format or 'xlsx').lower()
    if output_format == 'xlsx':
        save_to_excel(data, output_file, city=city, append=append_to_template)
    elif output_format == 'csv':
        save_to_csv(data, output_file if output_file.endswith('.csv') else f"{os.path.splitext(output_file)[0]}.csv")
    elif output_format == 'json':
        save_to_json(data, output_file if output_file.endswith('.json') else f"{os.path.splitext(output_file)[0]}.json")
    else:
        logger.error("Unsupported output format: %s", output_format)

def main():
    parser = argparse.ArgumentParser(description="Multi-source business lead scraper")
    parser.add_argument("--query", help="Industry or search query")
    parser.add_argument("--location", help="City or location")
    parser.add_argument("--emails", action="store_true", help="Extract emails from websites")
    parser.add_argument("--use-google", action="store_true", help="Use Google Places API")
    parser.add_argument("--use-booksy", action="store_true", help="Use Booksy (free listings)")
    parser.add_argument("--output", default=OUTPUT_FILE, help="Output filename")
    parser.add_argument("--format", default="xlsx", choices=["xlsx", "csv", "json"], help="Output format")
    parser.add_argument("--max-pages", type=int, default=3, help="Max pages per source")
    parser.add_argument("--respect-robots", action="store_true", help="Respect robots.txt for website scraping")
    parser.add_argument("--cache-db", default="cache.sqlite", help="SQLite cache file for email extraction")
    parser.add_argument("--use-headless", action="store_true", default=True, help="Use headless Chrome for JS/blocked pages (default: on)")
    parser.add_argument("--no-headless", dest="use_headless", action="store_false", help="Disable headless browser")
    parser.add_argument("--headless-wait", type=int, default=3, help="Seconds to wait after render in headless mode")
    parser.add_argument("--sync-attio", action="store_true", help="Automatically sync deduplicated CSV leads to Attio after scraping")
    parser.add_argument("--attio-dry-run", action="store_true", help="Run Attio sync in dry-run mode (no writes)")
    parser.add_argument("--attio-fill-missing", action="store_true", help="Fill only empty fields (WWW, IG, FB, new emails) on existing Attio leads without overwriting")
    parser.add_argument(
        "--sources",
        default="all",
        help="Comma-separated sources or preset: 'all' (default), 'fast' (google,booksy,firmynet,znanylekarz), or explicit list e.g. 'google,booksy'",
    )
    args = parser.parse_args()

    # Walidacja: odrzuć argumenty wyglądające jak ścieżki pliku (artefakty bat/shell)
    if args.query and is_probably_path(args.query):
        parser.error(
            f"--query wygląda jak ścieżka pliku: {args.query!r}\n"
            "Podaj nazwę branży, np. 'spa' lub 'fryzjer'."
        )
    if args.location and is_probably_path(args.location):
        parser.error(
            f"--location wygląda jak ścieżka pliku: {args.location!r}\n"
            "Podaj nazwę miasta, np. 'Katowice' lub 'Kraków'."
        )
    if args.query and len(args.query) > 120:
        parser.error(f"--query jest za długi ({len(args.query)} znaków). Max 120.")
    if args.location and len(args.location) > 120:
        parser.error(f"--location jest za długi ({len(args.location)} znaków). Max 120.")

    # Variables to store interactive choices, defaults from args
    selected_query = args.query
    selected_location = args.location
    selected_voivodeship = ''
    # Always extract emails unless explicitly disabled via future flag
    selected_emails = True if not args.emails else True
    
    # helper for entering filenames
    def prompt_for_filename(default_name):
        print(f"\n--- ZAPISYWANIE WYNIKÓW ---")
        name = input(f"Podaj nazwę pliku do zapisu (domyślnie: {default_name}): ").strip()
        return name or default_name

    def ensure_output_path(output_name, output_format, scraped_dir=None):
        out_dir = scraped_dir or OUTPUT_DIR
        os.makedirs(out_dir, exist_ok=True)
        base_name = os.path.basename(output_name or OUTPUT_FILE)
        root, _ext = os.path.splitext(base_name)
        if not root:
            root = "results"
        fmt = (output_format or "xlsx").lower()
        ext = ".xlsx" if fmt == "xlsx" else ".csv" if fmt == "csv" else ".json"
        return os.path.join(out_dir, f"{root}{ext}")

    def resolve_existing_output(path, output_format):
        if not os.path.exists(path):
            return path
        confirm = input(f"Plik {path} już istnieje. Nadpisać? (t/n): ").strip().lower()
        if confirm == 't':
            return path
        new_name = input("Podaj nową nazwę pliku: ").strip()
        if not new_name:
            return path
        return ensure_output_path(new_name, output_format)

    # City -> Voivodeship lookup (used in wizard and CLI mode)
    POLAND_LOCATIONS = {
        "Dolnośląskie": ["Wrocław", "Wałbrzych", "Legnica", "Jelenia Góra", "Lubin", "Głogów", "Świdnica"],
        "Kujawsko-Pomorskie": ["Bydgoszcz", "Toruń", "Włocławek", "Grudziądz", "Inowrocław"],
        "Lubelskie": ["Lublin", "Zamość", "Chełm", "Biała Podlaska"],
        "Lubuskie": ["Zielona Góra", "Gorzów Wielkopolski", "Nowa Sól"],
        "Łódzkie": ["Łódź", "Piotrków Trybunalski", "Pabianice", "Tomaszów Mazowiecki", "Bełchatów"],
        "Małopolskie": ["Kraków", "Tarnów", "Nowy Sącz", "Oświęcim", "Chrzanów"],
        "Mazowieckie": ["Warszawa", "Radom", "Płock", "Siedlce", "Pruszków", "Legionowo"],
        "Opolskie": ["Opole", "Kędzierzyn-Koźle", "Nysa"],
        "Podkarpackie": ["Rzeszów", "Przemyśl", "Stalowa Wola", "Mielec"],
        "Podlaskie": ["Białystok", "Suwałki", "Łomża"],
        "Pomorskie": ["Gdańsk", "Gdynia", "Sopot", "Słupsk", "Tczew", "Wejherowo"],
        "Śląskie": ["Katowice", "Bielsko-Biała", "Częstochowa", "Gliwice", "Zabrze", "Bytom", "Rybnik", "Tychy", "Dąbrowa Górnicza", "Chorzów", "Sosnowiec"],
        "Świętokrzyskie": ["Kielce", "Ostrowiec Świętokrzyski", "Starachowice"],
        "Warmińsko-Mazurskie": ["Olsztyn", "Elbląg", "Ełk"],
        "Wielkopolskie": ["Poznań", "Kalisz", "Konin", "Piła", "Ostrów Wielkopolski", "Gniezno"],
        "Zachodniopomorskie": ["Szczecin", "Koszalin", "Stargard", "Kołobrzeg", "Świnoujście"]
    }

    # Interactive Wizard Mode
    if not any([args.query, args.location]):
        print("\n" + "="*40)
        print("   SCRAPER CONFIGURATION WIZARD")
        print("="*40)
        
        # 1. Choose Industry (PL / EN aligned with Attio `soundyou_industry`)
        print("\nSTEP 1/3: Choose industry")
        known_industries = [
            "spa", "masaż", "uroda", "hotel", "restaurant", "kawiarnia",
            "fitness", "joga", "fryzjer", "fizjoterapia", "sklep",
        ]
        industry_labels = {
            "spa":         "SPA & Wellness (EN: spa / wellness)",
            "masaż":       "Kobido & Masaż (EN: massage / kobido)",
            "uroda":       "Beauty / Salon Urody (EN: beauty salon)",
            "hotel":       "Hotele i Pensjonaty (EN: hotel / resort)",
            "restaurant":  "Restauracje (EN: restaurant / bistro)",
            "kawiarnia":   "Kawiarnie (EN: cafe / coffee shop)",
            "fitness":     "Fitness / Siłownia (EN: gym / fitness)",
            "joga":        "Joga / Pilates / Medytacja (EN: yoga / pilates)",
            "fryzjer":     "Salon Fryzjerski / Barber (EN: hairdresser / barber)",
            "fizjoterapia":"Poczekalnia / Gabinet (EN: clinic / physiotherapy)",
            "sklep":       "Sklep / Retail (EN: shop / boutique / retail)",
        }
        for i, k in enumerate(known_industries, 1):
            print(f"{i}. {industry_labels.get(k, k.upper())}")
        print(f"{len(known_industries)+1}. Other (type manually)")

        while not selected_query:
            choice = input("Your choice: ").strip()
            if choice.isdigit():
                idx = int(choice)
                if 1 <= idx <= len(known_industries):
                    selected_query = known_industries[idx-1]
                elif idx == len(known_industries) + 1:
                    raw_input = input("Enter industry name: ").strip()
                    if raw_input and not is_probably_path(raw_input):
                        selected_query = raw_input
                    else:
                        print("[!] Invalid name. Cannot be a file path.")
                else:
                    print("[!] Invalid number.")
            else:
                print("[!] Enter a number from the list.")

        # 2. Choose Location (Voivodeship -> City)
        print(f"\nSTEP 2/3: Choose location")

        while not selected_location:
            print("\n--- Voivodeships ---")
            voivodeships = sorted(POLAND_LOCATIONS.keys())
            for i, v in enumerate(voivodeships, 1):
                print(f"{i}. {v}")
            print(f"{len(voivodeships)+1}. Other / Enter city manually")

            v_choice = input("Choose voivodeship: ").strip()

            if v_choice.isdigit():
                v_idx = int(v_choice)
                if 1 <= v_idx <= len(voivodeships):
                    selected_v = voivodeships[v_idx-1]
                    selected_voivodeship = selected_v
                    cities = sorted(POLAND_LOCATIONS[selected_v])
                    print(f"\n--- Cities ({selected_v}) ---")
                    for j, c in enumerate(cities, 1):
                        print(f"{j}. {c}")
                    print(f"{len(cities)+1}. Enter another city from this voivodeship")

                    c_choice = input("Choose city: ").strip()
                    if c_choice.isdigit():
                        c_idx = int(c_choice)
                        if 1 <= c_idx <= len(cities):
                            selected_location = cities[c_idx-1]
                        elif c_idx == len(cities) + 1:
                            custom_city = input("Enter city name: ").strip()
                            if custom_city: selected_location = custom_city
                        else:
                            print("[!] Invalid city number.")
                    else:
                        print("[!] Invalid choice.")
                elif v_idx == len(voivodeships) + 1:
                    custom_loc = input("Enter city name: ").strip()
                    if custom_loc: selected_location = custom_loc
                else:
                    print("[!] Invalid voivodeship number.")
            else:
                print("[!] Enter a number from the list.")

        # 3. Emails (always on)
        print(f"\nSTEP 3/3: Email extraction")
        print("Emails will be collected automatically on every run.")
        selected_emails = True

        print("\n" + "="*40)
        print(f"CONFIGURATION READY:")
        print(f"Industry: {selected_query}")
        print(f"City: {selected_location}")
        print(f"Emails: {'YES' if selected_emails else 'NO'}")
        print("="*40)
        print("Uruchamiam proces scrapowania...\n")

    def prompt_non_empty(prompt_text, disallow_path=False):
        while True:
            value = input(prompt_text).strip()
            if not value:
                print("Wartość nie może być pusta. Spróbuj ponownie.")
                continue
            if disallow_path and is_probably_path(value):
                print("To wygląda jak ścieżka do pliku. Podaj normalną nazwę.")
                continue
            return value

    # Finalize variables
    query = (selected_query or args.query or "").strip()
    if not query:
        query = prompt_non_empty("Podaj branżę (query): ", disallow_path=True)
    
    location = (selected_location or args.location or "").strip()
    if not location:
        location = prompt_non_empty("Podaj miasto (location): ")

    # Derive voivodeship from city if not captured interactively (CLI mode)
    if not selected_voivodeship and location:
        _city_to_voi = {c.lower(): v for v, cities in POLAND_LOCATIONS.items() for c in cities}
        selected_voivodeship = _city_to_voi.get(location.lower(), '')

    scrape_emails_choice = True

    template_path = os.path.join(OUTPUT_DIR, 'SoundYouLeads.xlsx')

    # Dual-save mode (default): per-run archive + auto-append to master SoundYouLeads.xlsx.
    # When --output is set to a custom file, fall back to single-file behavior.
    custom_output = (args.output != OUTPUT_FILE)

    if custom_output:
        output_path = ensure_output_path(args.output, args.format)
        append_to_template = os.path.abspath(output_path) == os.path.abspath(template_path)
        if not append_to_template:
            output_path = resolve_existing_output(output_path, args.format)
        per_run_path = None
    else:
        from datetime import date as _date
        run_date = _date.today().strftime("%Y%m%d")
        per_run_root = f"{slugify(query)}_{slugify(location)}_{run_date}"
        per_run_path = os.path.join(OUTPUT_DIR, f"{per_run_root}.xlsx")
        output_path = per_run_path
        append_to_template = True

    setup_run_logging(output_path)
    use_google_choice = True
    use_booksy_choice = True

    # Wczesna walidacja klucza Google API — nie marnuj czasu na scrapowanie jeśli klucz jest zły
    if use_google_choice and API_KEY:
        try:
            _test_resp = requests.get(
                "https://maps.googleapis.com/maps/api/place/findplacefromtext/json",
                params={"input": "test", "inputtype": "textquery", "key": API_KEY},
                timeout=10
            )
            _test_status = _test_resp.json().get("status", "")
            if _test_status == "REQUEST_DENIED":
                logger.error(
                    "GOOGLE API KEY NIEPRAWIDŁOWY (REQUEST_DENIED) — wyłączam Google Places. "
                    "Sprawdź zmienną GOOGLE_MAPS_API_KEY w pliku .env"
                )
                use_google_choice = False
        except Exception as _e:
            logger.warning("Nie można zweryfikować klucza Google API: %s", _e)

    normalized_query = normalize_query_for_sources(query)
    if normalized_query != query:
        logger.info("Normalized query for sources: '%s' -> '%s'", query, normalized_query)
    
    all_google_details = []
    all_panorama_results = []
    all_pkt_results = []
    all_booksy_results = []
    all_spaeden_results = []
    all_fresha_results = []
    all_znanylekarz_results = []
    all_fixly_results = []
    all_moment_results = []
    all_cylex_results = []
    all_oferteo_results = []
    all_firmynet_results = []
    all_biznesfinder_results = []
    all_krs_results = []
    all_aleo_results = []
    
    session = create_session()
    cffi_session = create_cffi_session()
    cache_conn = setup_cache(args.cache_db) if scrape_emails_choice else None

    # Rozwiąż aktywne źródła z flagi --sources
    _ALL_SOURCES = {
        "panoramafirm", "pkt", "booksy", "spaeden", "fresha", "moment",
        "cylex", "oferteo", "firmynet", "biznesfinder", "znanylekarz",
        "fixly", "aleo", "google",
    }
    _FAST_SOURCES = {"google", "booksy", "firmynet", "znanylekarz"}
    raw_sources = (args.sources or "all").strip().lower()
    if raw_sources == "all":
        enabled_sources = set(_ALL_SOURCES)
    elif raw_sources == "fast":
        enabled_sources = set(_FAST_SOURCES)
    else:
        enabled_sources = {s.strip() for s in raw_sources.split(",") if s.strip()}

    # Gwarancja zamknięcia cache_conn nawet przy nieoczekiwanym wyjątku
    import atexit as _atexit
    _atexit.register(lambda: cache_conn.close() if cache_conn is not None else None)

    # Fetch data from Panorama Firm (używa curl_cffi TLS impersonation!)
    if "panoramafirm" in enabled_sources:
        logger.info("=== Fetching data from Panorama Firm ===")
        all_panorama_results = scrape_panorama_firm(
            normalized_query, location, max_pages=args.max_pages, session=cffi_session,
            use_headless=args.use_headless
        )
        annotate_sources(all_panorama_results, 'panoramafirm')

    # Fetch data from PKT.pl (używa curl_cffi TLS impersonation!)
    if "pkt" in enabled_sources:
        logger.info("=== Fetching data from PKT.pl ===")
        all_pkt_results = scrape_pkt_pl(
            normalized_query, location, max_pages=args.max_pages, session=cffi_session,
            use_headless=args.use_headless
        )
        annotate_sources(all_pkt_results, 'pkt')

    # Fetch data from Booksy (przekazujemy oryginalne query, aby obsłużyć wielokategorialne mapowanie!)
    if use_booksy_choice and "booksy" in enabled_sources:
        logger.info("=== Fetching data from Booksy ===")
        all_booksy_results = scrape_booksy(query, location, max_pages=args.max_pages, session=cffi_session)
        annotate_sources(all_booksy_results, 'booksy')

    # Fetch data from SPAeden rankings (filtrowane po lokalizacji!)
    if "spaeden" in enabled_sources and map_query_to_category(normalized_query) in ("spa", "wellness", "masaz", "hotel"):
        logger.info("=== Fetching data from SPAeden rankings ===")
        all_spaeden_results = scrape_spaeden_rankings(session=session, location=location)
        annotate_sources(all_spaeden_results, 'spaeden')

    # Fetch data from Fresha
    if "fresha" in enabled_sources:
        logger.info("=== Fetching data from Fresha ===")
        all_fresha_results = scrape_fresha(
            normalized_query,
            location,
            session=cffi_session,
            max_pages=args.max_pages,
            use_headless=args.use_headless,
            headless_wait=args.headless_wait
        )
        annotate_sources(all_fresha_results, 'fresha')

    # Fetch data from Moment.pl (Booksy mirror)
    if "moment" in enabled_sources:
        logger.info("=== Fetching data from Moment.pl ===")
        all_moment_results = scrape_moment(normalized_query, location, session=session, max_pages=args.max_pages)
        annotate_sources(all_moment_results, 'moment')

    # Fetch data from Cylex (używa curl_cffi TLS impersonation!)
    if "cylex" in enabled_sources:
        logger.info("=== Fetching data from Cylex ===")
        all_cylex_results = scrape_cylex(
            normalized_query,
            location,
            session=cffi_session,
            max_pages=args.max_pages,
            use_headless=args.use_headless,
            headless_wait=args.headless_wait
        )
        annotate_sources(all_cylex_results, 'cylex')

    # Fetch data from Oferteo
    if "oferteo" in enabled_sources:
        logger.info("=== Fetching data from Oferteo ===")
        all_oferteo_results = scrape_oferteo(normalized_query, location, session=cffi_session, max_pages=args.max_pages)
        annotate_sources(all_oferteo_results, 'oferteo')

    # Fetch data from Firmy.net
    if "firmynet" in enabled_sources:
        logger.info("=== Fetching data from Firmy.net ===")
        all_firmynet_results = scrape_firmynet(
            normalized_query,
            location,
            session=cffi_session,
            max_pages=args.max_pages,
            use_headless=args.use_headless,
            headless_wait=args.headless_wait
        )
        annotate_sources(all_firmynet_results, 'firmynet')

    # Fetch data from BiznesFinder
    if "biznesfinder" in enabled_sources:
        logger.info("=== Fetching data from BiznesFinder ===")
        all_biznesfinder_results = scrape_biznesfinder(
            normalized_query,
            location,
            session=cffi_session,
            max_pages=args.max_pages,
            use_headless=args.use_headless,
            headless_wait=args.headless_wait
        )
        annotate_sources(all_biznesfinder_results, 'biznesfinder')

    # Fetch data from ZnanyLekarz (masaże, fizjoterapia, poczekalnie/gabinety, medycyna estetyczna)
    if "znanylekarz" in enabled_sources and map_query_to_category(normalized_query) in ("masaz", "fizjoterapia"):
        logger.info("=== Fetching data from ZnanyLekarz ===")
        all_znanylekarz_results = scrape_znanylekarz(normalized_query, location, session=cffi_session, max_pages=args.max_pages)
        annotate_sources(all_znanylekarz_results, 'znanylekarz')

    # Fetch data from Fixly (best-effort, JS-rendered)
    if "fixly" in enabled_sources:
        logger.info("=== Fetching data from Fixly ===")
        all_fixly_results = scrape_fixly(
            normalized_query,
            location,
            session=cffi_session,
            max_pages=args.max_pages,
            use_headless=args.use_headless,
            headless_wait=args.headless_wait
        )
        annotate_sources(all_fixly_results, 'fixly')

    # KRS search API (api-rs.ms.gov.pl) is unavailable — DNS fails.
    # KRS enrichment (enrich_with_krs) still works for records that already have a KRS
    # number in their profile_url or extracted from website RODO/footer; runs later after merge.
    all_krs_results = []

    # Fetch data from Aleo.com (używa curl_cffi TLS impersonation!)
    if "aleo" in enabled_sources:
        logger.info("=== Fetching data from Aleo.com ===")
        all_aleo_results = scrape_aleo(normalized_query, location, session=cffi_session, max_pages=args.max_pages)
        annotate_sources(all_aleo_results, 'aleo')

    # Fetch data from Google Places (optional)
    if use_google_choice and API_KEY and "google" in enabled_sources:
        logger.info("=== Fetching data from Google Places API ===")
        next_page_token_val = None # Renamed 'token' to avoid conflict with token module
        page_count = 0
        max_google_pages = args.max_pages  # Limit number of pages to avoid API limits/costs
        
        try:
            while page_count < max_google_pages:
                if next_page_token_val:
                    logger.info("Fetching page %s from Google Places...", page_count + 1)
                    time.sleep(2) # API best practice: wait before requesting next page
                    results, next_page_token_val = search_next_page(next_page_token_val, session=session)
                else:
                    results, next_page_token_val = search_places(normalized_query, location, session=session)
                    
                if not results:
                    if page_count == 0: # Only print if no results on the first try
                        logger.info("No results found in Google Places.")
                    break # Exit loop if no results
                    
                logger.info("Found %s places on page %s", len(results), page_count + 1)
                
                for i, place in enumerate(results):
                    place_id_val = place.get('place_id') # Renamed 'pid'
                    if place_id_val:
                        logger.info("Fetching details %s/%s: %s", i + 1, len(results), place.get('name', 'Unknown Name'))
                        details = get_place_details(place_id_val, session=session)
                        
                        if details:
                            details['emails'] = [] # Initialize emails for Google results
                            all_google_details.append(details)
                            
                        time.sleep(random.uniform(0.2, 0.5)) # Shorter delay for details
                
                page_count += 1
                if not next_page_token_val: # If no more pages
                    break
        except Exception as e:
            logger.error("Error fetching data from Google Places: %s", e)
    
    # Ensure Google results are annotated
    annotate_sources(all_google_details, 'google')

    # Merge all results
    all_results = merge_results(all_google_details, all_panorama_results, all_pkt_results)
    if all_booksy_results:
        all_results = merge_results(all_results, all_booksy_results)
    if all_spaeden_results:
        all_results = merge_results(all_results, all_spaeden_results)
    if all_fresha_results:
        all_results = merge_results(all_results, all_fresha_results)
    if all_moment_results:
        all_results = merge_results(all_results, all_moment_results)
    if all_cylex_results:
        all_results = merge_results(all_results, all_cylex_results)
    if all_oferteo_results:
        all_results = merge_results(all_results, all_oferteo_results)
    if all_firmynet_results:
        all_results = merge_results(all_results, all_firmynet_results)
    if all_biznesfinder_results:
        all_results = merge_results(all_results, all_biznesfinder_results)
    if all_znanylekarz_results:
        all_results = merge_results(all_results, all_znanylekarz_results)
    if all_fixly_results:
        all_results = merge_results(all_results, all_fixly_results)
    if all_krs_results:
        all_results = merge_results(all_results, all_krs_results)
    if all_aleo_results:
        all_results = merge_results(all_results, all_aleo_results)
    logger.info("After deduplication, we have %s unique businesses.", len(all_results))

    # Filter out records whose name contains the word "auto" (e.g. "auto spa", "auto - spa")
    before_auto_filter = len(all_results)
    all_results = [
        r for r in all_results
        if not re.search(r'\bauto\b', r.get('name', ''), re.IGNORECASE)
    ]
    filtered_auto = before_auto_filter - len(all_results)
    if filtered_auto:
        logger.info("Filtered out %s records containing 'auto' in name.", filtered_auto)

    # KRS enrichment: uzupełnij brakujące pola dla rekordów z numerem KRS w profile_url
    # Działa dla wyników z każdego źródła (Panorama, PKT, Aleo, KRS search, itp.)
    logger.info("=== KRS enrichment (OdpisAktualny) ===")
    enrich_with_krs(all_results, session=session)

    # Google Places enrichment: dla rekordów bez telefonu i bez strony www
    # używa findplacefromtext (tańsze niż textsearch) zamiast pełnego scrapowania
    if API_KEY:
        enrich_with_google_places(all_results, location, session=session, cache_conn=cache_conn)

    # If user wants emails, fetch them for each business with a website URL
    if scrape_emails_choice:
        logger.info("=== Fetching emails from websites ===")
        # Count businesses that already have emails from deep scrape
        pre_email_count = sum(1 for r in all_results if r.get('emails'))
        logger.info("Businesses with emails from deep scrape: %s", pre_email_count)
        total_with_website = sum(1 for result in all_results if result.get('website'))
        logger.info("Found %s businesses with website addresses.", total_with_website)
        
        processed_websites = 0
        for result in all_results: # No need for index 'i' if not used
            website = result.get('website')
            profile_url = result.get('profile_url') or website
            # Replace catalog URLs with real company websites when possible
            if website and is_catalog_url(website) and profile_url:
                if 'booksy.com' in profile_url:
                    pdata = extract_booksy_profile_data(profile_url, session=session)
                    external = pdata.get('website', '')
                    if pdata.get('facebook'):
                        result['facebook'] = result.get('facebook') or pdata['facebook']
                    if pdata.get('instagram'):
                        result['instagram'] = result.get('instagram') or pdata['instagram']
                    if pdata.get('emails'):
                        existing = result.get('emails') or []
                        result['emails'] = list(dict.fromkeys(existing + pdata['emails']))
                else:
                    parsed = urlparse(profile_url)
                    skip_domains = [
                        parsed.netloc.lower(),
                        'facebook.com', 'instagram.com', 'youtube.com', 'tiktok.com',
                        'linkedin.com', 'google.', 'maps.google.'
                    ]
                    external = extract_external_website_from_profile(
                        profile_url,
                        session=session,
                        skip_domains=skip_domains
                    )
                if external:
                    website = external
                    result['website'] = external
            if website:
                processed_websites += 1
                existing_emails = result.get('emails') or []
                if existing_emails:
                    logger.info("[%s/%s] Skipping website scrape (emails already known): %s", processed_websites, total_with_website, result.get('name', 'Unknown Name'))
                else:
                    logger.info("[%s/%s] Fetching emails for: %s", processed_websites, total_with_website, result.get('name', 'Unknown Name'))
                    emails = extract_emails_from_website(
                        website,
                        session=session,
                        cache_conn=cache_conn,
                        respect_robots=args.respect_robots,
                        use_headless=args.use_headless,
                        headless_wait=args.headless_wait
                    )
                    result['emails'] = list(dict.fromkeys([e for e in emails if e]))
                    wmeta = _WEBSITE_META_CACHE.get(website) or _WEBSITE_META_CACHE.get(
                        website if website.startswith(('http://', 'https://')) else 'https://' + website
                    )
                    if wmeta:
                        result['facebook'] = result.get('facebook') or wmeta.get('facebook', '')
                        result['instagram'] = result.get('instagram') or wmeta.get('instagram', '')
                        result['site_title'] = result.get('site_title') or wmeta.get('site_title', '')
                    # Delay to avoid overloading servers
                    time.sleep(random.uniform(1.0, 2.0))

    # Inject metadata into every record before saving
    for _r in all_results:
        _r.setdefault('branża', query)
        _r.setdefault('miasto', location)
        _r.setdefault('województwo', selected_voivodeship)

    # Kanoniczna klasyfikacja branż SoundYou + automatyczne ustalanie nazwy firmy i linków social media
    try:
        from attio_sync import (
            classify_soundyou_industries,
            extract_social_links,
            resolve_company_name,
            sync_csv_to_attio,
        )
    except ImportError:
        classify_soundyou_industries = None
        extract_social_links = None
        resolve_company_name = None
        sync_csv_to_attio = None

    _SPA_LIKE = {"spa", "uroda", "fryzjer", "masaz", "masaż", "wellness", "fizjoterapia", "joga"}
    for _r in all_results:
        if extract_social_links is not None:
            soc = extract_social_links(_r.get('facebook', ''), _r.get('instagram', ''), _r.get('website', ''), _r.get('profile_url', ''))
            _r['facebook'] = soc['facebook']
            _r['instagram'] = soc['instagram']
        if resolve_company_name is not None:
            resolved = resolve_company_name(
                _r.get('name', ''),
                site_title=_r.get('site_title', ''),
                website=_r.get('website', ''),
                facebook=_r.get('facebook', ''),
                instagram=_r.get('instagram', ''),
                emails=_r.get('emails') or [],
                city=_r.get('miasto', ''),
            )
            if resolved:
                _r['name'] = resolved
        raw_branza = _r.get('branża', '')
        if raw_branza.lower() in _SPA_LIKE and _is_hotel_record(_r):
            raw_branza = 'hotel-spa'
        if classify_soundyou_industries is not None:
            sig_urls = ' '.join(x for x in (_r.get('website', ''), _r.get('facebook', ''), _r.get('instagram', '')) if x)
            canonical = classify_soundyou_industries(
                raw_branza,
                name=_r.get('name', ''),
                website=sig_urls,
            )
            _r['branża'] = ', '.join(canonical)
        else:
            _r['branża'] = raw_branza

    # Save all data
    if all_results:
        if per_run_path is not None:
            save_results(all_results, per_run_path, 'xlsx', city=location, append_to_template=False)
            logger.info("Per-run file saved: %s (%d records)", per_run_path, len(all_results))
            save_results(all_results, template_path, 'xlsx', city=location, append_to_template=True)
            logger.info("Appended %d records to master: %s", len(all_results), template_path)
        else:
            save_results(all_results, output_path, args.format, city=location, append_to_template=append_to_template)
        csv_master_path = os.path.join(OUTPUT_DIR, 'SoundYouLeads.csv')
        append_to_csv_master(all_results, csv_master_path)
        logger.info("Appended %d records to CSV master: %s", len(all_results), csv_master_path)

        if (args.sync_attio or args.attio_dry_run or args.attio_fill_missing) and sync_csv_to_attio is not None:
            try:
                logger.info("Synchronizing CSV master to Attio (%s)...", "dry-run" if args.attio_dry_run else "apply")
                batch_label = f"{datetime.now().strftime('%Y-%m-%d')} | {query}-{location}"
                attio_report = sync_csv_to_attio(
                    csv_master_path,
                    dry_run=args.attio_dry_run,
                    ensure_schema=True,
                    sync_people=True,
                    fill_missing=args.attio_fill_missing,
                    lead_batch=batch_label,
                )
                logger.info("Attio sync complete: %s", json.dumps(attio_report.get("stats", {}), ensure_ascii=False))
            except Exception as exc:
                logger.error("Attio sync failed: %s", exc)
    else:
        logger.info("No data to save.")

    if cache_conn is not None:
        cache_conn.close()

if __name__ == '__main__':
    main()
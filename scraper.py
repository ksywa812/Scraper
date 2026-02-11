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
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Safari/605.1.15',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:90.0) Gecko/20100101 Firefox/90.0',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/92.0.4515.107 Safari/537.36'
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
        "firmy.net",
        "oferteo.pl",
        "cylex-polska.pl",
        "fresha.com",
        "fixly.pl",
        "panoramafirm.pl",
        "pkt.pl",
        "biznesfinder.pl",
        "znanylekarz.pl",
        "moment.pl",
        "itunes.apple.com",
        "apps.apple.com",
        "play.google.com",
        "apple.com/app",
    ])


def extract_external_website_from_profile(profile_url, session=None, skip_domains=None):
    if not profile_url:
        return ""
    session = session or create_session()
    skip_domains = skip_domains or []

    def is_valid_external(url):
        if not url or not url.startswith('http'):
            return False
        lower = url.lower()
        return not any(sd in lower for sd in skip_domains)

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


CATEGORY_KEYWORDS = {
    "spa": [
        "spa", "day spa", "spa & wellness", "rytual spa", "rytuał spa", "salon spa",
        "masaz", "masaź", "masaż", "massage", "masazysta", "masażysta",
        "masaz relaksacyjny", "masaz klasyczny", "masaz leczniczy",
        "masaz sportowy", "masaz balijski", "masaz tajski", "masaz kobido",
        "masaz lomi", "masaz tkanek", "masaz goracymi kamieniami",
        "masaz aroma", "aromaterapia", "bodywork", "kobido"
    ],
    "wellness": ["wellness", "odnowa biologiczna", "relaks", "relaksacja"],
    "joga": ["joga", "yoga", "hatha", "vinyasa", "ashtanga", "yin", "kundalini", "joga nidra"],
    "fizjoterapia": ["fizjoterapia", "rehabilitacja", "fizjo", "terapia manualna", "kinezyterapia"],
}

CATEGORY_PRIORITY = ["spa", "wellness", "joga", "fizjoterapia"]


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
    return "spas"


def category_keywords_for_query(query):
    category = map_query_to_category(query)
    return CATEGORY_KEYWORDS.get(category, []) if category else []


def znanylekarz_path_for_query(query):
    category = map_query_to_category(query)
    if category == "masaz":
        return "uslugi-zabiegi/masaz"
    if category == "fizjoterapia":
        return "fizjoterapeuta"
    return None


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
    try:
        import undetected_chromedriver as uc
    except Exception as e:
        logger.warning("Headless unavailable (undetected_chromedriver): %s", e)
        return None

    options = uc.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1280,720")

    # Detect installed Chrome version and force matching driver
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
            "joga": "joga",
        }
        return booksy_category_map.get(mapped, mapped)

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


def clean_booksy_name(text):
    if not text:
        return ""
    text = text.replace("Promowany", "").strip()
    if " - " in text:
        text = text.split(" - ")[0]
    text = re.sub(r"\b\d+(?:\.\d+)?\s*km\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\b\d+,\d+\b", "", text)
    text = re.sub(r"\b\d+\s*opin\w*\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_booksy_profile_data(profile_url, session=None):
    """Extract website, email, and phone from a Booksy profile via __NUXT__ data."""
    result = {'website': '', 'emails': [], 'phone': ''}
    if not profile_url:
        return result
    session = session or create_session()
    try:
        resp = session.get(profile_url, headers={'User-Agent': get_random_user_agent()}, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')

        skip_domains = [
            'booksy.com', 'facebook.com', 'instagram.com', 'youtube.com',
            'tiktok.com', 'linkedin.com', 'google.', 'maps.google.',
            'itunes.apple.com', 'apps.apple.com', 'play.google.com',
            'apple.com/app', 'microsoft.com', 'apps.microsoft.com',
            'appsflyer.com', 'doubleclick.net', 'cloudfront.net',
            'googleapis.com', 'googletagmanager.com', 'feroot.com',
            'onelink.me', 'apple-mapkit.com', 'app.link',
        ]

        def is_valid_external(url):
            if not url or not url.startswith('http'):
                return False
            lower = url.lower()
            return not any(sd in lower for sd in skip_domains)

        # PRIMARY: Parse window.__NUXT__ — Booksy embeds all business data here
        for script in soup.find_all('script'):
            text = script.get_text(' ', strip=True)
            if 'window.__NUXT__' not in text:
                continue
            decoded = text.replace('\\u002F', '/')

            # Extract website
            website_match = re.search(r'website:"(https?://[^"]+)"', decoded)
            if website_match:
                w = website_match.group(1)
                if is_valid_external(w):
                    result['website'] = w

            # Extract phone
            phone_match = re.search(r'phone:"([^"]+)"', decoded)
            if phone_match:
                result['phone'] = phone_match.group(1)

            # Extract emails (skip booksy.com emails)
            all_emails = re.findall(r'[\w.+-]+@[\w-]+\.[\w.]+', decoded)
            result['emails'] = list(dict.fromkeys(
                e for e in all_emails if 'booksy.com' not in e.lower()
            ))
            break  # Only process first __NUXT__ script

        # FALLBACK: Try explicit website links in HTML if __NUXT__ didn't yield website
        if not result['website']:
            for a in soup.find_all('a', href=True):
                text = " ".join(a.stripped_strings).lower()
                href = a.get('href')
                if any(k in text for k in ['www', 'strona', 'website', 'witryna', 'odwiedz', 'odwiedź']):
                    if is_valid_external(href):
                        result['website'] = href
                        break

        # FALLBACK: JSON-LD
        if not result['website']:
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
                                        result['website'] = u
                                        break
                            elif is_valid_external(url):
                                result['website'] = url
                            if result['website']:
                                break
                except Exception:
                    continue
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

        if href.startswith("http"):
            full_url = href
        elif href.startswith("/pl-pl/"):
            full_url = f"https://booksy.com{href}"
        else:
            full_url = f"{BOOKSY_BASE}{href}"
        text = " ".join(a.stripped_strings)
        if not text:
            continue

        entry = entries.setdefault(full_url, {"name": "", "address": "", "profile_url": full_url})

        if "•" in text:
            left, right = text.split("•", 1)
            name = clean_booksy_name(left)
            address = right.replace("Karty podarunkowe Booksy", "").strip()
            if name and not entry["name"]:
                entry["name"] = name
            if address and not entry["address"]:
                entry["address"] = address
        else:
            name = clean_booksy_name(text)
            if name and not entry["name"]:
                entry["name"] = name

    results = []
    for url, data in entries.items():
        if not data["name"]:
            continue
        results.append({
            'name': data["name"],
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

    category_slug = get_booksy_category_slug(query, session=session)
    if not category_slug:
        logger.info("Booksy: no matching category for query '%s'.", query)
        return results

    city_slug = get_booksy_city_slug(category_slug, location, session=session)
    if not city_slug:
        logger.info("Booksy: could not resolve city slug for '%s'.", location)
        return results

    logger.info("Scraping Booksy category '%s' for %s", category_slug, location)
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
            logger.info("Booksy: found %s listings on page %s", len(page_results), page)
            results.extend(page_results)
            time.sleep(random.uniform(1.0, 2.0))
        except Exception as e:
            logger.warning("Booksy: error fetching %s: %s", page_url, e)
            break

    # Deep scrape Booksy profiles to get real external website URLs, emails, and phones
    if results:
        logger.info("Booksy: deep scraping %s profiles for external websites & emails", len(results))
    for idx, item in enumerate(results, start=1):
        profile_url = item.get('profile_url') or item.get('website')
        if not profile_url:
            continue
        logger.info("Booksy: profile %s/%s  %s", idx, len(results), item.get('name', ''))
        profile_data = extract_booksy_profile_data(profile_url, session=session)
        # Website
        if profile_data['website'] and not is_catalog_url(profile_data['website']):
            logger.info("Booksy:   → website: %s", profile_data['website'])
            item['website'] = profile_data['website']
        else:
            item['website'] = ""
        # Emails
        if profile_data['emails']:
            logger.info("Booksy:   → emails: %s", ', '.join(profile_data['emails']))
            existing = item.get('emails') or []
            item['emails'] = list(dict.fromkeys(existing + profile_data['emails']))
        # Phone
        if profile_data['phone'] and not item.get('formatted_phone_number'):
            item['formatted_phone_number'] = profile_data['phone']
        time.sleep(random.uniform(0.5, 1.0))

    return results


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
        key = f"{normalize_name(name)}|{normalize_address(place)}"
        if not name or key in seen:
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


def scrape_spaeden_rankings(session=None):
    results = []
    session = session or create_session()
    try:
        resp = session.get(SPAEDEN_RANKING_URL, headers={'User-Agent': get_random_user_agent()}, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        text = soup.get_text("\n")
        results = parse_spaeden_rankings(text)
        logger.info("SPAeden: extracted %s ranking entries", len(results))
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
    for base_url in base_urls:
        total_added_for_base = 0
        for page in range(1, max_pages + 1):
            url = base_url if page == 1 else f"{base_url}?page={page}"
            try:
                resp = session.get(url, headers={'User-Agent': get_random_user_agent()}, timeout=20)
                if resp.status_code == 404:
                    logger.warning("Fresha: 404 for %s", url)
                    break
                resp.raise_for_status()
                page_added = parse_fresha_html(resp.text, seen)
                if page_added == 0 and use_headless:
                    rendered = get_rendered_html(url, wait_seconds=headless_wait, timeout=25)
                    if rendered:
                        page_added = parse_fresha_html(rendered, seen)
                logger.info("Fresha: extracted %s listings from %s", page_added, url)
                total_added_for_base += page_added
                if page_added == 0:
                    break
                time.sleep(random.uniform(1.0, 2.0))
            except Exception as e:
                logger.warning("Fresha: error fetching %s: %s", url, e)
                break

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
    session = session or create_session()
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
                    if len(seen_profiles) >= max_profiles:
                        break

                logger.info("Oferteo: collected %s profiles from %s", len(results), url)
                if len(seen_profiles) >= max_profiles:
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
    return re.sub(r"\D+", "", phone or "")


def normalize_name(name):
    return re.sub(r"[^\w\s]", "", (name or "").lower()).strip()


def normalize_address(address):
    address = (address or "").lower()
    address = re.sub(r"\s+", " ", address)
    return address.strip()

def scrape_panorama_firm(query, location, max_pages=3, session=None):
    """Scrapes data from Panorama Firm website."""
    results = []
    session = session or create_session()
    
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
            businesses = soup.select('div.card.company-item')
            
            if not businesses:
                logger.info("No more results found on page %s", page)
                break
                
            for business in businesses:
                try:
                    # Basic data
                    name_elem = business.select_one('h2.company-name')
                    name = name_elem.text.strip() if name_elem else "Unknown Name"
                    
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
                    
                    result = {
                        'name': name,
                        'formatted_address': address,
                        'formatted_phone_number': phone,
                        'website': website,
                        'emails': [] # Initialize emails list
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

def scrape_pkt_pl(query, location, max_pages=3, session=None):
    """Scrapes data from PKT.pl website."""
    results = []
    session = session or create_session()
    
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
            businesses = soup.select('li.list-items')
            
            if not businesses:
                logger.info("No more results found on page %s", page)
                break
                
            for business in businesses:
                try:
                    # Basic data
                    name_elem = business.select_one('h2.company-name a')
                    name = name_elem.text.strip() if name_elem else "Unknown Name"
                    
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
                    
                    result = {
                        'name': name,
                        'formatted_address': address,
                        'formatted_phone_number': phone,
                        'website': website,
                        'emails': []
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

def get_place_details(place_id, session=None):
    """Fetches place details from Google Places API."""
    if not API_KEY:
        return {}
    session = session or create_session()
        
    try:
        url = 'https://maps.googleapis.com/maps/api/place/details/json'
        params = {
            'place_id': place_id,
            'fields': 'name,formatted_address,formatted_phone_number,website', # Requested fields
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
        
        # Use regex to find email addresses
        email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,7}\b' # Updated TLD length
        emails = re.findall(email_pattern, response.text)
        
        # Also, get emails from contact pages if available
        soup = BeautifulSoup(response.text, 'html.parser')
        
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
                contact_emails = re.findall(email_pattern, contact_response.text)
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
                    emails.extend(re.findall(email_pattern, resp.text))
                except Exception:
                    continue

        # Headless fallback for JS-rendered pages (Playwright first, then Chromium)
        if not emails and use_headless:
            rendered = render_with_playwright(url, timeout_ms=20000)
            if not rendered:
                rendered = get_rendered_html(url, wait_seconds=headless_wait, timeout=20)
            if rendered:
                emails.extend(re.findall(email_pattern, rendered))
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
                        contact_emails = re.findall(email_pattern, contact_response.text)
                        emails.extend(contact_emails)
                    except Exception:
                        pass
        
        # Check 'data-email' attribute often used for hidden emails
        for element in soup.find_all(attrs={"data-email": True}):
            data_email = element.get('data-email')
            if data_email and '@' in data_email: # Ensure data_email is not None
                emails.append(data_email)
        
        # Remove duplicates and filter emails
        unique_emails = list(set(emails))
        
        # Filter out common generic or spammy domains
        filtered_emails = [email for email in unique_emails if not any(
            domain in email.lower() for domain in ['example.com', 'domain.com', 'yourmail.com', 'wixpress.com', 'sentry.io'] # Added more common spam/service domains
        )]
        
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

    Matching key: normalized_name | simple_address | normalized_phone
    Merging rules:
    - Union emails (unique); if a source labelled 'booksy' provides emails, place them first.
    - Fill empty fields (website, profile_url, phone, address) from other sources when available.
    - Collect 'sources' for each merged record.
    """
    all_results = []
    for lst in lists:
        if not lst:
            continue
        all_results.extend(lst)

    merged = {}
    insertion_order = []

    for r in all_results:
        name = r.get('name', '').strip()
        normalized_name = normalize_name(name)
        address_parts = normalize_address(r.get('formatted_address', '')).split(',')
        simple_address = address_parts[0].strip() if address_parts else ""
        phone = normalize_phone(r.get('formatted_phone_number', ''))

        if not normalized_name:
            continue

        key = f"{normalized_name}|{simple_address}|{phone}"

        if key in merged:
            existing = merged[key]
            # Merge sources
            for s in r.get('sources', []):
                if s not in existing['sources']:
                    existing['sources'].append(s)

            # Merge emails with Booksy priority
            existing_emails = existing.get('emails') or []
            new_emails = r.get('emails') or []
            if 'booksy' in r.get('sources', []):
                combined = new_emails + [e for e in existing_emails if e not in new_emails]
            else:
                combined = existing_emails + [e for e in new_emails if e not in existing_emails]

            # Deduplicate preserving order
            seen = set()
            final_emails = []
            for e in combined:
                if e and e not in seen:
                    seen.add(e)
                    final_emails.append(e)
            existing['emails'] = final_emails

            # Fill missing fields (prefer non-empty)
            if not existing.get('website') and r.get('website'):
                existing['website'] = r.get('website')
            if not existing.get('profile_url') and r.get('profile_url'):
                existing['profile_url'] = r.get('profile_url')
            if not existing.get('formatted_phone_number') and r.get('formatted_phone_number'):
                existing['formatted_phone_number'] = r.get('formatted_phone_number')
            if not existing.get('formatted_address') and r.get('formatted_address'):
                existing['formatted_address'] = r.get('formatted_address')
            if not existing.get('name') and r.get('name'):
                existing['name'] = r.get('name')

        else:
            new = dict(r)
            new.setdefault('sources', list(r.get('sources', [])))
            new['emails'] = list(dict.fromkeys(new.get('emails') or []))
            merged[key] = new
            insertion_order.append(key)

    return [merged[k] for k in insertion_order]

def save_to_excel(data, filename=OUTPUT_FILE, city=None, append=False):
    """Saves data to an Excel file. If target is the IdeaMusicLeads template, append into it
    and preserve layout/style, insert a colored city header row, set Status='to call', and
    ensure email2/email3 columns exist (hidden).
    """
    try:
        template_path = os.path.join('scraped', 'IdeaMusicLeads.xlsx')
        target_basename = os.path.basename(filename or '')

        # If we're updating the live IdeaMusicLeads.xlsx, open and append
        if os.path.exists(template_path) and target_basename.lower() == os.path.basename(template_path).lower():
            wb = openpyxl.load_workbook(template_path)
            ws = wb[wb.sheetnames[0]]

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
                            insert_row = ws.max_row + 1

                    else:
                        # No existing city header: insert after last non-empty row
                        last_non_empty = 1
                        for r in range(ws.max_row, 1, -1):
                            has_any = False
                            for c in range(1, len(headers) + 1):
                                if ws.cell(row=r, column=c).value not in (None, ''):
                                    has_any = True
                                    break
                            if has_any:
                                last_non_empty = r
                                break

                        insert_row = last_non_empty + 1
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
                            else:
                                row_vals.append('')

                        ws.insert_rows(insert_row + idx)
                        for ci, v in enumerate(row_vals, start=1):
                            ws.cell(row=insert_row + idx, column=ci).value = v

                    # Hide email 2/3 columns
                    try:
                        col_idx_email2 = headers.index('Adres E-mail 2') + 1
                        col_idx_email3 = headers.index('Adres E-mail 3') + 1
                        col_letter_2 = openpyxl.utils.get_column_letter(col_idx_email2)
                        col_letter_3 = openpyxl.utils.get_column_letter(col_idx_email3)
                        ws.column_dimensions[col_letter_2].hidden = True
                        ws.column_dimensions[col_letter_3].hidden = True
                        ws.column_dimensions[col_letter_2].width = 0
                        ws.column_dimensions[col_letter_3].width = 0
                    except Exception:
                        pass

                # Save and finish merge/append (avoid appending all items again)
                appended_count = len(to_append)
                wb.save(template_path)
                logger.info("Updated %s existing rows and appended %s new records to %s", updated_count, appended_count, template_path)
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

                # Hide email 2/3 columns to avoid taking screen space
                try:
                    col_idx_email2 = headers.index('Adres E-mail 2') + 1
                    col_idx_email3 = headers.index('Adres E-mail 3') + 1
                    col_letter_2 = openpyxl.utils.get_column_letter(col_idx_email2)
                    col_letter_3 = openpyxl.utils.get_column_letter(col_idx_email3)
                    ws.column_dimensions[col_letter_2].hidden = True
                    ws.column_dimensions[col_letter_3].hidden = True
                    ws.column_dimensions[col_letter_2].width = 0
                    ws.column_dimensions[col_letter_3].width = 0
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
                    else:
                        # Unknown column from template: leave empty
                        row_vals.append('')

                ws.append(row_vals)

            # Save workbook back to template path
            wb.save(template_path)
            logger.info("Appended %s records to %s", len(data), template_path)
            return

        # Fallback behaviour: create a new workbook (previous behaviour)
        wb = openpyxl.Workbook()
        ws = wb.active

        # Fallback headers (Polish names matching template intent)
        headers = ['Nazwa Salonu', 'Adres', 'Numer Telefonu', 'Adres E-mail', 'Adres E-mail 2', 'Adres E-mail 3', 'Other Emails', 'Sources']
        ws.append(headers)

        for item in data:
            emails = item.get('emails', []) or []
            sources = item.get('sources') or []
            row = [
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
        logger.error("Error saving to Excel: %s", e)


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
            writer.writerow(['Name', 'Address', 'Phone', 'Website', 'Email 1', 'Email 2', 'Email 3', 'Other Emails', 'Sources'])
            for item in data:
                emails = item.get('emails', [])
                row_data = [
                    item.get('name', ''),
                    item.get('formatted_address', ''),
                    item.get('formatted_phone_number', ''),
                    item.get('website', '')
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
    args = parser.parse_args()

    # Variables to store interactive choices, defaults from args
    selected_query = args.query
    selected_location = args.location
    # Always extract emails unless explicitly disabled via future flag
    selected_emails = True if not args.emails else True
    
    # helper for entering filenames
    def prompt_for_filename(default_name):
        print(f"\n--- ZAPISYWANIE WYNIKÓW ---")
        name = input(f"Podaj nazwę pliku do zapisu (domyślnie: {default_name}): ").strip()
        return name or default_name

    def ensure_output_path(output_name, output_format, scraped_dir="scraped"):
        os.makedirs(scraped_dir, exist_ok=True)
        base_name = os.path.basename(output_name or OUTPUT_FILE)
        root, _ext = os.path.splitext(base_name)
        if not root:
            root = "results"
        fmt = (output_format or "xlsx").lower()
        ext = ".xlsx" if fmt == "xlsx" else ".csv" if fmt == "csv" else ".json"
        return os.path.join(scraped_dir, f"{root}{ext}")

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

    # Interactive Wizard Mode
    if not any([args.query, args.location]):
        print("\n" + "="*40)
        print("   KREATOR KONFIGURACJI SCRAPERA")
        print("="*40)
        
        # 1. Choose Industry
        print("\nKROK 1/3: Wybierz branżę")
        known_industries = ["spa", "wellness", "joga", "fizjoterapia"]
        industry_labels = {
            "spa": "SPA / Masaż / Kobido",
            "wellness": "Wellness",
            "joga": "Joga",
            "fizjoterapia": "Fizjoterapia",
        }
        for i, k in enumerate(known_industries, 1):
            print(f"{i}. {industry_labels.get(k, k.upper())}")
        print(f"{len(known_industries)+1}. Inna (wpisz ręcznie)")
        
        while not selected_query:
            choice = input("Twój wybór: ").strip()
            if choice.isdigit():
                idx = int(choice)
                if 1 <= idx <= len(known_industries):
                    selected_query = known_industries[idx-1]
                elif idx == len(known_industries) + 1:
                    raw_input = input("Wpisz nazwę branży: ").strip()
                    if raw_input and not is_probably_path(raw_input):
                        selected_query = raw_input
                    else:
                        print("[!] Nieprawidłowa nazwa. Nie może być ścieżką do pliku.")
                else:
                    print("[!] Nieprawidłowy numer.")
            else:
                 print("[!] Wpisz numer z listy.")

        # 2. Choose Location (Voivodeship -> City)
        print(f"\nKROK 2/3: Wybierz lokalizację")
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
        
        while not selected_location:
            print("\n--- Województwa ---")
            voivodeships = sorted(POLAND_LOCATIONS.keys())
            for i, v in enumerate(voivodeships, 1):
                print(f"{i}. {v}")
            print(f"{len(voivodeships)+1}. Inne / Wpisz ręcznie miasto")
            
            v_choice = input("Wybierz województwo: ").strip()
            
            if v_choice.isdigit():
                v_idx = int(v_choice)
                if 1 <= v_idx <= len(voivodeships):
                    selected_v = voivodeships[v_idx-1]
                    cities = sorted(POLAND_LOCATIONS[selected_v])
                    print(f"\n--- Miasta ({selected_v}) ---")
                    for j, c in enumerate(cities, 1):
                        print(f"{j}. {c}")
                    print(f"{len(cities)+1}. Wpisz inne miasto z tego województwa")
                    
                    c_choice = input("Wybierz miasto: ").strip()
                    if c_choice.isdigit():
                        c_idx = int(c_choice)
                        if 1 <= c_idx <= len(cities):
                            selected_location = cities[c_idx-1]
                        elif c_idx == len(cities) + 1:
                            custom_city = input("Wpisz nazwę miasta: ").strip()
                            if custom_city: selected_location = custom_city
                        else:
                            print("[!] Nieprawidłowy numer miasta.")
                    else:
                        print("[!] Nieprawidłowy wybór.")
                elif v_idx == len(voivodeships) + 1:
                     custom_loc = input("Wpisz nazwę miasta: ").strip()
                     if custom_loc: selected_location = custom_loc
                else:
                    print("[!] Nieprawidłowy numer województwa.")
            else:
                print("[!] Wpisz numer z listy.")

        # 3. Emails (always on)
        print(f"\nKROK 3/3: Pobieranie emaili")
        print("Emaile będą pobierane automatycznie przy każdym uruchomieniu.")
        selected_emails = True

        print("\n" + "="*40)
        print(f"KONFIGURACJA GOTOWA:")
        print(f"Branża: {selected_query}")
        print(f"Miasto: {selected_location}")
        print(f"Emaile: {'TAK' if selected_emails else 'NIE'}")
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
        
    scrape_emails_choice = True

    # Resolve output path before scraping (to keep log filename aligned)
    if not any([args.query, args.location]) and args.output == OUTPUT_FILE:
        suggested_root = f"{slugify(query)}_{slugify(location)}"
        chosen_name = prompt_for_filename(f"{suggested_root}")
        output_path = ensure_output_path(chosen_name, args.format)
    else:
        output_path = ensure_output_path(args.output, args.format)

    # If the chosen output is the standard template, ask whether to append to it
    template_path = os.path.join('scraped', 'IdeaMusicLeads.xlsx')
    append_to_template = False

    if os.path.exists(template_path) and os.path.abspath(output_path) == os.path.abspath(template_path):
        ans = input(f"Plik {template_path} już istnieje. Dopisać do niego? (t/n): ").strip().lower()
        if ans == 't':
            append_to_template = True
        else:
            # Ask for alternative filename
            new_name = input("Podaj nazwę pliku do zapisu (bez rozszerzenia, pusty = anuluj): ").strip()
            if new_name:
                output_path = ensure_output_path(new_name, args.format)
            else:
                print("Anulowano zapis do szablonu. Kontynuuję; podaj inną nazwę przy ponownym uruchomieniu.")
                # keep output_path as originally provided (will trigger overwrite prompt)

    if not append_to_template:
        output_path = resolve_existing_output(output_path, args.format)

    setup_run_logging(output_path)
    use_google_choice = True
    use_booksy_choice = True

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
    
    session = create_session()
    cache_conn = setup_cache(args.cache_db) if scrape_emails_choice else None

    # Fetch data from Panorama Firm (always)
    logger.info("=== Fetching data from Panorama Firm ===")
    all_panorama_results = scrape_panorama_firm(normalized_query, location, max_pages=args.max_pages, session=session)
    annotate_sources(all_panorama_results, 'panoramafirm')
    
    # Fetch data from PKT.pl (always)
    logger.info("=== Fetching data from PKT.pl ===")
    all_pkt_results = scrape_pkt_pl(normalized_query, location, max_pages=args.max_pages, session=session)
    annotate_sources(all_pkt_results, 'pkt')

    # Fetch data from Booksy (optional)
    if use_booksy_choice:
        logger.info("=== Fetching data from Booksy ===")
        all_booksy_results = scrape_booksy(normalized_query, location, max_pages=args.max_pages, session=session)
        annotate_sources(all_booksy_results, 'booksy')

    # Fetch data from SPAeden rankings (always for spa/wellness/massage)
    if map_query_to_category(normalized_query) in ("spa", "wellness", "masaz"):
        logger.info("=== Fetching data from SPAeden rankings ===")
        all_spaeden_results = scrape_spaeden_rankings(session=session)
        annotate_sources(all_spaeden_results, 'spaeden')

    # Fetch data from Fresha (always)
    logger.info("=== Fetching data from Fresha ===")
    all_fresha_results = scrape_fresha(
        normalized_query,
        location,
        session=session,
        max_pages=args.max_pages,
        use_headless=args.use_headless,
        headless_wait=args.headless_wait
    )
    annotate_sources(all_fresha_results, 'fresha')

    # Fetch data from Moment.pl (Booksy mirror)
    logger.info("=== Fetching data from Moment.pl ===")
    all_moment_results = scrape_moment(normalized_query, location, session=session, max_pages=args.max_pages)
    annotate_sources(all_moment_results, 'moment')

    # Fetch data from Cylex
    logger.info("=== Fetching data from Cylex ===")
    all_cylex_results = scrape_cylex(
        normalized_query,
        location,
        session=session,
        max_pages=args.max_pages,
        use_headless=args.use_headless,
        headless_wait=args.headless_wait
    )
    annotate_sources(all_cylex_results, 'cylex')

    # Fetch data from Oferteo
    logger.info("=== Fetching data from Oferteo ===")
    all_oferteo_results = scrape_oferteo(normalized_query, location, session=session, max_pages=args.max_pages)
    annotate_sources(all_oferteo_results, 'oferteo')

    # Fetch data from Firmy.net
    logger.info("=== Fetching data from Firmy.net ===")
    all_firmynet_results = scrape_firmynet(
        normalized_query,
        location,
        session=session,
        max_pages=args.max_pages,
        use_headless=args.use_headless,
        headless_wait=args.headless_wait
    )
    annotate_sources(all_firmynet_results, 'firmynet')

    # Fetch data from BiznesFinder
    logger.info("=== Fetching data from BiznesFinder ===")
    all_biznesfinder_results = scrape_biznesfinder(
        normalized_query,
        location,
        session=session,
        max_pages=args.max_pages,
        use_headless=args.use_headless,
        headless_wait=args.headless_wait
    )
    annotate_sources(all_biznesfinder_results, 'biznesfinder')

    # Fetch data from ZnanyLekarz (only relevant for massage/physio)
    if map_query_to_category(normalized_query) in ("masaz", "fizjoterapia"):
        logger.info("=== Fetching data from ZnanyLekarz ===")
        all_znanylekarz_results = scrape_znanylekarz(normalized_query, location, session=session, max_pages=args.max_pages)
        annotate_sources(all_znanylekarz_results, 'znanylekarz')

    # Fetch data from Fixly (best-effort, JS-rendered)
    logger.info("=== Fetching data from Fixly ===")
    all_fixly_results = scrape_fixly(
        normalized_query,
        location,
        session=session,
        max_pages=args.max_pages,
        use_headless=args.use_headless,
        headless_wait=args.headless_wait
    )
    annotate_sources(all_fixly_results, 'fixly')
    
    # Fetch data from Google Places (optional)
    if use_google_choice and API_KEY:
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
    logger.info("After deduplication, we have %s unique businesses.", len(all_results))
    
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
                logger.info("[%s/%s] Fetching emails for: %s", processed_websites, total_with_website, result.get('name', 'Unknown Name'))
                existing_emails = result.get('emails') or []
                emails = extract_emails_from_website(
                    website,
                    session=session,
                    cache_conn=cache_conn,
                    respect_robots=args.respect_robots,
                    use_headless=args.use_headless,
                    headless_wait=args.headless_wait
                )
                merged = list(dict.fromkeys([e for e in (existing_emails + emails) if e]))
                result['emails'] = merged
                # Delay to avoid overloading servers
                time.sleep(random.uniform(1.0, 2.0))
    
    # Save all data to Excel file
    if all_results:
        save_results(all_results, output_path, args.format, city=location, append_to_template=append_to_template)
    else:
        logger.info("No data to save.")

    if cache_conn is not None:
        cache_conn.close()

if __name__ == '__main__':
    main()
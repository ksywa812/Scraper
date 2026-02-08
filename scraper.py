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


def strip_accents(text):
    return ''.join(c for c in unicodedata.normalize('NFKD', text) if not unicodedata.combining(c))


def slugify(text):
    text = strip_accents(text or "")
    text = re.sub(r"[^a-zA-Z0-9\s-]", "", text)
    text = re.sub(r"\s+", "-", text.strip().lower())
    text = re.sub(r"-+", "-", text)
    return text


CATEGORY_KEYWORDS = {
    "masaz": [
        "masaz", "masaź", "masaż", "massage", "masazysta", "masażysta",
        "masaz relaksacyjny", "masaz klasyczny", "masaz leczniczy",
        "masaz sportowy", "masaz balijski", "masaz tajski", "masaz kobido",
        "masaz lomi", "masaz tkanek", "masaz goracymi kamieniami",
        "masaz aroma", "aromaterapia", "bodywork"
    ],
    "spa": ["spa", "day spa", "spa & wellness", "rytual spa", "rytuał spa", "salon spa"],
    "wellness": ["wellness", "odnowa biologiczna", "relaks", "relaksacja"],
    "joga": ["joga", "yoga", "hatha", "vinyasa", "ashtanga", "yin", "kundalini", "joga nidra"],
    "fizjoterapia": ["fizjoterapia", "rehabilitacja", "fizjo", "terapia manualna", "kinezyterapia"],
}

CATEGORY_PRIORITY = ["masaz", "spa", "wellness", "joga", "fizjoterapia"]


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
    category = map_query_to_category(query)
    city_slug = slugify(location)
    if not city_slug:
        return []

    candidates = []
    if category == "masaz":
        candidates.append(f"{OFERTEO_BASE}/masazysci/{city_slug}")
        candidates.append(f"{OFERTEO_BASE}/masaz/{city_slug}")
    elif category == "fizjoterapia":
        candidates.append(f"{OFERTEO_BASE}/fizjoterapeuci/{city_slug}")
        candidates.append(f"{OFERTEO_BASE}/fizjoterapia/{city_slug}")
    elif category == "joga":
        candidates.append(f"{OFERTEO_BASE}/szkoly-jogi/{city_slug}")
        candidates.append(f"{OFERTEO_BASE}/joga/{city_slug}")
    elif category in ("spa", "wellness"):
        candidates.append(f"{OFERTEO_BASE}/salony-spa/{city_slug}")
        candidates.append(f"{OFERTEO_BASE}/spa/{city_slug}")

    # Generic search fallback
    candidates.append(f"{OFERTEO_BASE}/firmy/{city_slug}?q={quote_plus(query)}")
    return candidates


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

    driver = None
    try:
        driver = uc.Chrome(options=options)
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
        return mapped

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


def get_booksy_city_slug(category_slug, location, session=None):
    session = session or create_session()
    location_lower = (location or "").strip().lower()
    location_slug = slugify(location_lower)
    if not location_slug:
        return None

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
            if location_lower in text or f"_{location_slug}" in href:
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


def parse_booksy_listings(html, category_slug):
    soup = BeautifulSoup(html, 'html.parser')
    entries = {}

    href_pattern = re.compile(rf"^/pl-pl/\d+_.+_{re.escape(category_slug)}_", re.IGNORECASE)

    for a in soup.find_all('a', href=True):
        href = a['href']
        if not href_pattern.match(href):
            continue

        full_url = href if href.startswith("http") else f"{BOOKSY_BASE}{href}"
        text = " ".join(a.stripped_strings)
        if not text:
            continue

        entry = entries.setdefault(full_url, {"name": "", "address": ""})

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


def scrape_fresha(query, location, session=None, max_pages=3):
    results = []
    session = session or create_session()

    category = map_query_to_category(query) or "spa"
    keyword_filter = fresha_category_keywords(category)
    business_type = fresha_business_type_for_query(query)
    location_slug = f"pl-{slugify(location)}"
    base_url = f"{FRESHA_BASE}/lp/en/bt/{business_type}/in/{location_slug}"

    seen = set()
    for page in range(1, max_pages + 1):
        url = base_url if page == 1 else f"{base_url}?page={page}"
        try:
            resp = session.get(url, headers={'User-Agent': get_random_user_agent()}, timeout=20)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, 'html.parser')

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
                if key in seen:
                    continue
                seen.add(key)
                page_added += 1
                results.append({
                    'name': name,
                    'formatted_address': address,
                    'formatted_phone_number': "",
                    'website': full_url,
                    'emails': []
                })

            logger.info("Fresha: extracted %s listings from %s", page_added, url)
            if page_added == 0:
                break
            time.sleep(random.uniform(1.0, 2.0))
        except Exception as e:
            logger.warning("Fresha: error fetching %s: %s", url, e)
            break

    return results


def scrape_moment(query, location, session=None, max_pages=3):
    logger.info("Moment.pl redirects to Booksy; reusing Booksy results.")
    return scrape_booksy(query, location, max_pages=max_pages, session=session)


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
                continue
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

        website = ""
        for a in soup.find_all('a', href=True):
            href = a['href']
            if href.startswith('http') and FIRMYNET_BASE not in href and 'st-firmy.net' not in href:
                website = href
                break

        address = ""
        meta_desc = soup.find('meta', attrs={'name': 'description'})
        if meta_desc and meta_desc.get('content'):
            address = meta_desc['content']

        return {
            'name': name,
            'formatted_address': address,
            'formatted_phone_number': phone,
            'website': website or profile_url,
            'emails': []
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
    for a in soup.find_all('a', href=True):
        href = a['href']
        if href.startswith('/'):
            href = urljoin(FIRMYNET_BASE, href)
        if not href.startswith(FIRMYNET_BASE):
            continue
        if ',' in href and href.endswith('.html'):
            profile_links.append(href)

    profile_links = list(dict.fromkeys(profile_links))
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


def extract_emails_from_website(url, session=None, cache_conn=None, respect_robots=False):
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
        for link in soup.find_all('a', href=True):
            href = link['href'].strip()
            # Skip mailto:, tel:, javascript: and anchor-only links
            if href.startswith(('mailto:', 'tel:', 'javascript:', '#', 'data:')):
                continue
            if any(keyword in href.lower() for keyword in ['kontakt', 'contact', 'about', 'o-nas']):
                # Add full URL if it's relative
                if href.startswith('/'):
                    contact_links.append(base_domain + href)
                elif href.startswith('http'):
                    contact_links.append(href)
                elif '/' in href or '.' in href: # Only append if it looks like a path
                    contact_links.append(base_domain + '/' + href)
        
        # Remove duplicate contact links
        contact_links = list(set(contact_links))
        
        # Visit found contact pages (limit to 2 to avoid excessive requests)
        for contact_url in contact_links[:2]:
            try:
                if not can_fetch_url(contact_url, respect_robots=respect_robots):
                    continue
                contact_response = session.get(contact_url, headers={'User-Agent': get_random_user_agent()}, timeout=10, allow_redirects=True)
                contact_response.raise_for_status()
                contact_emails = re.findall(email_pattern, contact_response.text)
                emails.extend(contact_emails)
            except Exception as e:
                logger.warning("Could not fetch contact page %s: %s", contact_url, e)
        
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

def merge_results(google_results, panorama_results, pkt_results):
    """Merges results from different sources and removes duplicates."""
    all_results = []
    all_results.extend(google_results)
    all_results.extend(panorama_results)
    all_results.extend(pkt_results)
    
    # Remove duplicates based on name and address
    unique_results = []
    unique_keys = set()
    
    for result in all_results:
        name = result.get('name', '').strip()
        normalized_name = normalize_name(name)
        address_parts = normalize_address(result.get('formatted_address', '')).split(',')
        simple_address = address_parts[0].strip() if address_parts else ""
        
        if not normalized_name:
            continue  # Skip entries without a name

        phone = normalize_phone(result.get('formatted_phone_number', ''))
        key = f"{normalized_name}|{simple_address}|{phone}"

        if key in unique_keys:
            continue

        unique_keys.add(key)
        unique_results.append(result)

    return unique_results

def save_to_excel(data, filename=OUTPUT_FILE):
    """Saves data to an Excel file."""
    # Check if file exists
    if os.path.exists(filename):
        confirm = input(f"File {filename} already exists. Overwrite? (y/n): ").lower()
        if confirm != 'y':
            new_name = input("Enter a new filename: ")
            if new_name:
                filename = new_name if new_name.endswith('.xlsx') else f"{new_name}.xlsx"
    
    try:
        wb = openpyxl.Workbook()
        ws = wb.active
        
        # Headers with three separate columns for email addresses
        ws.append(['Name', 'Address', 'Phone', 'Website', 'Email 1', 'Email 2', 'Email 3', 'Other Emails'])
        
        for item in data:
            # Get the list of emails
            emails = item.get('emails', [])
            
            # Prepare list of values to add to the row
            row_data = [
                item.get('name', ''),
                item.get('formatted_address', ''),
                item.get('formatted_phone_number', ''),
                item.get('website', '')
            ]
            
            # Add the first three emails in separate columns
            for i in range(3):
                if i < len(emails):
                    row_data.append(emails[i])
                else:
                    row_data.append('')  # Empty column if no email
            
            # Add remaining emails in the last column, comma-separated
            if len(emails) > 3:
                row_data.append(', '.join(emails[3:]))
            else:
                row_data.append('')
                
            ws.append(row_data)
            
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
            writer.writerow(['Name', 'Address', 'Phone', 'Website', 'Email 1', 'Email 2', 'Email 3', 'Other Emails'])
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
                writer.writerow(row_data)
        logger.info("Saved %s records to %s", len(data), filename)
    except Exception as e:
        logger.error("Error saving to CSV: %s", e)


def save_to_json(data, filename):
    if os.path.exists(filename):
        confirm = input(f"File {filename} already exists. Overwrite? (y/n): ").lower()
        if confirm != 'y':
            new_name = input("Enter a new filename: ")
            if new_name:
                filename = new_name if new_name.endswith('.json') else f"{new_name}.json"
    try:
        with open(filename, mode='w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info("Saved %s records to %s", len(data), filename)
    except Exception as e:
        logger.error("Error saving to JSON: %s", e)


def save_results(data, output_file, output_format):
    output_format = (output_format or 'xlsx').lower()
    if output_format == 'xlsx':
        save_to_excel(data, output_file)
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

    query = (args.query or input("Enter the industry (e.g., hairdresser): ")).strip()
    location = (args.location or input("Enter the city (e.g., Krakow): ")).strip()

    scrape_emails_choice = args.emails or (input("Do you want to extract emails from websites? (y/n): ").lower() == 'y')
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
    
    # Fetch data from PKT.pl (always)
    logger.info("=== Fetching data from PKT.pl ===")
    all_pkt_results = scrape_pkt_pl(normalized_query, location, max_pages=args.max_pages, session=session)

    # Fetch data from Booksy (optional)
    if use_booksy_choice:
        logger.info("=== Fetching data from Booksy ===")
        all_booksy_results = scrape_booksy(normalized_query, location, max_pages=args.max_pages, session=session)

    # Fetch data from SPAeden rankings (always for spa/wellness/massage)
    if map_query_to_category(normalized_query) in ("spa", "wellness", "masaz"):
        logger.info("=== Fetching data from SPAeden rankings ===")
        all_spaeden_results = scrape_spaeden_rankings(session=session)

    # Fetch data from Fresha (always)
    logger.info("=== Fetching data from Fresha ===")
    all_fresha_results = scrape_fresha(normalized_query, location, session=session, max_pages=args.max_pages)

    # Fetch data from Moment.pl (Booksy mirror)
    logger.info("=== Fetching data from Moment.pl ===")
    all_moment_results = scrape_moment(normalized_query, location, session=session, max_pages=args.max_pages)

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

    # Fetch data from Oferteo
    logger.info("=== Fetching data from Oferteo ===")
    all_oferteo_results = scrape_oferteo(normalized_query, location, session=session, max_pages=args.max_pages)

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

    # Fetch data from ZnanyLekarz (only relevant for massage/physio)
    if map_query_to_category(normalized_query) in ("masaz", "fizjoterapia"):
        logger.info("=== Fetching data from ZnanyLekarz ===")
        all_znanylekarz_results = scrape_znanylekarz(normalized_query, location, session=session, max_pages=args.max_pages)

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
    
    # Merge all results
    all_results = merge_results(all_google_details, all_panorama_results, all_pkt_results)
    if all_booksy_results:
        all_results = merge_results(all_results, all_booksy_results, [])
    if all_spaeden_results:
        all_results = merge_results(all_results, all_spaeden_results, [])
    if all_fresha_results:
        all_results = merge_results(all_results, all_fresha_results, [])
    if all_moment_results:
        all_results = merge_results(all_results, all_moment_results, [])
    if all_cylex_results:
        all_results = merge_results(all_results, all_cylex_results, [])
    if all_oferteo_results:
        all_results = merge_results(all_results, all_oferteo_results, [])
    if all_firmynet_results:
        all_results = merge_results(all_results, all_firmynet_results, [])
    if all_biznesfinder_results:
        all_results = merge_results(all_results, all_biznesfinder_results, [])
    if all_znanylekarz_results:
        all_results = merge_results(all_results, all_znanylekarz_results, [])
    if all_fixly_results:
        all_results = merge_results(all_results, all_fixly_results, [])
    logger.info("After deduplication, we have %s unique businesses.", len(all_results))
    
    # If user wants emails, fetch them for each business with a website URL
    if scrape_emails_choice:
        logger.info("=== Fetching emails from websites ===")
        total_with_website = sum(1 for result in all_results if result.get('website'))
        logger.info("Found %s businesses with website addresses.", total_with_website)
        
        processed_websites = 0
        for result in all_results: # No need for index 'i' if not used
            website = result.get('website')
            if website:
                processed_websites += 1
                logger.info("[%s/%s] Fetching emails for: %s", processed_websites, total_with_website, result.get('name', 'Unknown Name'))
                emails = extract_emails_from_website(
                    website,
                    session=session,
                    cache_conn=cache_conn,
                    respect_robots=args.respect_robots
                )
                result['emails'] = emails
                # Delay to avoid overloading servers
                time.sleep(random.uniform(1.0, 2.0))
    
    # Save all data to Excel file
    if all_results:
        save_results(all_results, args.output, args.format)
    else:
        logger.info("No data to save.")

    if cache_conn is not None:
        cache_conn.close()

if __name__ == '__main__':
    main()
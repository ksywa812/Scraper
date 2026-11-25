# Handoff: Integracja Web-Crunchy-Scrap CSV → Attio CRM (Branże SoundYou, Deduplikacja, Social Media & Auto-Nazwa)

**Data:** 2026-11-25  
**Zakres:** Docelowy workflow `Scraper → CSV → Attio` ze ścisłą deduplikacją po stronie skryptu (istniejące leady w Attio są pomijane), automatycznym zbieraniem linków Facebook/Instagram, samodzielnym ustalaniem nazwy firmy z dostępnych danych oraz pełną zgodnością 1:1 z 12 branżami SoundYou (`soundyou_industry`).

---

## 1. Stan docelowy w Attio (zweryfikowany na żywo przez MCP)

- **Workspace:** SoundYou (`soundyou`)
- **Atrybut źródłowy na `Companies`:** `Branża SoundYou` (`api_slug: soundyou_industry`, typ `select`, `is_multiselect: true`, `attribute_id: 7972058f-4037-4ebf-b61e-c4c527a01c0d`)
- **Natywne pola social media na `Companies`:** `facebook`, `instagram` (uzupełniane przez `attio_sync.py`)
- **Dziedziczenie na `People`:** widoki `People` (`Kontakty Resend`, `Kontakty i firmy`) wyświetlają `Company → Branża SoundYou` automatycznie — brak duplikowania pola branży na `People`.
- **12 kanonicznych opcji `soundyou_industry`:**
  1. `SPA & Wellness`
  2. `Kobido & Masaż`
  3. `Beauty / Salon Urody`
  4. `Hotele i Pensjonaty`
  5. `Restauracje`
  6. `Kawiarnie`
  7. `Fitness / Siłownia`
  8. `Joga / Pilates / Medytacja`
  9. `Salon Fryzjerski / Barber`
  10. `Poczekalnia / Gabinet`
  11. `Sklep / Retail`
  12. `Lokal Usługowy / Inne`
- **8 dedykowanych widoków branżowych w `Companies`:**
  - `Branża — SPA & Wellness`
  - `Branża — Kobido & Masaż`
  - `Branża — Beauty / Salon Urody`
  - `Branża — Hotele i Pensjonaty`
  - `Branża — Restauracje i Kawiarnie`
  - `Branża — Fitness / Joga / Pilates`
  - `Branża — Fryzjer / Barber`
  - `Branża — Gabinety / Sklepy / Inne`
- **Widoki ogólne i Resend:** `All Companies`, `Firmy SoundYou - data dodania`, oraz 6 widoków kampanijnych Resend opartych o zsynchronizowane pola podsumowujące (`resend_last_sent_at`, `resend_last_campaign`, `resend_last_delivery_status`, `resend_any_opened`, `resend_any_clicked`, `resend_sends_count`).

---

## 2. Docelowy workflow (`Scraper → CSV → Attio`)

1. **Etap 1 — Scraping do pliku `.csv` (`scraper.py`):**
   - Scraper zbiera leady z wybranych źródeł, pobiera adresy e-mail, linki **Facebook** / **Instagram** (ze stron WWW `<a href>`, JSON-LD `sameAs` i profili Booksy `__NUXT__`, z odfiltrowaniem kont katalogowych typu `booksypl`, `/sharer`, `/reel`, `/p/`), oraz tytuł strony (`og:site_name` / JSON-LD `name` / `<title>`).
   - Automatycznie ustala nazwę firmy (`resolve_company_name`), jeśli nazwa ze scrapingu jest pusta, generyczna lub techniczna (np. `Strona główna`, `Home`, `Brak`, `R. · hash`, surowy URL/e-mail, samo słowo `SPA` / `Salon` / `Gabinet`).
   - Klasyfikuje lead do kanonicznych branż SoundYou (`classify_soundyou_industries`) i dopisuje do `data/Raport/SoundYouLeads.csv` (z kolumnami `Facebook` i `Instagram` oraz deduplikacją w `append_to_csv_master` po znormalizowanym telefonie bez `+48`, domenie firmowej, URL social media, e-mailu i `(nazwa, miasto)`).
2. **Etap 2 — Ścisła deduplikacja w skrypcie i uzupełnianie Attio (`attio_sync.py`):**
   - **Deduplikacja lokalna batcha CSV:** łączy duplikaty wewnątrz CSV po telefonie (`+48XXXXXXXXX`), e-mailu, URL Facebook/Instagram, domenie firmowej, `(nazwa, miasto)` oraz `(nazwa, kod pocztowy)`.
   - **Deduplikacja względem istniejących rekordów w Attio (6 sygnałów):**
     1. `companies_by_domain` (domena firmowa),
     2. `companies_by_social` (URL `facebook` / `instagram` z pól natywnych oraz z opisu),
     3. `companies_by_email` i `people_by_email` (e-maile z `People` i opisów `Companies`),
     4. `companies_by_phone` i `people_by_phone` (telefony `+48XXXXXXXXX` z `People` i opisów `Companies`),
     5. `companies_by_name_city` i `companies_by_name_postcode` (`XX-XXX` z `primary_location` i `line_1`),
     6. `companies_by_unique_name` (unikalna nazwa firmy w Attio o długości ≥ 5 znaków).
   - **Zasada „istnieje = pomiń” (`enrich_existing=False` domyślnie):** jeśli lead z wysyłanego batcha istnieje już w Attio po którymkolwiek z powyższych sygnałów, **nie jest dodawany ani modyfikowany (`skipped_existing_companies`, 0 zapisów `POST`/`PATCH`)**. Dla nowo tworzonych firm indeks w pamięci jest aktualizowany w locie, aby uniknąć duplikatów w obrębie tego samego batcha. *(Opcjonalna flaga `--enrich-existing` pozwala w razie potrzeby na wzbogacanie istniejących firm o brakujące branże/domeny.)*
3. **Resend pozostaje osobnym, świadomym krokiem:**
   - Skrypty `run_pipeline.bat`, `Scraper/run_scraper.bat` i `run_agglomeration.bat` wykonują wyłącznie `Scraper → CSV → Attio` (nie wysyłają automatycznie do Resend).
   - Ręczny eksport do Resend Audiences pozostaje dostępny w osobnym `..\send_to_resend.bat`.

---

## 3. Automatyczne ustalanie nazwy firmy (`resolve_company_name`)

Gdy nazwa ze scrapingu jest niejasna (`is_unclear_company_name`), skrypt sam wybiera najlepszą nazwę w kolejności priorytetów:
1. **`site_title`** ze strony WWW (`og:site_name` → JSON-LD `Organization`/`LocalBusiness` `name` → pierwszy niegeneryczny człon `<title>`).
2. **Nazwa profilu Facebook / Instagram** — rozdzielenie `CamelCase` i separatorów `._-`, usunięcie numerycznego ID stron FB (np. `facebook.com/SalonUrodyMagnolia-102938475610293` → `Salon Urody Magnolia`, `instagram.com/kobido.studio.krakow` → `Kobido Studio Krakow`).
3. **Domena strony WWW** — usunięcie `www.` i końcówek `.pl`/`.com.pl`/`.eu` + rozklejanie słów branżowych (np. `enklawaspa.com` → `Enklawa SPA`, `safari-wellness.pl` → `Safari Wellness`).
4. **Adres e-mail** — najpierw własna domena firmowa (`recepcja@hotelmarta.com.pl` → `Hotel Marta`), a dla darmowych skrzynek (`@gmail.com`, `@wp.pl`, `@o2.pl` itd.) część przed `@` po oczyszczeniu prefiksów typu `kontakt`/`biuro`/`recepcja` i cyfr (`yin.studiospa@gmail.com` → `Yin Studio SPA`, `ladna.ja2024@gmail.com` → `Ladna Ja`).
5. **Fallback:** dla ogólnej nazwy kategorii z miastem — np. `Gabinet masażu (Środa Śląska)`.

---

## 4. Klucz mapowania EN ↔ PL (`INDUSTRY_EN_PL_MAP`)

Zarówno `attio_sync.py`, jak i `scraper.py` obsługują zapytania i etykiety po angielsku i po polsku, mapując je 1:1 do 12 opcji w Attio (oraz wzbogacając multiselect na podstawie nazwy firmy, domeny i linków social media):

| Opcja Attio (`soundyou_industry`) | Przykładowe klucze EN | Przykładowe klucze PL |
|---|---|---|
| `SPA & Wellness` | `spa`, `day spa`, `wellness`, `health & wellness`, `sauna`, `float`, `thermal bath` | `spa`, `wellness`, `odnowa biologiczna`, `termy`, `saunarium` |
| `Kobido & Masaż` | `massage`, `massage parlor`, `thai massage`, `balinese massage`, `bodywork` | `kobido`, `masaż`, `masaz`, `masażysta` |
| `Beauty / Salon Urody` | `beauty`, `beauty salon`, `cosmetics`, `cosmetology`, `aesthetic medicine`, `nail salon`, `brow bar`, `lash` | `uroda`, `salon urody`, `salon kosmetyczny`, `kosmetyka`, `kosmetologia`, `medycyna estetyczna` |
| `Hotele i Pensjonaty` | `hotel`, `hotels`, `resort`, `guesthouse`, `b&b`, `aparthotel`, `hostel`, `hospitality` | `hotel`, `hotele`, `pensjonat`, `pałac`, `dwór`, `willa` |
| `Restauracje` | `restaurant`, `restaurants`, `bistro`, `dining`, `trattoria`, `pizzeria`, `sushi`, `steakhouse`, `bar`, `pub` | `restauracja`, `restauracje`, `gastronomia`, `karczma` |
| `Kawiarnie` | `cafe`, `coffee`, `coffee shop`, `espresso bar`, `bakery`, `patisserie`, `tea house` | `kawiarnia`, `kawiarnie`, `cukiernia`, `palarnia kawy` |
| `Fitness / Siłownia` | `fitness`, `gym`, `fitness club`, `health club`, `sports & fitness`, `crossfit`, `personal training` | `fitness`, `siłownia`, `silownia`, `klub fitness` |
| `Joga / Pilates / Medytacja` | `yoga`, `yoga studio`, `pilates`, `meditation`, `mindfulness` | `joga`, `pilates`, `medytacja` |
| `Salon Fryzjerski / Barber` | `hair`, `hair salon`, `hairdresser`, `barber`, `barbershop` | `fryzjer`, `fryzjerstwo`, `salon fryzjerski`, `barber` |
| `Poczekalnia / Gabinet` | `physiotherapy`, `physical therapy`, `rehab`, `clinic`, `dental`, `dentist`, `waiting room`, `osteopathy` | `fizjoterapia`, `rehabilitacja`, `gabinet`, `klinika`, `przychodnia`, `stomatolog`, `poczekalnia` |
| `Sklep / Retail` | `shop`, `store`, `retail`, `boutique`, `showroom`, `concept store` | `sklep`, `butik`, `sklep / retail` |
| `Lokal Usługowy / Inne` | `other`, `service`, `services`, `local business` | `inne`, `lokal usługowy` |

---

## 5. Zakres plików zmodyfikowanych

### W repozytorium git (`Scraper/`)
- **`attio_sync.py`**: klient Attio REST API v2, `ensure_soundyou_schema()`, `extract_social_links()`, `resolve_company_name()`, wzmocniona deduplikacja CSV i 6-sygnałowa deduplikacja względem Attio (domyślny tryb ścisłego pomijania istniejących leadów + opcjonalny `--enrich-existing`), zapis pól `facebook` i `instagram` na `Companies`.
- **`scraper.py`**: pełne `CATEGORY_KEYWORDS` / `CATEGORY_PRIORITY` (dodane `fitness`, `sklep`, `pilates`, `medytacja`, `poczekalnia`, `stomatolog`, `klinika`, aliasy EN), ekstrakcja `facebook` / `instagram` / `site_title` ze stron WWW i Booksy, automatyczne `resolve_company_name`, kolumny `Facebook` i `Instagram` w `SoundYouLeads.csv` oraz wzmocniona deduplikacja w `append_to_csv_master`.
- **`run_scraper.bat`**: zaktualizowane menu 11 branż SoundYou + wpis ręczny, przepływ `Scraper → CSV → Attio` (bez automatycznego Resend).
- **`tests/test_attio_sync.py`**: 17 testów jednostkowych (12 opcji branż, EN ↔ PL, ścisłe pomijanie istniejących leadów w Attio po domenie/telefonie/e-mailu/social/batchu, `--enrich-existing`, `--dry-run`, ekstrakcja FB/IG, automatyczne ustalanie nazwy firmy).
- **`.env.example`**: dokumentacja `ATTIO_API_KEY`.

### Poza repozytorium git (`c:\IdeaMusicPro\Web-Crunchy-Scrap\`)
- **`run_pipeline.bat`**: `Scraper → CSV → Attio` (istniejące leady pomijane).
- **`run_agglomeration.bat`**: scraping wielu miast → `CSV → Attio`.
- **`send_to_attio.bat`**: ręczna synchronizacja dowolnego pliku `.csv` do Attio (wspiera `--dry-run`).

---

## 6. Weryfikacja (stan przed pierwszą wysyłką live)

- **Pytest (`tests/test_attio_sync.py`, `tests/test_pure_logic.py`, `tests/test_merge_dedup.py`):** `135 passed in 0.86s`.
- **Offline dry-run (`python attio_sync.py --offline-dry-run` na `data/Raport/SoundYouLeads.csv`):**
  - `339` wierszy źródłowych → `36` scalonych duplikatów w CSV, `2` odrzucone (junk) → **`301` unikalnych firm** (`189` z e-mailem).
  - Rozkład multiselect: `SPA & Wellness: 301`, `Kobido & Masaż: 79`, `Beauty / Salon Urody: 45`, `Hotele i Pensjonaty: 28`, `Poczekalnia / Gabinet: 11`, `Fitness / Siłownia: 7`, `Joga / Pilates / Medytacja: 4`, `Salon Fryzjerski / Barber: 4`, `Sklep / Retail: 2`.
- **Stan wysyłki do Attio:** `ATTIO_API_KEY` jest już dodany w lokalnym `Scraper/.env`, **nic nie zostało jeszcze wysłane do Attio** (na prośbę użytkownika).

---

## 7. Jak uruchomić, gdy będziesz gotowy

1. Bezpieczny podgląd względem bieżącej bazy Attio (tylko odczyt, 0 zapisów):
   ```powershell
   .\.venv\Scripts\python.exe attio_sync.py --dry-run
   ```
2. Właściwe uzupełnienie brakujących leadów z CSV w Attio (istniejące zostaną pominięte):
   ```powershell
   .\.venv\Scripts\python.exe attio_sync.py
   ```
3. Nowy scraping z automatycznym zapisem do CSV i uzupełnieniem Attio:
   - kliknij `Scraper\run_scraper.bat`, `..\run_pipeline.bat` lub `..\run_agglomeration.bat`, albo uruchom:
   ```powershell
   .\.venv\Scripts\python.exe scraper.py --query kawiarnia --location Wroclaw --emails --use-google --sync-attio
   ```

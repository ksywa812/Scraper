# Handoff: Integracja Web-Crunchy-Scrap CSV → Attio CRM (Branże SoundYou, Deduplikacja, Social Media, Auto-Nazwa & Quality Gate)

**Data:** 2026-11-25  
**Zakres:** Kompletny workflow `Scraper → Quality Gate & CSV → Attio CRM` obejmujący:
- 12 kanonicznych branż SoundYou (`soundyou_industry`, multiselect, mapowanie EN ↔ PL),
- ścisłą deduplikację po stronie skryptu (6 sygnałów, istniejące rekordy w Attio są pomijane),
- automatyczne zbieranie linków **Facebook** i **Instagram** oraz osobne kolumny w Attio,
- samodzielne rozpoznawanie nazwy firmy (`resolve_company_name`) przy braku/niejasnej nazwie,
- **Krok A:** pipeline sprzedażowy **`Status leada`** (`lead_status` + widok Kanban), **`Partia importu`** (`lead_batch`) i ustrukturyzowane raporty JSON z każdego przebiegu (`logs/attio_sync_*.json`),
- **Krok B:** bramę jakości e-maili (literówki TLD np. `.coml`/`.pll`, domeny jednorazowe, weryfikacja MX/disposable w **UserCheck API** ze współdzielonym cache oraz tarcza **DNC** z Attio) i filtr **anty-ICP** (odrzucanie Auto SPA, pralni, agencji IT/SEO/hostingów),
- **Krok 4 (Krok D):** opcjonalny tryb **`--fill-missing`** (*fill-blanks-only*) — bezpieczne uzupełnianie wyłącznie pustych pól (brakującej strony WWW, Instagrama, Facebooka, branży, lokalizacji, telefonu oraz nowo odkrytych adresów e-mail jako `People`) na firmach już istniejących w Attio, **bez nadpisywania jakichkolwiek istniejących wartości**.

---

## 1. Stan docelowy w Attio (zweryfikowany na żywo przez MCP)

- **Workspace:** SoundYou (`soundyou`)
- **Atrybuty na `Companies`:**
  - `Branża SoundYou` (`soundyou_industry`, typ `select`, `is_multiselect: true`, 12 opcji)
  - `Status leada` (`lead_status`, typ `status`, opcje: `Nowy` → `Gotowy do kampanii` → `Wysłano kampanię` → `Zainteresowany` → `Rozmowa / Demo` → `Trial SoundYou` → `Klient` / `Odrzucony / DNC`)
  - `Partia importu` (`lead_batch`, typ `text`, np. `2026-11-25 | kawiarnia-wroclaw`)
  - Osobne kolumny kontaktowe/social we wszystkich 18 widokach tabelarycznych (`Companies` i `People`): **Nazwa firmy**, **Strona WWW** (`domains`), **Instagram** (`instagram`), **Facebook** (`facebook`), **Branża SoundYou** (`soundyou_industry`).
- **Widok Kanban:** `Pipeline — Status leada (Kanban)` (grupowany po `lead_status`).
- **8 dedykowanych widoków branżowych w `Companies`:**
  1. `Branża — SPA & Wellness`
  2. `Branża — Kobido & Masaż`
  3. `Branża — Beauty / Salon Urody`
  4. `Branża — Hotele i Pensjonaty`
  5. `Branża — Restauracje i Kawiarnie`
  6. `Branża — Fitness / Joga / Pilates`
  7. `Branża — Fryzjer / Barber`
  8. `Branża — Gabinety / Sklepy / Inne`
- **6 widoków kampanijnych Resend (`People` + `Companies`):** kliknięcia (follow-up), otwarcia bez kliknięcia, odbicia (`bounced` / `suppressed`).

---

## 2. Docelowy workflow (`Scraper → Quality Gate & CSV → Attio`)

1. **Etap 1 — Scraping, wzbogacenie i filtr jakości do `.csv` (`scraper.py`):**
   - Zbiera dane kontaktowe, adresy e-mail, linki **Facebook** i **Instagram** (ze stron WWW `<a href>`, JSON-LD `sameAs` i profili Booksy `__NUXT__`, z wykluczeniem kont katalogów) oraz tytuł marki (`og:site_name` / JSON-LD `name` / `<title>`).
   - Automatycznie ustala nazwę firmy (`resolve_company_name`), jeśli nazwa ze scrapingu jest pusta lub niejasna.
   - Odrzuca błędne e-maile (w tym literówki TLD typu `.coml`, `.pll`, obrazy i domeny jednorazowe) oraz firmy spoza ICP (`is_non_icp_business`).
   - Zapisuje unikalne rekordy do `data/Raport/SoundYouLeads.csv` (z kolumnami `Facebook` i `Instagram`).

2. **Etap 2 — Bramka jakości, ścisła deduplikacja i zapis do Attio (`attio_sync.py`):**
   - **Lokalna deduplikacja CSV + filtr anty-ICP:** odrzuca rekordy spoza ICP (`skipped_non_icp`, wyłączalne przez `--include-non-icp`), scala wewnętrzne duplikaty i loguje wszystkie automatycznie wywnioskowane nazwy (`audit.inferred_names`).
   - **6-sygnałowa deduplikacja względem Attio:** sprawdza istniejące rekordy w Attio po:
     1. domenie firmowej (`companies_by_domain`),
     2. linku `facebook` / `instagram` (`companies_by_social`),
     3. adresie e-mail (`companies_by_email`, `people_by_email`),
     4. numerze telefonu `+48XXXXXXXXX` (`companies_by_phone`, `people_by_phone`),
     5. `(nazwa, miasto)` oraz `(nazwa, kod pocztowy)` (`XX-XXX`),
     6. unikalnej nazwie firmy w Attio (≥ 5 znaków).
   - **Zasada „istnieje = pomiń” (`fill_missing=False`, `enrich_existing=False` domyślnie):** rekordy istniejące w Attio są całkowicie pomijane (`0` zapisów `POST`/`PATCH`).
   - **Opcjonalny tryb `--fill-missing` (*fill-blanks-only*):**
     - gdy firma już jest w Attio (np. wcześniej dodana z samego telefonu z Google Maps), a kolejny scraping znajdzie jej stronę WWW, Instagrama, Facebooka lub adres e-mail, flaga `--fill-missing` (lub `--attio-fill-missing` w `scraper.py`) dopisze **wyłącznie pola, które w Attio są puste**:
       - na `Companies`: puste `domains`, `instagram`, `facebook`, `soundyou_industry`, `primary_location` (nigdy nie nadpisuje nazwy, opisu, statusu, partii ani ustawionych już wartości!),
       - na `People`: tworzy nowe rekordy `People` dla nowo odkrytych adresów e-mail (po przejściu przez bramę DNC + UserCheck) oraz dopisuje brakujące powiązanie `company` lub pusty `phone_numbers` na istniejącym kontakcie,
       - pełna lista uzupełnionych pól trafia do raportu przebiegu pod kluczem `audit.filled_missing_companies`.
   - **Brama dostarczalności e-maili dla nowych leadów:**
     - blokuje adresy z listy **DNC** w Attio (`resend_unsubscribed = true` lub `bounced`/`suppressed`),
     - weryfikuje nowe adresy przez **UserCheck API** (`mx`, `disposable`, `blocklisted`, `spam`) ze współdzielonym z `resend_sender.py` cache w `data/Raport/email_verify_cache.json` (odpytywane są wyłącznie nowe leady nieistniejące jeszcze w Attio).
   - ** Automatyczne oznaczanie leada i raport przebiegu:**
     - każdy nowy rekord `Companies` otrzymuje `lead_status = "Nowy"` oraz `lead_batch` (np. `2026-11-25 | kawiarnia-wroclaw` lub `YYYY-MM-DD | SoundYouLeads`),
     - po każdym przebiegu zapisywany jest pełny raport JSON w `logs/attio_sync_YYYYMMDD_HHMMSS.json` (statystyki, wywnioskowane nazwy, odrzucone e-maile i odfiltrowane firmy spoza ICP).

---

## 3. Automatyczne ustalanie nazwy firmy (`resolve_company_name`)

Gdy nazwa ze scrapingu jest niejasna (`is_unclear_company_name`), skrypt sam wybiera najlepszą nazwę w kolejności priorytetów:
1. **`site_title`** ze strony WWW (`og:site_name` → JSON-LD `Organization`/`LocalBusiness` `name` → pierwszy niegeneryczny człon `<title>`).
2. **Nazwa profilu Facebook / Instagram** (np. `facebook.com/SalonUrodyMagnolia-102938475610293` → `Salon Urody Magnolia`, `instagram.com/kobido.studio.krakow` → `Kobido Studio Krakow`).
3. **Domena strony WWW** (np. `enklawaspa.com` → `Enklawa SPA`, `safari-wellness.pl` → `Safari Wellness`).
4. **Adres e-mail** — własna domena firmowa (`recepcja@hotelmarta.com.pl` → `Hotel Marta`) lub część przed `@` dla darmowych skrzynek (`yin.studiospa@gmail.com` → `Yin Studio SPA`).
5. **Fallback:** `Kategoria (Miasto)`.

---

## 4. Zakres zmodyfikowanych plików w repozytorium (`Scraper/`)

- **`attio_sync.py`**: pełny pipeline CSV → Attio (`soundyou_industry`, `lead_status`, `lead_batch`, `facebook`, `instagram`, `resolve_company_name`, `validate_email_syntax`, `verify_email_usercheck`, `is_non_icp_business`, tarcza DNC, 6-sygnałowa deduplikacja i raporty JSON w `logs/`).
- **`scraper.py`**: pełne `CATEGORY_KEYWORDS` / `CATEGORY_PRIORITY`, ekstrakcja social media i `site_title`, `resolve_company_name`, filtr składni e-maili i anty-ICP w `append_to_csv_master`, przekazywanie `lead_batch` przy `--sync-attio`.
- **`resend_sender.py`**: odrzucanie literówek TLD (np. `.coml`, `.con`, `.pll`) w `is_valid_email`.
- **`run_scraper.bat`** (oraz `..\run_pipeline.bat`, `..\run_agglomeration.bat`, `..\send_to_attio.bat`): menu 11 branż SoundYou + wpis ręczny, przepływ `Scraper → CSV → Attio`.
- **`tests/test_attio_sync.py`**: 21 testów jednostkowych obejmujących wszystkie reguły branż, deduplikacji, social media, auto-nazw, `lead_status`/`lead_batch`, UserCheck cache, DNC, filtr anty-ICP i raporty JSON.

---

## 5. Jak uruchomić

1. Podgląd offline (bez sieci, z zapisem raportu audytowego w `logs/`):
   ```powershell
   .\.venv\Scripts\python.exe attio_sync.py --offline-dry-run
   ```
2. Podgląd względem żywej bazy Attio (tylko odczyt, 0 zapisów):
   ```powershell
   .\.venv\Scripts\python.exe attio_sync.py --dry-run
   ```
3. Uzupełnienie wyłącznie nowych leadów w Attio (istniejące zostaną pominięte):
   ```powershell
   .\.venv\Scripts\python.exe attio_sync.py --batch "2026-11-25 | SoundYouLeads"
   ```
4. Tryb *fill-blanks-only* (dodaj nowe leady + uzupełnij wyłącznie puste pola WWW/IG/FB/nowe e-maile na istniejących firmach, bez nadpisywania):
   ```powershell
   .\.venv\Scripts\python.exe attio_sync.py --fill-missing
   ```
   *(lub podgląd bez zapisu: `python attio_sync.py --fill-missing --dry-run`)*
5. Pełny scraping nowej branży/miasta z zapisem do CSV i Attio:
   ```powershell
   .\.venv\Scripts\python.exe scraper.py --query kawiarnia --location Wroclaw --emails --use-google --sync-attio
   ```
   *(z uzupełnianiem pustych pól na istniejących firmach: dodaj `--attio-fill-missing`)*

# Handoff: Integracja Web-Crunchy-Scrap CSV → Attio CRM (Branże SoundYou + Deduplikacja)

**Data:** 2026-11-25  
**Zakres:** Ujednolicenie taksonomii branżowej SoundYou (klucz EN ↔ PL, 12 opcji multiselect) między scraperem CSV/Excel a Attio CRM oraz wdrożenie bezpośredniej, zdeduplikowanej synchronizacji `Companies` + `People`.

---

## 1. Stan docelowy w Attio (zweryfikowany na żywo przez MCP)

- **Workspace:** SoundYou (`soundyou`)
- **Atrybut źródłowy na `Companies`:** `Branża SoundYou` (`api_slug: soundyou_industry`, typ `select`, `is_multiselect: true`, `attribute_id: 7972058f-4037-4ebf-b61e-c4c527a01c0d`)
- **Dziedziczenie na `People`:** widoki `People` (`Kontakty Resend`, `Kontakty i firmy`) wyświetlają `Company → Branża SoundYou` automatycznie — nie ma potrzeby duplikowania pola branży na `People`.
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
- **Widoki ogólne:** `All Companies`, `Firmy SoundYou - data dodania`, oraz 6 widoków kampanijnych Resend (`resend_last_sent_at`, `resend_last_campaign`, `resend_last_delivery_status`, `resend_any_opened`, `resend_any_clicked`, `resend_sends_count`).

---

## 2. Klucz mapowania EN ↔ PL (`INDUSTRY_EN_PL_MAP`)

Zarówno `attio_sync.py`, jak i `scraper.py` obsługują zapytania i etykiety po angielsku i po polsku, mapując je 1:1 do 12 opcji w Attio (oraz wzbogacając multiselect na podstawie nazwy firmy i domeny):

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

Rekordy wielobranżowe (np. `hotel-spa`, `spa hotel`, `massage spa`) otrzymują wszystkie pasujące tagi multiselect, więc pojawiają się jednocześnie w każdym odpowiednim widoku Attio oraz w odpowiadających arkuszach Excela.

---

## 3. Wprowadzone zmiany

### W repozytorium (`Scraper/`)
- **`attio_sync.py`** *(nowy)*:
  - Pełny klient Attio REST API v2 z obsługą retry/backoff (`429`, `5xx`).
  - `ensure_soundyou_schema()` — idempotentne sprawdzanie/tworzenie `soundyou_industry` i wszystkich 12 opcji + weryfikacja 8 widoków branżowych.
  - `load_and_dedup_csv()` — czyszczenie nazw (usunięcie emoji i śmieci), normalizacja telefonów (`+48XXXXXXXXX`), filtrowanie domen/maili katalogowych (Booksy, Fresha, social media), lokalne scalanie duplikatów (sumowanie branż, maili, telefonów, domen).
  - `sync_leads_to_attio()` — deduplikacja względem Attio (`Companies` po domenach, `(name, city)` i e-mailach; `People` po `email_addresses`). Istniejące firmy są bezpiecznie wzbogacane o brakujące tagi `soundyou_industry` i unikalne domeny (bez nadpisywania opisów ani pól Resend). Dla każdego e-maila tworzony jest rekord `People` powiązany z firmą (`company`) lub podpinany jest istniejący niepowiązany kontakt.
- **`scraper.py`** *(zmodyfikowany)*:
  - Dodana kategoria `kawiarnia` w `CATEGORY_KEYWORDS` / `CATEGORY_PRIORITY`.
  - Zaktualizowany kreator interaktywny (`known_industries` + etykiety PL/EN) i podział arkuszy Excel (`_rebuild_branza_sheets` z obsługą multiselect i nazw arkuszy bez znaku `/`).
  - Automatyczna klasyfikacja nowych leadów przez `classify_soundyou_industries` przed zapisem do `SoundYouLeads.csv` / `.xlsx`.
  - Nowe flagi CLI: `--sync-attio` oraz `--attio-dry-run`.
- **`.env.example`** *(zmodyfikowany)*: dodano `ATTIO_API_KEY=your_attio_api_key_here`.
- **`tests/test_attio_sync.py`** *(nowy)*: testy klasyfikacji 12 opcji, klucza EN ↔ PL, deduplikacji CSV, idempotentności schematu, synchronizacji `Companies` + `People` oraz trybu `--dry-run`.

### Poza repozytorium git (katalog nadrzędny `c:\IdeaMusicPro\Web-Crunchy-Scrap\`)
- **`run_pipeline.bat`** *(zmodyfikowany)*: dodany krok `[2/4] python attio_sync.py --csv %MASTER%` pomiędzy scraperem a Resend.
- **`send_to_attio.bat`** *(nowy)*: skrót do ręcznej synchronizacji dowolnego CSV z Attio.

---

## 4. Weryfikacja

- **Pytest (`tests/test_attio_sync.py`, `tests/test_pure_logic.py`, `tests/test_merge_dedup.py`):** `128 passed in 0.98s`.
- **Offline dry-run na `data/Raport/SoundYouLeads.csv`:**
  - `339` wierszy → `2` odrzucone (junk), `19` scalonych duplikatów → **`318` unikalnych firm** (`199` z e-mailem).
  - Rozkład multiselect: `SPA & Wellness: 318`, `Kobido & Masaż: 83`, `Beauty / Salon Urody: 46`, `Hotele i Pensjonaty: 29`, `Poczekalnia / Gabinet: 12`, `Fitness / Siłownia: 9`, `Joga / Pilates / Medytacja: 4`, `Salon Fryzjerski / Barber: 4`, `Sklep / Retail: 2`.

---

## 5. Jak uruchomić na żywo (Next Steps)

1. Ustaw `ATTIO_API_KEY` w lokalnym pliku `Scraper/.env` (plik `.env` jest ignorowany przez git).
2. Zweryfikuj podgląd bez zapisu względem bieżącej bazy Attio:
   ```powershell
   .\.venv\Scripts\python.exe attio_sync.py --dry-run
   ```
3. Uruchom synchronizację docelową:
   ```powershell
   .\.venv\Scripts\python.exe attio_sync.py
   ```
4. Przy kolejnych przebiegach scrapera użyj flagi `--sync-attio` lub uruchom `..\run_pipeline.bat` / `..\send_to_attio.bat`:
   ```powershell
   .\.venv\Scripts\python.exe scraper.py --query kawiarnia --location Wroclaw --emails --use-google --sync-attio
   ```

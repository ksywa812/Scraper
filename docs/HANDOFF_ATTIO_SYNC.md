# Handoff: Kompletny Workflow Lead-Gen & CRM SoundYou (`Scraper ↔ Attio CRM ↔ Resend ↔ Supabase`)

**Data aktualizacji:** 2026-11-26  
**Zakres:** Domknięty, dwukierunkowy ekosystem pozyskiwania leadów, wysyłek kampanii i synchronizacji konwersji produktowej SoundYou:
- **12 kanonicznych branż SoundYou** (`soundyou_industry`, multiselect, mapowanie EN ↔ PL + pełna zgodność z dropdownem aplikacji `WEBPlayerPV`),
- **Ścisła 6-sygnałowa deduplikacja** po stronie skryptu (domyślnie „istnieje = pomiń”),
- **Automatyczne zbieranie Facebook & Instagram** oraz samodzielne rozpoznawanie nazwy firmy (`resolve_company_name`),
- **Krok 1 (Krok A):** pipeline sprzedażowy **`Status leada`** (`lead_status` + widok Kanban), **`Partia importu`** (`lead_batch`) i raporty JSON z każdego przebiegu (`logs/*.json`),
- **Krok 2 (Krok B):** brama jakości e-maili (literówki TLD `.coml`/`.pll`, domeny jednorazowe, weryfikacja MX/disposable w **UserCheck API** ze współdzielonym cache, tarcza **DNC** z Attio) i filtr **anty-ICP** (Auto SPA, pralnie, agencje IT/SEO/hosting),
- **Krok 3 (`resend_attio_sync.py`):** automatyczna pętla zwrotna **`Resend → Attio`** (synchronizacja wysyłek do `resend_promotional_sends` bez duplikowania historycznych 857 rekordów, przeliczanie 6 pól podsumowujących `resend_*` na `People` i `Companies`, obsługa wypisań z Audience oraz automatyczny awans `lead_status` do `Wysłano kampanię` / `Zainteresowany` / `Odrzucony / DNC` z ochroną wyższych etapów),
- **Krok 4 (`--fill-missing`):** bezpieczny tryb *fill-blanks-only* (dopisuje wyłącznie puste pola na istniejących rekordach w Attio bez nadpisywania jakichkolwiek wartości),
- **Krok 6a (`attio_resend_export.py` + `resend_sender.py --from-attio`):** eksport odbiorców kampanii **`Attio → Resend`** z filtrami branży, twardym wykluczeniem DNC, klientów/triali (`Trial SoundYou`, `Klient`, `Rozmowa / Demo`, `Odrzucony / DNC`), okresu karencji (`--cooldown-days`, domyślnie 30 dni) i limitem kontaktów na firmę (`--max-per-company`, domyślnie 2),
- **Krok 6b (`supabase_attio_sync.py`):** bezpieczna synchronizacja **READ-ONLY** **`SoundYou Supabase (WEBPlayerPV) → Attio`** oznaczająca konta `Trial SoundYou` i płatnych `Klient` (również dla salonów na `@gmail.com`/`@wp.pl` powiązanych przez `People`), synchronizująca `marketing_opt_out` i opcjonalnie `demo_plays` (`Rozmowa / Demo`), bez ryzyka cofnięcia statusu `Klient`.

---

## 1. Architektura przepływu end-to-end

```text
[Google Maps / Booksy / WWW]
           │
           ▼  (1) scraper.py  ──► data/Raport/SoundYouLeads.csv
           │
           ▼  (2) attio_sync.py (6-sygnałowy dedup, UserCheck MX, anty-ICP, --fill-missing)
           │
     ┌─────┴───────────────────────────────────────────┐
     ▼                                                 │
[ Attio CRM — Single Source of Truth ]                 │
  • Companies (Branża SoundYou, Status leada, Kanban)  │
  • People (e-maile, telefony, flagi DNC / Resend)     │
  • Resend Promotional Sends (historia kampanii)       │
     │                                                 ▲
     │ (6a) attio_resend_export.py                     │ (3) resend_attio_sync.py
     │      lub resend_sender.py --from-attio          │     (otwarcia, kliknięcia, DNC,
     ▼      (filtr branży, DNC, cooldown 30d, Trial)   │      awans -> Wysłano / Zainteresowany)
[ Resend Audience / Kampanie e-mail ] ─────────────────┘
                                                       ▲
[ Aplikacja SoundYou — Supabase WEBPlayerPV ] ─────────┘
  • GET /rest/v1/profiles (READ-ONLY)            (6b) supabase_attio_sync.py
  • GET /rest/v1/demo_plays (opcjonalnie)             (awans -> Trial SoundYou / Klient)
```

---

## 2. Stan docelowy w Attio (zweryfikowany w workspace `soundyou`)

- **Atrybuty na `Companies`:**
  - `Branża SoundYou` (`soundyou_industry`, `select`, `is_multiselect: true`, 12 opcji)
  - `Status leada` (`lead_status`, `status`: `Nowy` → `Gotowy do kampanii` → `Wysłano kampanię` → `Zainteresowany` → `Rozmowa / Demo` → `Trial SoundYou` → `Klient` ↔ `Były klient` / `Odrzucony / DNC`)
  - `Plan SoundYou` (`soundyou_plan`, `text`, np. `Trial`, `Standard`, `Business`, `Pro`)
  - `Subskrypcja do` (`soundyou_valid_until`, `date`, `YYYY-MM-DD` z `profiles.valid_until`)
  - `Partia importu` (`lead_batch`, `text`, np. `2026-11-25 | kawiarnia-wroclaw` lub `SoundYou App (Supabase)`)
  - 6 pól podsumowujących kampanie Resend (`resend_last_sent_at`, `resend_last_campaign`, `resend_last_delivery_status`, `resend_any_opened`, `resend_any_clicked`, `resend_sends_count`) — dostępne identycznie na `People` i `Companies`.
  - Osobne kolumny kontaktowe/social we wszystkich widokach tabelarycznych (`Companies` i `People`): **Nazwa firmy**, **Strona WWW** (`domains`), **Instagram** (`instagram`), **Facebook** (`facebook`), **Branża SoundYou**, **Status leada**, **Plan SoundYou**, **Subskrypcja do**, **Partia importu**.
- **Widoki w Attio:**
  - 1 widok Kanban: `Pipeline — Status leada (Kanban)` (z kolumną `Były klient` oraz polami `Plan SoundYou` i `Subskrypcja do` na kartach)
  - 1 widok kliencki: `Klienci SoundYou (Obecni, Byli i Trial)` (filtruje `Klient`, `Były klient`, `Trial SoundYou`, sortuje po statusie i dacie ważności subskrypcji)
  - 8 widoków branżowych na `Companies` (`Branża — SPA & Wellness`, `Kobido & Masaż`, `Beauty / Salon Urody`, `Hotele i Pensjonaty`, `Restauracje i Kawiarnie`, `Fitness / Joga / Pilates`, `Fryzjer / Barber`, `Gabinety / Sklepy / Inne`)
  - 6 widoków follow-up Resend (`People` + `Companies`: kliknięcia, otwarcia bez kliknięcia, odbicia).

---

## 3. Szczegółowy opis modułów i kroków

### Krok 1, 2 i 4 — Scraping, Quality Gate, Deduplikacja i `--fill-missing` (`scraper.py`, `attio_sync.py`)
- **6-sygnałowa deduplikacja względem Attio:** domena WWW (`companies_by_domain`), linki FB/IG (`companies_by_social`), e-mail (`companies_by_email`, `people_by_email`), telefon E.164 (`+48...`), `(nazwa, miasto)` / `(nazwa, kod pocztowy)` oraz unikalna nazwa firmy (≥ 5 znaków).
- **Domyślna zasada „istnieje = pomiń”:** istniejące w Attio firmy nie są nadpisywane ani duplikowane (`0` zapisów).
- **Tryb `--fill-missing` (*fill-blanks-only*):** dopisuje wyłącznie puste pola (`domains`, `instagram`, `facebook`, `soundyou_industry`, `primary_location`, brakujący `phone_numbers` i nowo odkryte adresy e-mail jako `People`), logując zmiany w `audit.filled_missing_companies`.
- **Brama dostarczalności i anty-ICP:** odrzuca literówki TLD (`.coml`, `.con`, `.pll`), domeny jednorazowe, rekordy z listy DNC w Attio, weryfikuje MX przez **UserCheck API** (cache w `data/Raport/email_verify_cache.json`) i odfiltrowuje firmy spoza ICP (`is_non_icp_business`).
- **Auto-nazwa (`resolve_company_name`):** kolejno z `site_title` (`og:site_name` / JSON-LD / `<title>`), slugu profilu FB/IG, domeny WWW, domeny lub prefiksu e-maila.

### Krok 3 — Automatyczna pętla zwrotna `Resend → Attio` (`resend_attio_sync.py`)
- Pobiera wysłane wiadomości z `GET /emails` (z paginacją) oraz wypisania z `GET /audiences/{id}/contacts`.
- Dopasowuje rekordy `resend_promotional_sends` zarówno po `resend_message_key` (SHA-256), jak i po krotce `(person_id, kampania, data_wysyłki_UTC_co_do_sekundy)`, dzięki czemu **nigdy nie duplikuje historycznych 857 rekordów**.
- Przelicza i aktualizuje 6 pól `resend_*` na `People` i `Companies` **wyłącznie wtedy, gdy wartości uległy zmianie** (pełna idempotentność przy ponownym uruchomieniu).
- Automatycznie awansuje `Companies.lead_status`:
  - kliknięcie w link (`resend_any_clicked = true`) → `Zainteresowany`,
  - dostarczona wysyłka dla `Nowy` / `Gotowy do kampanii` → `Wysłano kampanię`,
  - wypisanie wszystkich kontaktów firmy lub same `bounced`/`suppressed` bez otwarć → `Odrzucony / DNC`,
  - **twarda ochrona wyższych etapów:** nigdy nie nadpisuje ani nie cofa `Rozmowa / Demo`, `Trial SoundYou`, `Klient`.

### Krok 6a — Eksport odbiorców kampanii `Attio → Resend` (`attio_resend_export.py`, `resend_sender.py`)
- Buduje czystą listę odbiorców bezpośrednio z Attio CRM zamiast surowego CSV ze scrapera:
  - filtruje po branży (`--industry`, obsługuje polskie nazwy kanoniczne i angielskie skróty np. `spa`, `hotel`, `kawiarnia`),
  - twardo wyklucza etapy `Trial SoundYou`, `Klient`, `Rozmowa / Demo`, `Odrzucony / DNC` (lub przyjmuje własną białą listę `--allowed-statuses`),
  - blokuje kontakty z `resend_unsubscribed = true`, statusem `bounced`/`suppressed`, błędną składnią lub spoza ICP,
  - egzekwuje **okres karencji (`--cooldown-days`, domyślnie 30 dni)** zarówno na poziomie kontaktu (`People`), jak i całej firmy (`Companies`), aby nie wysyłać zbyt częstych kampanii do tego samego lokalu,
  - ogranicza liczbę skrzynek na jedną firmę (`--max-per-company`, domyślnie 2), priorytetyzując e-maile recepcyjne/kontaktowe (`recepcja@`, `kontakt@`, `biuro@`) przed księgowymi (`faktury@`, `ksiegowosc@`).
- Zintegrowany bezpośrednio z `resend_sender.py` poprzez flagi `--from-attio`, `--industry` i `--cooldown-days`.

### Krok 6b — Synchronizacja `SoundYou Supabase → Attio` (`supabase_attio_sync.py`)
- **Ściśle READ-ONLY wobec bazy produkcyjnej `WEBPlayerPV`:** klient `SupabaseReadClient` wykonuje wyłącznie zapytania `GET /rest/v1/profiles` (dla `role=eq.owner` i `parent_profile_id=is.null`) oraz opcjonalnie `GET /rest/v1/demo_plays` — zero operacji zapisu w Supabase.
- Dopasowuje właścicieli kont SoundYou do Attio w 4 krokach:
  1. istniejący `Person` po adresie e-mail → jego `company` (**rozpoznaje również salony na `@gmail.com` / `@wp.pl`!**),
  2. e-mail w opisie firmy (`companies_by_email`),
  3. domena firmowa e-maila (`companies_by_domain` z wykluczeniem darmowych skrzynek),
  4. znormalizowana nazwa firmy `business_name`.
- Reguły statusów (`compute_status_upgrade`):
  - płatny plan (`plan` różny od pustego / `Trial` / `Free` / `Demo`) → **`Klient`** (najwyższy priorytet — nigdy nie jest cofany do `Trial SoundYou` ani `Odrzucony / DNC`),
  - okres próbny (`plan = 'Trial'`, `onboarding_completed = true` lub `stripe_customer_id`) → **`Trial SoundYou`**,
  - `marketing_opt_out = true` → ustawia `resend_unsubscribed = true` na `People` (oraz `Odrzucony / DNC` na firmie, o ile nie jest w `Rozmowa / Demo` / `Trial SoundYou` / `Klient`),
  - `--include-demo-plays` → awansuje dopasowane firmy z wczesnych etapów do **`Rozmowa / Demo`**.
- Dopisuje `soundyou_industry` z dropdownu aplikacji SoundYou (w tym `Studio Jogi` → `Joga / Pilates / Medytacja`) wyłącznie wtedy, gdy pole branży w Attio jest puste, a dla organicznych rejestracji tworzy brakujące `Company` + `Person` z partią `SoundYou App (Supabase)` (można wyłączyć przez `--no-create-missing`).

---

## 4. Konfiguracja `.env` (`Scraper/.env`)

```ini
GOOGLE_MAPS_API_KEY=your_google_maps_api_key_here
RESEND_API_KEY=your_resend_api_key_here
RESEND_AUDIENCE_ID=your_resend_audience_id_here
USERCHECK_API_KEY=uc_your_api_key_here
ATTIO_API_KEY=your_attio_api_key_here
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_ROLE_KEY=your_supabase_service_role_key_here
```

---

## 5. Ściąga uruchomieniowa (CLI oraz gotowe skrypty `.bat`)

Wszystkie komendy uruchamiamy z katalogu `C:\IdeaMusicPro\Web-Crunchy-Scrap\Scraper` (lub dwuklikiem z nadrzędnego katalogu `C:\IdeaMusicPro\Web-Crunchy-Scrap\*.bat`):

1. **Import nowych leadów z CSV do Attio (`attio_sync.py` / `..\sync_to_attio.bat`):**
   ```powershell
   .\.venv\Scripts\python.exe attio_sync.py --offline-dry-run
   .\.venv\Scripts\python.exe attio_sync.py --dry-run
   .\.venv\Scripts\python.exe attio_sync.py --batch "2026-11-26 | SoundYouLeads"
   .\.venv\Scripts\python.exe attio_sync.py --fill-missing --dry-run
   ```

2. **Synchronizacja wyników kampanii `Resend → Attio` (`resend_attio_sync.py` / `..\sync_resend_to_attio.bat`):**
   ```powershell
   .\.venv\Scripts\python.exe resend_attio_sync.py --dry-run
   .\.venv\Scripts\python.exe resend_attio_sync.py
   ```

3. **Synchronizacja kont Trial i Klient `Supabase → Attio` (`supabase_attio_sync.py` / `..\sync_supabase_to_attio.bat`):**
   ```powershell
   .\.venv\Scripts\python.exe supabase_attio_sync.py --dry-run
   .\.venv\Scripts\python.exe supabase_attio_sync.py --include-demo-plays
   ```

4. **Eksport odbiorców z Attio do kampanii Resend (`attio_resend_export.py` / `resend_sender.py` / `..\export_attio_to_resend.bat`):**
   ```powershell
   # Podgląd i eksport CSV czystych odbiorców dla wybranych branż (cooldown 30 dni):
   .\.venv\Scripts\python.exe attio_resend_export.py --industry "SPA & Wellness" --industry "Kobido & Masaż" --cooldown-days 30 --dry-run

   # Eksport z jednoczesnym wgraniem kontaktów do Resend Audience:
   .\.venv\Scripts\python.exe attio_resend_export.py --industry "Kawiarnie" --sync-audience

   # Bezpośrednia wysyłka / podgląd w resend_sender.py prosto z Attio CRM:
   .\.venv\Scripts\python.exe resend_sender.py --from-attio --industry "SPA & Wellness" --cooldown-days 30 --dry-run
   ```

5. **Testy jednostkowe (169 testów w 7 zestawach):**
   ```powershell
   .\.venv\Scripts\python.exe -m pytest -q tests\test_attio_sync.py tests\test_resend_attio_sync.py tests\test_attio_resend_export.py tests\test_supabase_attio_sync.py tests\test_resend_sender.py tests\test_pure_logic.py tests\test_merge_dedup.py
   ```

---

## 6. Automatyzacja 100% Remote (Hybryda: Supabase Event-Driven + GitHub Actions)

Docelowy model pracy działa w 100% zdalnie (24/7, bez włączonego komputera lokalnego) i oszczędza limity Supabase:

1. **Rejestracje, Demo i Subskrypcje (`WEBPlayerPV → Attio`, Event-Driven w Supabase):**
   - W projekcie `WEBPlayerPV` Edge Function `sync-crm` (`supabase/functions/sync-crm` + `_shared/crm-provider.ts`) wyzwalana przez kolejkę `crm_sync_outbox` aktualizuje w Attio:
     - `Status leada` (`Rozmowa / Demo` → `Trial SoundYou` → `Klient` ↔ `Były klient`, bez cofania klienta do `Trial`),
     - `Plan SoundYou` (`soundyou_plan`) i `Subskrypcja do` (`soundyou_valid_until`),
     - `Branża SoundYou` (`soundyou_industry` — tylko gdy pole w Attio jest puste),
     - `Partia importu` (`SoundYou App (Supabase)` dla nowych firm),
     - `resend_unsubscribed` przy `marketing_opt_out = true`,
     - firmy rejestrujące się na darmowych skrzynkach (`@gmail.com`, `@wp.pl` itd.) — dopasowując istniejące rekordy przez powiązanie `People → company` / nazwę firmy albo tworząc nowe rekordy dla rejestracji organicznych.
   - Gdy brak nowych zdarzeń w aplikacji, `crm_internal.dispatch()` kończy pracę w Postgresie i wykonuje **0 wywołań Edge Function**.

2. **Pętla zwrotna kampanii `Resend → Attio` + poranny bezpiecznik Supabase (GitHub Actions):**
   - Plik [`.github/workflows/sync-crm.yml`](../.github/workflows/sync-crm.yml) (zmergowany na domyślną gałąź `main`) uruchamia się automatycznie **2x dziennie** (`07:00 UTC` oraz `17:00 UTC`) oraz na żądanie przez przycisk **Run workflow** (z opcjami `dry_run`, `sync_resend`, `sync_supabase`, `include_demo_plays`).
   - Obsługuje nowy globalny endpoint Resend **`GET /contacts`** (oraz `POST /contacts` w `resend_sender.py`) bez potrzeby podawania `RESEND_AUDIENCE_ID`.
   - Korzysta wyłącznie ze standardowej biblioteki Pythona 3.12 (czas wykonania ~15–25 s, zużycie ~15–30 min/mies. z darmowych 2000 min GitHub Actions).
   - Zapisuje raporty `logs/*_sync_*.json` jako artefakty przebiegu (14 dni retencji).

### Wymagane sekrety do skonfigurowania ręcznie (aktywacja produkcyjna)

- **W GitHub (`Scraper → Settings → Secrets and variables → Actions → Repository secrets`):**
  - `ATTIO_API_KEY`
  - `RESEND_API_KEY`
  - `SUPABASE_URL`
  - `SUPABASE_SERVICE_ROLE_KEY`
  - *(Opcjonalnie: `RESEND_AUDIENCE_ID` nie jest już wymagane — przy jego braku skrypt korzysta z globalnego `GET /contacts`.)*

- **W Supabase (`WEBPlayerPV` — dla natychmiastowego syncu rejestracji i Stripe):**
  - Wdrożyć samą funkcję: `supabase functions deploy sync-crm` *(bez `supabase db push`!)*.
  - **Edge Function Secrets (`sync-crm`):** `CRM_PROVIDER=attio`, `ATTIO_API_KEY=<klucz>`, `ATTIO_WORKSPACE_SLUG=soundyou`.
  - **Supabase Vault:** sekret `crm_sync_enabled` = `true` (oraz `crm_sync_project_url` i `service_role_key`).

---

## 7. Powiązana dokumentacja i grafiki (Cross-Repo)

- **Grafiki architektury workflow (PNG):** [soundyou_workflow.png](./soundyou_workflow.png) (PL) · [soundyou_workflow_en.png](./soundyou_workflow_en.png) (EN)
- **Dokumentacja aplikacji odtwarzacza (`WEBPlayerPV`):**
  - [WEBPlayerPV `README.md`](https://github.com/ksywa812/WEBPlayerPV/blob/main/README.md) (sekcja *Integracja CRM & Lead-Gen*)
  - [WEBPlayerPV `docs/admin-dashboard-crm-handoff-2026-11-25.md`](https://github.com/ksywa812/WEBPlayerPV/blob/main/docs/admin-dashboard-crm-handoff-2026-11-25.md) (szczegóły kolejki `crm_sync_outbox`, migracji i panelu admina)
  - [WEBPlayerPV `supabase/functions/_shared/crm-provider.ts`](https://github.com/ksywa812/WEBPlayerPV/blob/main/supabase/functions/_shared/crm-provider.ts) (natywny adapter `AttioCrmAdapter`)

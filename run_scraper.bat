@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "VENV_PY=%~dp0.venv\Scripts\python.exe"
set MASTER=Data/Raport/SoundYouLeads.csv

echo.
echo ============================================================
echo  IdeaMusicPro -- Scraper
echo ============================================================
echo  [1] Pojedyncze miasto (wizard interaktywny)
echo  [2] Aglomeracja (wiele miast, jedna branza)
echo ============================================================
set /p MODE="Wybierz tryb [1/2]: "

if "%MODE%"=="1" goto MODE1
if "%MODE%"=="2" goto MODE2

echo [BLAD] Nieprawidlowy wybor. Uruchom ponownie.
pause
exit /b 1

REM ── TRYB 1: Wizard interaktywny ──────────────────────────────
:MODE1
echo.
echo ============================================================
echo  [1/2] Scraper -- tryb interaktywny
echo ============================================================
if exist "%VENV_PY%" (
    "%VENV_PY%" scraper.py --output Data/Raport/SoundYouLeads.xlsx --emails --use-google
) else (
    py scraper.py --output Data/Raport/SoundYouLeads.xlsx --emails --use-google
)
if errorlevel 1 (
    echo [BLAD] Scraper zakonczyl sie bledem. Pipeline przerwany.
    pause
    exit /b 1
)
goto RESEND

REM ── TRYB 2: Aglomeracja ──────────────────────────────────────
:MODE2
echo.
echo ============================================================
echo  Wybierz branze (zgodne z Attio soundyou_industry):
echo ============================================================
echo  [1]  SPA ^& Wellness
echo  [2]  Kobido ^& Masaz
echo  [3]  Beauty / Salon Urody
echo  [4]  Hotele i Pensjonaty
echo  [5]  Restauracje
echo  [6]  Kawiarnie
echo  [7]  Fitness / Silownia
echo  [8]  Joga / Pilates / Medytacja
echo  [9]  Salon Fryzjerski / Barber
echo  [10] Poczekalnia / Gabinet / Fizjoterapia
echo  [11] Sklep / Retail
echo  [12] Wpisz recznie
echo ============================================================
set /p BRANZA="Wybierz branze [1-12]: "

set QUERY=
if "!BRANZA!"=="1" set QUERY=spa
if "!BRANZA!"=="2" set QUERY=masaz
if "!BRANZA!"=="3" set QUERY=uroda
if "!BRANZA!"=="4" set QUERY=hotel
if "!BRANZA!"=="5" set QUERY=restaurant
if "!BRANZA!"=="6" set QUERY=kawiarnia
if "!BRANZA!"=="7" set QUERY=fitness
if "!BRANZA!"=="8" set QUERY=joga
if "!BRANZA!"=="9" set QUERY=fryzjer
if "!BRANZA!"=="10" set QUERY=fizjoterapia
if "!BRANZA!"=="11" set QUERY=sklep
if "!BRANZA!"=="12" set /p QUERY="Wpisz nazwe branzy: "

if "!QUERY!"=="" (
    echo [BLAD] Nieprawidlowy wybor branzy.
    pause
    exit /b 1
)

echo.
echo  Dostepne aglomeracje:
echo  [1] Wroclaw i okolice
echo  [2] Warszawa i okolice
echo  [3] Krakow i okolice
echo  [4] Trojmiasto
echo  [5] Wpisz miasta recznie
echo.
set /p AGLO="Wybierz aglomeracje [1-5]: "

if "!AGLO!"=="1" goto AGLO1
if "!AGLO!"=="2" goto AGLO2
if "!AGLO!"=="3" goto AGLO3
if "!AGLO!"=="4" goto AGLO4
if "!AGLO!"=="5" goto AGLO5

echo [BLAD] Nieprawidlowy wybor aglomeracji.
pause
exit /b 1

:AGLO1
set CITIES=Wroclaw Swidnica Olawa "Jelcz-Laskowice" Olesnica Trzebnica "Sroda Slaska" Kobierzyce Dlugoleka
goto RUN_AGLO

:AGLO2
set CITIES=Warszawa Piaseczno Pruszkow Legionowo Wolomin Marki Lomianki Jozefow Otwock
goto RUN_AGLO

:AGLO3
set CITIES=Krakow Wieliczka Niepolomice Skawina Krzeszowice Myslowice Chrzanow
goto RUN_AGLO

:AGLO4
set CITIES=Gdansk Gdynia Sopot Rumia Reda Wejherowo Pruszcz-Gdanski
goto RUN_AGLO

:AGLO5
echo  Wpisz miasta oddzielone spacjami (miasta wieloczlonowe w cudzyslowie):
set /p CITIES="Miasta: "
goto RUN_AGLO

:RUN_AGLO
echo.
echo ============================================================
echo  Aglomeracja -- branza: !QUERY!
echo  Miasta: !CITIES!
echo ============================================================

set FAILED=0
for %%C in (!CITIES!) do (
    echo.
    echo  --- Scrapowanie: %%C ---
    if exist "%VENV_PY%" (
        "%VENV_PY%" scraper.py --query "!QUERY!" --location "%%C" --emails --use-google --output Data/Raport/SoundYouLeads.xlsx
    ) else (
        py scraper.py --query "!QUERY!" --location "%%C" --emails --use-google --output Data/Raport/SoundYouLeads.xlsx
    )
    if errorlevel 1 (
        echo [WARN] Blad dla miasta %%C -- kontynuuje...
        set /a FAILED+=1
    )
)
echo.
echo  Miasta zakonczone. Bledy: !FAILED!

REM ── ATTIO SYNC (tylko nowe leady; istniejace w Attio sa pomijane) ──
:RESEND
echo.
echo ============================================================
echo  [2/2] Uzupelnienie nowych leadow w Attio CRM (istniejace pomijane)
echo ============================================================
if exist "%VENV_PY%" (
    "%VENV_PY%" attio_sync.py --csv "%MASTER%"
) else (
    py attio_sync.py --csv "%MASTER%"
)
if errorlevel 1 (
    echo [OSTRZEZENIE] Synchronizacja z Attio nie powiodla sie ^(sprawdz ATTIO_API_KEY w .env^).
)

echo.
echo ============================================================
echo  Gotowe! Sprawdz:
echo    - %MASTER%
echo    - https://app.attio.com
echo ============================================================
echo.
pause

@echo off
cd /d "%~dp0"

set "VENV_PY=%~dp0.venv\Scripts\python.exe"
set MASTER=Data/Raport/SoundYouLeads.csv

echo.
echo ============================================================
echo  IdeaMusicPro — Scraper
echo ============================================================
echo  [1] Pojedyncze miasto (wizard interaktywny)
echo  [2] Aglomeracja (wiele miast, jedna branza)
echo ============================================================
set /p MODE="Wybierz tryb [1/2]: "

REM ── TRYB 1: Wizard interaktywny ──────────────────────────────
if "%MODE%"=="1" (
    echo.
    echo ============================================================
    echo  [1/2] Scraper — tryb interaktywny
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
)

REM ── TRYB 2: Aglomeracja ──────────────────────────────────────
if "%MODE%"=="2" (
    echo.
    set /p QUERY="Branza (np. spa, uroda, masaz): "

    echo.
    echo  Dostepne aglomeracje:
    echo  [1] Wroclaw i okolice
    echo  [2] Warszawa i okolice
    echo  [3] Krakow i okolice
    echo  [4] Trojmiasto
    echo  [5] Wpisz miasta recznie
    echo.
    set /p AGLO="Wybierz aglomeracje [1-5]: "

    if "%AGLO%"=="1" set CITIES=Wroclaw Swidnica Olawa "Jelcz-Laskowice" Olesnica Trzebnica "Sroda Slaska" Kobierzyce Dlugoleka
    if "%AGLO%"=="2" set CITIES=Warszawa Piaseczno Pruszkow Legionowo Wolomin Marki Lomianki Jozefow Otwock
    if "%AGLO%"=="3" set CITIES=Krakow Wieliczka Niepolomice Skawina Krzeszowice Myslowice Chrzanow
    if "%AGLO%"=="4" set CITIES=Gdansk Gdynia Sopot Rumia Reda Wejherowo Pruszcz-Gdanski
    if "%AGLO%"=="5" (
        echo  Wpisz miasta oddzielone spacjami (miasta wieloczlonowe w cudzyslowie):
        set /p CITIES="Miasta: "
    )

    echo.
    echo ============================================================
    echo  Aglomeracja — branza: %QUERY%
    echo  Miasta: %CITIES%
    echo ============================================================

    set FAILED=0
    for %%C in (%CITIES%) do (
        echo.
        echo  --- Scrapowanie: %%C ---
        if exist "%VENV_PY%" (
            "%VENV_PY%" scraper.py --query %QUERY% --location %%C --emails --use-google --output Data/Raport/SoundYouLeads.xlsx
        ) else (
            py scraper.py --query %QUERY% --location %%C --emails --use-google --output Data/Raport/SoundYouLeads.xlsx
        )
        if errorlevel 1 (
            echo [WARN] Blad dla miasta %%C — kontynuuje...
            set /a FAILED+=1
        )
    )
    echo.
    echo  Miasta zakonczone. Bledy: %FAILED%
    goto RESEND
)

echo [BLAD] Nieprawidlowy wybor. Uruchom ponownie.
pause
exit /b 1

REM ── RESEND ───────────────────────────────────────────────────
:RESEND
echo.
echo ============================================================
echo  [2/2] Import do Resend Audiences
echo ============================================================
if exist "%VENV_PY%" (
    "%VENV_PY%" resend_sender.py --csv "%MASTER%"
) else (
    py resend_sender.py --csv "%MASTER%"
)

echo.
echo ============================================================
echo  Gotowe! Sprawdz: https://resend.com/audiences
echo ============================================================
echo.
pause

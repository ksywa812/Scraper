@echo off
REM Przechodzimy do folderu, w którym leży ten plik .bat
cd /d "%~dp0"
REM Uruchamiamy Twój scraper przez Pythona i domyślnie zapisujemy do SoundYouLeads.xlsx
REM Use python from .venv if available, otherwise fall back to system 'py'
set "VENV_PY=%~dp0.venv\Scripts\python.exe"
if exist "%VENV_PY%" (
    "%VENV_PY%" scraper.py --output Data/Raport/SoundYouLeads.xlsx
) else (
    py scraper.py --output Data/Raport/SoundYouLeads.xlsx
)
REM Nie zamykamy od razu okna, czekamy na naciśnięcie klawisza
echo.
echo Press any key to exit...
pause >nul

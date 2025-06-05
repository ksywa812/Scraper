@echo off
REM Przechodzimy do folderu, w którym le¿y ten plik .bat
cd /d "%~dp0"
REM Uruchamiamy Twój scraper przez Pythona
py scraper.py
REM Nie zamykamy od razu okna, czekamy na naciœniêcie klawisza
echo.
echo Press any key to exit...
pause >nul

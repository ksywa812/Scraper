@echo off
REM Przechodzimy do folderu, w kt�rym le�y ten plik .bat
cd /d "%~dp0"
REM Uruchamiamy Tw�j scraper przez Pythona
py scraper.py
REM Nie zamykamy od razu okna, czekamy na naci�ni�cie klawisza
echo.
echo Press any key to exit...
pause >nul

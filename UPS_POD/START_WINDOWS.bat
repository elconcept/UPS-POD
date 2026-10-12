@echo off
rem UPS POD - uruchomienie na Windows (dwuklik). Pierwszy raz instaluje biblioteki.
cd /d "%~dp0"
title UPS POD

py -3 --version >nul 2>nul
if errorlevel 1 goto szukaj_python
py -3 ups_pod.py start
goto koniec

:szukaj_python
python --version >nul 2>nul
if errorlevel 1 goto brak_pythona
python ups_pod.py start
goto koniec

:brak_pythona
echo.
echo Nie znaleziono Pythona 3.
echo Zainstaluj go ze strony https://www.python.org/downloads/
echo W pierwszym oknie instalatora zaznacz "Add python.exe to PATH".
echo Potem uruchom ten plik ponownie.
echo.
pause
exit /b 1

:koniec
if errorlevel 1 pause

@echo off
setlocal
cd /d "%~dp0"
py -3.12 -m venv .venv-build
if errorlevel 1 goto :error
call .venv-build\Scripts\activate.bat
python -m pip install --upgrade pip
if errorlevel 1 goto :error
python -m pip install -r requirements-desktop.txt
if errorlevel 1 goto :error
python build\build_app.py
if errorlevel 1 goto :error
echo.
echo Gotowe: dist\UPSPOD\UPSPOD.exe
pause
exit /b 0
:error
echo Blad budowania. Sprawdz Python 3.12 i polaczenie z internetem.
pause
exit /b 1

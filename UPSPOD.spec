# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller desktop: GUI + core + Chromium headless shell + Qt.

Uruchamiaj z katalogu projektu: python build/build_app.py
Wymaga środowiska danego OS. Dane UPS/Excel nie wchodzą do binarki.
"""
import os
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH)
PW_BROWSERS = Path(os.environ.get('UPS_POD_PW_BROWSERS', str(ROOT / 'build' / 'pw-browsers')))
if not PW_BROWSERS.is_dir() or not any(PW_BROWSERS.iterdir()):
    raise SystemExit('Brakuje Chromium w build/pw-browsers. Uruchom: python build/build_app.py')

# Playwright używa drivera Node.js i zasobów niezauważanych przez zwykłą analizę importów.
pw_datas, pw_binaries, pw_hidden = collect_all('playwright')

analysis = Analysis(
    [str(ROOT / 'UPS_POD' / 'gui.py')],
    pathex=[str(ROOT / 'UPS_POD')],
    binaries=pw_binaries,
    datas=pw_datas + [(str(PW_BROWSERS), 'pw-browsers'),
                      (str(ROOT / 'UPS_POD' / 'ustawienia.ini'), '.')],
    hiddenimports=pw_hidden + ['openpyxl', 'requests', 'playwright.sync_api'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'PyQt5', 'PyQt6'],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name='UPSPOD',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)
coll = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name='UPSPOD',
)
if sys.platform == 'darwin':
    app = BUNDLE(
        coll,
        name='UPSPOD.app',
        icon=None,
        bundle_identifier='pl.upspod.desktop',
        version='1.1.0',
        info_plist={'NSHighResolutionCapable': True},
    )

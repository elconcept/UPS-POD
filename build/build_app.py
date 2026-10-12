#!/usr/bin/env python3
"""Jeden punkt wejścia do spakowania aplikacji dla systemu kompilującego."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BROWSERS = ROOT / 'build' / 'pw-browsers'


def main():
    if sys.platform not in ('darwin', 'win32', 'linux'):
        sys.exit('Nieobsługiwany system: ' + sys.platform)
    BROWSERS.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env['PLAYWRIGHT_BROWSERS_PATH'] = str(BROWSERS)
    env['UPS_POD_PW_BROWSERS'] = str(BROWSERS)
    env['PLAYWRIGHT_SKIP_BROWSER_GC'] = '1'
    print('Downloading bundled Chromium headless shell...', flush=True)
    subprocess.run([sys.executable, '-m', 'playwright', 'install', '--only-shell', 'chromium'],
                   env=env, check=True, cwd=ROOT)
    print('Pakowanie PyInstaller dla: ' + sys.platform, flush=True)
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
                    '--distpath', str(ROOT / 'dist'),
                    '--workpath', str(ROOT / 'build' / 'pyinstaller'),
                    str(ROOT / 'UPSPOD.spec')], env=env, cwd=ROOT, check=True)
    print('GOTOWE: ' + str(ROOT / 'dist'), flush=True)


if __name__ == '__main__':
    main()

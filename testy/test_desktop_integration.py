"""Szybkie testy integracji z binarką; bez zależności od Qt i sieci."""
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'UPS_POD'))
import gui
import ups_pod


class DesktopIntegration(unittest.TestCase):
    def test_frozen_data_is_writable_and_chromium_inside_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pack = root / 'bundle'
            (pack / 'pw-browsers' / 'chromium_headless_shell-1').mkdir(parents=True)
            (pack / 'ustawienia.ini').write_text('[PLIKI]\nplik_excel = apka.xlsx\n', encoding='utf-8')
            target = root / 'my-ups-home'
            with patch.object(sys, 'frozen', True, create=True), \
                 patch.object(sys, '_MEIPASS', str(pack), create=True), \
                 patch.dict(os.environ, {'UPS_POD_HOME': str(target)}, clear=False):
                gui.przygotuj_srodowisko()
                self.assertEqual(os.environ['PLAYWRIGHT_BROWSERS_PATH'], str(pack / 'pw-browsers'))
                self.assertTrue((target / 'ustawienia.ini').is_file())
                (target / 'ustawienia.ini').write_text('NIE NADPISUJ', encoding='utf-8')
                gui.przygotuj_srodowisko()
                self.assertEqual((target / 'ustawienia.ini').read_text(), 'NIE NADPISUJ')
            os.environ.pop('PLAYWRIGHT_BROWSERS_PATH', None)

    def test_frozen_schedulers_use_executable_not_python(self):
        with patch.object(sys, 'frozen', True, create=True), \
             patch.object(sys, 'executable', '/fake/UPSPOD.exe'):
            self.assertEqual(str(ups_pod.python_venv()), '/fake/UPSPOD.exe')
            xml = ups_pod.xml_zadania_windows([2, 4], (15, 0), 'DOMAIN\\USER')
            self.assertIn('<Arguments>run --auto</Arguments>', xml)
            self.assertNotIn('ups_pod.py', xml.split('<Actions')[1])
            blob = ups_pod.plist_mac([2, 4], (15, 0))
            import plistlib
            p = plistlib.loads(blob)
            self.assertEqual(p['ProgramArguments'], ['/fake/UPSPOD.exe', 'run', '--auto'])

    def test_playwright_works_with_system_chromium_if_available(self):
        import shutil
        if not shutil.which('chromium'):
            self.skipTest('brak systemowego chromium (na Windows jest w binarce)')
        with tempfile.TemporaryDirectory() as tmp:
            c = ups_pod.KonwerterPDF()
            try:
                cel = Path(tmp) / 'sample.pdf'
                c.html_na_pdf('<html><body>TEST PDF UPS</body></html>', cel)
                self.assertTrue(cel.read_bytes().startswith(b'%PDF'))
            finally:
                c.zamknij()


if __name__ == '__main__':
    unittest.main()

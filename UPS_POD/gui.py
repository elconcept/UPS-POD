#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Qt UI dla istniejącego UPS POD. Tryb CLI w spakowanej aplikacji obsługuje harmonogram."""
import os
import sys
from pathlib import Path


def przygotuj_srodowisko():
    """Przenośny katalog danych; w frozen nie zapisujemy niczego wewnątrz .app."""
    if getattr(sys, 'frozen', False):
        os.environ.setdefault('UPS_POD_HOME', str(Path.home() / 'UPS_POD'))
        zasoby = Path(sys._MEIPASS)
        przegladarki = zasoby / 'pw-browsers'
        if przegladarki.is_dir():
            os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(przegladarki)
        dom = Path(os.environ['UPS_POD_HOME'])
        dom.mkdir(parents=True, exist_ok=True)
        konfig = dom / 'ustawienia.ini'
        wzor = zasoby / 'ustawienia.ini'
        if not konfig.exists() and wzor.exists():
            import shutil
            shutil.copyfile(wzor, konfig)


def main():
    przygotuj_srodowisko()
    import ups_pod as core
    # Offline smoke test invoked on every OS against the frozen release binary.
    # It verifies that Chromium is present, executable, and able to generate PDF.
    if len(sys.argv) > 1 and sys.argv[1] == 'selftest-pdf':
        import tempfile
        try:
            with tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / 'upspod-smoke.pdf'
                converter = core.KonwerterPDF()
                try:
                    converter.html_na_pdf(
                        '<!doctype html><html><body>UPS POD PDF smoke test</body></html>',
                        target,
                    )
                    if target.read_bytes()[:4] != b'%PDF':
                        raise RuntimeError('PDF signature missing')
                finally:
                    converter.zamknij()
            return 0
        except Exception as exc:
            if sys.stderr is not None:
                print('PDF smoke test failed: ' + str(exc), file=sys.stderr)
            return 2
    komendy_cli = {'start', 'menu', 'run', 'check', 'test', 'klucze',
                   'harmonogram-wlacz', 'harmonogram-wylacz', 'harmonogram-status'}
    if len(sys.argv) > 1 and sys.argv[1] in komendy_cli:
        # Ważne: uruchomienie przez launchd/schtasks nie importuje Qt.
        # Parametr -psn... przekazywany dawniej przez Finder nie jest CLI.
        if getattr(sys, 'frozen', False) and sys.stdout is None:
            sys.stdout = open(os.devnull, 'w', encoding='utf-8')
            sys.stderr = open(os.devnull, 'w', encoding='utf-8')
        return core.main(sys.argv[1:])
    from PySide6.QtWidgets import QApplication
    app = QApplication(sys.argv)
    app.setApplicationName('UPS POD')
    window = OknoGlowne(core)
    window.show()
    return app.exec()


# Funkcje Qt importowane na poziomie modułu dopiero w sekcji GUI, ale definicje
# klasy są przygotowane dynamicznie, aby tryb harmonogramu nie ładował Qt.
def OknoGlowne(core):
    from PySide6.QtCore import Qt, QThread, Signal, QObject
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtCore import QUrl
    from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
        QLabel, QPushButton, QLineEdit, QPlainTextEdit, QGroupBox, QMessageBox,
        QFileDialog, QProgressBar, QTableWidget, QTableWidgetItem, QHeaderView,
        QAbstractItemView)
    import contextlib
    import io
    import logging
    import re

    class Strumien(io.TextIOBase):
        def __init__(self, wyslij):
            super().__init__()
            self.wyslij = wyslij
            self.buf = ''
        def writable(self):
            return True
        def write(self, t):
            self.buf += str(t)
            while '\n' in self.buf:
                line, self.buf = self.buf.split('\n', 1)
                if line.strip():
                    self.wyslij(line)
            return len(t)
        def flush(self):
            if self.buf.strip():
                self.wyslij(self.buf)
            self.buf = ''

    class Praca(QObject):
        komunikat = Signal(str)
        zakonczono = Signal(int, str)
        def __init__(self, zadanie, arg=None):
            super().__init__()
            self.zadanie, self.arg = zadanie, arg
        def uruchom(self):
            core.konfiguruj_log(auto=False)
            strumien = Strumien(self.komunikat.emit)
            try:
                with contextlib.redirect_stdout(strumien):
                    if self.zadanie == 'run':
                        wynik = core.przebieg('run', auto=False)
                    elif self.zadanie == 'check':
                        wynik = core.przebieg('check', auto=False)
                    elif self.zadanie == 'test':
                        wynik = core.test_trackingu(self.arg)
                    elif self.zadanie == 'api':
                        cid, sec = core.wczytaj_klucze()
                        if not cid or not sec:
                            print('Brak zapisanych kluczy Client ID / Client Secret.')
                            wynik = 1
                        else:
                            u = core.wczytaj_ustawienia()
                            core.KlientUPS(u['adres_api'], cid, sec, u['jezyk'], u['numer_konta'], 0).token()
                            print('Uwierzytelnienie UPS OAuth: poprawne.')
                            wynik = 0
                    elif self.zadanie == 'schedule_on':
                        wynik = core.harmonogram_wlacz()
                    elif self.zadanie == 'schedule_off':
                        wynik = core.harmonogram_wylacz()
                    else:
                        raise ValueError('Nieznane zadanie')
            except Exception as e:
                core.log.exception('Błąd akcji GUI: %s', self.zadanie)
                self.komunikat.emit('Błąd: ' + str(e))
                wynik = 9
            finally:
                strumien.flush()
            self.zakonczono.emit(wynik, self.zadanie)

    class Okno(QWidget):
        def __init__(self):
            super().__init__()
            self.thread = None
            self.worker = None
            self.setWindowTitle('UPS POD • potwierdzenia doręczenia')
            self.setMinimumSize(790, 650)
            self.resize(980, 780)
            lay = QVBoxLayout(self)
            lay.setSpacing(12)

            title = QLabel('UPS POD  |  Amazon FBA')
            title.setStyleSheet('font-size: 22px; font-weight: 650;')
            lay.addWidget(title)
            descr = QLabel('Statusy UPS, potwierdzenia dostawy PDF oraz automatyczny arkusz CHECK.')
            lay.addWidget(descr)

            ram = QGroupBox('Dane i konto UPS')
            form = QGridLayout(ram)
            self.xlsx = QLabel('—')
            self.xlsx.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.keys = QLabel('—')
            self.schedule = QLabel('—')
            form.addWidget(QLabel('Excel:'), 0, 0)
            form.addWidget(self.xlsx, 0, 1)
            browse = QPushButton('Wybierz Excel…')
            browse.clicked.connect(self.wybierz_excel)
            form.addWidget(browse, 0, 2)
            form.addWidget(QLabel('Klucze UPS:'), 1, 0)
            form.addWidget(self.keys, 1, 1)
            creds_btn = QPushButton('Zapisz klucze…')
            creds_btn.clicked.connect(self.klucze)
            form.addWidget(creds_btn, 1, 2)
            api_btn = QPushButton('Sprawdź API')
            api_btn.clicked.connect(lambda: self.zadanie('api'))
            form.addWidget(api_btn, 1, 3)
            form.addWidget(QLabel('Harmonogram:'), 2, 0)
            form.addWidget(self.schedule, 2, 1)
            on = QPushButton('Włącz')
            on.clicked.connect(lambda: self.zadanie('schedule_on'))
            off = QPushButton('Wyłącz')
            off.clicked.connect(lambda: self.zadanie('schedule_off'))
            form.addWidget(on, 2, 2)
            form.addWidget(off, 2, 3)
            lay.addWidget(ram)

            czyn = QGroupBox('Działania')
            row = QGridLayout(czyn)
            self.run_btn = QPushButton('▶ Pobierz POD i zaktualizuj CHECK')
            self.run_btn.setMinimumHeight(40)
            self.run_btn.clicked.connect(lambda: self.zadanie('run'))
            self.check_btn = QPushButton('Odśwież tylko CHECK')
            self.check_btn.clicked.connect(lambda: self.zadanie('check'))
            row.addWidget(self.run_btn, 0, 0, 1, 3)
            row.addWidget(self.check_btn, 0, 3)
            self.tracking = QLineEdit()
            self.tracking.setPlaceholderText('Numer UPS, np. 1Z004R3168…')
            test_btn = QPushButton('Test jednego trackingu')
            test_btn.clicked.connect(self.test)
            row.addWidget(self.tracking, 1, 0, 1, 3)
            row.addWidget(test_btn, 1, 3)
            buttons = QHBoxLayout()
            for label, action in [
                ('Otwórz Excel', self.open_excel),
                ('Otwórz PDF-y', lambda: self.open_dir(core.wczytaj_ustawienia()['folder_pdf'])),
                ('Otwórz logi', lambda: self.open_dir(core.LOG_DIR)),
            ]:
                b = QPushButton(label)
                b.clicked.connect(action)
                buttons.addWidget(b)
            row.addLayout(buttons, 2, 0, 1, 4)
            lay.addWidget(czyn)

            self.busy = QProgressBar()
            self.busy.setRange(0, 1)
            self.busy.setValue(0)
            self.busy.setTextVisible(False)
            lay.addWidget(self.busy)
            lay.addWidget(QLabel('Arkusz CHECK — podgląd (maks. 250 pozycji):'))
            self.table = QTableWidget(0, 4)
            self.table.setHorizontalHeaderLabels(['Data', 'Shipment ID', 'Tracking UPS', 'Status'])
            self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
            self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
            lay.addWidget(self.table, stretch=2)
            lay.addWidget(QLabel('Dziennik działania:'))
            self.logs = QPlainTextEdit()
            self.logs.setReadOnly(True)
            self.logs.setMaximumBlockCount(1000)
            lay.addWidget(self.logs, stretch=1)
            self.aktywne = [self.run_btn, self.check_btn, test_btn, on, off, api_btn, browse, creds_btn]
            self.odswiez()

        def informacja(self, s):
            self.logs.appendPlainText(s)

        def odswiez(self):
            try:
                u = core.wczytaj_ustawienia()
                self.xlsx.setText(str(u['plik_excel']))
                cid, _ = core.wczytaj_klucze()
                self.keys.setText(('Zapisane • Client ID …' + cid[-4:]) if cid else 'Brak danych')
                self.schedule.setText('%s  (%s, %02d:%02d)' %
                                      (core.harmonogram_status(), core.opis_dni(u['dni']), *u['godzina']))
                self.odswiez_tabele(u['plik_excel'], u['arkusz_check'])
            except Exception as e:
                self.informacja('Nie można odczytać ustawień: ' + str(e))

        def odswiez_tabele(self, xlsx, sheet):
            self.table.setRowCount(0)
            if not xlsx.is_file():
                return
            import openpyxl
            wb = openpyxl.load_workbook(xlsx, read_only=True, data_only=True)
            try:
                if sheet not in wb:
                    return
                ws = wb[sheet]
                # Projekt: wiersz 2 nagłówki, kolejne wiersze to trackingi.
                wiersze = []
                for data, sh, tr, status in ws.iter_rows(min_row=3, max_col=4, values_only=True):
                    if tr:
                        wiersze.append((str(data or ''), str(sh or ''), str(tr), str(status or '')))
                    if len(wiersze) >= 250:
                        break
                self.table.setRowCount(len(wiersze))
                for i, row in enumerate(wiersze):
                    for j, value in enumerate(row):
                        self.table.setItem(i, j, QTableWidgetItem(value))
            finally:
                wb.close()

        def wybierz_excel(self):
            path, _ = QFileDialog.getOpenFileName(self, 'Wybierz arkusz UPS POD',
                            str(Path.home()), 'Arkusze Excel (*.xlsx)')
            if not path:
                return
            if not Path(path).is_file():
                QMessageBox.warning(self, 'Błąd', 'Nie odnaleziono pliku.')
                return
            # Zachowujemy komentarze i resztę ustawień.ini bez configparser.write().
            ini = core.PLIK_USTAWIEN
            ini.parent.mkdir(parents=True, exist_ok=True)
            if not ini.exists():
                from configparser import ConfigParser
                cp = ConfigParser(interpolation=None)
                cp.read_dict(core.DOMYSLNE)
                with ini.open('w', encoding='utf-8') as f:
                    cp.write(f)
            content = ini.read_text(encoding='utf-8-sig')
            if not re.search(r'(?m)^plik_excel\s*=', content):
                QMessageBox.warning(self, 'Ustawienia',
                          'Nie znaleziono pola plik_excel w ustawienia.ini.')
                return
            content = re.sub(r'(?m)^plik_excel\s*=.*$',
                             lambda m: 'plik_excel = ' + path, content)
            ini.write_text(content, encoding='utf-8')
            self.odswiez()

        def klucze(self):
            from PySide6.QtWidgets import QDialog, QDialogButtonBox, QFormLayout
            dlg = QDialog(self)
            dlg.setWindowTitle('Klucze UPS API')
            form = QFormLayout(dlg)
            ident = QLineEdit()
            secret = QLineEdit()
            secret.setEchoMode(QLineEdit.Password)
            ident.setPlaceholderText('Client ID z developer.ups.com')
            secret.setPlaceholderText('Client Secret (nie będzie wyświetlany)')
            form.addRow('Client ID:', ident)
            form.addRow('Client Secret:', secret)
            box = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
            box.accepted.connect(dlg.accept)
            box.rejected.connect(dlg.reject)
            form.addRow(box)
            if dlg.exec() != QDialog.Accepted:
                return
            if not ident.text().strip() or not secret.text().strip():
                QMessageBox.warning(self, 'Błąd', 'Podaj obie wartości: Client ID i Client Secret.')
                return
            core.zapisz_klucze(ident.text().strip(), secret.text().strip())
            secret.clear()
            self.informacja('Klucze zapisano lokalnie. Aby potwierdzić, wybierz „Sprawdź API”.')
            self.odswiez()

        def open_dir(self, path):
            path.mkdir(parents=True, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

        def open_excel(self):
            p = core.wczytaj_ustawienia()['plik_excel']
            if p.is_file():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))
            else:
                QMessageBox.warning(self, 'Nie znaleziono Excela',
                                    'Wybierz plik Excel przyciskiem „Wybierz Excel…”.')

        def test(self):
            tr = self.tracking.text().strip()
            if not tr:
                QMessageBox.information(self, 'Numer UPS', 'Wpisz numer trackingu UPS.')
                return
            self.zadanie('test', tr)

        def zadanie(self, typ, arg=None):
            if self.thread is not None:
                return
            self.informacja('── %s ──' % typ)
            self.busy.setRange(0, 0)
            for b in self.aktywne:
                b.setEnabled(False)
            self.thread = QThread(self)
            self.worker = Praca(typ, arg)
            self.worker.moveToThread(self.thread)
            self.thread.started.connect(self.worker.uruchom)
            self.worker.komunikat.connect(self.informacja)
            self.worker.zakonczono.connect(self.zakoncz)
            self.worker.zakonczono.connect(self.thread.quit)
            self.thread.finished.connect(self.worker.deleteLater)
            self.thread.finished.connect(self.thread.deleteLater)
            self.thread.finished.connect(self.gotowy)
            self.thread.start()

        def zakoncz(self, kod, zadanie):
            self.informacja('Zakończono: %s • kod %s' % (zadanie, kod))
            self.busy.setRange(0, 1)
            self.busy.setValue(1)
            if kod != 0:
                QMessageBox.warning(self, 'UPS POD',
                     'Operacja zakończyła się kodem %s. Szczegóły w dzienniku.' % kod)
            self.odswiez()

        def gotowy(self):
            self.worker = None
            self.thread = None
            for b in self.aktywne:
                b.setEnabled(True)

        def closeEvent(self, event):
            if self.thread is not None:
                QMessageBox.information(self, 'UPS POD',
                     'Trwa operacja. Zamknij okno po jej zakończeniu.')
                event.ignore()
            else:
                event.accept()

    return Okno()


if __name__ == '__main__':
    sys.exit(main())

"""Testy end-to-end aplikacji UPS POD na symulatorze UPS API (aplikacja uruchamiana jako osobny proces)."""
import os, sys, shutil, subprocess, tempfile, time, json, glob, threading
from pathlib import Path
import openpyxl

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mock_ups as M

TU = Path(__file__).resolve().parent
ZRODLO = Path(os.environ.get('UPS_POD_DIR', str(TU.parent / 'UPS_POD')))
ORYG = TU / 'apka_oryginal.xlsx'   # plik użytkownika z 2026-10-02 (DATA + wzorzec CHECK)
WYNIKI = []


def ok(warunek, opis):
    WYNIKI.append((bool(warunek), opis))
    print(('PASS ' if warunek else 'FAIL ') + opis, flush=True)


def kopia_aplikacji():
    d = Path(tempfile.mkdtemp(prefix='upspod_')) / 'UPS_POD'
    d.mkdir()
    for n in ('ups_pod.py', 'ustawienia.ini', 'requirements.txt'):
        shutil.copy2(ZRODLO / n, d / n)
    shutil.copy2(ORYG, d / 'apka.xlsx')
    return d


def uruchom(app, port, *args, env_extra=None, wejscie=None):
    env = dict(os.environ, UPS_POD_ADRES_API='http://127.0.0.1:%d' % port, UPS_CLIENT_ID='TESTID',
               UPS_CLIENT_SECRET='TESTSECRET')
    if env_extra:
        env.update(env_extra)
    r = subprocess.run([os.environ.get("APP_PY", sys.executable), str(app / "ups_pod.py"), *args], capture_output=True, text=True,
                       env=env, input=wejscie, timeout=600)
    return r


def check(app):
    ws = openpyxl.load_workbook(app / 'apka.xlsx')['CHECK']
    return {r[2]: r for r in ws.iter_rows(min_row=3, max_col=6, values_only=True) if r[2]}


def ustaw_czekanie(app, minuty):
    p = app / 'ustawienia.ini'
    t = p.read_text(encoding='utf-8').replace('czekaj_minut = 10', 'czekaj_minut = %s' % minuty)
    p.write_text(t, encoding='utf-8')


S = {
    '1Z004R316825433131': 'dor_html',
    '1Z004R316814333779': 'dor_pdf',
    '1Z004R316815119786': 'w_drodze',
    '1Z004R316803829951': 'dor_bez_pod',
    '1Z004R316823132924': '404',
    '1Z004R316808804727': 'dor_gif',
    '1Z004R316832936101': 'dor_html',
    '1Z004R316825551496': 'ostrzezenie',
    '1Z004R316839236819': 'dor_html',
    '1Z004R316830928874': 'w_doreczeniu',
    '1Z004R316825503083': 'etykieta',
    '1Z004R316820163861': 'dor_pdf',
    '1Z004R316837115828': '500',
    '1Z004R316838024059': 'w_drodze',
}

srv = M.uruchom()
port = srv.server_address[1]
M.STAN['scenariusz'] = dict(S)
M.STAN['raz_429'] = {'1Z004R316808804727'}
M.STAN['raz_401'] = {'1Z004R316832936101'}

app = kopia_aplikacji()
ustaw_czekanie(app, 0)

# ── T1: pierwszy pełny przebieg ──
t0 = time.time()
r = uruchom(app, port, 'run', '--auto')
ok(r.returncode == 0, 'T1 kod wyjścia 0 (był %s) stderr=%s' % (r.returncode, r.stderr[-300:]))
ok(r.stdout == '', 'T1 tryb --auto nic nie wypisuje na konsolę (pythonw-safe)')
c = check(app)
ok(len(c) == 14, 'T1 CHECK ma 14 trackingów (jest %d)' % len(c))
pdfdir = app / 'POD'
oczek_pdf = {'1Z004R316825433131': 'FBA15MGCZZDB', '1Z004R316814333779': 'FBA15MGCZZDB', '1Z004R316808804727': 'FBA15MGCZZDB',
             '1Z004R316832936101': 'FBA15MGCZZDB', '1Z004R316839236819': 'FBA15MGCM33K', '1Z004R316820163861': 'FBA15MGCM33K'}
for t, sh in oczek_pdf.items():
    p = pdfdir / ('2026-09-15-%s' % sh) / ('2026-09-15-%s-%s.pdf' % (sh, t))
    ok(p.exists() and p.read_bytes()[:4] == b'%PDF', 'T1 PDF istnieje i jest PDF-em: %s' % p.relative_to(app))
    ok(c[t][3] == 'Doręczono' and str(c[t][5]).startswith('Pobrano i zapisano'), 'T1 D/F dla %s: %s | %s' % (t, c[t][3], c[t][5]))
ok(len(list(pdfdir.rglob('*.pdf'))) == 6, 'T1 dokładnie 6 PDF (jest %d)' % len(list(pdfdir.rglob('*.pdf'))))
ok(not list(pdfdir.rglob('.*.tmp')), 'T1 brak plików tymczasowych')
ok(c['1Z004R316815119786'][3] == 'W drodze' and 'Niedoręczona' in c['1Z004R316815119786'][5], 'T1 w drodze: %s | %s' % c['1Z004R316815119786'][3:6:2])
ok(c['1Z004R316830928874'][3] == 'Przesyłka jest w doręczeniu', 'T1 w doręczeniu: %s' % c['1Z004R316830928874'][3])
ok(c['1Z004R316803829951'][3] == 'Doręczono' and 'nie zwróciło dokumentu POD' in c['1Z004R316803829951'][5], 'T1 doręczono bez POD: %s' % c['1Z004R316803829951'][5])
ok(c['1Z004R316823132924'][3] == 'Nie znaleziono w UPS', 'T1 404 → Nie znaleziono w UPS')
ok(c['1Z004R316825551496'][3] == 'Nie znaleziono w UPS' and 'Tracking Information Not Found' in c['1Z004R316825551496'][5], 'T1 ostrzeżenie UPS → %s' % c['1Z004R316825551496'][5])
ok(c['1Z004R316837115828'][3] in (None, '') and 'błąd UPS API: HTTP 500' in c['1Z004R316837115828'][5], 'T1 HTTP 500 → %s' % c['1Z004R316837115828'][5])
ok(M.STAN['wywolania']['1Z004R316837115828'] == 4, 'T1 HTTP 500 ponowiony 3 razy (4 wywołania, było %d)' % M.STAN['wywolania']['1Z004R316837115828'])
ok(M.STAN['wywolania']['1Z004R316808804727'] == 2, 'T1 429 → ponowienie i sukces')
ok(M.STAN['wywolania']['1Z004R316832936101'] == 2 and M.STAN['n'] == 2, 'T1 401 → nowy token i sukces (tokenów: %d)' % M.STAN['n'])
ok(M.STAN['brak_naglowkow'] == 0 and M.STAN['brak_returnPOD'] == 0, 'T1 nagłówki transId/transactionSrc i parametry returnPOD/locale zgodne ze specyfikacją')
# tekst PDF z HTML (polskie znaki)
p = pdfdir / '2026-09-15-FBA15MGCZZDB' / '2026-09-15-FBA15MGCZZDB-1Z004R316825433131.pdf'
txt = subprocess.run(['pdftotext', str(p), '-'], capture_output=True, text=True).stdout
ok('ŻÓŁĆ Gęślą' in txt and '1Z004R316825433131' in txt, 'T1 PDF z HTML zawiera polskie znaki i numer: %r' % txt[:80])
p = pdfdir / '2026-09-15-FBA15MGCZZDB' / '2026-09-15-FBA15MGCZZDB-1Z004R316814333779.pdf'
ok(p.read_bytes() == M.MIN_PDF, 'T1 POD w formacie PDF zapisany bajt w bajt')
wb = openpyxl.load_workbook(app / 'apka.xlsx')
ok(wb['CHECK']['A1'].value == 'powielone' and wb['CHECK']['E2'].value.strip() == 'NAZWA PLIKU PDF', 'T1 wiersze 1–2 arkusza CHECK nienaruszone')
ok(wb['CHECK']['A3'].number_format.lower() == 'yyyy-mm-dd', 'T1 format daty w A: %s' % wb['CHECK']['A3'].number_format)
ok([c.value for c in wb['DATA'][2]] == [c.value for c in openpyxl.load_workbook(
    ORYG)['DATA'][2]], 'T1 arkusz DATA nienaruszony')
ok(len(list((app / 'dane' / 'kopie_arkusza').glob('*.xlsx'))) == 1, 'T1 kopia zapasowa arkusza przed zapisem')
print('   czas T1: %.1fs' % (time.time() - t0))

# ── T2: drugi przebieg – bez zapytań o przesyłki z PDF ──
przed = dict(M.STAN['wywolania'])
r = uruchom(app, port, 'run', '--auto')
nowe = {k: M.STAN['wywolania'].get(k, 0) - przed.get(k, 0) for k in S}
ok(r.returncode == 0, 'T2 kod 0')
ok(all(nowe[t] == 0 for t in oczek_pdf), 'T2 brak zapytań dla trackingów z PDF na dysku')
ok(nowe['1Z004R316815119786'] == 1 and nowe['1Z004R316803829951'] == 1, 'T2 niedoręczone i doręczone-bez-POD sprawdzone ponownie')
c2 = check(app)
ok(all(c2[t][5] == c[t][5] for t in oczek_pdf), 'T2 komentarze F dla pobranych bez zmian')

# ── T3: zmiana statusu w UPS → pobranie przy następnym uruchomieniu ──
M.STAN['scenariusz']['1Z004R316815119786'] = 'dor_html'
M.STAN['scenariusz']['1Z004R316803829951'] = 'dor_pdf'
r = uruchom(app, port, 'run', '--auto')
c3 = check(app)
ok(c3['1Z004R316815119786'][3] == 'Doręczono' and c3['1Z004R316815119786'][5].startswith('Pobrano i zapisano'), 'T3 przesyłka doręczona później → PDF pobrany')
ok(c3['1Z004R316803829951'][5].startswith('Pobrano i zapisano'), 'T3 POD udostępniony później → PDF pobrany')

# ── T4: plik otwarty w Excelu ──
M.STAN['scenariusz']['1Z004R316838024059'] = 'dor_html'
zamek = app / '~$apka.xlsx'
zamek.write_bytes(b'owner')
mtime = (app / 'apka.xlsx').stat().st_mtime
r = uruchom(app, port, 'run', '--auto')
ok(r.returncode == 3, 'T4 kod 3 przy otwartym pliku (był %s)' % r.returncode)
ok((app / 'apka.xlsx').stat().st_mtime == mtime, 'T4 plik Excel nie został zmieniony')
ok((app / 'POD' / '2026-09-15-FBA15MGCM33K' / '2026-09-15-FBA15MGCM33K-1Z004R316838024059.pdf').exists(), 'T4 PDF pobrany mimo otwartego pliku')
raporty = list((app / 'dane' / 'raporty').glob('*.csv'))
ok(raporty and '1Z004R316838024059' in raporty[0].read_text(encoding='utf-8-sig'), 'T4 wyniki zapisane w CSV')
zamek.unlink()
r = uruchom(app, port, 'run', '--auto')
c4 = check(app)
ok(r.returncode == 0 and c4['1Z004R316838024059'][5].startswith('Pobrano i zapisano'), 'T4 po zamknięciu pliku arkusz uzupełniony')

# ── T5: zmiana DATA – nowy wiersz w środku, nowy shipment, niepoprawny token ──
wb = openpyxl.load_workbook(app / 'apka.xlsx')
ws = wb['DATA']
ws.insert_rows(3)
import datetime as dt
ws['A3'] = dt.datetime(2026, 9, 15); ws['B3'] = 'FBA15MGCZZDB'; ws['C3'] = '1Z004R316899999991, 1Z004R316825433131 XX12'
ws['A12'] = '2026-09-20'; ws['B12'] = 'FBA15NOWYXXX'; ws['C12'] = '1Z004R316811111112\n1Z 004 R31 68 1111 1112'
wb.save(app / 'apka.xlsx')
M.STAN['scenariusz']['1Z004R316899999991'] = 'w_drodze'
M.STAN['scenariusz']['1Z004R316811111112'] = 'dor_html'
r = uruchom(app, port, 'run', '--auto')
c5 = check(app)
ws5 = openpyxl.load_workbook(app / 'apka.xlsx')['CHECK']
kol = [r_[2] for r_ in ws5.iter_rows(min_row=3, max_col=3, values_only=True) if r_[2]]
ok(len(kol) == 16 and kol.index('1Z004R316899999991') == 4, 'T5 nowy tracking dopisany w obrębie shipmentu (pozycja %s, razem %d)' % (kol.index('1Z004R316899999991'), len(kol)))
ok(all(c5[t][5] == c4[t][5] for t in oczek_pdf), 'T5 D/F istniejących wierszy zachowane po przebudowie')
ok((app / 'POD' / '2026-09-20-FBA15NOWYXXX' / '2026-09-20-FBA15NOWYXXX-1Z004R316811111112.pdf').exists(), 'T5 nowy shipment → nowy folder i PDF')
log = (app / 'dane' / 'logi').glob('*.log')
logtxt = ''.join(p.read_text(encoding='utf-8') for p in log)
ok('pominięto niepoprawne trackingi: XX12' in logtxt, 'T5 niepoprawny token zalogowany i pominięty')

# ── T6: złe klucze ──
r = uruchom(app, port, 'run', '--auto', env_extra={'UPS_CLIENT_SECRET': 'ZLE'})
ok(r.returncode == 2, 'T6 złe klucze → kod 2')
ok('UPS odrzucił logowanie (HTTP 401): 10401 ClientId is Invalid' in (app / 'dane' / 'logi').joinpath(
    sorted(os.listdir(app / 'dane' / 'logi'))[-1]).read_text(encoding='utf-8'), 'T6 czytelny komunikat w logu')

# ── T7: dwa przebiegi jednocześnie ──
M.STAN['scenariusz']['1Z004R316825503083'] = '500'   # wydłuża przebieg (ponowienia)
env = dict(os.environ, UPS_POD_ADRES_API='http://127.0.0.1:%d' % port, UPS_CLIENT_ID='TESTID', UPS_CLIENT_SECRET='TESTSECRET')
p1 = subprocess.Popen([sys.executable, str(app / 'ups_pod.py'), 'run'], env=env, stdout=subprocess.PIPE, text=True)
time.sleep(3)
r2 = uruchom(app, port, 'run')
p1.communicate(timeout=300)
ok(r2.returncode == 4 and 'już działa' in r2.stdout, 'T7 drugi równoczesny przebieg odrzucony (kod %s)' % r2.returncode)
ok(p1.returncode == 0, 'T7 pierwszy przebieg zakończony poprawnie')

# ── T8: test jednego trackingu ──
M.STAN['scenariusz']['1Z004R316877777773'] = 'dor_bez_pod'
r = uruchom(app, port, 'test', '1Z004R316877777773')
ok('Dokument POD w API:       NIE' in r.stdout and 'WNIOSEK' in r.stdout and 'brak (UPS nie widzi' in r.stdout, 'T8 test: doręczono bez POD – wniosek i relacja')
M.STAN['scenariusz']['1Z004R316877777774'] = 'dor_html'
r = uruchom(app, port, 'test', '1z004r31 6877777774')
ok('Dokument POD w API:       TAK (html' in r.stdout and 'Testowy PDF zapisany' in r.stdout and 'SHIPPER' in r.stdout, 'T8 test: POD HTML → testowy PDF')
print(r.stdout)

# ── T9: tryb interaktywny (menu) przez stdin ──
r = uruchom(app, port, 'menu', wejscie='4\n\n0\n')
ok('DATA: 3 shipment(ów), 16 tracking(ów) w CHECK.' in r.stdout and r.returncode == 0, 'T9 menu → opcja 4 (tylko CHECK) działa')

srv.shutdown()
nie = [o for w, o in WYNIKI if not w]
print('\n==== %d/%d testów zaliczonych ====' % (len(WYNIKI) - len(nie), len(WYNIKI)))
for o in nie:
    print('FAIL:', o)
print('folder testu:', app)

# CI musi przerwać publikację Release, jeśli choć jeden test nie przeszedł.
sys.exit(1 if nie else 0)

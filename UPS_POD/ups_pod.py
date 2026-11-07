#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
UPS POD – automat potwierdzeń doręczenia (Windows + macOS)
==========================================================

Plik Excel (domyślnie apka.xlsx w folderze aplikacji):

  Arkusz DATA  (wypełniasz Ty)
    A  DATA          np. 2026-09-15
    B  Shipment ID   np. FBA15MGCZZDB
    C  List UPS      trackingi rozdzielone ";" (także przecinek, spacja, nowa linia)

  Arkusz CHECK (wypełnia aplikacja)
    A  DATA          powielone z DATA
    B  Shipment ID   powielone z DATA
    C  List UPS      jeden tracking w wierszu, bez powtórzeń w obrębie shipmentu
    D  STATUS        aktualny status z UPS Tracking API
    E  NAZWA PLIKU   <DATA>-<Shipment ID>-<tracking>
    F  KOMENTARZ     czy PDF pobrano i zapisano na dysku

PDF: <folder_pdf>/<DATA>-<Shipment ID>/<NAZWA PLIKU>.pdf
Przesyłki niedoręczone są sprawdzane ponownie przy każdym uruchomieniu.
Przesyłki, których PDF już jest na dysku, nie są ponownie odpytywane.

Uruchamianie: START_WINDOWS.bat / START_MAC.command (menu) albo harmonogram systemowy.
Polecenia: start | menu | run [--auto] | check | test <tracking> | klucze |
           harmonogram-wlacz | harmonogram-wylacz | harmonogram-status
"""

import argparse
import base64
import configparser
import csv
import datetime as dt
import getpass
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import uuid
from collections import OrderedDict
from pathlib import Path

WERSJA = '1.1 (2026-10-12, GUI + binaria)'

# Wersja źródłowa zachowuje dotychczasowe ścieżki. Wersja spakowana
# przechowuje wszystkie dane zapisywalne w ~/UPS_POD, poza .app/_internal.
APP_DIR = Path(os.environ.get('UPS_POD_HOME', str(Path(__file__).resolve().parent)))
PLIK_USTAWIEN = APP_DIR / 'ustawienia.ini'
DANE_DIR = APP_DIR / 'dane'
PLIK_KLUCZY = DANE_DIR / 'klucze_ups.json'
LOG_DIR = DANE_DIR / 'logi'
KOPIE_DIR = DANE_DIR / 'kopie_arkusza'
RAPORTY_DIR = DANE_DIR / 'raporty'
TEST_DIR = DANE_DIR / 'test'
VENV_DIR = APP_DIR / '.venv'
ZNACZNIK_INSTALACJI = DANE_DIR / '.zainstalowano'

JEST_WINDOWS = os.name == 'nt'
JEST_MAC = sys.platform == 'darwin'

NAZWA_ZADANIA_WIN = 'UPS POD automat'
# dekodowanie komunikatów schtasks/launchctl (Windows: strona kodowa konsoli)
TEKST = {'text': True, 'errors': 'replace', 'encoding': 'oem' if os.name == 'nt' else None}
ETYKIETA_MAC = 'pl.upspod.automat'

NAGLOWKI_CHECK = ['DATA', 'Shipment ID', 'List UPS', 'STATUS', 'NAZWA PLIKU PDF', 'KOMENTARZ']
STATUS_DORECZONO = 'Doręczono'
WZOR_TRACKINGU = re.compile(r'^[0-9A-Z]{7,34}$')   # limit UPS Track API: 7–34 znaki
WZOR_1Z = re.compile(r'^1Z[0-9A-Z]{16}$')           # numer UPS 1Z: 18 znaków

DNI = {
    'poniedzialek': 1, 'poniedziałek': 1, 'wtorek': 2, 'sroda': 3, 'środa': 3,
    'czwartek': 4, 'piatek': 5, 'piątek': 5, 'sobota': 6, 'niedziela': 0,
}
DNI_WIN = {0: 'Sunday', 1: 'Monday', 2: 'Tuesday', 3: 'Wednesday', 4: 'Thursday', 5: 'Friday', 6: 'Saturday'}

log = logging.getLogger('ups_pod')


# ═══════════════════════════════ USTAWIENIA ═══════════════════════════════

DOMYSLNE = {
    'PLIKI': {'plik_excel': 'apka.xlsx', 'folder_pdf': 'POD'},
    'ARKUSZE': {'arkusz_dane': 'DATA', 'arkusz_check': 'CHECK'},
    'UPS': {
        'adres_api': 'https://onlinetools.ups.com',
        'jezyk': 'pl_PL',
        'numer_konta': '',
        'przerwa_miedzy_zapytaniami_s': '0.3',
    },
    'HARMONOGRAM': {'dni': 'wtorek, czwartek', 'godzina': '15:00'},
    'PLIK_OTWARTY': {'czekaj_minut': '10'},
}


def wczytaj_ustawienia():
    cp = configparser.ConfigParser(interpolation=None)
    cp.read_dict(DOMYSLNE)
    if PLIK_USTAWIEN.exists():
        cp.read(PLIK_USTAWIEN, encoding='utf-8-sig')
    u = {
        'plik_excel': sciezka(cp['PLIKI']['plik_excel']),
        'folder_pdf': sciezka(cp['PLIKI']['folder_pdf']),
        'arkusz_dane': cp['ARKUSZE']['arkusz_dane'].strip() or 'DATA',
        'arkusz_check': cp['ARKUSZE']['arkusz_check'].strip() or 'CHECK',
        'adres_api': os.environ.get('UPS_POD_ADRES_API') or cp['UPS']['adres_api'].strip().rstrip('/'),
        'jezyk': cp['UPS']['jezyk'].strip() or 'pl_PL',
        'numer_konta': re.sub(r'\s+', '', cp['UPS']['numer_konta']),
        'przerwa': float(cp['UPS']['przerwa_miedzy_zapytaniami_s'].replace(',', '.') or 0),
        'czekaj_minut': float(cp['PLIK_OTWARTY']['czekaj_minut'].replace(',', '.') or 0),
    }
    dni = []
    for d in re.split(r'[,;\s]+', cp['HARMONOGRAM']['dni'].strip().lower()):
        if not d:
            continue
        if d not in DNI:
            raise ValueError('Nieznany dzień w ustawienia.ini [HARMONOGRAM] dni: „%s”' % d)
        dni.append(DNI[d])
    if not dni:
        raise ValueError('ustawienia.ini [HARMONOGRAM] dni: podaj co najmniej jeden dzień.')
    m = re.match(r'^\s*(\d{1,2})[:.](\d{2})\s*$', cp['HARMONOGRAM']['godzina'])
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError('ustawienia.ini [HARMONOGRAM] godzina: użyj formatu GG:MM, np. 15:00')
    u['dni'] = sorted(set(dni))
    u['godzina'] = (int(m.group(1)), int(m.group(2)))
    return u


def sciezka(v):
    p = Path(os.path.expandvars(os.path.expanduser(str(v).strip().strip('"'))))
    return p if p.is_absolute() else (APP_DIR / p)


def opis_dni(dni):
    nazwy = {0: 'niedziela', 1: 'poniedziałek', 2: 'wtorek', 3: 'środa', 4: 'czwartek', 5: 'piątek', 6: 'sobota'}
    return ', '.join(nazwy[d] for d in dni)


# ═══════════════════════════════ KLUCZE UPS ═══════════════════════════════

def wczytaj_klucze():
    cid = os.environ.get('UPS_CLIENT_ID')
    sec = os.environ.get('UPS_CLIENT_SECRET')
    if cid and sec:
        return cid.strip(), sec.strip()
    if PLIK_KLUCZY.exists():
        try:
            j = json.loads(PLIK_KLUCZY.read_text(encoding='utf-8'))
            if j.get('client_id') and j.get('client_secret'):
                return j['client_id'].strip(), j['client_secret'].strip()
        except Exception:
            pass
    return None, None


def zapisz_klucze(cid, sec):
    DANE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PLIK_KLUCZY.with_suffix('.tmp')
    tmp.write_text(json.dumps({'client_id': cid, 'client_secret': sec}, indent=2), encoding='utf-8')
    if not JEST_WINDOWS:
        os.chmod(tmp, 0o600)
    os.replace(tmp, PLIK_KLUCZY)


# ═══════════════════════════════ UPS API ═══════════════════════════════

class BladUPS(Exception):
    pass


class KlientUPS:
    """Oficjalne UPS Tracking API (OAuth 2.0 client credentials)."""

    def __init__(self, adres, client_id, client_secret, jezyk='pl_PL', numer_konta='', przerwa=0.3):
        import requests  # instalowane w .venv
        self.requests = requests
        self.adres = adres.rstrip('/')
        self.cid = client_id
        self.sec = client_secret
        self.jezyk = jezyk
        self.konto = numer_konta
        self.przerwa = przerwa
        self.sesja = requests.Session()
        self._token = None
        self._token_do = 0
        self._ostatnie = 0
        self.licznik_zapytan = 0

    def token(self, wymus=False):
        if self._token and not wymus and time.time() < self._token_do:
            return self._token
        nagl = {'Content-Type': 'application/x-www-form-urlencoded'}
        if self.konto:
            nagl['x-merchant-id'] = self.konto
        try:
            r = self.sesja.post(self.adres + '/security/v1/oauth/token',
                                data={'grant_type': 'client_credentials'},
                                auth=(self.cid, self.sec), headers=nagl, timeout=30)
        except self.requests.RequestException as e:
            raise BladUPS('Brak połączenia z UPS (%s). Sprawdź internet.' % e.__class__.__name__)
        if r.status_code != 200:
            raise BladUPS('UPS odrzucił logowanie (HTTP %s): %s. Sprawdź Client ID i Client Secret.'
                          % (r.status_code, bledy_z_odpowiedzi(r)))
        j = r.json()
        self._token = j['access_token']
        try:
            waznosc = int(float(j.get('expires_in') or 3600))
        except ValueError:
            waznosc = 3600
        self._token_do = time.time() + max(60, waznosc - 300)
        return self._token

    def sledz(self, nr):
        """Zwraca (kod_http, json_lub_None, tekst). Ponawia przy 401/429/5xx/błędach sieci."""
        url = '%s/api/track/v1/details/%s' % (self.adres, nr)
        params = {'locale': self.jezyk, 'returnSignature': 'true', 'returnMilestones': 'false', 'returnPOD': 'true'}
        proby_401 = 0
        opoznienia = [2, 5, 15]
        proba = 0
        while True:
            odstep = time.time() - self._ostatnie
            if odstep < self.przerwa:
                time.sleep(self.przerwa - odstep)
            nagl = {
                'Authorization': 'Bearer ' + self.token(),
                'transId': uuid.uuid4().hex[:32],
                'transactionSrc': 'ups-pod-automat',
                'Accept': 'application/json',
            }
            try:
                self._ostatnie = time.time()
                self.licznik_zapytan += 1
                r = self.sesja.get(url, params=params, headers=nagl, timeout=45)
            except self.requests.RequestException as e:
                if proba < len(opoznienia):
                    time.sleep(opoznienia[proba])
                    proba += 1
                    continue
                return 0, None, 'Brak połączenia z UPS (%s)' % e.__class__.__name__
            if r.status_code == 401 and proby_401 == 0:
                proby_401 += 1
                self.token(wymus=True)
                continue
            if r.status_code in (429, 500, 502, 503, 504) and proba < len(opoznienia):
                czekaj = opoznienia[proba]
                ra = r.headers.get('Retry-After')
                if ra and ra.isdigit():
                    czekaj = min(60, int(ra))
                time.sleep(czekaj)
                proba += 1
                continue
            try:
                js = r.json()
            except ValueError:
                js = None
            return r.status_code, js, r.text


def bledy_z_odpowiedzi(r):
    try:
        j = r.json()
        errs = (j.get('response') or {}).get('errors') or []
        if errs:
            return '; '.join('%s %s' % (e.get('code', ''), e.get('message', '')) for e in errs)
    except Exception:
        pass
    t = (r.text or '').strip()
    return t[:200] if t else '(pusta odpowiedź)'


def analizuj(kod, js, tekst):
    """Sprowadza odpowiedź UPS do słownika używanego przez aplikację."""
    w = {'ok': False, 'nie_znaleziono': False, 'blad': '', 'doreczono': False, 'opis': '',
         'pod': None, 'pod_typ': '', 'podpis': False, 'relacja': [], 'data_dor': '', 'godz_dor': '',
         'odebral': '', 'miejsce': '', 'status': '', 'status_typ': '',
         'ship_date': '', 'ship_date_source': ''}
    if kod == 200 and isinstance(js, dict) and 'trackResponse' in js:
        shipy = (js.get('trackResponse') or {}).get('shipment') or []
        sh = shipy[0] if shipy else {}
        w['relacja'] = sh.get('userRelation') or []
        paczki = sh.get('package') or []
        if not paczki:
            ostrz = '; '.join(x.get('message', '') for x in (sh.get('warnings') or []) if isinstance(x, dict))
            w['nie_znaleziono'] = True
            w['blad'] = 'UPS: ' + (ostrz or 'brak danych przesyłki')
            return w
        p = paczki[0]
        cs = p.get('currentStatus') or {}
        akt = p.get('activity') or []
        st0 = (akt[0].get('status') if akt and isinstance(akt[0], dict) else None) or {}
        daty = p.get('deliveryDate') or []
        del_data = next((d for d in daty if isinstance(d, dict) and d.get('type') == 'DEL'), None)
        typ = str(cs.get('type') or st0.get('type') or '').upper()
        w['doreczono'] = typ == 'D' or str(st0.get('type', '')).upper() == 'D' or del_data is not None
        w['status_typ'] = typ
        w['status'] = {'D': 'Doręczono', 'I': 'W drodze', 'M': 'Etykieta utworzona',
                       'MV': 'Etykieta anulowana', 'X': 'Wyjątek w doręczeniu',
                       'U': 'Aktualizacja terminu'}.get(typ, '')
        w['opis'] = str(cs.get('description') or cs.get('simplifiedTextDescription') or st0.get('description')
                        or p.get('statusDescription') or 'Brak opisu statusu').strip()
        di = p.get('deliveryInformation') or {}
        w['odebral'] = di.get('receivedBy') or ''
        w['miejsce'] = di.get('location') or ''
        w['podpis'] = bool((di.get('signature') or {}).get('image'))
        tresc = (di.get('pod') or {}).get('content')
        if tresc:
            try:
                s = re.sub(r'\s+', '', tresc)
                w['pod'] = base64.b64decode(s + '=' * (-len(s) % 4))
                w['pod_typ'] = rozpoznaj_typ(w['pod'])
            except Exception:
                w['pod'] = None
        # Explicit shipment-level pickup date from the real UPS Tracking response.
        pickup_date = data_ups(sh.get('pickupDate'))
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', pickup_date):
            w['ship_date'] = pickup_date
            w['ship_date_source'] = 'ups_pickup_date'
        # Physical first acceptance by UPS, not the label/manifest creation date.
        if not w['ship_date']:
            for activity in reversed(akt):
                if not isinstance(activity, dict):
                    continue
                ac_status = activity.get('status') or {}
                if str(ac_status.get('code') or '').upper() in {'PU', 'OR', 'PC', 'PT'}:
                    date = data_ups(activity.get('date'))
                    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', date):
                        w['ship_date'] = date
                        w['ship_date_source'] = 'ups_pickup_scan'
                        break
        # Some original UPS POD HTML contains "Shipped or Billed on".
        # This is a billed/shipped date, NOT guaranteed physical handover.
        if not w['ship_date'] and w['pod_typ'] == 'html' and w['pod']:
            from html import unescape
            visible = unescape(re.sub(r'<[^>]*>', ' ', dekoduj_tekst(w['pod'])))
            matched = re.search(
                r'Shipped\s+or\s+Billed\s+on\s*:\s*([A-Za-z]{3}\s+\d{1,2},\s*\d{4})',
                visible, re.IGNORECASE,
            )
            if matched:
                try:
                    w['ship_date'] = dt.datetime.strptime(
                        matched.group(1).replace('  ', ' ').strip(), '%b %d, %Y'
                    ).date().isoformat()
                    w['ship_date_source'] = 'pod_shipped_or_billed'
                except ValueError:
                    pass
        if del_data:
            w['data_dor'] = data_ups(del_data.get('date'))
        elif w['doreczono'] and akt:
            w['data_dor'] = data_ups(akt[0].get('date'))
        dt_ = p.get('deliveryTime') or {}
        if dt_.get('type') == 'DEL':
            w['godz_dor'] = czas_ups(dt_.get('endTime'))
        w['ok'] = True
        return w
    if kod == 404:
        w['nie_znaleziono'] = True
    if kod == 0:
        w['blad'] = tekst
    else:
        errs = ((js or {}).get('response') or {}).get('errors') if isinstance(js, dict) else None
        opis = '; '.join('%s %s' % (e.get('code', ''), e.get('message', '')) for e in errs) if errs else (tekst or '')[:200]
        w['blad'] = 'HTTP %s: %s' % (kod, opis)
    return w


def data_ups(s):
    s = str(s or '')
    return '%s-%s-%s' % (s[:4], s[4:6], s[6:8]) if re.match(r'^\d{8}$', s) else s


def czas_ups(s):
    s = str(s or '')
    return '%s:%s' % (s[:2], s[2:4]) if re.match(r'^\d{6}$', s) else s


def rozpoznaj_typ(b):
    if b[:4] == b'%PDF':
        return 'pdf'
    if b[:3] == b'GIF':
        return 'gif'
    if b[:8] == b'\x89PNG\r\n\x1a\n':
        return 'png'
    if b[:2] == b'\xff\xd8':
        return 'jpeg'
    poczatek = b[:2000].lower()
    if b'<html' in poczatek or b'<!doctype' in poczatek or b'<table' in poczatek or b'<body' in poczatek or b'<div' in poczatek:
        return 'html'
    return 'tekst'


# ═══════════════════════════════ PDF ═══════════════════════════════

class KonwerterPDF:
    """HTML → PDF przez przeglądarkę Chromium (Playwright). Zapasowo: Edge / Chrome z systemu."""

    def __init__(self):
        self._pw = None
        self._przegladarka = None

    def _uruchom(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        ostatni = None
        for kanal in (None, 'msedge', 'chrome'):
            try:
                self._przegladarka = self._pw.chromium.launch(channel=kanal) if kanal else self._pw.chromium.launch()
                return
            except Exception as e:
                ostatni = e
        # Systemowy Chromium to ostatnia droga ratunkowa (m.in. Linux w testach).
        for nazwa in ('chromium', 'chromium-browser'):
            sciezka = shutil.which(nazwa)
            if sciezka:
                try:
                    self._przegladarka = self._pw.chromium.launch(executable_path=sciezka)
                    return
                except Exception as e:
                    ostatni = e
        # Po błędzie nie pozostawiaj aktywnego Playwright/greenleta.
        self.zamknij()
        raise RuntimeError('Nie można uruchomić przeglądarki do tworzenia PDF: %s' % ostatni)

    def html_na_pdf(self, html, cel):
        if self._przegladarka is None:
            self._uruchom()
        strona = self._przegladarka.new_page()
        try:
            # UPS POD HTML is untrusted; never load remote resources or leak its contents.
            strona.route('**/*', lambda route: route.abort())
            try:
                strona.set_content(html, wait_until='load', timeout=30000)
            except Exception:
                strona.set_content(html, wait_until='domcontentloaded', timeout=30000)
            strona.pdf(path=str(cel), format='A4', print_background=True,
                       margin={'top': '12mm', 'bottom': '12mm', 'left': '10mm', 'right': '10mm'})
        finally:
            strona.close()

    def zamknij(self):
        try:
            if self._przegladarka:
                self._przegladarka.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        finally:
            self._przegladarka = None
            self._pw = None


def dekoduj_tekst(b):
    for kod in ('utf-8', 'cp1252', 'latin-1'):
        try:
            return b.decode(kod)
        except UnicodeDecodeError:
            continue
    return b.decode('utf-8', 'replace')


def uzupelnij_grafiki_pod(html):
    """Resolve relative UPS graphics without external requests or modifying original HTML."""
    logo_file = Path(__file__).with_name('ups-logo.svg')
    if logo_file.is_file():
        logo = 'data:image/svg+xml;base64,' + base64.b64encode(logo_file.read_bytes()).decode('ascii')
        html = re.sub(r'(?i)(<img\b[^>]*\bsrc\s*=\s*["\'])\./?ups_logo\.gif(["\'])',
                      lambda m: m.group(1) + logo + m.group(2), html)
    else:
        html = re.sub(r'(?i)<img\b[^>]*\bsrc\s*=\s*["\']\./?ups_logo\.gif["\'][^>]*>',
                      '<span style="font-weight:bold">UPS</span>', html)
    # The historical UPS banner image is not supplied by this endpoint. Use
    # explicit text rather than a broken picture or a fabricated UPS graphic.
    html = re.sub(
        r'(?i)<img\b[^>]*\bsrc\s*=\s*["\']\./?ups_banner\.gif["\'][^>]*>',
        '<span style="display:inline-block;border-bottom:2px solid #704214;'
        'padding:9px 13px;font:bold 19px Arial;color:#704214">United Parcel Service</span>',
        html,
    )
    return html


def zapisz_pod(bajty, typ, cel, konwerter):
    """Zapisuje dokument POD z UPS jako PDF (zapis atomowy). Zwraca opis rodzaju."""
    cel.parent.mkdir(parents=True, exist_ok=True)
    tmp = cel.with_name('.' + cel.name + '.tmp')
    try:
        if typ == 'pdf':
            tmp.write_bytes(bajty)
            rodzaj = 'PDF z UPS'
        else:
            if typ in ('gif', 'png', 'jpeg'):
                html = ('<html><head><meta charset="utf-8"></head><body style="margin:0">'
                        '<img style="max-width:100%%" src="data:image/%s;base64,%s"></body></html>'
                        % (typ, base64.b64encode(bajty).decode('ascii')))
                rodzaj = 'obraz POD z UPS → PDF'
            elif typ == 'html':
                html = uzupelnij_grafiki_pod(dekoduj_tekst(bajty))
                rodzaj = 'POD z UPS (HTML) → PDF'
            else:
                t = dekoduj_tekst(bajty)
                html = '<html><head><meta charset="utf-8"></head><body><pre style="white-space:pre-wrap">%s</pre></body></html>' % (
                    t.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))
                rodzaj = 'POD z UPS (tekst) → PDF'
            konwerter.html_na_pdf(html, tmp)
        if not tmp.exists() or tmp.stat().st_size == 0:
            raise RuntimeError('pusty plik PDF')
        os.replace(tmp, cel)
        return rodzaj
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def bezpieczna_nazwa(s):
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', '_', str(s)).strip()
    return s.rstrip('. ') or '_'


# ═══════════════════════════════ EXCEL ═══════════════════════════════

def normalizuj_date(v):
    """Zwraca (date, 'RRRR-MM-DD') albo (None, '')."""
    if v is None or v == '':
        return None, ''
    if isinstance(v, dt.datetime):
        d = v.date()
        return d, d.isoformat()
    if isinstance(v, dt.date):
        return v, v.isoformat()
    if isinstance(v, (int, float)) and 20000 < float(v) < 80000:      # liczba seryjna Excela
        d = (dt.datetime(1899, 12, 30) + dt.timedelta(days=float(v))).date()
        return d, d.isoformat()
    s = str(v).strip()
    for wzor in ('%Y-%m-%d', '%d.%m.%Y', '%d-%m-%Y', '%Y.%m.%d', '%Y/%m/%d', '%d/%m/%Y'):
        try:
            d = dt.datetime.strptime(s[:10], wzor).date()
            return d, d.isoformat()
        except ValueError:
            continue
    return None, s


def rozbij_trackingi(v):
    """Zwraca (lista_poprawnych, lista_odrzuconych)."""
    if v is None:
        return [], []
    dobre, zle = [], []
    for fragment in re.split(r'[;,\r\n]+', str(v)):
        razem = re.sub(r'[^0-9A-Za-z]', '', fragment).upper()
        if not razem:
            continue
        # „1Z 004 R31 68 2543 3131” (numer UPS ze spacjami) = jeden tracking
        if WZOR_1Z.match(razem) or (WZOR_TRACKINGU.match(razem) and len(fragment.split()) == 1):
            dobre.append(razem)
            continue
        for kawalek in fragment.split():        # kilka trackingów rozdzielonych spacją
            t = re.sub(r'[^0-9A-Za-z]', '', kawalek).upper()
            if t:
                (dobre if WZOR_TRACKINGU.match(t) else zle).append(t)
    return dobre, zle


def czytaj_dane(ws):
    """DATA → OrderedDict[(data_str, shipment)] = {'data': date|None, 'trackingi': [unikalne, kolejność]}."""
    grupy = OrderedDict()
    uwagi = []
    ost_data, ost_ship = (None, ''), ''
    for nr, (a, b, c) in enumerate(ws.iter_rows(min_row=2, max_col=3, values_only=True), start=2):
        if a in (None, '') and b in (None, '') and c in (None, ''):
            continue
        d = normalizuj_date(a)
        s = str(b).strip() if b not in (None, '') else ''
        if d[1]:
            ost_data = d
        else:
            d = ost_data
        if s:
            ost_ship = s
        else:
            s = ost_ship
        dobre, zle = rozbij_trackingi(c)
        if zle:
            uwagi.append('DATA wiersz %d: pominięto niepoprawne trackingi: %s' % (nr, ', '.join(zle)))
        if not dobre:
            continue
        if not d[1] or not s:
            uwagi.append('DATA wiersz %d: brak daty lub Shipment ID – wiersz pominięty' % nr)
            continue
        if d[0] is None:
            uwagi.append('DATA wiersz %d: data „%s” nie jest w formacie RRRR-MM-DD' % (nr, d[1]))
        g = grupy.setdefault((d[1], s), {'data': d[0], 'trackingi': []})
        for t in dobre:
            if t not in g['trackingi']:
                g['trackingi'].append(t)
    return grupy, uwagi


def wiersze_check(grupy):
    out = []
    for (data_s, ship), g in grupy.items():
        for t in g['trackingi']:
            nazwa = bezpieczna_nazwa('%s-%s-%s' % (data_s, ship, t))
            out.append({'data': g['data'], 'data_s': data_s, 'shipment': ship, 'tracking': t,
                        'nazwa': nazwa, 'folder': bezpieczna_nazwa('%s-%s' % (data_s, ship)),
                        'status': '', 'komentarz': ''})
    return out


def wiersz_naglowka(ws):
    for r in range(1, 11):
        v = ws.cell(r, 3).value
        if isinstance(v, str) and v.strip().lower() == 'list ups':
            return r
    return None


def przygotuj_check(wb, nazwa):
    from openpyxl.styles import Font, PatternFill
    if nazwa in wb.sheetnames:
        ws = wb[nazwa]
    else:
        ws = wb.create_sheet(nazwa)
    hdr = wiersz_naglowka(ws)
    if hdr is None:
        hdr = 2
        for i, n in enumerate(NAGLOWKI_CHECK, start=1):
            c = ws.cell(hdr, i, n)
            c.font = Font(bold=True)
            c.fill = PatternFill('solid', fgColor='FFFF00' if i <= 3 else '00FFFF')
        for kol, szer in zip('ABCDEF', (14, 17, 22, 34, 48, 70)):
            ws.column_dimensions[kol].width = szer
    else:
        # uzupełnij brakujące nagłówki D–F, nie zmieniając istniejących
        for i in range(4, 7):
            if ws.cell(hdr, i).value in (None, ''):
                ws.cell(hdr, i, NAGLOWKI_CHECK[i - 1]).font = Font(bold=True)
    return ws, hdr


def czytaj_stary_check(ws, hdr):
    stare = {}
    for a, b, c, d, e, f in ws.iter_rows(min_row=hdr + 1, max_col=6, values_only=True):
        if c in (None, ''):
            continue
        klucz = (normalizuj_date(a)[1], str(b or '').strip(), str(c).strip().upper())
        stare[klucz] = (d if d is not None else '', f if f is not None else '')
    return stare


def zapisz_check(ws, hdr, wiersze):
    ostatni = max(ws.max_row, hdr)
    for r in range(hdr + 1, ostatni + 1):
        for k in range(1, 7):
            ws.cell(r, k).value = None
    for i, w in enumerate(wiersze):
        r = hdr + 1 + i
        ca = ws.cell(r, 1, w['data'] if w['data'] else w['data_s'])
        if w['data']:
            ca.number_format = 'yyyy-mm-dd'
        ws.cell(r, 2, w['shipment'])
        ws.cell(r, 3, w['tracking'])
        ws.cell(r, 4, w['status'] or None)
        ws.cell(r, 5, w['nazwa'])
        ws.cell(r, 6, w['komentarz'] or None)


def plik_otwarty(p):
    """True, jeśli Excel (Windows/Mac) ma plik otwarty."""
    for n in ('~$' + p.name, '~$' + p.name[2:]):
        if len(n) > 2 and (p.parent / n).exists():
            return True
    if JEST_WINDOWS and p.exists():
        try:
            with open(p, 'r+b'):
                pass
        except PermissionError:
            return True
        except OSError:
            pass
    return False


def zrob_kopie(p, ile=15):
    KOPIE_DIR.mkdir(parents=True, exist_ok=True)
    cel = KOPIE_DIR / ('%s_%s%s' % (p.stem, dt.datetime.now().strftime('%Y%m%d_%H%M%S'), p.suffix))
    shutil.copy2(p, cel)
    kopie = sorted(KOPIE_DIR.glob(p.stem + '_*' + p.suffix))
    for stara in kopie[:-ile]:
        try:
            stara.unlink()
        except OSError:
            pass


def zapisz_skoroszyt(wb, p):
    zrob_kopie(p)
    tmp = p.with_name('.' + p.stem + '.zapis' + p.suffix)
    wb.save(tmp)
    for proba in range(5):
        try:
            os.replace(tmp, p)
            return
        except PermissionError:
            time.sleep(2)
    tmp.unlink(missing_ok=True)
    raise PermissionError('Nie można nadpisać pliku %s (otwarty w innym programie?)' % p.name)


# ═══════════════════════════════ BLOKADA JEDNEGO PRZEBIEGU ═══════════════════════════════

class Blokada:
    def __init__(self, p):
        self.p = p
        self.f = None

    def __enter__(self):
        self.p.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(self.p, 'a+')
        try:
            if JEST_WINDOWS:
                import msvcrt
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.f.close()
            self.f = None
            raise RuntimeError('Aplikacja już działa (np. z harmonogramu). Spróbuj za kilka minut.')
        return self

    def __exit__(self, *a):
        if self.f:
            try:
                if JEST_WINDOWS:
                    import msvcrt
                    self.f.seek(0)
                    msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            self.f.close()


# ═══════════════════════════════ GŁÓWNY PRZEBIEG ═══════════════════════════════

def teraz():
    return dt.datetime.now().strftime('%Y-%m-%d %H:%M')


def wypisz(msg, auto):
    log.info(msg)
    if not auto:
        print(msg, flush=True)


def przebieg(tryb='run', auto=False):
    """tryb: 'check' (tylko arkusz CHECK) albo 'run' (CHECK + UPS + PDF). Zwraca kod wyjścia."""
    try:
        with Blokada(DANE_DIR / '.przebieg.lock'):
            return _przebieg(tryb, auto)
    except RuntimeError as e:
        wypisz(str(e), auto)
        return 4


def _przebieg(tryb, auto):
    import openpyxl
    u = wczytaj_ustawienia()
    xlsx = u['plik_excel']
    wypisz('──── %s  start (%s) ────' % (teraz(), 'pełny' if tryb == 'run' else 'tylko CHECK'), auto)
    if not xlsx.exists():
        wypisz('BŁĄD: nie ma pliku Excel: %s (ustawienia.ini → plik_excel)' % xlsx, auto)
        return 1

    klient = None
    if tryb == 'run':
        cid, sec = wczytaj_klucze()
        if not cid:
            wypisz('BŁĄD: brak kluczy UPS API. Menu → 1 (Wpisz klucze UPS API).', auto)
            return 1
        klient = KlientUPS(u['adres_api'], cid, sec, u['jezyk'], u['numer_konta'], u['przerwa'])
        try:
            klient.token()
        except BladUPS as e:
            wypisz('BŁĄD: %s' % e, auto)
            return 2

    # Plik otwarty w Excelu → czekaj
    czekano = 0.0
    limit = u['czekaj_minut'] * 60
    if plik_otwarty(xlsx):
        wypisz('Plik %s jest otwarty w Excelu – czekam na zamknięcie (do %d min)…' % (xlsx.name, u['czekaj_minut']), auto)
        while plik_otwarty(xlsx) and czekano < limit:
            time.sleep(30)
            czekano += 30

    wb = openpyxl.load_workbook(xlsx)
    if u['arkusz_dane'] not in wb.sheetnames:
        wypisz('BŁĄD: w pliku nie ma arkusza „%s”. Arkusze: %s' % (u['arkusz_dane'], ', '.join(wb.sheetnames)), auto)
        return 1
    grupy, uwagi = czytaj_dane(wb[u['arkusz_dane']])
    for x in uwagi:
        wypisz('Uwaga: ' + x, auto)
    wiersze = wiersze_check(grupy)
    ws, hdr = przygotuj_check(wb, u['arkusz_check'])
    stare = czytaj_stary_check(ws, hdr)
    for w in wiersze:
        s = stare.get((w['data_s'], w['shipment'], w['tracking']))
        if s:
            w['status'], w['komentarz'] = s
    wypisz('DATA: %d shipment(ów), %d tracking(ów) w CHECK.' % (len(grupy), len(wiersze)), auto)

    stat = {'zapisano': 0, 'juz_bylo': 0, 'niedoreczone': 0, 'bez_pod': 0, 'bledy': 0, 'zapytania': 0}
    if tryb == 'run':
        folder_pdf = u['folder_pdf']
        konwerter = KonwerterPDF()
        pamiec = {}
        try:
            for i, w in enumerate(wiersze, start=1):
                cel = folder_pdf / w['folder'] / (w['nazwa'] + '.pdf')
                wzgl = '%s/%s' % (w['folder'], cel.name)
                if cel.exists() and cel.stat().st_size > 0:
                    w['status'] = STATUS_DORECZONO
                    if not str(w['komentarz']).startswith('Pobrano i zapisano'):
                        kiedy = dt.datetime.fromtimestamp(cel.stat().st_mtime).strftime('%Y-%m-%d %H:%M')
                        w['komentarz'] = 'Pobrano i zapisano (%s): %s' % (kiedy, wzgl)
                    stat['juz_bylo'] += 1
                    wypisz('[%d/%d] %s → PDF już jest na dysku' % (i, len(wiersze), w['tracking']), auto)
                    continue

                if w['tracking'] not in pamiec:
                    pamiec[w['tracking']] = analizuj(*klient.sledz(w['tracking']))
                a = pamiec[w['tracking']]
                znak = teraz()

                if not a['ok']:
                    stat['bledy'] += 1
                    if a['nie_znaleziono']:
                        w['status'] = 'Nie znaleziono w UPS'
                        dop = ''
                        if grupy[(w['data_s'], w['shipment'])]['data'] and \
                                (dt.date.today() - grupy[(w['data_s'], w['shipment'])]['data']).days > 120:
                            dop = ' UPS przechowuje dane przez 120 dni – przesyłka może być starsza.'
                        w['komentarz'] = 'PDF niepobrany: %s.%s Sprawdzono %s.' % (a['blad'], dop, znak)
                    else:
                        w['komentarz'] = 'PDF niepobrany – błąd UPS API: %s. Ponowię przy następnym uruchomieniu (%s).' % (a['blad'], znak)
                    wypisz('[%d/%d] %s → %s' % (i, len(wiersze), w['tracking'], a['blad']), auto)
                    continue

                if not a['doreczono']:
                    w['status'] = a['opis']
                    w['komentarz'] = 'Niedoręczona – PDF zostanie pobrany po doręczeniu (sprawdzono %s).' % znak
                    stat['niedoreczone'] += 1
                    wypisz('[%d/%d] %s → %s' % (i, len(wiersze), w['tracking'], a['opis']), auto)
                    continue

                w['status'] = STATUS_DORECZONO
                kiedy_dor = (' %s %s' % (a['data_dor'], a['godz_dor'])).rstrip()
                if not a['pod']:
                    w['komentarz'] = ('Doręczono%s, ale UPS API nie zwróciło dokumentu POD – PDF niepobrany, '
                                      'ponowię przy następnym uruchomieniu (%s).' % (kiedy_dor, znak))
                    stat['bez_pod'] += 1
                    wypisz('[%d/%d] %s → Doręczono, UPS nie zwrócił dokumentu POD' % (i, len(wiersze), w['tracking']), auto)
                    continue
                try:
                    rodzaj = zapisz_pod(a['pod'], a['pod_typ'], cel, konwerter)
                    w['komentarz'] = 'Pobrano i zapisano (%s): %s [%s]' % (znak, wzgl, rodzaj)
                    stat['zapisano'] += 1
                    wypisz('[%d/%d] %s → Doręczono, PDF zapisany: %s' % (i, len(wiersze), w['tracking'], wzgl), auto)
                except Exception as e:
                    stat['bledy'] += 1
                    w['komentarz'] = 'Doręczono%s – błąd zapisu PDF: %s. Ponowię przy następnym uruchomieniu (%s).' % (kiedy_dor, e, znak)
                    wypisz('[%d/%d] %s → błąd zapisu PDF: %s' % (i, len(wiersze), w['tracking'], e), auto)
        finally:
            konwerter.zamknij()
        stat['zapytania'] = klient.licznik_zapytan

    zapisz_check(ws, hdr, wiersze)

    # Zapis arkusza
    kod = 0
    if plik_otwarty(xlsx):
        raport = zapisz_raport_csv(wiersze)
        wypisz('Plik %s nadal jest otwarty w Excelu – arkusza NIE zmieniono. PDF-y są zapisane. '
               'Wyniki: %s. Arkusz uzupełni się przy następnym uruchomieniu.' % (xlsx.name, raport), auto)
        kod = 3
    else:
        try:
            zapisz_skoroszyt(wb, xlsx)
            wypisz('Zapisano arkusz %s w pliku %s.' % (u['arkusz_check'], xlsx.name), auto)
        except Exception as e:
            raport = zapisz_raport_csv(wiersze)
            wypisz('Nie udało się zapisać pliku Excel (%s). Wyniki: %s' % (e, raport), auto)
            kod = 3

    if tryb == 'run':
        wypisz('Podsumowanie: zapisane PDF: %(zapisano)d | PDF były już wcześniej: %(juz_bylo)d | '
               'niedoręczone: %(niedoreczone)d | doręczone bez POD z UPS: %(bez_pod)d | błędy: %(bledy)d | '
               'zapytania do UPS: %(zapytania)d' % stat, auto)
    wypisz('──── %s  koniec ────' % teraz(), auto)
    return kod


def zapisz_raport_csv(wiersze):
    RAPORTY_DIR.mkdir(parents=True, exist_ok=True)
    p = RAPORTY_DIR / ('wynik_%s.csv' % dt.datetime.now().strftime('%Y%m%d_%H%M'))
    with open(p, 'w', newline='', encoding='utf-8-sig') as f:
        wr = csv.writer(f, delimiter=';')
        wr.writerow(NAGLOWKI_CHECK)
        for w in wiersze:
            wr.writerow([w['data_s'], w['shipment'], w['tracking'], w['status'], w['nazwa'], w['komentarz']])
    return p


# ═══════════════════════════════ TEST JEDNEGO TRACKINGU ═══════════════════════════════

def test_trackingu(nr):
    dobre, _ = rozbij_trackingi(nr)
    if not dobre:
        print('To nie wygląda na numer UPS: %s' % nr)
        return 1
    nr = dobre[0]
    u = wczytaj_ustawienia()
    cid, sec = wczytaj_klucze()
    if not cid:
        print('Brak kluczy UPS API. Najpierw opcja 1 w menu.')
        return 1
    k = KlientUPS(u['adres_api'], cid, sec, u['jezyk'], u['numer_konta'], 0)
    try:
        kod, js, tekst = k.sledz(nr)
    except BladUPS as e:
        print('BŁĄD: %s' % e)
        return 2
    TEST_DIR.mkdir(parents=True, exist_ok=True)
    surowe = TEST_DIR / ('%s_odpowiedz_ups.json' % nr)
    surowe.write_text(json.dumps(js, ensure_ascii=False, indent=2) if js is not None else (tekst or ''), encoding='utf-8')
    a = analizuj(kod, js, tekst)
    print()
    print('Tracking:                 %s' % nr)
    print('Odpowiedź UPS (HTTP):     %s' % kod)
    if not a['ok']:
        print('Wynik:                    %s' % a['blad'])
        print('Pełna odpowiedź zapisana: %s' % surowe)
        return 2
    print('Status UPS:               %s' % a['opis'])
    print('Doręczono:                %s' % ('TAK' if a['doreczono'] else 'NIE'))
    if a['doreczono']:
        print('Data/godzina doręczenia:  %s %s' % (a['data_dor'] or '—', a['godz_dor']))
        print('Odebrał(a) / miejsce:     %s / %s' % (a['odebral'] or '—', a['miejsce'] or '—'))
    print('Relacja konta z paczką:   %s' % (', '.join(a['relacja']) if a['relacja'] else 'brak (UPS nie widzi związku Twojego konta z tą przesyłką)'))
    print('Dokument POD w API:       %s' % ('TAK (%s, %d B)' % (a['pod_typ'], len(a['pod'])) if a['pod'] else 'NIE'))
    print('Obraz podpisu w API:      %s' % ('TAK' if a['podpis'] else 'NIE'))
    if a['pod']:
        cel = TEST_DIR / ('%s_POD_test.pdf' % nr)
        k2 = KonwerterPDF()
        try:
            rodzaj = zapisz_pod(a['pod'], a['pod_typ'], cel, k2)
            print('Testowy PDF zapisany:     %s  [%s]' % (cel, rodzaj))
        except Exception as e:
            print('Nie udało się zapisać testowego PDF: %s' % e)
        finally:
            k2.zamknij()
    elif a['doreczono']:
        print()
        print('WNIOSEK: UPS API nie udostępnia dokumentu POD dla tej przesyłki Twojemu kontu.')
        print('Specyfikacja UPS: POD „może być zwrócony tylko użytkownikowi, który ma prawa do przesyłki”.')
    print('Pełna odpowiedź zapisana: %s' % surowe)
    return 0


# ═══════════════════════════════ HARMONOGRAM ═══════════════════════════════

def python_venv(okno=False):
    # Harmonogram zainstalowanej aplikacji uruchamia własną binarkę bez Pythona.
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).absolute()
    if JEST_WINDOWS:
        p = VENV_DIR / 'Scripts' / ('pythonw.exe' if okno else 'python.exe')
    else:
        p = VENV_DIR / 'bin' / 'python'
    return p if p.exists() else Path(sys.executable)


def xml_zadania_windows(dni, godzina, uzytkownik):
    from xml.sax.saxutils import escape
    dzisiaj = dt.date.today()
    start = dt.datetime(dzisiaj.year, dzisiaj.month, dzisiaj.day, godzina[0], godzina[1]).strftime('%Y-%m-%dT%H:%M:%S')
    dni_xml = ''.join('<%s />' % DNI_WIN[d] for d in dni)
    return '''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>UPS POD: statusy przesylek i pobieranie potwierdzen doreczenia (ups_pod.py)</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{start}</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByWeek>
        <DaysOfWeek>{dni}</DaysOfWeek>
        <WeeksInterval>1</WeeksInterval>
      </ScheduleByWeek>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT2H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{cmd}</Command>
      <Arguments>{args}</Arguments>
      <WorkingDirectory>{wd}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
'''.format(start=start, dni=dni_xml, user=escape(uzytkownik),
           cmd=escape(str(python_venv(okno=True))),
           args=escape('run --auto' if getattr(sys, 'frozen', False) else '"%s" run --auto' % (APP_DIR / 'ups_pod.py')),
           wd=escape(str(APP_DIR)))


def plist_mac(dni, godzina):
    import plistlib
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return plistlib.dumps({
        'Label': ETYKIETA_MAC,
        'ProgramArguments': ([str(python_venv()), 'run', '--auto'] if getattr(sys, 'frozen', False) else
                             [str(python_venv()), str(APP_DIR / 'ups_pod.py'), 'run', '--auto']),
        'WorkingDirectory': str(APP_DIR),
        'StartCalendarInterval': [{'Weekday': d, 'Hour': godzina[0], 'Minute': godzina[1]} for d in dni],
        'StandardOutPath': str(LOG_DIR / 'harmonogram_mac.log'),
        'StandardErrorPath': str(LOG_DIR / 'harmonogram_mac.log'),
        'RunAtLoad': False,
        'ProcessType': 'Background',
    })


def sciezka_plist():
    return Path.home() / 'Library' / 'LaunchAgents' / (ETYKIETA_MAC + '.plist')


def ostrzezenie_tcc_mac():
    if not JEST_MAC:
        return ''
    chronione = [Path.home() / n for n in ('Desktop', 'Documents', 'Downloads')]
    try:
        u = wczytaj_ustawienia()
        sprawdz = [APP_DIR, u['plik_excel'], u['folder_pdf']]
    except Exception:
        sprawdz = [APP_DIR]
    for p in sprawdz:
        for c in chronione:
            try:
                p.resolve().relative_to(c.resolve())
                return ('UWAGA (macOS): %s leży w folderze %s. macOS blokuje programom działającym w tle '
                        'dostęp do Biurka, Dokumentów i Pobranych. Przenieś folder UPS_POD do folderu domowego '
                        '(%s), inaczej harmonogram nie zadziała.' % (p, c.name, Path.home() / 'UPS_POD'))
            except ValueError:
                continue
    return ''


def harmonogram_wlacz():
    u = wczytaj_ustawienia()
    opis = '%s, godz. %02d:%02d' % (opis_dni(u['dni']), u['godzina'][0], u['godzina'][1])
    if JEST_WINDOWS:
        uz = os.environ.get('USERDOMAIN', '') + '\\' + os.environ.get('USERNAME', getpass.getuser())
        DANE_DIR.mkdir(parents=True, exist_ok=True)
        plik = DANE_DIR / 'zadanie_windows.xml'
        plik.write_text(xml_zadania_windows(u['dni'], u['godzina'], uz), encoding='utf-16')
        r = subprocess.run(['schtasks', '/Create', '/TN', NAZWA_ZADANIA_WIN, '/XML', str(plik), '/F'],
                           capture_output=True, **TEKST)
        if r.returncode != 0:
            print('Nie udało się utworzyć zadania w Harmonogramie zadań:\n%s%s' % (r.stdout, r.stderr))
            return 1
        print('Harmonogram włączony: %s (Harmonogram zadań Windows, zadanie „%s”).' % (opis, NAZWA_ZADANIA_WIN))
        print('Komputer musi być włączony i zalogowany. Jeśli o tej porze był wyłączony, zadanie wykona się po włączeniu.')
        return 0
    if JEST_MAC:
        ost = ostrzezenie_tcc_mac()
        if ost:
            print(ost)
            return 1
        p = sciezka_plist()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(plist_mac(u['dni'], u['godzina']))
        uid = str(os.getuid())
        subprocess.run(['launchctl', 'bootout', 'gui/%s/%s' % (uid, ETYKIETA_MAC)], capture_output=True)
        r = subprocess.run(['launchctl', 'bootstrap', 'gui/%s' % uid, str(p)], capture_output=True, **TEKST)
        if r.returncode != 0:
            r = subprocess.run(['launchctl', 'load', '-w', str(p)], capture_output=True, **TEKST)
        if r.returncode != 0:
            print('Nie udało się włączyć harmonogramu (launchd):\n%s%s' % (r.stdout, r.stderr))
            return 1
        print('Harmonogram włączony: %s (launchd, %s).' % (opis, p))
        print('Mac musi być włączony i zalogowany. Jeśli o tej porze był uśpiony, zadanie wykona się po wybudzeniu.')
        return 0
    print('Harmonogram obsługiwany jest na Windows i macOS. Na tym systemie użyj crona: '
          '"0 15 * * 2,4 %s %s run --auto"' % (python_venv(), APP_DIR / 'ups_pod.py'))
    return 1


def harmonogram_wylacz():
    if JEST_WINDOWS:
        r = subprocess.run(['schtasks', '/Delete', '/TN', NAZWA_ZADANIA_WIN, '/F'], capture_output=True, **TEKST)
        print('Harmonogram wyłączony.' if r.returncode == 0 else 'Zadania nie było lub nie udało się go usunąć.')
        return 0
    if JEST_MAC:
        uid = str(os.getuid())
        subprocess.run(['launchctl', 'bootout', 'gui/%s/%s' % (uid, ETYKIETA_MAC)], capture_output=True)
        p = sciezka_plist()
        if p.exists():
            p.unlink()
        print('Harmonogram wyłączony.')
        return 0
    return 1


def harmonogram_status():
    if JEST_WINDOWS:
        r = subprocess.run(['schtasks', '/Query', '/TN', NAZWA_ZADANIA_WIN], capture_output=True, **TEKST)
        return 'włączony' if r.returncode == 0 else 'wyłączony'
    if JEST_MAC:
        if not sciezka_plist().exists():
            return 'wyłączony'
        r = subprocess.run(['launchctl', 'print', 'gui/%s/%s' % (os.getuid(), ETYKIETA_MAC)], capture_output=True)
        return 'włączony' if r.returncode == 0 else 'plik jest, ale launchd go nie wczytał – włącz ponownie'
    return 'nieobsługiwany na tym systemie'


# ═══════════════════════════════ INSTALACJA (start) ═══════════════════════════════

def w_venv():
    try:
        return Path(sys.prefix).resolve() == VENV_DIR.resolve()
    except OSError:
        return False


def instaluj():
    """Tworzy .venv i instaluje biblioteki. Uruchamiane przez START_WINDOWS.bat / START_MAC.command."""
    if sys.version_info < (3, 9):
        print('Potrzebny Python 3.9 lub nowszy (masz %s). Zainstaluj z python.org.' % platform.python_version())
        return False
    wymagania = APP_DIR / 'requirements.txt'
    skrot = hashlib.sha256(wymagania.read_bytes()).hexdigest()[:16] + sys.version.split()[0]
    py = python_venv()
    if ZNACZNIK_INSTALACJI.exists() and ZNACZNIK_INSTALACJI.read_text().strip() == skrot and py != Path(sys.executable):
        return True
    print('Pierwsze uruchomienie – instaluję potrzebne biblioteki (2–5 min, wymaga internetu)…', flush=True)
    if not (VENV_DIR / ('Scripts' if JEST_WINDOWS else 'bin')).exists():
        import venv
        venv.EnvBuilder(with_pip=True, clear=False).create(str(VENV_DIR))
    py = python_venv()
    pip = [str(py), '-m', 'pip', 'install', '--disable-pip-version-check', '-q']
    subprocess.run(pip + ['--upgrade', 'pip'], check=False)
    r = subprocess.run(pip + ['openpyxl>=3.1.2', 'requests>=2.31'])
    if r.returncode != 0:
        print('BŁĄD: nie udało się zainstalować bibliotek openpyxl/requests. Sprawdź internet i uruchom ponownie.')
        return False
    r = subprocess.run(pip + ['playwright>=1.49'])
    if r.returncode != 0:
        print('UWAGA: nie zainstalowano Playwright – konwersja POD w formacie HTML do PDF nie będzie działać.')
    else:
        print('Pobieram silnik PDF (Chromium, ok. 150 MB)…', flush=True)
        r = subprocess.run([str(py), '-m', 'playwright', 'install', 'chromium'])
        if r.returncode != 0:
            print('UWAGA: nie pobrano Chromium – aplikacja spróbuje użyć Edge/Chrome z systemu.')
    DANE_DIR.mkdir(parents=True, exist_ok=True)
    ZNACZNIK_INSTALACJI.write_text(skrot)
    print('Instalacja zakończona.\n', flush=True)
    return True


def start():
    if not w_venv():
        if not instaluj():
            pauza()
            return 1
        r = subprocess.run([str(python_venv()), str(APP_DIR / 'ups_pod.py'), 'menu'])
        return r.returncode
    return menu()


# ═══════════════════════════════ MENU ═══════════════════════════════

def pauza():
    try:
        input('\nNaciśnij Enter, aby kontynuować…')
    except EOFError:
        pass


def ustaw_klucze_interaktywnie():
    print('\nKlucze znajdziesz na developer.ups.com → My Apps → Twoja aplikacja (Client ID, Client Secret).')
    cid = input('Wklej Client ID i naciśnij Enter: ').strip()
    if not cid:
        print('Przerwano.')
        return 1
    sec = getpass.getpass('Wklej Client Secret (nie będzie widoczny) i naciśnij Enter: ').strip()
    if not sec:
        print('Przerwano.')
        return 1
    print('Client Secret: %d znaków, kończy się na …%s' % (len(sec), sec[-4:]))
    u = wczytaj_ustawienia()
    try:
        KlientUPS(u['adres_api'], cid, sec, u['jezyk'], u['numer_konta'], 0).token()
    except BladUPS as e:
        print('Logowanie NIE powiodło się: %s' % e)
        odp = input('Zapisać klucze mimo to? (t/n): ').strip().lower()
        if odp != 't':
            return 1
    else:
        print('Logowanie do UPS API działa.')
    zapisz_klucze(cid, sec)
    print('Klucze zapisane w %s' % PLIK_KLUCZY)
    return 0


def otworz_folder(p):
    p.mkdir(parents=True, exist_ok=True)
    if JEST_WINDOWS:
        os.startfile(str(p))
    elif JEST_MAC:
        subprocess.run(['open', str(p)])
    else:
        print(p)


def menu():
    konfiguruj_log(auto=False)
    while True:
        try:
            u = wczytaj_ustawienia()
        except ValueError as e:
            print('Błąd w ustawienia.ini: %s' % e)
            pauza()
            return 1
        cid, _ = wczytaj_klucze()
        print('\n' + '═' * 66)
        print(' UPS POD – automat potwierdzeń doręczenia   wersja %s' % WERSJA)
        print('═' * 66)
        print(' Plik Excel : %s' % u['plik_excel'])
        print(' Folder PDF : %s' % u['folder_pdf'])
        print(' Klucze UPS : %s' % (('zapisane (Client ID …%s)' % cid[-4:]) if cid else 'BRAK – wybierz 1'))
        print(' Harmonogram: %s (%s, %02d:%02d)' % (harmonogram_status(), opis_dni(u['dni']), u['godzina'][0], u['godzina'][1]))
        ost = ostrzezenie_tcc_mac()
        if ost:
            print('\n ' + ost)
        print('─' * 66)
        print(' 1  Wpisz / zmień klucze UPS API (i sprawdź logowanie)')
        print(' 2  Test jednego trackingu (czy UPS zwraca dokument POD)')
        print(' 3  Uruchom teraz: CHECK + statusy + pobieranie PDF')
        print(' 4  Tylko odśwież arkusz CHECK (bez łączenia z UPS)')
        print(' 5  Włącz harmonogram')
        print(' 6  Wyłącz harmonogram')
        print(' 7  Otwórz folder z PDF')
        print(' 0  Wyjście')
        try:
            wybor = input('\nWybierz numer i naciśnij Enter: ').strip()
        except EOFError:
            return 0
        if wybor == '0':
            return 0
        if wybor not in ('1', '2', '3', '4', '5', '6', '7'):
            continue
        try:
            if wybor == '1':
                ustaw_klucze_interaktywnie()
            elif wybor == '2':
                nr = input('Podaj numer trackingu UPS (najlepiej DORĘCZONEJ przesyłki): ').strip()
                test_trackingu(nr)
            elif wybor == '3':
                przebieg('run', auto=False)
            elif wybor == '4':
                przebieg('check', auto=False)
            elif wybor == '5':
                harmonogram_wlacz()
            elif wybor == '6':
                harmonogram_wylacz()
            elif wybor == '7':
                otworz_folder(u['folder_pdf'])
                continue
        except Exception as e:
            log.exception('Błąd w menu')
            print('Nieoczekiwany błąd: %s (szczegóły w %s)' % (e, LOG_DIR))
        pauza()


# ═══════════════════════════════ LOG + CLI ═══════════════════════════════

def konfiguruj_log(auto):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    plik = LOG_DIR / ('ups_pod_%s.log' % dt.date.today().strftime('%Y-%m'))
    h = logging.FileHandler(plik, encoding='utf-8')
    h.setFormatter(logging.Formatter('%(asctime)s %(message)s', '%Y-%m-%d %H:%M:%S'))
    log.handlers[:] = [h]
    log.setLevel(logging.INFO)


def main(argv=None):
    ap = argparse.ArgumentParser(description='UPS POD – automat potwierdzeń doręczenia')
    sub = ap.add_subparsers(dest='cmd')
    sub.add_parser('start')
    sub.add_parser('menu')
    r = sub.add_parser('run')
    r.add_argument('--auto', action='store_true', help='tryb harmonogramu (bez pytań, tylko log)')
    sub.add_parser('check')
    t = sub.add_parser('test')
    t.add_argument('tracking')
    sub.add_parser('klucze')
    sub.add_parser('harmonogram-wlacz')
    sub.add_parser('harmonogram-wylacz')
    sub.add_parser('harmonogram-status')
    a = ap.parse_args(argv)

    if a.cmd in (None, 'start'):
        return start()
    if a.cmd == 'menu':
        return menu()
    konfiguruj_log(auto=getattr(a, 'auto', False))
    try:
        if a.cmd == 'run':
            return przebieg('run', auto=a.auto)
        if a.cmd == 'check':
            return przebieg('check', auto=False)
        if a.cmd == 'test':
            return test_trackingu(a.tracking)
        if a.cmd == 'klucze':
            return ustaw_klucze_interaktywnie()
        if a.cmd == 'harmonogram-wlacz':
            return harmonogram_wlacz()
        if a.cmd == 'harmonogram-wylacz':
            return harmonogram_wylacz()
        if a.cmd == 'harmonogram-status':
            print(harmonogram_status())
            return 0
    except Exception as e:  # zapis każdego nieprzewidzianego błędu do logu (tryb harmonogramu nie ma okna)
        log.exception('Nieoczekiwany błąd')
        if not getattr(a, 'auto', False):
            print('Nieoczekiwany błąd: %s (szczegóły w %s)' % (e, LOG_DIR))
        return 9
    return 0


if __name__ == '__main__':
    sys.exit(main())

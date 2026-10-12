"""Symulator UPS API zgodny ze specyfikacją UPS-API/api-documentation (Tracking.yaml, OAuthClientCredentials.yaml)."""
import base64, json, sys, threading, re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ID, SECRET = 'TESTID', 'TESTSECRET'
STAN = {'tokeny': set(), 'n': 0, 'wywolania': {}, 'raz_429': set(), 'raz_401': set(), 'brak_naglowkow': 0,
        'brak_returnPOD': 0, 'scenariusz': {}}

POD_HTML = ('<!DOCTYPE html><html><head><meta charset="utf-8"><title>Proof of Delivery</title></head><body>'
            '<h1>Potwierdzenie doręczenia</h1><p>Numer przesyłki: {nr}</p><p>Odebrał: ŻÓŁĆ Gęślą – magazyn</p>'
            '<p>Doręczono: 2026-09-18 10:42</p></body></html>')
GIF_1PX = base64.b64decode('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7')
MIN_PDF = (b'%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n'
           b'3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n')


def paczka(nr, typ, opis, pod=None, podpis=False, del_data='20260918'):
    p = {'trackingNumber': nr,
         'currentStatus': {'type': typ, 'code': 'KB' if typ == 'D' else 'IT', 'description': opis,
                           'simplifiedTextDescription': opis, 'statusCode': '011' if typ == 'D' else '005'},
         'activity': [{'date': del_data, 'time': '104200', 'status': {'type': typ, 'description': opis},
                       'location': {'address': {'city': 'Bad Hersfeld', 'countryCode': 'DE'}}}],
         'deliveryDate': [{'type': 'DEL' if typ == 'D' else 'SDD', 'date': del_data}]}
    if typ == 'D':
        p['deliveryTime'] = {'type': 'DEL', 'endTime': '104200'}
        di = {'receivedBy': 'MAGAZYN', 'location': 'DOCK'}
        if podpis:
            di['signature'] = {'image': base64.b64encode(GIF_1PX).decode()}
        if pod is not None:
            di['pod'] = {'content': base64.b64encode(pod).decode()}
        p['deliveryInformation'] = di
    return p


def odpowiedz(nr):
    sc = STAN['scenariusz'].get(nr, 'w_drodze')
    rel = []
    if sc == 'dor_html':
        pk = paczka(nr, 'D', 'Doręczono', POD_HTML.format(nr=nr).encode('utf-8'), True); rel = ['SHIPPER']
    elif sc == 'dor_pdf':
        pk = paczka(nr, 'D', 'Doręczono', MIN_PDF, True); rel = ['SHIPPER']
    elif sc == 'dor_gif':
        pk = paczka(nr, 'D', 'Doręczono', GIF_1PX)
    elif sc == 'dor_bez_pod':
        pk = paczka(nr, 'D', 'Doręczono')
    elif sc == 'w_doreczeniu':
        pk = paczka(nr, 'I', 'Przesyłka jest w doręczeniu')
    elif sc == 'etykieta':
        pk = paczka(nr, 'M', 'Etykieta wysyłkowa utworzona, firma UPS oczekuje na przesyłkę')
    elif sc == 'ostrzezenie':
        return 200, {'trackResponse': {'shipment': [{'inquiryNumber': nr, 'warnings': [
            {'code': 'TW0001', 'message': 'Tracking Information Not Found'}]}]}}
    elif sc == '404':
        return 404, {'response': {'errors': [{'code': 'TV1002', 'message': 'Tracking number not found'}]}}
    elif sc == '500':
        return 500, {'response': {'errors': [{'code': '9999', 'message': 'Internal error'}]}}
    else:
        pk = paczka(nr, 'I', 'W drodze')
    return 200, {'trackResponse': {'shipment': [{'inquiryNumber': nr, 'userRelation': rel, 'package': [pk]}]}}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, kod, obj):
        b = json.dumps(obj).encode()
        self.send_response(kod)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        if self.path != '/security/v1/oauth/token':
            return self._json(404, {})
        dl = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(dl).decode()
        auth = self.headers.get('Authorization', '')
        ok = auth == 'Basic ' + base64.b64encode(f'{ID}:{SECRET}'.encode()).decode()
        if 'grant_type=client_credentials' not in body:
            return self._json(400, {'response': {'errors': [{'code': '10400', 'message': 'Invalid grant'}]}})
        if not ok:
            return self._json(401, {'response': {'errors': [{'code': '10401', 'message': 'ClientId is Invalid'}]}})
        STAN['n'] += 1
        t = 'tok%d' % STAN['n']
        STAN['tokeny'].add(t)
        self._json(200, {'token_type': 'Bearer', 'access_token': t, 'expires_in': '14399', 'status': 'approved'})

    def do_GET(self):
        u = urlparse(self.path)
        m = re.match(r'^/api/track/v1/details/([0-9A-Z]+)$', u.path)
        if not m:
            return self._json(404, {})
        nr = m.group(1)
        q = parse_qs(u.query)
        if not self.headers.get('transId') or not self.headers.get('transactionSrc'):
            STAN['brak_naglowkow'] += 1
            return self._json(400, {'response': {'errors': [{'code': '10002', 'message': 'Missing transId'}]}})
        if q.get('returnPOD') != ['true'] or q.get('locale') != ['pl_PL']:
            STAN['brak_returnPOD'] += 1
        tok = self.headers.get('Authorization', '').replace('Bearer ', '')
        STAN['wywolania'][nr] = STAN['wywolania'].get(nr, 0) + 1
        if nr in STAN['raz_401']:
            STAN['raz_401'].discard(nr)
            STAN['tokeny'].discard(tok)
        if tok not in STAN['tokeny']:
            return self._json(401, {'response': {'errors': [{'code': '250002', 'message': 'Invalid Authentication Information.'}]}})
        if nr in STAN['raz_429']:
            STAN['raz_429'].discard(nr)
            return self._json(429, {'response': {'errors': [{'code': '429', 'message': 'Too Many Requests'}]}})
        kod, obj = odpowiedz(nr)
        self._json(kod, obj)


def uruchom(port=0):
    s = ThreadingHTTPServer(('127.0.0.1', port), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s

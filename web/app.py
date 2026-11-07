"""UPS POD browser UI. Secrets are supplied only through Docker environment."""
from __future__ import annotations
import datetime as dt
import hmac
import os
import re
import secrets
import shutil
import threading
from pathlib import Path
from urllib.parse import urlparse

import openpyxl
from flask import Flask, Response, abort, flash, jsonify, redirect, render_template_string, request, send_file, session, url_for
import ups_pod as core

HOME = Path(os.environ.get("UPS_POD_HOME", "/data"))
HOME.mkdir(parents=True, exist_ok=True)
SETTINGS = HOME / "ustawienia.ini"
if not SETTINGS.exists():
    shutil.copyfile("/app/ustawienia.ini", SETTINGS)

keyfile = HOME / ".web_session_key"
if not keyfile.exists():
    try:
        fd = os.open(keyfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as out:
            out.write(secrets.token_urlsafe(48))
    except FileExistsError:
        pass

app = Flask(__name__)
app.config.update(
    SECRET_KEY=keyfile.read_text().strip(),
    MAX_CONTENT_LENGTH=12 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PREFERRED_URL_SCHEME="https",
)
HOST = os.environ.get("PUBLIC_HOST", "ups.xlap.pl").lower()
JOB = {"running": False, "started": None, "finished": None, "exit_code": None, "error": None}
JOB_LOCK = threading.Lock()

PAGE = r"""
<!DOCTYPE html><html lang="pl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>UPS POD | WDT</title>
<style>
:root{font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;color:#16263b;background:#f3f6fb}
*{box-sizing:border-box}body{margin:0}.wrap{max-width:1260px;padding:26px;margin:0 auto}
header{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:22px}
h1{font-size:25px;letter-spacing:-.04em;margin:0}h2{font-size:17px;margin:0 0 14px}p{line-height:1.5}
small,.muted{color:#66758c}.pill{border-radius:99px;padding:6px 12px;background:#e1e9f6;font-size:12px}
.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:16px}
.stat,.panel{background:#fff;border:1px solid #dce4ef;border-radius:13px;box-shadow:0 1px 3px #132e4a0a}
.stat{padding:16px}.stat strong{font-size:27px;display:block}.stat span{color:#69778a;font-size:13px}
.two{display:grid;grid-template-columns:1.2fr 1fr;gap:15px}.panel{padding:20px;margin-bottom:16px}
label{display:block;font-weight:600;font-size:13px;margin:10px 0 5px}input,textarea{border:1px solid #cbd5e1;border-radius:8px;padding:10px;font:inherit;width:100%;background:white}
textarea{min-height:76px;resize:vertical}.fields{display:grid;grid-template-columns:1fr 1fr;gap:10px}
button,.btn{font:inherit;font-size:13px;font-weight:650;cursor:pointer;padding:10px 14px;background:#174a8b;border:1px solid #174a8b;border-radius:8px;color:#fff;text-decoration:none;display:inline-block}
.btn.secondary,button.secondary{background:#fff;color:#174a8b}.toolbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
button:disabled{opacity:.4;cursor:not-allowed}.notice{background:#fff3db;border:1px solid #f0daaa;border-radius:8px;padding:10px;margin-bottom:14px}
.flash{background:#e6f2ee;border:1px solid #c5e3d9;padding:10px;border-radius:8px;margin-bottom:10px}
.table-wrap{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:13px}th{text-align:left;background:#f8fafc;color:#536579;font-weight:650}
th,td{padding:11px;border-bottom:1px solid #e6edf5;vertical-align:top}td{overflow-wrap:anywhere}
a{color:#1261a7}.tag{font-size:11px;border-radius:5px;background:#edf1f7;padding:4px 6px;display:inline-block}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#111f31;color:#d6e4ef;padding:14px;border-radius:9px;font-size:12px;max-height:260px;overflow:auto}
@media(max-width:800px){.stats{grid-template-columns:repeat(2,1fr)}.two,.fields{grid-template-columns:1fr}header{align-items:flex-start;flex-direction:column}}
</style></head><body><div class="wrap">
<header><div><h1>UPS POD <span class="muted">/ WDT</span></h1><small>Archiwizacja potwierdzeń doręczenia • UPS Tracking API</small></div>
<span class="pill">Cloudflare Access • {{ hostname }}</span></header>
{% with notes=get_flashed_messages() %}{% for n in notes %}<div class="flash">{{n}}</div>{% endfor %}{% endwith %}
{% if not credentials %}<div class="notice"><b>Brak konfiguracji UPS.</b> Wypełnij UPS_CLIENT_ID i UPS_CLIENT_SECRET w <code>/volume2/docker/ups-pod/.env</code>, a następnie uruchom ponownie kontenery.</div>{% endif %}
<div class="stats"><div class="stat"><strong>{{ rows|length }}</strong><span>Numery przesyłek</span></div>
<div class="stat"><strong>{{ delivered }}</strong><span>Potwierdzone doręczenia</span></div>
<div class="stat"><strong>{{ pdf_count }}</strong><span>Zarchiwizowane POD</span></div>
<div class="stat"><strong>{{ pending }}</strong><span>W kolejce / bez POD</span></div></div>
<div class="two"><section class="panel"><h2>Nowa grupa przesyłek</h2>
<form action="{{url_for('add_shipments')}}" method="post"><input type="hidden" name="csrf" value="{{csrf}}">
<div class="fields"><div><label>Data wysyłki</label><input name="date" type="date" value="{{today}}" required></div>
<div><label>Shipment ID / numer referencyjny</label><input name="shipment" placeholder="FBA... lub nr faktury" maxlength="120" required></div></div>
<label>Numery UPS (oddziel średnikiem, przecinkiem lub nową linią)</label>
<textarea name="trackings" placeholder="1Z..." required></textarea>
<div class="toolbar" style="margin-top:10px"><button type="submit">Dodaj przesyłki</button><small>Wpis trafia do arkusza DATA</small></div></form>
</section><section class="panel"><h2>Operacje</h2>
<div class="toolbar"><form method="post" action="{{url_for('run_scan')}}"><input type="hidden" name="csrf" value="{{csrf}}"><button {% if job.running or not credentials %}disabled{% endif %}>Sprawdź i pobierz POD</button></form>
<a href="{{url_for('export_excel')}}" class="btn secondary">Pobierz Excel</a></div>
<p class="muted" id="job">Automat: {% if job.running %}przetwarzanie trwa{% else %}bezczynny{% endif %}. Harmonogram: wtorek i czwartek, 15:00 (Europe/Warsaw).</p>
<hr style="border:0;border-top:1px solid #e6edf5;margin:19px 0">
<h2>Import arkusza XLSX</h2><form action="{{url_for('upload_excel')}}" method="post" enctype="multipart/form-data">
<input type="hidden" name="csrf" value="{{csrf}}"><input type="file" name="file" accept=".xlsx" required style="margin-bottom:10px"><button class="secondary">Zastąp arkusz (z kopią zapasową)</button></form>
<small>Wymagany arkusz DATA; oryginał jest archiwizowany przed zastąpieniem.</small></section></div>
<section class="panel"><div class="toolbar" style="justify-content:space-between"><h2>Przesyłki — DATA / CHECK</h2><input id="filter" placeholder="Filtruj tracking, status, Shipment ID..." style="max-width:360px" oninput="filterRows()"></div>
<div class="table-wrap"><table id="shipments"><thead><tr><th>Data</th><th>Shipment ID</th><th>UPS tracking</th><th>Status</th><th>Dokument POD</th><th>Komentarz</th></tr></thead><tbody>
{% for r in rows %}<tr><td>{{r.date}}</td><td>{{r.shipment}}</td><td><code>{{r.tracking}}</code></td><td><span class="tag">{{r.status or 'Oczekuje'}}</span></td>
<td>{% if r.pdf %}<a href="{{url_for('download_pod',folder=r.folder,filename=r.filename)}}">Pobierz PDF</a>{% else %}—{% endif %}</td><td class="muted">{{r.comment}}</td></tr>{% else %}<tr><td colspan="6">Brak przesyłek. Dodaj numery lub zaimportuj arkusz.</td></tr>{% endfor %}
</tbody></table></div></section>
<section class="panel"><h2>Ostatnie zdarzenia</h2><pre>{{log_text}}</pre></section>
</div><script>
function filterRows(){let t=document.getElementById('filter').value.toLowerCase();document.querySelectorAll('#shipments tbody tr').forEach(r=>r.style.display=r.textContent.toLowerCase().includes(t)?'':'none')}
setInterval(async()=>{try{let r=await fetch('/api/status',{credentials:'same-origin'});if(r.ok){let j=await r.json();document.getElementById('job').textContent='Automat: '+(j.running?'przetwarzanie trwa':'bezczynny')+(j.finished?', ostatnie zakończenie: '+j.finished:'');if(window.lastRunning && !j.running)location.reload();window.lastRunning=j.running}}catch(e){}},6000)
</script></body></html>
"""

def workbook_path() -> Path:
    return core.wczytaj_ustawienia()["plik_excel"]

def ensure_workbook():
    path = workbook_path()
    if not path.exists():
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "DATA"
        ws.append(["DATA", "Shipment ID", "List UPS"])
        wb.create_sheet("CHECK")
        wb.save(path)

def csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]

@app.before_request
def guard():
    actual = (request.host or "").split(":")[0].lower()
    if actual not in {HOST, "localhost", "127.0.0.1"}:
        abort(400, "Invalid host")
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        if request.content_length and request.content_length > app.config["MAX_CONTENT_LENGTH"]:
            abort(413)
        origin = request.headers.get("Origin") or request.headers.get("Referer") or ""
        if origin and urlparse(origin).hostname != actual:
            abort(403)
        supplied = request.form.get("csrf") or request.headers.get("X-CSRF-Token") or ""
        if not hmac.compare_digest(str(supplied), str(session.get("csrf", ""))) or not supplied:
            abort(403, "Invalid CSRF token")

def rows_for_page():
    ensure_workbook()
    wb = openpyxl.load_workbook(workbook_path(), read_only=True, data_only=True)
    try:
        groups, _ = core.czytaj_dane(wb["DATA"])
        rows = core.wiersze_check(groups)
        old = {}
        if "CHECK" in wb:
            ws = wb["CHECK"]
            hdr = core.wiersz_naglowka(ws)
            if hdr is not None:
                old = core.czytaj_stary_check(ws, hdr)
        root = core.wczytaj_ustawienia()["folder_pdf"]
        for item in rows:
            item["date"] = item["data_s"]
            item["filename"] = item["nazwa"] + ".pdf"
            p = root / item["folder"] / item["filename"]
            item["pdf"] = p.is_file() and p.stat().st_size > 4 and p.open("rb").read(4) == b"%PDF"
            item["status"], item["comment"] = old.get((item["data_s"], item["shipment"], item["tracking"]), ("", ""))
        return rows
    finally:
        wb.close()

def logs_tail():
    files = sorted(core.LOG_DIR.glob("ups_pod_*.log"))
    if not files:
        return "Brak logów. Uruchom sprawdzanie UPS."
    with files[-1].open(encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    return "".join(lines[-35:])[-5500:]

@app.get("/healthz")
def health():
    return jsonify(ok=True, service="ups-pod"), 200

@app.get("/")
def index():
    rows = rows_for_page()
    return render_template_string(PAGE, rows=rows, pdf_count=sum(bool(r["pdf"]) for r in rows),
        delivered=sum(r["status"] == core.STATUS_DORECZONO for r in rows),
        pending=sum(not r["pdf"] for r in rows),
        csrf=csrf_token(), today=dt.date.today().isoformat(), job=dict(JOB),
        credentials=bool(os.environ.get("UPS_CLIENT_ID") and os.environ.get("UPS_CLIENT_SECRET")),
        hostname=HOST, log_text=logs_tail())

@app.post("/shipments")
def add_shipments():
    date = request.form.get("date", "")
    shipment = request.form.get("shipment", "").strip()
    trackings, rejected = core.rozbij_trackingi(request.form.get("trackings", ""))
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) or not shipment or not trackings:
        abort(400, "Podaj datę, Shipment ID i poprawne numery UPS")
    try:
        dt.date.fromisoformat(date)
    except ValueError:
        abort(400, "Nieprawidłowa data")
    if len(shipment) > 120 or len(trackings) > 500 or len(set(trackings)) > 500:
        abort(400, "Przekroczono limit")
    try:
        with core.Blokada(core.DANE_DIR / ".przebieg.lock"):
            ensure_workbook()
            wb = openpyxl.load_workbook(workbook_path())
            try:
                wb["DATA"].append([date, shipment, ";".join(dict.fromkeys(trackings))])
                core.zapisz_skoroszyt(wb, workbook_path())
            finally:
                wb.close()
    except RuntimeError:
        abort(409, "Trwa przetwarzanie przesyłek")
    flash(f"Dodano {len(set(trackings))} numerów. Pominięto niepoprawne: {len(rejected)}.")
    return redirect(url_for("index"), code=303)

@app.post("/upload")
def upload_excel():
    f = request.files.get("file")
    if not f or not f.filename.lower().endswith(".xlsx"):
        abort(400, "Wymagany plik .xlsx")
    raw = f.read()
    if len(raw) > 12 * 1024 * 1024 or len(raw) < 100 or raw[:2] != b"PK":
        abort(400, "Nieprawidłowy plik XLSX")
    target = workbook_path()
    temp = target.with_name(".ups_uploaded.xlsx")
    try:
        with core.Blokada(core.DANE_DIR / ".przebieg.lock"):
            temp.write_bytes(raw)
            wb = openpyxl.load_workbook(temp, read_only=True, data_only=True)
            try:
                if "DATA" not in wb.sheetnames:
                    abort(400, "Brak arkusza DATA")
                _ = core.czytaj_dane(wb["DATA"])
            finally:
                wb.close()
            if target.exists():
                core.zrob_kopie(target, ile=15)
            os.replace(temp, target)
    except RuntimeError:
        abort(409, "Trwa przetwarzanie")
    finally:
        temp.unlink(missing_ok=True)
    flash("Zaimportowano arkusz XLSX; poprzednią wersję zachowano w kopiach.")
    return redirect(url_for("index"), code=303)

def process_job():
    try:
        core.konfiguruj_log(auto=True)
        exitcode = core.przebieg("run", auto=True)
        with JOB_LOCK:
            JOB["exit_code"] = exitcode
    except Exception as exc:
        core.log.exception("Błąd przetwarzania UPS POD")
        with JOB_LOCK:
            JOB["error"] = str(exc)[:300]
    finally:
        with JOB_LOCK:
            JOB["running"] = False
            JOB["finished"] = dt.datetime.now().isoformat(timespec="seconds")

@app.post("/run")
def run_scan():
    if not os.environ.get("UPS_CLIENT_ID") or not os.environ.get("UPS_CLIENT_SECRET"):
        abort(503, "Uzupełnij .env")
    with JOB_LOCK:
        if JOB["running"]:
            abort(409, "Przetwarzanie już trwa")
        JOB.update(running=True, started=dt.datetime.now().isoformat(timespec="seconds"), error=None, exit_code=None)
    threading.Thread(target=process_job, name="ups-scan", daemon=True).start()
    flash("Uruchomiono sprawdzanie statusów i archiwizację POD.")
    return redirect(url_for("index"), code=303)

@app.get("/api/status")
def status():
    return jsonify(dict(JOB))

@app.get("/excel")
def export_excel():
    ensure_workbook()
    return send_file(workbook_path(), as_attachment=True, download_name="ups-pod.xlsx")

@app.get("/pod/<folder>/<filename>")
def download_pod(folder, filename):
    if "/" in folder or "\\" in folder or "/" in filename or "\\" in filename:
        abort(404)
    root = core.wczytaj_ustawienia()["folder_pdf"].resolve()
    path = (root / folder / filename).resolve()
    if not path.is_relative_to(root) or path.suffix.lower() != ".pdf":
        abort(404)
    if not any(x["folder"] == folder and x["filename"] == filename and x["pdf"] for x in rows_for_page()):
        abort(404)
    return send_file(path, as_attachment=True, download_name=filename, mimetype="application/pdf")

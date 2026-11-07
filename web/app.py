"""Flask HTTP API for the UPS POD web dashboard, guarded upstream by Cloudflare Access."""
from __future__ import annotations

import io
import os
import secrets
import zipfile
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, abort, jsonify, render_template, request, send_file, session
from werkzeug.exceptions import HTTPException

import service

ROOT = Path(__file__).resolve().parent
HOST = os.getenv("PUBLIC_HOST", "ups.xlap.pl").lower()
SESSION_KEY = service.DATA_DIR / ".web_session_key"


def session_secret() -> str:
    service.DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not SESSION_KEY.exists():
        try:
            fd = os.open(SESSION_KEY, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.write(secrets.token_urlsafe(48))
        except FileExistsError:
            pass
    return SESSION_KEY.read_text(encoding="utf-8").strip()


def create_app(testing: bool = False) -> Flask:
    web = Flask(__name__, static_folder=str(ROOT / "static"),
                template_folder=str(ROOT / "templates"))
    web.config.update(
        SECRET_KEY=session_secret(),
        TESTING=testing,
        MAX_CONTENT_LENGTH=1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=not testing,
        SESSION_COOKIE_SAMESITE="Strict",
        PREFERRED_URL_SCHEME="https",
    )
    service.init_db()

    @web.before_request
    def security() -> None:
        hostname = (request.host or "").split(":")[0].lower()
        if not testing and hostname != HOST:
            abort(400, "Nieprawidłowy Host.")
        if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return
        if not testing:
            origin = request.headers.get("Origin") or request.headers.get("Referer")
            if not origin or urlparse(origin).hostname != HOST:
                abort(403, "Nieprawidłowy Origin.")
        supplied = request.headers.get("X-CSRF-Token", "")
        expected = session.get("csrf", "")
        if not supplied or not expected or not secrets.compare_digest(supplied, expected):
            abort(403, "Nieprawidłowy token CSRF.")

    @web.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
            "frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    @web.errorhandler(HTTPException)
    def handle_http_error(error):
        if request.path.startswith("/api/"):
            return jsonify(error=error.description), error.code
        return error

    @web.get("/healthz")
    def health():
        return jsonify(status="ok", database=service.DB_PATH.is_file())

    @web.get("/")
    def index():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        cid = os.getenv("UPS_CLIENT_ID") or os.getenv("client_id")
        secret = os.getenv("UPS_CLIENT_SECRET") or os.getenv("client_secret")
        return render_template("index.html", csrf=session["csrf"],
                               credentials=bool(cid and secret), hostname=HOST)

    @web.get("/api/shipments")
    def shipments():
        try:
            limit = int(request.args.get("limit", 200))
            offset = int(request.args.get("offset", 0))
            result = service.list_numbers(request.args.get("q", ""),
                                          request.args.get("scope", "all"), limit, offset)
        except ValueError as exc:
            abort(400, str(exc))
        return jsonify(**result, stats=service.stats())

    @web.post("/api/shipments")
    def add():
        data = request.get_json(silent=True) or {}
        raw = data.get("trackings", "")
        if not isinstance(raw, str):
            abort(400, "Pole trackings musi być tekstem.")
        try:
            result = service.add_numbers(raw)
        except ValueError as exc:
            abort(400, str(exc))
        return jsonify(result), 201 if result["added"] else 200

    @web.get("/api/logs")
    def logs():
        try:
            after = max(0, int(request.args.get("after", 0)))
            before = max(0, int(request.args.get("before", 0)))
        except ValueError:
            abort(400, "Nieprawidłowy kursor.")
        levels = [x.strip().upper() for x in request.args.get("levels", "").split(",") if x.strip()]
        try:
            return jsonify(items=service.audit_rows(after, before, levels))
        except ValueError as exc:
            abort(400, str(exc))

    @web.get("/api/tracking-fields")
    def tracking_fields():
        return jsonify(paths=service.tracking_field_paths())

    @web.post("/api/fba")
    def assign_fba():
        data = request.get_json(silent=True) or {}
        try:
            if isinstance(data.get("text"), str):
                entries = service.parse_fba_text(data["text"])
            else:
                entries = data.get("entries")
            if not isinstance(entries, list):
                abort(400, "Wymagana tabela FBA lub lista przypisań.")
            result = service.assign_fba_rows(entries, replace=data.get("replace") is True)
        except ValueError as exc:
            abort(400, str(exc))
        return jsonify(result)

    @web.post("/api/retry")
    def retry():
        data = request.get_json(silent=True) or {}
        numbers = data.get("trackings", [])
        if not isinstance(numbers, list) or not all(isinstance(n, str) for n in numbers):
            abort(400, "Podaj listę numerów.")
        try:
            count = service.retry_now(numbers)
        except ValueError as exc:
            abort(400, str(exc))
        service.audit("user", "retry", description=f"Ponowiono {count}.")
        return jsonify(requeued=count)

    @web.get("/api/shipments/<tracking>/pdf")
    def pdf(tracking: str):
        if not service.NUMBER.fullmatch(tracking):
            abort(404)
        matching = service.ready_paths([tracking])
        if not matching:
            abort(404, "Brak dokumentu PDF dla tego numeru.")
        try:
            filename = service.export_filename(tracking)
        except ValueError as exc:
            abort(409, str(exc))
        service.audit("user", "download_pdf", tracking)
        return send_file(matching[0][1], mimetype="application/pdf",
                         as_attachment=True, download_name=filename)

    @web.post("/api/zip")
    def zip_download():
        data = request.get_json(silent=True) or {}
        if data.get("all") is True:
            selected = None
        else:
            selected = data.get("trackings")
            if not isinstance(selected, list) or not all(isinstance(x, str) for x in selected):
                abort(400, "Wymagana lista numerów.")
        try:
            paths = service.ready_paths(selected)
        except ValueError as exc:
            abort(400, str(exc))
        if not paths:
            abort(404, "Nie znaleziono gotowych potwierdzeń.")
        if sum(p.stat().st_size for _, p in paths) > 150 * 1024 * 1024:
            abort(413, "Paczka przekracza 150 MB; pobierz mniejszy zestaw.")
        try:
            names = [(tracking, path, service.export_filename(tracking)) for tracking, path in paths]
        except ValueError as exc:
            abort(409, str(exc))
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED,
                             allowZip64=True) as archive:
            for _tracking, path, name in names:
                archive.write(path, arcname=name)
        service.audit("user", "download_zip", description=f"Dokumentów {len(paths)}.")
        stream.seek(0)
        return send_file(stream, mimetype="application/zip", as_attachment=True,
                         download_name="ups-pod-potwierdzenia.zip")

    return web


app = create_app()

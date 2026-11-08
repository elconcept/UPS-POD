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
import support_mail
import config_store
import gmail_smtp
import reports
import pod_thumbnail

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
        # The UI is behind Cloudflare Access; mutating requests require
        # same-origin checks and a CSRF token.
        SESSION_COOKIE_SAMESITE="Lax",
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
        db_ok = service.DB_PATH.is_file()
        pod_ok = service.DOCUMENTS_DIR.is_dir() and os.access(service.DOCUMENTS_DIR, os.W_OK)
        return jsonify(status="ok" if db_ok and pod_ok else "degraded",
                       database=db_ok, pod_archive_writable=pod_ok), 200 if db_ok and pod_ok else 503

    @web.get("/")
    def index():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        cid, secret = config_store.ups_credentials()
        return render_template("index.html", csrf=session["csrf"],
                               credentials=bool(cid and secret), hostname=HOST)

    @web.get("/api/config")
    def config_status():
        return jsonify(**config_store.masked_status(), gmail_app=gmail_smtp.status(),
                       api_archive=service.tracking_payload_info(),
                       employee={"name": config_store.get("SUPPORT_STAFF_NAME"),
                                 "email": config_store.get("SUPPORT_NOTIFY_EMAIL")})

    @web.post("/api/config/ups")
    def config_ups():
        data = request.get_json(silent=True) or {}
        try:
            changed = config_store.update({
                "UPS_CLIENT_ID": data.get("client_id", ""),
                "UPS_CLIENT_SECRET": data.get("client_secret", ""),
            })
            if changed:
                service.audit("user", "ups_config_updated",
                              description="Zmodyfikowano pola konfiguracji UPS: "
                              + ", ".join(changed))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(saved=True, changed=changed, **config_store.masked_status())

    @web.post("/api/config/employee")
    def config_employee():
        data = request.get_json(silent=True) or {}
        name = data.get("name", "")
        email = str(data.get("email", "")).strip().lower()
        if not isinstance(name, str):
            abort(400, "Nieprawidłowe imię i nazwisko.")
        if email and not support_mail.MAIL_RE.fullmatch(email):
            abort(400, "Nieprawidłowy adres pracownika.")
        try:
            changed = config_store.update({
                "SUPPORT_STAFF_NAME": name.strip(),
                "SUPPORT_NOTIFY_EMAIL": email,
            })
        except ValueError as exc:
            abort(400, str(exc))
        if changed:
            service.audit("user", "staff_config_updated",
                          description="Zmieniono dane pracownika: " + ", ".join(changed))
        return jsonify(saved=True, changed=changed,
                       employee={"name": config_store.get("SUPPORT_STAFF_NAME"),
                                 "email": config_store.get("SUPPORT_NOTIFY_EMAIL")})

    @web.post("/api/config/gmail-app")
    def gmail_app_save():
        data = request.get_json(silent=True) or {}
        email = data.get("email", "")
        password = data.get("app_password", "")
        if not isinstance(email, str) or not isinstance(password, str):
            return jsonify(error="Nieprawidłowe dane konfiguracji Gmail."), 400
        email = email.strip().lower()
        if email and not gmail_smtp.MAIL_RE.fullmatch(email):
            return jsonify(error="Nieprawidłowy adres Gmail."), 400
        try:
            password = gmail_smtp.normalize_password(password) if password.strip() else ""
        except gmail_smtp.GmailConfigError as exc:
            return jsonify(error=str(exc)), 400
        current_email, current_password = gmail_smtp.credentials()
        if email and email != current_email and not password:
            return jsonify(error="Zmiana adresu nadawcy wymaga nowego hasła aplikacji."), 400
        updates = {}
        if email and email != current_email:
            updates["GMAIL_SMTP_EMAIL"] = email
        if password and password != current_password:
            updates["GMAIL_SMTP_APP_PASSWORD"] = password
        if updates and not (email or current_email):
            return jsonify(error="Najpierw podaj adres Gmail."), 400
        try:
            changed = config_store.update(updates) if updates else []
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        if changed:
            service.audit("user", "gmail_app_config_updated",
                          description="Zmieniono konfigurację nadawcy Gmail App Password.")
        return jsonify(saved=bool(changed), changed=changed,
                       gmail_app=gmail_smtp.status())

    @web.post("/api/config/gmail-app/check")
    def gmail_app_check():
        try:
            result = gmail_smtp.check_login()
        except gmail_smtp.GmailConfigError as exc:
            return jsonify(error=str(exc)), 422
        except Exception as exc:
            # Do not return or log SMTP exception text: it can contain account
            # information, authentication data, or remote server banners.
            service.audit("system", "gmail_app_login_failed",
                          description="Weryfikacja Gmail SMTP: " + type(exc).__name__)
            return jsonify(error="Logowanie Gmail SMTP nie powiodło się. "
                           "Sprawdź 2FA, hasło aplikacji i politykę Google Workspace."), 502
        service.audit("user", "gmail_app_verified",
                      description="Gmail SMTP: logowanie zweryfikowano bez wysyłania wiadomości.")
        return jsonify(**result)

    @web.get("/api/shipments")
    def shipments():
        try:
            limit = int(request.args.get("limit", 1500))
            offset = int(request.args.get("offset", 0))
            result = service.list_numbers(
                request.args.get("q", ""), request.args.get("scope", "all"),
                limit, offset, request.args.get("month", "all"),
                sort_by=request.args.get("sort", "ship_date"),
                sort_dir=request.args.get("direction", "desc"),
                column_filters={key: request.args.get("f_" + key, "")
                                for key in service.LIST_COLUMNS},
            )
        except ValueError as exc:
            abort(400, str(exc))
        # Database-internal paths and recipient metadata are not needed by the UI.
        # Never expose NAS filesystem paths through the public dashboard API.
        for row in result["rows"]:
            row["support_eligible"] = support_mail.eligible(row)
            for key in ("pod_path", "received_by", "photo_filename", "delivered_at"):
                row.pop(key, None)
        return jsonify(**result, stats=service.stats(),
                       columns=[{"key": key, "label": entry[0], "path": entry[1]}
                                for key, entry in service.UPS_EXTRA_COLUMNS.items()],
                       support_configured=support_mail.configured())

    @web.get("/api/shipments/<tracking>/photo")
    def package_photo(tracking: str):
        target = service.photo_path(tracking.upper())
        if target is None:
            abort(404, "UPS nie udostępnił zdjęcia doręczenia.")
        return send_file(target, conditional=True, max_age=0)

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
            tracking_filter = request.args.get("tracking", "").strip().upper()
            if tracking_filter and not service.NUMBER.fullmatch(tracking_filter):
                abort(400, "Nieprawidłowy numer UPS w filtrze logów.")
            return jsonify(items=service.audit_rows(after, before, levels, tracking_filter))
        except ValueError as exc:
            abort(400, str(exc))

    @web.post("/api/support/<tracking>/send")
    def send_support(tracking: str):
        try:
            result = support_mail.send_support(tracking.upper())
            return jsonify(result), 200
        except support_mail.SupportNotConfigured as exc:
            return jsonify(error=str(exc), code="GMAIL_APP_PASSWORD_REQUIRED"), 503
        except support_mail.SupportIneligible as exc:
            return jsonify(error=str(exc), code="INELIGIBLE"), 422
        except support_mail.SupportAlreadySent as exc:
            return jsonify(error=str(exc), code="ALREADY_SENT"), 409
        except support_mail.SupportSendUncertain as exc:
            return jsonify(error=str(exc), code="SEND_UNCERTAIN"), 502

    @web.get("/api/tracking-fields")
    def tracking_fields():
        return jsonify(paths=service.tracking_field_paths())

    @web.post("/api/fba")
    def assign_fba():
        data = request.get_json(silent=True) or {}
        try:
            if isinstance(data.get("text"), str):
                # Unified parser: a pasted sequence may also include UPS numbers
                # without a preceding FBA, and existing UPS rows gain a new FBA.
                result = service.add_numbers(data["text"])
                return jsonify(result)
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

    @web.get("/api/shipments/<tracking>/thumbnail")
    def pod_preview(tracking: str):
        size = request.args.get("size", "small")
        if size not in {"small", "large"}:
            abort(400, "Nieobsługiwany rozmiar podglądu.")
        target = pod_thumbnail.thumbnail(tracking.upper(), large=(size == "large"))
        if target is None:
            abort(404, "Brak dostępnej miniatury dokumentu POD.")
        return send_file(target, mimetype="image/png", max_age=0)

    @web.post("/api/report/xlsx")
    def report_xlsx():
        data = request.get_json(silent=True) or {}
        try:
            stream, count = reports.build_report(data.get("trackings", []))
        except ValueError as exc:
            abort(400, str(exc))
        service.audit("user", "xlsx_report_generated",
                      description=f"Wygenerowano zestawienie dla {count} przesyłek.")
        response = send_file(
            stream,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name="ups-zestawienie-" + service.current_month() + ".xlsx",
        )
        response.headers["X-UPS-POD-Count"] = str(count)
        return response

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
        response = send_file(stream, mimetype="application/zip", as_attachment=True,
                             download_name="ups-pod-potwierdzenia.zip")
        response.headers["X-UPS-POD-Count"] = str(len(paths))
        return response

    return web


app = create_app()

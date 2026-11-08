"""Gmail / Google Workspace SMTP using a dedicated 16-character App Password.

Only the operator-initiated "Check login" performs a live SMTP authentication.
Mail sends are allowed after an authenticated login check for the exact saved
email and password. No Gmail OAuth scopes, Gmail inbox access, or tokens.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import smtplib
import ssl
from email.message import EmailMessage

import config_store
import service

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
MAIL_RE = re.compile(r"^[A-Za-z0-9_.+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


class GmailConfigError(ValueError):
    pass


def normalize_password(raw: str) -> str:
    if not isinstance(raw, str):
        raise GmailConfigError("Nieprawidłowe hasło aplikacji Gmail.")
    value = re.sub(r"\s+", "", raw)
    if not re.fullmatch(r"[A-Za-z0-9]{16}", value):
        raise GmailConfigError("Hasło aplikacji Google powinno zawierać 16 znaków.")
    return value


def credentials() -> tuple[str, str]:
    return (config_store.get("GMAIL_SMTP_EMAIL").strip().lower(),
            config_store.get("GMAIL_SMTP_APP_PASSWORD"))


def configured() -> bool:
    email, password = credentials()
    return bool(MAIL_RE.fullmatch(email) and re.fullmatch(r"[A-Za-z0-9]{16}", password))


def _fingerprint(email: str, password: str) -> str:
    return hashlib.sha256((email + "\0" + password).encode("utf-8")).hexdigest()


def verified() -> bool:
    if not configured():
        return False
    email, password = credentials()
    with service.connection() as db:
        row = db.execute("SELECT secret_hash FROM verified_gmail_smtp "
                         "WHERE email=?", (email,)).fetchone()
    return bool(row and hmac.compare_digest(
        row["secret_hash"], _fingerprint(email, password)))


def status() -> dict:
    email, _ = credentials()
    return {
        "configured": configured(),
        "email": email if configured() else "",
        "verified": verified(),
        "method": "smtp.gmail.com:465 / App Password",
    }


def _session():
    return smtplib.SMTP_SSL(
        SMTP_HOST, SMTP_PORT, timeout=20, context=ssl.create_default_context())


def check_login() -> dict:
    if not configured():
        raise GmailConfigError(
            "Zapisz adres nadawcy i 16-znakowe hasło aplikacji Gmail.")
    email, password = credentials()
    try:
        with _session() as server:
            server.login(email, password)
            # This check MUST NOT send a message.
    except Exception:
        # A failed recheck invalidates any earlier verified mark. No message
        # may be sent with a revoked / blocked App Password.
        with service.connection() as db:
            db.execute("DELETE FROM verified_gmail_smtp WHERE email=?", (email,))
        raise
    with service.connection() as db:
        db.execute(
            "INSERT INTO verified_gmail_smtp(email,secret_hash,verified_at) "
            "VALUES(?,?,?) ON CONFLICT(email) DO UPDATE SET "
            "secret_hash=excluded.secret_hash,verified_at=excluded.verified_at",
            (email, _fingerprint(email, password), service.utcnow()),
        )
    return {"verified": True, "email": email}


def send(message: EmailMessage) -> dict:
    if not verified():
        raise GmailConfigError(
            "Sprawdź logowanie hasłem aplikacji Gmail w Konfiguracji.")
    email, password = credentials()
    # Enforce sender identity, even for future callers of this module.
    if message.get("From", "").strip().lower() != email:
        raise GmailConfigError("Nadawca nie jest zgodny ze skonfigurowanym kontem.")
    with _session() as server:
        server.login(email, password)
        refused = server.send_message(message)
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)
    return {"id": message.get("Message-ID", ""), "channel": "gmail_smtp"}

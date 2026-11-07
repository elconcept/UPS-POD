"""Explicit, one-click UPS customer-service request via configured TLS SMTP.

UPS Tracking API is read-only. This module NEVER corrects UPS statuses or
claims delivery. Missing credentials are fatal; no pretend sending.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
import smtplib
import ssl
import uuid
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import service
import gmail_oauth
import config_store

OFFICIAL_CONTACT = "customer.service@ups.com"
MAIL_RE = re.compile(r"^[A-Za-z0-9_.+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


class SupportNotConfigured(ValueError):
    pass


class SupportIneligible(ValueError):
    pass


class SupportAlreadySent(ValueError):
    pass


class SupportSendUncertain(RuntimeError):
    pass


def smtp_config() -> dict[str, str | int]:
    host = os.getenv("UPS_SUPPORT_SMTP_HOST", "").strip()
    sender = os.getenv("UPS_SUPPORT_SMTP_FROM", "").strip()
    recipient = os.getenv("UPS_SUPPORT_TO", OFFICIAL_CONTACT).strip()
    security = os.getenv("UPS_SUPPORT_SMTP_SECURITY", "starttls").lower().strip()
    try:
        port = int(os.getenv("UPS_SUPPORT_SMTP_PORT", "465" if security == "ssl" else "587"))
    except ValueError as exc:
        raise SupportNotConfigured("Nieprawidłowy port serwera SMTP.") from exc
    if not host or not sender:
        raise SupportNotConfigured(
            "Wysyłka SMTP nie jest skonfigurowana. Ustaw UPS_SUPPORT_SMTP_HOST "
            "i UPS_SUPPORT_SMTP_FROM w chronionym środowisku kontenera."
        )
    if (not MAIL_RE.fullmatch(sender) or not MAIL_RE.fullmatch(recipient)
            or not recipient.lower().endswith("@ups.com")
            or security not in {"starttls", "ssl"} or not 1 <= port <= 65535):
        raise SupportNotConfigured("Nieprawidłowa konfiguracja bezpiecznej poczty SMTP.")
    user = os.getenv("UPS_SUPPORT_SMTP_USER", "").strip()
    password = os.getenv("UPS_SUPPORT_SMTP_PASSWORD", "")
    if bool(user) != bool(password):
        raise SupportNotConfigured("Uzupełnij zarówno użytkownika, jak i hasło SMTP.")
    return dict(host=host, port=port, sender=sender, recipient=recipient,
                security=security, user=user, password=password)


def gmail_app_config() -> dict[str, str] | None:
    """Google SMTP with app password: no user-managed GCP OAuth project."""
    email = config_store.get("GMAIL_SMTP_EMAIL").strip().lower()
    password = config_store.get("GMAIL_SMTP_APP_PASSWORD").replace(" ", "")
    if not email or not password:
        return None
    if not MAIL_RE.fullmatch(email) or len(password) != 16 or not password.isalnum():
        return None
    return {"email": email, "password": password}


def gmail_app_status() -> dict:
    cfg = gmail_app_config()
    return {"configured": bool(cfg), "email": cfg["email"] if cfg else "",
            "verified": gmail_app_verified() if cfg else False,
            "method": "smtp.gmail.com:465 / hasło aplikacji"}


def gmail_app_check() -> dict:
    cfg = gmail_app_config()
    if cfg is None:
        raise SupportNotConfigured("Podaj adres Gmail i 16-znakowe hasło aplikacji.")
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=18,
                              context=ssl.create_default_context()) as smtp:
            smtp.login(cfg["email"], cfg["password"])
    except (smtplib.SMTPException, OSError) as exc:
        raise SupportNotConfigured("Gmail odrzucił logowanie lub serwer jest niedostępny."
                                   " Sprawdź hasło aplikacji i ustawienia Google Workspace.") from exc
    # Persist only a fingerprint. Changing the SMTP app password requires
    # another successful login before autonomous operator notifications.
    fingerprint = hashlib.sha256(
        (cfg["email"] + "\0" + cfg["password"]).encode("utf-8")).hexdigest()
    with service.connection() as db:
        db.execute("INSERT INTO verified_gmail_smtp(email,secret_hash,verified_at) "
                   "VALUES(?,?,?) ON CONFLICT(email) DO UPDATE SET "
                   "secret_hash=excluded.secret_hash,verified_at=excluded.verified_at",
                   (cfg["email"], fingerprint, service.utcnow()))
    return {"ok": True, "email": cfg["email"]}


def gmail_app_verified() -> bool:
    cfg = gmail_app_config()
    if not cfg:
        return False
    fingerprint = hashlib.sha256(
        (cfg["email"] + "\0" + cfg["password"]).encode("utf-8")).hexdigest()
    with service.connection() as db:
        row = db.execute("SELECT secret_hash FROM verified_gmail_smtp "
                         "WHERE email=?", (cfg["email"],)).fetchone()
    return bool(row and row["secret_hash"] == fingerprint)


def configured() -> bool:
    if gmail_app_config():
        return True
    if gmail_oauth.status()["configured"]:
        return True
    try:
        smtp_config()
        return True
    except SupportNotConfigured:
        return False


def grace_expired(row: dict) -> bool:
    """At least 24h after the *end* of a delivery day, or last delivery scan."""
    now = dt.datetime.now(ZoneInfo("Europe/Warsaw"))
    date = row.get("delivery_date", "")
    kind = row.get("delivery_date_type", "")
    if kind in {"SDD", "RDD", "DEL"} and re.fullmatch(r"\d{8}", date or ""):
        try:
            start = dt.datetime.strptime(date, "%Y%m%d").replace(
                tzinfo=ZoneInfo("Europe/Warsaw"))
        except ValueError:
            return False
        # Any time on the scheduled day is a legitimate delivery.
        # Alert only after 24 further hours following that entire day.
        return now >= start + dt.timedelta(days=2)

    status = (row.get("ups_status") or "").strip().lower()
    if status not in {"out for delivery", "w doręczeniu"}:
        return False
    import json
    extras = row.get("ups_extra") or row.get("tracking_extras") or {}
    if isinstance(extras, str):
        try:
            extras = json.loads(extras)
        except (ValueError, TypeError):
            return False
    date = extras.get("ups.activity_date", "")
    clock = extras.get("ups.activity_time", "")
    if not re.fullmatch(r"\d{8}", date or ""):
        return False
    # Without event time, use the end of the scan day to avoid premature alerts.
    clock = clock if re.fullmatch(r"\d{6}", clock or "") else "235959"
    try:
        scanned = dt.datetime.strptime(date + clock, "%Y%m%d%H%M%S").replace(
            tzinfo=ZoneInfo("Europe/Warsaw"))
    except ValueError:
        return False
    return now >= scanned + dt.timedelta(hours=24)


def eligible(row: dict) -> bool:
    """Operator may request UPS investigation, never change status directly."""
    status = (row.get("ups_status") or "").strip().lower()
    return (row.get("state") != "ready"
            and row.get("ups_status_code") != "D"
            and status not in {"delivered", "doręczono"}
            and grace_expired(row))


def send_support(tracking: str) -> dict:
    if not service.NUMBER.fullmatch(tracking):
        raise SupportIneligible("Nieprawidłowy numer UPS.")
    # Validate configuration before creating a case, so unconfigured clicks
    # never reserve an idempotency key.
    gmail_app = gmail_app_config()
    gmail = gmail_oauth.status()
    provider = "gmail_app" if gmail_app else "gmail" if gmail["configured"] else "smtp"
    if provider == "gmail_app":
        config = {"sender": gmail_app["email"], "recipient": OFFICIAL_CONTACT}
    elif provider == "gmail":
        config = {"sender": gmail["email"], "recipient": OFFICIAL_CONTACT}
    else:
        try:
            config = smtp_config()
        except SupportNotConfigured as exc:
            if gmail["oauth_ready"]:
                raise SupportNotConfigured(
                    "Połącz konto Gmail w zakładce Konfiguracja, aby wysyłać zgłoszenia."
                ) from exc
            raise
    with service.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        record = db.execute(
            "SELECT tracking,shipment_id,state,ups_status,ups_status_code,"
            "delivery_date,delivery_date_type,tracking_extras FROM shipments WHERE tracking=?",
            (tracking,)
        ).fetchone()
        if record is None or not eligible(dict(record)):
            raise SupportIneligible(
                "Zgłoszenie przysługuje tylko niedoręczonym przesyłkom "
                "po upływie planowanej daty dostawy."
            )
        old = db.execute(
            "SELECT status FROM support_cases WHERE tracking=?", (tracking,)
        ).fetchone()
        if old:
            raise SupportAlreadySent(
                "Zgłoszenie dla tej przesyłki zostało już wysłane lub rozpoczęte; "
                "sprawdź historię zgłoszeń, aby nie tworzyć duplikatu."
            )
        db.execute("INSERT INTO support_cases(tracking,status,attempts) VALUES(?, 'pending', 1)",
                   (tracking,))
    reference = "<" + uuid.uuid4().hex + "@ups-pod.local>"
    msg = EmailMessage()
    msg["From"] = str(config["sender"])
    msg["To"] = str(config["recipient"])
    msg["Subject"] = "Request to investigate missing UPS delivery scan – " + tracking
    msg["Message-ID"] = reference
    msg.set_content(
        "Dear UPS Customer Support,\n\n"
        "Please investigate the delivery status of UPS tracking number " + tracking + ".\n"
        "The last scheduled delivery date in the UPS tracking response was "
        + record["delivery_date"] + " (" + record["delivery_date_type"] + "), "
        "but no confirmed Delivered status or Proof of Delivery is available.\n"
        "The current UPS status is: " + (record["ups_status"] or "not provided") + ".\n\n"
        "We do not assert that delivery has occurred. Please verify the actual "
        "delivery/scan history and shipment location. If UPS has confirmed "
        "physical delivery, please update tracking and provide official "
        "Proof of Delivery. Otherwise please provide the current investigation "
        "result and expected date.\n\n"
        "Please quote this tracking number in your reply.\n\n"
        "Regards,\n" + (config_store.get("SUPPORT_STAFF_NAME") or "UPS-POD shipment administration") + "\n"
    )
    try:
        if provider == "gmail_app":
            _send_smtp({
                "security": "ssl", "host": "smtp.gmail.com", "port": 465,
                "user": gmail_app["email"], "password": gmail_app["password"],
            }, msg)
        elif provider == "gmail":
            gmail_oauth.send(msg)
        else:
            _send_smtp(config, msg)
    except (OSError, smtplib.SMTPException, TimeoutError,
            gmail_oauth.GoogleConfigError) as exc:
        # After ambiguous failure no automatic retries; Gmail/SMTP may have
        # accepted the message before the client observed a network error.
        with service.connection() as db:
            db.execute("UPDATE support_cases SET status='uncertain', last_error=? "
                       "WHERE tracking=?", (type(exc).__name__, tracking))
        service.audit("system", "ups_support_send_uncertain", tracking,
                      description=type(exc).__name__)
        raise SupportSendUncertain(
            "Nie udało się potwierdzić wysyłki. Sprawdź Gmail Wysłane lub "
            "skrzynkę SMTP przed kolejnym zgłoszeniem."
        ) from exc
    with service.connection() as db:
        db.execute("UPDATE support_cases SET status='sent', sent_at=?, message_id=?,"
                   "last_error='' WHERE tracking=?", (service.utcnow(), reference, tracking))
    service.audit("user", "ups_support_sent", tracking,
                  description="Wysłano zapytanie do obsługi UPS przez " + provider + ".")
    return {"sent": True, "tracking": tracking, "recipient": str(config["recipient"]),
            "message_id": reference, "channel": provider}


def _send_smtp(config: dict, msg: EmailMessage) -> None:
    context = ssl.create_default_context()
    if config["security"] == "ssl":
        connection = smtplib.SMTP_SSL(str(config["host"]), int(config["port"]),
                                      timeout=20, context=context)
    else:
        connection = smtplib.SMTP(str(config["host"]), int(config["port"]), timeout=20)
    with connection as smtp:
        smtp.ehlo()
        if config["security"] == "starttls":
            smtp.starttls(context=context)
            smtp.ehlo()
        if config["user"]:
            smtp.login(str(config["user"]), str(config["password"]))
        smtp.send_message(msg)

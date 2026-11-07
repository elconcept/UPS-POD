"""Explicit, one-click UPS customer-service request via configured TLS SMTP.

UPS Tracking API is read-only. This module NEVER corrects UPS statuses or
claims delivery. Missing credentials are fatal; no pretend sending.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import smtplib
import ssl
import uuid
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import service
import gmail_oauth

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


def configured() -> bool:
    if gmail_oauth.status()["configured"]:
        return True
    try:
        smtp_config()
        return True
    except SupportNotConfigured:
        return False


def eligible(row: dict) -> bool:
    now = dt.datetime.now(ZoneInfo("Europe/Warsaw")).strftime("%Y%m%d")
    return (
        row["state"] != "ready"
        and row["ups_status_code"] != "D"
        and row["delivery_date_type"] in {"SDD", "RDD"}
        and bool(re.fullmatch(r"\d{8}", row["delivery_date"] or ""))
        and row["delivery_date"] < now
        and not (row["ups_status"] or "").lower().startswith("delivered")
        and "doręczon" not in (row["ups_status"] or "").lower()
    )


def send_support(tracking: str) -> dict:
    if not service.NUMBER.fullmatch(tracking):
        raise SupportIneligible("Nieprawidłowy numer UPS.")
    # Validate configuration before creating a case, so unconfigured clicks
    # never reserve an idempotency key.
    gmail = gmail_oauth.status()
    provider = "gmail" if gmail["configured"] else "smtp"
    if provider == "gmail":
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
            "delivery_date,delivery_date_type FROM shipments WHERE tracking=?",
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
        "Regards,\nUPS-POD shipment administration\n"
    )
    try:
        if provider == "gmail":
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

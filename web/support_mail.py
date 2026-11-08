"""One-click UPS delivery inquiry via Gmail SMTP with an App Password.
Never infers a delivery, never sends without operator click, and never retries
an unacknowledged send automatically.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import config_store
import gmail_smtp
import service

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


def configured() -> bool:
    return gmail_smtp.verified()


def grace_expired(row: dict) -> bool:
    """24 hours after the entire UPS delivery day, or the delivery scan."""
    now = dt.datetime.now(ZoneInfo("Europe/Warsaw"))
    date = row.get("delivery_date", "")
    kind = row.get("delivery_date_type", "")
    if kind in {"SDD", "RDD", "DEL"} and re.fullmatch(r"\d{8}", date or ""):
        try:
            start = dt.datetime.strptime(date, "%Y%m%d").replace(
                tzinfo=ZoneInfo("Europe/Warsaw"))
        except ValueError:
            return False
        return now >= start + dt.timedelta(days=2)

    status = (row.get("ups_status") or "").strip().lower()
    if status not in {"out for delivery", "w doręczeniu"}:
        return False
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
    clock = clock if re.fullmatch(r"\d{6}", clock or "") else "235959"
    try:
        scanned = dt.datetime.strptime(date + clock, "%Y%m%d%H%M%S").replace(
            tzinfo=ZoneInfo("Europe/Warsaw"))
    except ValueError:
        return False
    return now >= scanned + dt.timedelta(hours=24)


def eligible(row: dict) -> bool:
    status = (row.get("ups_status") or "").strip().lower()
    return (row.get("state") != "ready"
            and row.get("ups_status_code") != "D"
            and status not in {"delivered", "doręczono"}
            and grace_expired(row))


def send_support(tracking: str) -> dict:
    if not service.NUMBER.fullmatch(tracking):
        raise SupportIneligible("Nieprawidłowy numer UPS.")
    gmail = gmail_smtp.status()
    if not gmail["verified"]:
        raise SupportNotConfigured(
            "Zapisz adres Gmail i hasło aplikacji, następnie użyj "
            "«Sprawdź logowanie» w Konfiguracji."
        )

    with service.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        record = db.execute(
            "SELECT tracking,shipment_id,state,ups_status,ups_status_code,"
            "delivery_date,delivery_date_type,tracking_extras FROM shipments WHERE tracking=?",
            (tracking,),
        ).fetchone()
        if record is None or not eligible(dict(record)):
            raise SupportIneligible(
                "Zgłoszenie przysługuje wyłącznie niedoręczonym przesyłkom "
                "po przekroczeniu terminu lub utrzymującym się statusie «W doręczeniu»."
            )
        if db.execute("SELECT 1 FROM support_cases WHERE tracking=?", (tracking,)).fetchone():
            raise SupportAlreadySent(
                "Zgłoszenie tej przesyłki już wysłano albo rozpoczęto. "
                "Sprawdź historię przed ponowną próbą."
            )
        db.execute(
            "INSERT INTO support_cases(tracking,status,attempts) "
            "VALUES(?, 'pending', 1)", (tracking,)
        )

    reference = "<" + uuid.uuid4().hex + "@ups-pod.local>"
    msg = EmailMessage()
    msg["From"] = gmail["email"]
    msg["To"] = OFFICIAL_CONTACT
    msg["Subject"] = "Request to investigate missing UPS delivery scan – " + tracking
    msg["Message-ID"] = reference
    planned = (record["delivery_date"] or "").strip()
    date_info = ("The last recorded UPS delivery date was " + planned + " ("
                 + (record["delivery_date_type"] or "unknown") + "). "
                 if planned else "No current scheduled delivery date is available. ")
    msg.set_content(
        "Dear UPS Customer Support,\n\n"
        "Please investigate UPS tracking number " + tracking + ".\n"
        + date_info
        + "The shipment is still missing an official delivery scan or Proof of Delivery.\n"
        + "The last UPS status is: " + (record["ups_status"] or "unavailable") + ".\n\n"
        + "We do not assert that delivery has occurred. Please verify the actual "
        "delivery/scan history and the shipment location. If UPS confirms delivery, "
        "please correct the tracking information and provide the official Proof "
        "of Delivery. Otherwise please advise the investigation result and "
        "expected delivery date.\n\n"
        "Please quote this tracking number in your reply.\n\n"
        "Regards,\n" + (config_store.get("SUPPORT_STAFF_NAME")
                       or "UPS-POD shipment administration") + "\n"
    )
    try:
        result = gmail_smtp.send(msg)
    except Exception as exc:
        # A timeout can occur after Gmail accepts the message. No blind retry.
        with service.connection() as db:
            db.execute("UPDATE support_cases SET status='uncertain',last_error=? "
                       "WHERE tracking=?", (type(exc).__name__, tracking))
        service.audit("system", "ups_support_send_uncertain", tracking,
                      description=type(exc).__name__)
        raise SupportSendUncertain(
            "Gmail SMTP nie potwierdziło wysyłki. Sprawdź folder Wysłane "
            "zanim wyślesz kolejne zgłoszenie."
        ) from exc

    with service.connection() as db:
        db.execute(
            "UPDATE support_cases SET status='sent',sent_at=?,message_id=?,"
            "last_error='' WHERE tracking=?",
            (service.utcnow(), str(result.get("id") or reference), tracking),
        )
    service.audit("user", "ups_support_sent", tracking,
                  description="Zgłoszenie przesłano przez Gmail SMTP / App Password.")
    return {"sent": True, "tracking": tracking, "recipient": OFFICIAL_CONTACT,
            "message_id": str(result.get("id") or reference), "channel": "gmail_smtp"}

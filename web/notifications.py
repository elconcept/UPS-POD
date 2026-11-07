"""Operator notifications: durable outbox, at-most-once dispatch, no UPS emails."""
from __future__ import annotations

import logging
import uuid
from email.message import EmailMessage

import config_store
import gmail_oauth
import service
import support_mail

logger = logging.getLogger("ups_pod.notifications")


def recipient() -> str:
    email = config_store.get("SUPPORT_NOTIFY_EMAIL").strip().lower()
    return email if support_mail.MAIL_RE.fullmatch(email) else ""


def available() -> bool:
    return bool(recipient() and (support_mail.gmail_app_verified()
                or gmail_oauth.status()["configured"]))


def queue_overdue() -> int:
    """Queue a single operator alert for each >24-hour late POD.

    No guess from the shipment intake date; status of a 24-hour-old delivery
    scan or UPS official delivery day is required. SQLite unique key dedupes.
    """
    found: list[str] = []
    with service.connection() as db:
        for row in db.execute("""
            SELECT tracking,state,attempts,ups_status,delivery_date,delivery_date_type,
                   tracking_extras FROM shipments
            WHERE state='retry' AND attempts>0
        """):
            if support_mail.grace_expired(dict(row)):
                found.append(row["tracking"])
        db.executemany("""
            INSERT OR IGNORE INTO mail_notifications(tracking,event,state,created_at)
            VALUES(?,'pod_overdue_24h','queued',?)
        """, [(number, service.utcnow()) for number in found])
    return len(found)


def tick() -> int:
    """One aggregate notification per run. Unknown SMTP outcomes never retry."""
    if not available():
        return 0
    queue_overdue()
    email = recipient()
    app = support_mail.gmail_app_config() if support_mail.gmail_app_verified() else None
    oauth = gmail_oauth.status()
    if not app and not oauth["configured"]:
        return 0
    with service.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        rows = [dict(r) for r in db.execute("""
            SELECT id,tracking,event FROM mail_notifications
            WHERE state='queued' ORDER BY id LIMIT 150
        """).fetchall()]
        if not rows:
            return 0
        ids = [r["id"] for r in rows]
        db.executemany("UPDATE mail_notifications SET state='sending' WHERE id=?",
                       [(i,) for i in ids])
    sender = app["email"] if app else oauth["email"]
    message = EmailMessage()
    message["From"] = sender
    message["To"] = email
    message["Subject"] = ("UPS-POD: " + str(len(rows)) + " powiadomień o przesyłkach")
    message["Message-ID"] = "<" + uuid.uuid4().hex + "@ups-pod.local>"
    person = config_store.get("SUPPORT_STAFF_NAME").strip() or "Pracowniku"
    lines = [
        "Powiadomienia systemu UPS-POD dla: " + person,
        "Identyfikacja według statusów otrzymanych z UPS Tracking API.",
        "Automatyczna informacja nie zmienia statusów UPS ani nie stanowi dowodu doręczenia.",
        "",
    ]
    for r in rows:
        topic = ("Brak POD ponad 24 godziny po terminie"
                 if r["event"] == "pod_overdue_24h"
                 else "Zmiana statusu: opóźniona → doręczono")
        lines.append(topic + ": " + r["tracking"]
                     + " https://www.ups.com/track?tracknum=" + r["tracking"]
                     + "&loc=pl_PL")
    lines.extend(["", "Panel: https://ups.xlap.pl/", ""])
    message.set_content("\n".join(lines))
    try:
        if app:
            support_mail._send_smtp({
                "security": "ssl", "host": "smtp.gmail.com", "port": 465,
                "user": app["email"], "password": app["password"],
            }, message)
        else:
            gmail_oauth.send(message)
    except Exception as exc:
        # The server may have accepted the message before client timeout:
        # "uncertain" requires manual reconciliation with Gmail Sent.
        with service.connection() as db:
            db.executemany(
                "UPDATE mail_notifications SET state='uncertain',last_error=? WHERE id=?",
                [(type(exc).__name__, i) for i in ids])
        service.audit("system", "notification_uncertain",
                      description="Niepotwierdzona wysyłka do pracownika: "
                      + type(exc).__name__ + ". Bez ponawiania.")
        logger.warning("Notification delivery uncertain: %s", type(exc).__name__)
        return 0
    with service.connection() as db:
        db.executemany(
            "UPDATE mail_notifications SET state='sent',sent_at=? WHERE id=?",
            [(service.utcnow(), i) for i in ids])
    service.audit("system", "notification_sent",
                  description="Powiadomiono pracownika o " + str(len(rows))
                  + " zdarzeniach. Kanał Gmail.")
    return len(rows)

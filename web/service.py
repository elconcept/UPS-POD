"""Persistent SQLite tracking queue shared by the web UI and a single worker."""
from __future__ import annotations

import base64
import binascii
import datetime as dt
import hashlib
import os
import re
import shutil
from zoneinfo import ZoneInfo
import sqlite3
from pathlib import Path

DATA_DIR = Path(os.getenv("UPS_POD_HOME", "/data"))
DB_PATH = DATA_DIR / "ups_pod.sqlite3"
DOCUMENTS_DIR = Path(os.getenv("UPS_POD_PDF_DIR", str(DATA_DIR / "pod")))
SOURCES_DIR = DATA_DIR / "source"
PHOTO_DIR = DATA_DIR / "delivery_photos"
LEGACY_DOCUMENTS_DIR = DATA_DIR / "pod"
LEGACY_EXPORTS_DIR = DATA_DIR / "fba_pod"
UNASSIGNED_FBA = "FBA0000000"
UNKNOWN_DATE = "BRAK_DATY"
MAX_BATCH = 5000
FBA_ID = re.compile(r"^FBA[A-Z0-9]+$")
NUMBER = re.compile(r"^[A-Z0-9]{7,34}$")


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=30000")
    db.execute("PRAGMA journal_mode=WAL")
    return db


def init_db() -> None:
    DOCUMENTS_DIR.mkdir(parents=True, exist_ok=True)
    SOURCES_DIR.mkdir(parents=True, exist_ok=True)
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    with connection() as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS shipments (
              tracking TEXT PRIMARY KEY,
              state TEXT NOT NULL DEFAULT 'queued',
              description TEXT NOT NULL DEFAULT '',
              error TEXT NOT NULL DEFAULT '',
              attempts INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              last_attempt_at TEXT,
              next_attempt_at TEXT NOT NULL,
              delivered_at TEXT,
              pod_path TEXT,
              source_path TEXT,
              source_type TEXT,
              sha256 TEXT
            )"""
        )
        db.execute("""CREATE TABLE IF NOT EXISTS audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
            actor TEXT NOT NULL, operation TEXT NOT NULL, tracking TEXT NOT NULL DEFAULT '',
            http_status INTEGER, description TEXT NOT NULL DEFAULT '')""")
        existing = {r["name"] for r in db.execute("PRAGMA table_info(shipments)")}
        for name, definition in {
            "shipment_id": "TEXT NOT NULL DEFAULT ''",
            "ship_date": "TEXT NOT NULL DEFAULT ''",
            "ship_date_source": "TEXT NOT NULL DEFAULT ''",
            "ups_status": "TEXT NOT NULL DEFAULT ''",
            "ups_status_code": "TEXT NOT NULL DEFAULT ''",
            "weight_value": "TEXT NOT NULL DEFAULT ''",
            "weight_unit": "TEXT NOT NULL DEFAULT ''",
            "delivery_time_type": "TEXT NOT NULL DEFAULT ''",
            "delivery_time_start": "TEXT NOT NULL DEFAULT ''",
            "delivery_time_end": "TEXT NOT NULL DEFAULT ''",
            "delivery_date": "TEXT NOT NULL DEFAULT ''",
            "delivery_date_type": "TEXT NOT NULL DEFAULT ''",
            "received_by": "TEXT NOT NULL DEFAULT ''",
            "photo_filename": "TEXT NOT NULL DEFAULT ''",
            "metadata_at": "TEXT NOT NULL DEFAULT ''",
        }.items():
            if name not in existing:
                db.execute(f"ALTER TABLE shipments ADD COLUMN {name} {definition}")
        audit_columns = {r["name"] for r in db.execute("PRAGMA table_info(audit)")}
        if "level" not in audit_columns:
            db.execute("ALTER TABLE audit ADD COLUMN level TEXT NOT NULL DEFAULT 'INFO'")
            db.execute("""UPDATE audit SET level = CASE
                WHEN operation IN ('ups_network_error', 'credentials_missing') OR http_status >= 500 THEN 'ERROR'
                WHEN operation IN ('retry_scheduled', 'retry') OR http_status BETWEEN 400 AND 499 THEN 'WARNING'
                WHEN operation = 'ready' THEN 'SUCCESS' ELSE 'INFO' END""")
        db.execute("""CREATE TABLE IF NOT EXISTS tracking_fields (
            path TEXT PRIMARY KEY, first_seen_at TEXT NOT NULL)""")
        db.execute("CREATE INDEX IF NOT EXISTS ix_audit_level_id ON audit(level,id)")
        db.execute("CREATE INDEX IF NOT EXISTS ix_shipments_due ON shipments(state, next_attempt_at)")
        db.execute("CREATE INDEX IF NOT EXISTS ix_shipments_created ON shipments(created_at)")
    # Historical entries can predate the audit subsystem. Materialize missing
    # audit events once, so no retry row is invisible in logs.
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        missing = [dict(r) for r in db.execute(
            "SELECT s.tracking,s.state,s.error,s.description,s.attempts FROM shipments s "
            "WHERE s.state='retry' AND NOT EXISTS "
            "(SELECT 1 FROM audit a WHERE a.tracking=s.tracking)")]
        for record in missing:
            op = ("awaiting_delivery" if record["error"] == "Przesyłka jeszcze niedoręczona."
                  else "pod_retry_error")
            desc = safe_description(
                f"Odtworzona historia: {record['error']}; prób {record['attempts']}.")
            db.execute("INSERT INTO audit(created_at,actor,operation,tracking,http_status,"
                       "description,level) VALUES(?,?,?,?,?,?,?)",
                       (utcnow(), "system", op, record["tracking"], None, desc,
                        audit_level(op)))
    # Moving legacy PDFs is idempotent and guarded by SQLite BEGIN IMMEDIATE.
    # We retain old /data/pod copies as a rollback backup; active paths are only under /pod.
    with connection() as db:
        ready = [row[0] for row in db.execute("SELECT tracking FROM shipments WHERE state='ready'")]
    for tracking in ready:
        try:
            sync_named_pdf(tracking)
        except (OSError, ValueError) as exc:
            audit("system", "pod_migration_error", tracking, description=type(exc).__name__)


def recover_in_progress() -> int:
    with connection() as db:
        return db.execute(
            "UPDATE shipments SET state='retry', next_attempt_at=?, updated_at=? "
            "WHERE state='processing'", (utcnow(), utcnow())
        ).rowcount


def parse_tracking_numbers(raw: str) -> tuple[list[str], list[str]]:
    """Extract UPS identifiers from pasted cells, prose, quotes and punctuation."""
    pattern = r"(?<![A-Z0-9])(?:1Z[A-Z0-9]{16}|T[0-9]{10}|[0-9]{12}|[0-9]{9})(?![A-Z0-9])"
    normalized = (raw or "").upper().replace("\u200b", "").replace("\ufeff", "")
    numbers = list(dict.fromkeys(re.findall(pattern, normalized)))
    return numbers, []


# Amazon FBA identifiers have variable lengths; never assume seven suffix characters.
# UPS standard 1Z identifiers have 18 characters (2 + 16).
MIXED_TOKEN = re.compile(
    r"(?<![A-Z0-9])(?:1Z[A-Z0-9]{16}|FBA[A-Z0-9]+)(?![A-Z0-9])",
    re.IGNORECASE,
)
FBA_HEADERS = {"SHIPMENT", "ID", "SHIPMENTID", "FBA", "UPS", "TRACKING", "TRACKINGS", "NUMERY", "NR", "LISTA", "DATA", "DATE"}


def parse_mixed_text(raw: str) -> tuple[list[dict], list[str]]:
    """Ordered scanner: FBA applies to following UPS numbers until the next FBA.

    Accept arbitrary delimiters, pasted Excel cells with several UPS in one
    cell, repeated FBA groups, and standalone UPS without assignment. Garbage
    is ignored and surfaced as nonmodal warnings, never silently accepted.
    """
    normalized = (raw or "").upper().replace("\ufeff", " ").replace("\u200b", " ")
    matches = list(MIXED_TOKEN.finditer(normalized))
    assignments: list[dict] = []
    rejected: list[str] = []
    active_fba = ""
    seen: dict[str, str] = {}
    cursor = 0

    def garbage(part: str) -> None:
        for token in re.findall(r"[A-Z0-9]+", part):
            if token not in FBA_HEADERS and token not in {"-", "I", "ORAZ"}:
                rejected.append(token[:70])

    for match in matches:
        garbage(normalized[cursor:match.start()])
        token = match.group().upper()
        if token.startswith("FBA"):
            if token != UNASSIGNED_FBA:
                active_fba = token
            else:
                active_fba = ""
        else:
            if token in seen and seen[token] != active_fba and active_fba:
                if seen[token]:
                    raise ValueError(f"Sprzeczne Shipment ID dla {token}: {seen[token]} i {active_fba}.")
                # Previously free-standing tracking gets the explicit FBA mapping.
                seen[token] = active_fba
            elif token not in seen:
                seen[token] = active_fba
        cursor = match.end()
    garbage(normalized[cursor:])
    for tracking, fba in seen.items():
        assignments.append({"tracking": tracking, "shipment_id": fba, "ship_date": ""})
    return assignments, rejected


def add_numbers(raw: str) -> dict:
    parsed, rejected = parse_mixed_text(raw)
    if not parsed:
        raise ValueError("Nie znaleziono numerów UPS. Sprawdź format 1Z + 16 znaków.")
    if len(parsed) > MAX_BATCH:
        raise ValueError(f"Limit jednorazowego importu: {MAX_BATCH} numerów.")
    inserted = 0
    assigned = 0
    changed = 0
    now = utcnow()
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        # Validate the complete import before any insertion.
        for row in parsed:
            fba = row["shipment_id"]
            if not fba:
                continue
            existing = db.execute("SELECT shipment_id FROM shipments WHERE tracking=?",
                                  (row["tracking"],)).fetchone()
            if existing and existing["shipment_id"] and existing["shipment_id"] != fba:
                raise ValueError(f"Konflikt FBA dla {row['tracking']}: "
                                 f"istnieje {existing['shipment_id']}, import zawiera {fba}.")
        for row in parsed:
            tracking, fba = row["tracking"], row["shipment_id"]
            cursor = db.execute(
                "INSERT OR IGNORE INTO shipments(tracking,state,created_at,updated_at,next_attempt_at) "
                "VALUES(?, 'queued', ?, ?, ?)", (tracking, now, now, now),
            )
            inserted += cursor.rowcount
            if fba:
                assigned += 1
                cur = db.execute(
                    "UPDATE shipments SET shipment_id=?,updated_at=? "
                    "WHERE tracking=? AND shipment_id!=?",
                    (fba, now, tracking, fba),
                )
                changed += cur.rowcount
    for row in parsed:
        if row["shipment_id"]:
            sync_named_pdf(row["tracking"])
    audit("user", "import", description=f"Dodano {inserted}, FBA {assigned}, "
          f"uzupełniono {changed}, odrzucono fragmentów {len(rejected)}.")
    for row in parsed:
        audit("user", "shipment_imported", row["tracking"],
              description=(f"Shipment ID {row['shipment_id']}" if row["shipment_id"]
                           else "Dodano UPS bez Shipment ID."))
    return {
        "added": inserted,
        "existing": len(parsed) - inserted,
        "assigned": assigned,
        "changed": changed,
        "rejected": rejected[:30],
        "rejected_count": len(rejected),
        "submitted_unique": len(parsed),
    }


def stats() -> dict:
    with connection() as db:
        rows = db.execute("SELECT state, COUNT(*) n FROM shipments GROUP BY state").fetchall()
    counts = {r["state"]: r["n"] for r in rows}
    return {
        "total": sum(counts.values()),
        "ready": counts.get("ready", 0),
        "queued": counts.get("queued", 0),
        "processing": counts.get("processing", 0),
        "retry": counts.get("retry", 0),
    }


def current_month() -> str:
    return dt.datetime.now(ZoneInfo("Europe/Warsaw")).strftime("%Y-%m")


def available_months() -> list[str]:
    """Calendar months represented by UPS pickupDate (fallback to intake date)."""
    with connection() as db:
        months = [r[0] for r in db.execute(
            "SELECT DISTINCT SUBSTR(COALESCE(NULLIF(ship_date,''), created_at),1,7) AS mon "
            "FROM shipments WHERE length(mon)=7 ORDER BY mon DESC"
        )]
    return sorted({*months, current_month()}, reverse=True)


def list_numbers(search: str = "", scope: str = "all", limit: int = 200,
                 offset: int = 0, month: str = "all") -> dict:
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    where = ["1=1"]
    params: list[str] = []
    if scope == "ready":
        where.append("state='ready'")
    elif scope == "missing":
        where.append("state!='ready'")
    elif scope != "all":
        raise ValueError("Nieprawidłowy filtr.")
    if month and month != "all":
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise ValueError("Miesiąc musi mieć postać RRRR-MM.")
        where.append("SUBSTR(COALESCE(NULLIF(ship_date,''),created_at),1,7)=?")
        params.append(month)
    if search:
        where.append("(tracking LIKE ? OR shipment_id LIKE ? OR description LIKE ? OR error LIKE ?)")
        params.extend([f"%{search[:100]}%"] * 4)
    condition = " AND ".join(where)
    with connection() as db:
        total = db.execute(f"SELECT COUNT(*) FROM shipments WHERE {condition}", params).fetchone()[0]
        rows = db.execute(
            "SELECT tracking,state,description,error,attempts,created_at,updated_at,"
            "last_attempt_at,next_attempt_at,delivered_at,pod_path,shipment_id,ship_date,"
            "ship_date_source,ups_status,ups_status_code,weight_value,weight_unit,"
            "delivery_time_type,delivery_time_start,delivery_time_end,delivery_date,"
            "delivery_date_type,received_by,photo_filename FROM shipments WHERE " + condition +
            " ORDER BY (ship_date='') ASC,ship_date DESC,created_at DESC,tracking ASC "
            "LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    return {"total": total, "rows": [dict(r) for r in rows],
            "months": available_months()}




def claim_next() -> str | None:
    now = utcnow()
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT tracking FROM shipments WHERE state IN ('queued','retry') "
            "AND next_attempt_at <= ? ORDER BY next_attempt_at ASC,created_at ASC LIMIT 1",
            (now,),
        ).fetchone()
        if row is None:
            return None
        number = row["tracking"]
        db.execute(
            "UPDATE shipments SET state='processing',attempts=attempts+1,"
            "last_attempt_at=?,updated_at=?,error='' WHERE tracking=?",
            (now, now, number),
        )
    audit("worker", "attempt_start", number, description="Zlecono pobranie z UPS.")
    return number


def mark_ready(number: str, description: str, delivered_at: str, pod_path: str,
               source_path: str, source_type: str, sha256: str) -> None:
    now = utcnow()
    with connection() as db:
        db.execute(
            "UPDATE shipments SET state='ready',description=?,error='',delivered_at=?,"
            "pod_path=?,source_path=?,source_type=?,sha256=?,updated_at=? WHERE tracking=?",
            (description[:500], delivered_at, pod_path, source_path, source_type,
             sha256, now, number),
        )
    sync_named_pdf(number)


def mark_retry(number: str, error: str, description: str = "", delay_hours: float = 3) -> None:
    next_time = (
        dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=max(0.001, delay_hours))
    ).isoformat(timespec="seconds")
    with connection() as db:
        db.execute(
            "UPDATE shipments SET state='retry',description=?,error=?,updated_at=?,"
            "next_attempt_at=? WHERE tracking=?",
            (safe_description(description)[:500], safe_description(error)[:800], utcnow(), next_time, number),
        )

    operation = "awaiting_delivery" if error == "Przesyłka jeszcze niedoręczona." else "pod_retry_error"
    audit("worker", operation, number,
          description=f"{safe_description(error)}; następna próba {next_time}")


def retry_now(numbers: list[str]) -> int:
    if len(numbers) > MAX_BATCH:
        raise ValueError("Zbyt wiele numerów.")
    if not all(NUMBER.fullmatch(s) for s in numbers):
        raise ValueError("Nieprawidłowy tracking.")
    with connection() as db:
        count = 0
        for number in set(numbers):
            cur = db.execute(
                "UPDATE shipments SET state='retry',next_attempt_at=?,updated_at=? "
                "WHERE tracking=? AND state='retry'",
                (utcnow(), utcnow(), number),
            )
            count += cur.rowcount
    return count


def ready_paths(numbers: list[str] | None, max_count: int = 1000) -> list[tuple[str, Path]]:
    """The database, never user-provided paths, determines downloadable file locations."""
    with connection() as db:
        if numbers is None:
            rows = db.execute(
                "SELECT tracking,pod_path FROM shipments WHERE state='ready' "
                "ORDER BY created_at DESC LIMIT ?", (max_count + 1,)
            ).fetchall()
        else:
            if len(numbers) > max_count:
                raise ValueError(f"Można pobrać maksymalnie {max_count} dokumentów naraz.")
            unique = list(dict.fromkeys(numbers))
            if not unique:
                return []
            placeholders = ",".join("?" for _ in unique)
            rows = db.execute(
                f"SELECT tracking,pod_path FROM shipments WHERE state='ready' "
                f"AND tracking IN ({placeholders})", unique
            ).fetchall()
    if len(rows) > max_count:
        raise ValueError(f"Można pobrać maksymalnie {max_count} dokumentów naraz.")
    root = DOCUMENTS_DIR.resolve()
    result = []
    for row in rows:
        if not row["pod_path"]:
            continue
        path = Path(row["pod_path"]).resolve()
        if path.is_relative_to(root) and path.is_file() and path.stat().st_size > 4:
            with path.open("rb") as file:
                if file.read(4) == b"%PDF":
                    result.append((row["tracking"], path))
    return result


def safe_description(value: str) -> str:
    value = str(value)
    for key, secret in os.environ.items():
        if secret and len(secret) > 3 and any(x in key.lower() for x in ("secret", "token", "client_id")):
            value = value.replace(secret, "[ukryte]")
    return re.sub(r"[\x00-\x1f\x7f]", " ", value)[:1000]


def audit_level(operation: str, http_status: int | None = None) -> str:
    if operation in {"ups_network_error", "credentials_missing", "pod_retry_error", "pod_migration_error"} or (http_status or 0) >= 500:
        return "ERROR"
    if operation in {"retry_scheduled", "retry", "metadata_backfill_failed"} or 400 <= (http_status or 0) < 500:
        return "WARNING"
    if operation == "ready":
        return "SUCCESS"
    return "INFO"


def audit(actor: str, operation: str, tracking: str = "", http_status: int | None = None,
          description: str = "") -> None:
    with connection() as db:
        db.execute("INSERT INTO audit(created_at,actor,operation,tracking,http_status,description,level) "
                   "VALUES(?,?,?,?,?,?,?)", (utcnow(), actor, operation, tracking, http_status,
                                           safe_description(description), audit_level(operation, http_status)))


def audit_rows(after: int = 0, before: int = 0, levels: list[str] | None = None,
               tracking: str = "") -> list[dict]:
    levels = levels or []
    if any(x not in {"INFO", "SUCCESS", "WARNING", "ERROR"} for x in levels):
        raise ValueError("Nieprawidłowy poziom logów.")
    extra = " AND level IN (" + ",".join("?" for _ in levels) + ")" if levels else ""
    if tracking:
        if not NUMBER.fullmatch(tracking):
            raise ValueError("Nieprawidłowy tracking w filtrze logów.")
        extra += " AND tracking=?"
    with connection() as db:
        return [dict(row) for row in db.execute(
            "SELECT * FROM audit WHERE id>? AND (?=0 OR id<?)" + extra +
            " ORDER BY id DESC LIMIT 200", (after, before, before, *levels,
                                           *([tracking] if tracking else [])))]


def parse_fba_text(raw: str) -> list[dict]:
    """Legacy caller: mixed Excel cells and ordered FBA+UPS streams."""
    assignments, _warnings = parse_mixed_text(raw)
    return [x for x in assignments if x["shipment_id"]]




def assign_fba_rows(entries: list[dict], replace: bool = False) -> dict:
    """One UPS tracking can have only one FBA; reject conflicts as one transaction."""
    if not entries or len(entries) > MAX_BATCH or not isinstance(replace, bool):
        raise ValueError("Nieprawidłowa liczba przypisań lub tryb zapisu.")
    checked: dict[str, tuple[str, str]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Niepoprawny rekord przypisania.")
        tracking = str(entry.get("tracking", "")).strip().upper()
        fba = str(entry.get("shipment_id", "")).strip().upper()
        date = str(entry.get("ship_date") or "").strip()
        if not NUMBER.fullmatch(tracking) or not FBA_ID.fullmatch(fba) or fba == UNASSIGNED_FBA:
            raise ValueError("Niepoprawny numer UPS lub identyfikator przesyłki Amazon FBA.")
        if date:
            try:
                if dt.date.fromisoformat(date).isoformat() != date:
                    raise ValueError
            except ValueError as exc:
                raise ValueError("Data nadania musi mieć format RRRR-MM-DD.") from exc
        if tracking in checked and checked[tracking] != (fba, date):
            raise ValueError(f"Sprzeczne przypisania dla {tracking}.")
        checked[tracking] = (fba, date)
    changed = 0
    now = utcnow()
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        for tracking, (fba, date) in checked.items():
            row = db.execute("SELECT shipment_id,ship_date,ship_date_source FROM shipments WHERE tracking=?",
                             (tracking,)).fetchone()
            if row and row["shipment_id"] and row["shipment_id"] != fba and not replace:
                raise ValueError(f"Konflikt FBA dla {tracking}: istnieje {row['shipment_id']}.")
            if (date and row and row["ship_date"] and row["ship_date"] != date
                    and row["shipment_id"] and row["ship_date_source"] != "ups_pickup_date"
                    and not replace):
                raise ValueError(f"Konflikt daty dla {tracking}: istnieje {row['ship_date']}.")
            if not row:
                db.execute("INSERT INTO shipments(tracking,created_at,updated_at,next_attempt_at) "
                           "VALUES(?,?,?,?)", (tracking, now, now, now))
            # An actual UPS pickupDate always wins over a historical operator date.
            ups_known = row and row["ship_date_source"] == "ups_pickup_date"
            final_date = (row["ship_date"] if ups_known else
                          date or (row["ship_date"] if row else ""))
            final_source = ("ups_pickup_date" if ups_known else
                            "manual" if date else (row["ship_date_source"] if row else ""))
            if not row or row["shipment_id"] != fba or row["ship_date"] != final_date:
                changed += 1
            db.execute("UPDATE shipments SET shipment_id=?,ship_date=?,ship_date_source=?,"
                       "updated_at=? WHERE tracking=?", (fba, final_date, final_source, now, tracking))
    for number in checked:
        sync_named_pdf(number)
        audit("user", "fba_mapping", number, description="Przypisanie Shipment ID.")
    audit("user", "fba_mapping", description=f"Przypisano {len(checked)} numerów; zmieniono {changed}.")
    return {"assigned": len(checked), "changed": changed}


def save_tracking_detail(tracking: str, detail: dict) -> None:
    """Prefer UPS pickupDate whenever supplied; preserve date only if API has none."""
    status = str(detail.get("status") or "")[:150]
    code = str(detail.get("status_typ") or "")[:20]
    date = str(detail.get("ship_date") or "")
    source = str(detail.get("ship_date_source") or "")
    with connection() as db:
        db.execute(
            """UPDATE shipments SET ups_status=?,ups_status_code=?,
               ship_date=CASE WHEN ?='' THEN ship_date ELSE ? END,
               ship_date_source=CASE WHEN ?='' THEN ship_date_source ELSE ? END,
               updated_at=? WHERE tracking=?""",
            (status, code, date, date, date, source, utcnow(), tracking),
        )
    sync_named_pdf(tracking)



def _first_package(payload: dict, tracking: str) -> dict:
    ships = (payload.get("trackResponse") or {}).get("shipment") or []
    for shipment in ships:
        for package in shipment.get("package") or []:
            if str(package.get("trackingNumber", "")).upper() == tracking.upper():
                return package
    for shipment in ships:
        packages = shipment.get("package") or []
        if packages:
            return packages[0]
    return {}


def save_tracking_metadata(tracking: str, payload: dict) -> None:
    """Persist fields returned in UPS Tracking JSON, not guessed document values.

    Photo is stored only when UPS explicitly provides valid image bytes.
    photoCaptureInd=Y alone is not a retrievable image.
    """
    if not NUMBER.fullmatch(tracking) or not isinstance(payload, dict):
        raise ValueError("Nieprawidłowe dane metadanych UPS.")
    p = _first_package(payload, tracking)
    if not p:
        return
    weight = p.get("weight") or {}
    time_data = p.get("deliveryTime") or {}
    dates = p.get("deliveryDate") or []
    selected_date = next((d for d in dates if d.get("type") == "DEL"), None)
    if selected_date is None:
        selected_date = next((d for d in dates if d.get("type") == "SDD"), None)
    if selected_date is None:
        selected_date = dates[0] if dates else {}
    delivered = p.get("deliveryInformation") or {}
    photo = (delivered.get("deliveryPhoto") or {}).get("photo")
    photo_filename = ""
    if isinstance(photo, str) and 0 < len(photo) < 8 * 1024 * 1024:
        if "," in photo and photo.startswith("data:image/"):
            photo = photo.split(",", 1)[1]
        try:
            image = base64.b64decode(photo, validate=True)
            ext = ("jpg" if image.startswith(b"\xff\xd8\xff") else
                   "png" if image.startswith(b"\x89PNG\r\n\x1a\n") else
                   "webp" if image.startswith(b"RIFF") and image[8:12] == b"WEBP" else "")
            if ext and len(image) <= 4 * 1024 * 1024:
                PHOTO_DIR.mkdir(parents=True, exist_ok=True)
                photo_filename = f"{tracking}.{ext}"
                target = PHOTO_DIR / photo_filename
                temp = PHOTO_DIR / f".{tracking}.image.part"
                try:
                    with temp.open("wb") as handle:
                        handle.write(image)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temp, target)
                finally:
                    temp.unlink(missing_ok=True)
        except (ValueError, binascii.Error):
            pass
    def value(obj: dict, key: str, limit: int = 160) -> str:
        return safe_description(obj.get(key) or "")[:limit]
    date_yyyymmdd = value(selected_date, "date", 8)
    if not re.fullmatch(r"\d{8}", date_yyyymmdd):
        date_yyyymmdd = ""
    existing_photo = ""
    with connection() as db:
        old = db.execute("SELECT photo_filename FROM shipments WHERE tracking=?", (tracking,)).fetchone()
        if old:
            existing_photo = old["photo_filename"] or ""
        db.execute(
            "UPDATE shipments SET weight_value=?,weight_unit=?,delivery_time_type=?,"
            "delivery_time_start=?,delivery_time_end=?,delivery_date=?,"
            "delivery_date_type=?,received_by=?,photo_filename=?,metadata_at=?,"
            "updated_at=? WHERE tracking=?",
            (value(weight, "weight", 30),value(weight,"unitOfMeasurement",12),
             value(time_data,"type",12),value(time_data,"startTime",12),
             value(time_data,"endTime",12),date_yyyymmdd,value(selected_date,"type",12),
             value(delivered,"receivedBy",120),photo_filename or existing_photo,
             utcnow(),utcnow(),tracking),
        )


def photo_path(tracking: str) -> Path | None:
    if not NUMBER.fullmatch(tracking):
        return None
    with connection() as db:
        row = db.execute("SELECT photo_filename FROM shipments WHERE tracking=?",
                         (tracking,)).fetchone()
    filename = row["photo_filename"] if row else ""
    if not filename or filename not in {f"{tracking}.{e}" for e in ("jpg","png","webp")}:
        return None
    target = (PHOTO_DIR / filename).resolve()
    if not target.is_relative_to(PHOTO_DIR.resolve()) or not target.is_file():
        return None
    return target



def policy_filename(tracking: str, shipment_id: str = "", ship_date: str = "") -> str:
    """No fictitious dates: BRAK_DATY is a visible placeholder until UPS supplies a date."""
    if not NUMBER.fullmatch(tracking):
        raise ValueError("Nieprawidłowy numer UPS.")
    date = ship_date or UNKNOWN_DATE
    if ship_date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise ValueError("Nieprawidłowa data nadania.")
    shipment = shipment_id or UNASSIGNED_FBA
    if not FBA_ID.fullmatch(shipment):
        raise ValueError("Nieprawidłowy identyfikator FBA.")
    return f"{date}-{shipment}-{tracking}.pdf"


def export_filename(tracking: str) -> str:
    with connection() as db:
        row = db.execute("SELECT shipment_id,ship_date FROM shipments WHERE tracking=?",
                         (tracking,)).fetchone()
    if not row:
        raise ValueError(f"Nieznany numer UPS {tracking}.")
    return policy_filename(tracking, row["shipment_id"], row["ship_date"])


def document_target(tracking: str) -> Path:
    return DOCUMENTS_DIR / export_filename(tracking)


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _copy_verified(source: Path, target: Path) -> None:
    """Migrate between filesystems without deleting source or overwriting differing files."""
    tmp = target.with_name("." + target.name + ".migration.part")
    try:
        with source.open("rb") as src, tmp.open("xb") as dst:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        if _digest(source) != _digest(tmp):
            raise OSError("Kopia PDF różni się od oryginału.")
        if target.exists():
            if _digest(source) != _digest(target):
                raise FileExistsError("Konflikt archiwum POD: nazwa jest już zajęta.")
            return
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def sync_named_pdf(tracking: str) -> Path | None:
    """Physically rename the single active PDF when FBA/date change.

    For legacy files copied from /data/pod or /data/fba_pod, originals remain as
    backup. In the mounted archive there is exactly one active filename per UPS.
    """
    DOCUMENTS_DIR.mkdir(parents=True, exist_ok=True)
    root = DOCUMENTS_DIR.resolve()
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT shipment_id,ship_date,pod_path,state FROM shipments WHERE tracking=?",
            (tracking,),
        ).fetchone()
        if row is None or row["state"] != "ready":
            return None
        target = root / policy_filename(tracking, row["shipment_id"], row["ship_date"])
        candidates = []
        if row["pod_path"]:
            candidates.append(Path(row["pod_path"]).resolve())
        # Interrupted rename/migration: recover from the canonical target or
        # a previous FBA/date filename; never read arbitrary paths from SQLite.
        candidates.append(target)
        candidates.extend(sorted(root.glob(f"*-{tracking}.pdf")))
        candidates.append(LEGACY_DOCUMENTS_DIR.resolve() / f"{tracking}.pdf")
        candidates.extend(sorted(LEGACY_EXPORTS_DIR.resolve().glob(f"*-{tracking}.pdf")))
        trusted = [root, LEGACY_DOCUMENTS_DIR.resolve(), LEGACY_EXPORTS_DIR.resolve()]
        source = next(
            (p for p in candidates if any(p.is_relative_to(base) for base in trusted)
             and p.is_file() and p.stat().st_size > 4),
            None,
        )
        if source is None:
            # No file: preserve database state for diagnosis rather than claiming success.
            return None
        with source.open("rb") as stream:
            if stream.read(4) != b"%PDF":
                raise ValueError("Archiwalny plik nie jest PDF.")
        if source != target:
            if source.is_relative_to(root):
                if target.exists():
                    if _digest(source) != _digest(target):
                        raise FileExistsError("Konflikt różnych PDF dla tej samej nazwy.")
                    source.unlink()
                else:
                    os.replace(source, target)
            else:
                _copy_verified(source, target)
        if not target.is_file():
            raise OSError("Nie udało się przenieść PDF do archiwum.")
        db.execute(
            "UPDATE shipments SET pod_path=?,sha256=?,updated_at=? WHERE tracking=?",
            (str(target), _digest(target), utcnow(), tracking),
        )
        # No duplicate active PDF filenames remain after a successful rename.
        for stale in root.glob(f"*-{tracking}.pdf"):
            if stale != target and _digest(stale) == _digest(target):
                stale.unlink()
    return target


def save_tracking_fields(payload: dict) -> None:
    """Store only JSON field names, never package data, addresses or signature/POD content."""
    paths: set[str] = set()
    def walk(obj, prefix: str = "") -> None:
        if len(paths) > 1000 or len(prefix) > 180:
            return
        if isinstance(obj, dict):
            for key, value in obj.items():
                if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,80}", key):
                    continue
                path = f"{prefix}.{key}" if prefix else key
                paths.add(path)
                walk(value, path)
        elif isinstance(obj, list):
            for item in obj[:5]:
                walk(item, prefix + "[]")
    walk(payload)
    with connection() as db:
        db.executemany("INSERT OR IGNORE INTO tracking_fields(path,first_seen_at) VALUES(?,?)",
                       [(path, utcnow()) for path in sorted(paths)])


def tracking_field_paths() -> list[str]:
    with connection() as db:
        return [r[0] for r in db.execute("SELECT path FROM tracking_fields ORDER BY path")]

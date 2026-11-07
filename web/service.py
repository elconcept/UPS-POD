"""Persistent SQLite tracking queue shared by the web UI and a single worker."""
from __future__ import annotations

import base64
import binascii
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import zlib
from zoneinfo import ZoneInfo
import sqlite3

import ups_localization
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
        db.execute("""CREATE TABLE IF NOT EXISTS support_cases (
            tracking TEXT PRIMARY KEY REFERENCES shipments(tracking),
            status TEXT NOT NULL,
            sent_at TEXT NOT NULL DEFAULT '',
            message_id TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            attempts INTEGER NOT NULL DEFAULT 0
        )""")
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
            "ups_status_detail": "TEXT NOT NULL DEFAULT ''",
            "ups_event_code": "TEXT NOT NULL DEFAULT ''",
            "ups_tracking_code": "TEXT NOT NULL DEFAULT ''",
            "tracking_extras": "TEXT NOT NULL DEFAULT '{}'",
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
        db.execute("""CREATE TABLE IF NOT EXISTS ups_tracking_payloads (
            tracking TEXT PRIMARY KEY REFERENCES shipments(tracking),
            response_json_zlib BLOB NOT NULL,
            response_sha256 TEXT NOT NULL,
            raw_bytes INTEGER NOT NULL,
            captured_at TEXT NOT NULL,
            http_status INTEGER NOT NULL DEFAULT 200
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS ups_backfill (
            tracking TEXT PRIMARY KEY REFERENCES shipments(tracking),
            state TEXT NOT NULL DEFAULT 'queued',
            attempts INTEGER NOT NULL DEFAULT 0,
            next_try TEXT NOT NULL DEFAULT '',
            last_http INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            finished_at TEXT NOT NULL DEFAULT ''
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS ix_ups_backfill_due "
                   "ON ups_backfill(state,next_try)")
        db.execute("""CREATE TABLE IF NOT EXISTS mail_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tracking TEXT NOT NULL REFERENCES shipments(tracking),
            event TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'queued',
            created_at TEXT NOT NULL,
            sent_at TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            UNIQUE(tracking,event)
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS ix_mail_notifications_state "
                   "ON mail_notifications(state,id)")
        db.execute("""CREATE TABLE IF NOT EXISTS verified_gmail_smtp (
            email TEXT PRIMARY KEY,
            secret_hash TEXT NOT NULL,
            verified_at TEXT NOT NULL
        )""")
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
    # Reconcile legacy or renamed PDFs, but do not hash and glob-scan thousands
    # of already canonical documents on every web and worker startup.
    # This keeps migrations idempotent and preserves legacy originals as backup.
    with connection() as db:
        ready = [dict(row) for row in db.execute(
            "SELECT tracking,shipment_id,ship_date,pod_path FROM shipments WHERE state='ready'"
        )]
    for record in ready:
        tracking = record["tracking"]
        try:
            expected = DOCUMENTS_DIR / policy_filename(
                tracking, record["shipment_id"], record["ship_date"])
            observed = Path(record["pod_path"]) if record["pod_path"] else None
            if observed == expected and not expected.is_symlink() and expected.is_file():
                with expected.open("rb") as src:
                    if src.read(4) == b"%PDF":
                        continue
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
    """Global POD and UPS-status counters; monthly buckets use UPS pickup dates.

    Shipment ship_date is the UPS pickup date when available. Legacy rows
    without a ship date use the date they entered the tracking queue. Missing
    POD means state != ready, irrespective of retry scheduling.
    """
    today = dt.datetime.now(ZoneInfo("Europe/Warsaw"))
    year = str(today.year)
    month_keys = []
    for shift in range(11, -1, -1):
        index = today.year * 12 + today.month - 1 - shift
        month_keys.append(f"{index // 12:04d}-{index % 12 + 1:02d}")
    per_month = dict.fromkeys(month_keys, 0)
    counts = {"ready": 0, "queued": 0, "processing": 0, "retry": 0}
    pending_year = 0
    delayed = 0
    on_way = 0
    rescheduled = 0
    with connection() as db:
        records = db.execute(
            "SELECT state,ups_status,ship_date,created_at FROM shipments"
        ).fetchall()
    for record in records:
        state = record["state"]
        counts[state] = counts.get(state, 0) + 1
        if state == "ready":
            continue
        shipped = record["ship_date"] or record["created_at"]
        month = shipped[:7]
        if month.startswith(year + "-"):
            pending_year += 1
        if month in per_month:
            per_month[month] += 1
        raw = (record["ups_status"] or "").casefold()
        if "delay" in raw or "opóźn" in raw:
            delayed += 1
        elif "reschedul" in raw or "przełożon" in raw:
            rescheduled += 1
        elif "on the way" in raw or "w drodze" in raw or "in transit" in raw:
            on_way += 1
    pending_total = sum(counts.values()) - counts.get("ready", 0)
    return {
        "total": sum(counts.values()),
        "ready": counts.get("ready", 0),
        "queued": counts.get("queued", 0),
        "processing": counts.get("processing", 0),
        "retry": counts.get("retry", 0),
        "pending": pending_total,
        "pending_year": pending_year,
        "year": today.year,
        "delayed": delayed,
        "on_way": on_way,
        "rescheduled": rescheduled,
        "on_way_or_rescheduled": on_way + rescheduled,
        "pending_months": [{"month": month, "pending": count}
                           for month, count in per_month.items()],
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


# Expose a deliberately bounded set of actual UPS API fields. Never export
# packageAddress, receiver identity, signature/photograph, raw POD or account data.
# A public-facing API schema can contain sensitive blobs; keys alone are not
# an authorization to expose their values to every dashboard operator.
UPS_EXTRA_COLUMNS = {
    "ups.service": ("Rodzaj usługi UPS", "shipment.service.description"),
    "ups.pickup_date": ("Data przejęcia przez UPS", "shipment.pickupDate"),
    "ups.package_count": ("Liczba paczek w przesyłce", "shipment.packageCount"),
    "ups.status_detail": ("Komunikat o statusie UPS", "package.currentStatus.simplifiedTextDescription"),
    "ups.status_code": ("Kod aktualnego statusu UPS", "package.currentStatus.code"),
    "ups.status_type": ("Kategoria ostatniego zdarzenia", "package.activity[0].status.type"),
    "ups.event_code": ("Kod ostatniego zdarzenia", "package.activity[0].status.code"),
    "ups.event_status_code": ("Kod skanu ostatniego zdarzenia", "package.activity[0].status.statusCode"),
    "ups.activity_description": ("Opis ostatniego skanu", "package.activity[0].status.description"),
    "ups.activity_date": ("Data ostatniego skanu", "package.activity[0].date"),
    "ups.activity_time": ("Godzina ostatniego skanu", "package.activity[0].time"),
    "ups.delivery_date_type": ("Rodzaj terminu doręczenia", "package.deliveryDate[0].type"),
    "ups.delivery_time_type": ("Rodzaj przedziału doręczenia", "package.deliveryTime.type"),
    "ups.delivery_start": ("Początek przedziału doręczenia", "package.deliveryTime.startTime"),
    "ups.delivery_end": ("Koniec przedziału doręczenia", "package.deliveryTime.endTime"),
    "ups.weight_unit": ("Jednostka masy paczki", "package.weight.unitOfMeasurement"),
    "ups.weight_value": ("Masa według UPS", "package.weight.weight"),
    "ups.service_code": ("Kod usługi przesyłki", "shipment.service.code"),
    "ups.package_service_code": ("Kod usługi paczki", "package.service.code"),
    "ups.package_service_desc": ("Nazwa usługi paczki", "package.service.description"),
    "ups.service_level": ("Poziom usługi", "package.service.levelCode"),
    "ups.shipment_type": ("Typ przesyłki", "shipment.shipmentType"),
    "ups.length": ("Długość paczki", "package.dimension.length"),
    "ups.width": ("Szerokość paczki", "package.dimension.width"),
    "ups.height": ("Wysokość paczki", "package.dimension.height"),
    "ups.dimension_unit": ("Jednostka wymiarów", "package.dimension.unitOfDimension"),
    "ups.activity_gmt_date": ("Data ostatniego skanu UTC", "package.activity[0].gmtDate"),
    "ups.activity_gmt_time": ("Godzina ostatniego skanu UTC", "package.activity[0].gmtTime"),
    "ups.activity_gmt_offset": ("Przesunięcie strefy skanu", "package.activity[0].gmtOffset"),
    "ups.activity_country": ("Kraj ostatniego skanu", "package.activity[0].destCountryCode"),
    "ups.activity_location": ("Kod placówki skanu", "package.activity[0].location.code"),
    "ups.activity_slic": ("Kod sortowni skanu", "package.activity[0].location.slic"),
    "ups.activity_logical": ("Rodzaj skanu systemowego", "package.activity[0].logicalScan"),
    "ups.exception_reason": ("Kod przyczyny wyjątku", "package.activity[0].xpldDetails.exceptionReasonCode"),
    "ups.exception_resolution": ("Kod rozstrzygnięcia wyjątku", "package.activity[0].xpldDetails.exceptionResolutionCode"),
    "ups.exception_status": ("Kod statusu wyjątku", "package.activity[0].xpldDetails.exceptionStatusCode"),
    "ups.healthcare": ("Paczka medyczna", "package.isHealthCarePackage"),
    "ups.smart_package": ("Paczka Smart Package", "package.isSmartPackage"),
    "ups.insurance": ("Ochrona Insurance Shield", "package.isInsureShieldProtected"),
    "ups.current_description": ("Pełny opis bieżącego statusu", "package.currentStatus.description"),
    "ups.delivery_date_value": ("Data terminu doręczenia UPS", "package.deliveryDate[0].date"),
    "ups.package_count_detail": ("Liczba elementów paczki", "package.packageCount"),
    "ups.activity_dest_slic": ("Sortownia docelowa ostatniego skanu", "package.activity[0].destSLIC"),
    "ups.ulo_number": ("Identyfikator placówki skanu", "package.activity[0].uloNumber"),
    "ups.ulo_type": ("Typ placówki skanu", "package.activity[0].uloTypeCode"),
    "ups.tax_indicator": ("Oznaczenie podatkowe paczki", "package.taxIndicator"),
    "ups.return_eligible": ("Możliwość zwrotu UPS", "package.preAuthorizedReturnInfo.returnEligibilityIndicator"),
}

# Client-visible fields are explicitly mapped to fixed SQL expressions.
# Never interpolate user-supplied names or directions into SQL.
LIST_COLUMNS = {
    "fba": "COALESCE(NULLIF(shipment_id,''),'Nieprzypisany')",
    "tracking": "tracking",
    "ship_date": "COALESCE(NULLIF(ship_date,''),'—')",
    "delivery_date": "CASE WHEN LENGTH(delivery_date)=8 THEN "
        "SUBSTR(delivery_date,1,4)||'-'||SUBSTR(delivery_date,5,2)||'-'||"
        "SUBSTR(delivery_date,7,2) ELSE '—' END",
     "weight": "CASE WHEN weight_value='' THEN '—' ELSE "
        "weight_value||' '||CASE weight_unit WHEN 'KGS' THEN 'kg' "
        "WHEN 'LBS' THEN 'lb' ELSE weight_unit END END",
    "ups_status": "CASE "
        "WHEN LOWER(ups_status)='on the way: delayed' THEN 'W drodze — opóźniona' "
        "WHEN LOWER(ups_status)='delayed' THEN 'Opóźniona' "
        "WHEN LOWER(ups_status)='on the way' THEN 'W drodze' "
        "WHEN LOWER(ups_status)='delivery rescheduled' THEN 'Zmieniono termin doręczenia' "
        "WHEN LOWER(ups_status)='reschedule delivery requested' "
        "THEN 'Zlecono zmianę terminu doręczenia' "
        "WHEN LOWER(ups_status)='out for delivery' THEN 'W doręczeniu' "
        "WHEN LOWER(ups_status)='delivered' THEN 'Doręczono' "
        "ELSE COALESCE(NULLIF(ups_status,''),'Bez statusu') END",
    "queue": "CASE WHEN state='ready' THEN 'POD pobrany' "
         "WHEN state='processing' THEN 'Pobieranie…' "
        "WHEN state='queued' THEN 'W kolejce' "
        "WHEN error='Przesyłka jeszcze niedoręczona.' OR "
        "LOWER(ups_status) LIKE '%w drodze%' THEN 'W drodze' "
         "ELSE 'Brak POD / ponowienie' END",
    "document": "CASE WHEN state='ready' THEN 'PDF' "
        "WHEN state='retry' AND NOT "
        "(error='Przesyłka jeszcze niedoręczona.' OR "
        "LOWER(ups_status) LIKE '%w drodze%') THEN 'Błąd' "
         "ELSE 'Oczekuje' END",
}
# All JSON paths below originate in the fixed server-side whitelist.
for _key in UPS_EXTRA_COLUMNS:
    LIST_COLUMNS[_key] = f"""COALESCE(json_extract(tracking_extras, '$."{_key}"'), '')"""
SORT_COLUMNS = dict(LIST_COLUMNS, weight="CAST(NULLIF(weight_value,'') AS REAL)")


def list_numbers(search: str = "", scope: str = "all", limit: int = 1500,
                 offset: int = 0, month: str = "all", sort_by: str = "ship_date",
                 sort_dir: str = "desc", column_filters: dict[str, str] | None = None) -> dict:
    """Page, filter and sort the complete SQLite dataset, not just the visible page."""
    limit = max(1, min(limit, 2000))
    offset = max(0, offset)
    if sort_by not in SORT_COLUMNS or sort_dir not in {"asc", "desc"}:
        raise ValueError("Nieprawidłowe sortowanie.")
    if column_filters is not None and (
        not isinstance(column_filters, dict)
        or set(column_filters).difference(LIST_COLUMNS)
        or any(not isinstance(v, str) or len(v) > 100 for v in column_filters.values())
    ):
        raise ValueError("Nieprawidłowe filtry kolumn.")
    where = ["1=1"]
    params: list[str] = []
    if scope == "ready":
        where.append("state='ready'")
    elif scope == "missing":
        where.append("state!='ready'")
    elif scope in ("delayed", "onway"):
        # Same disjoint grouping as global stats(): delayed always has priority.
        raw = "LOWER(COALESCE(ups_status,''))"
        is_delayed = f"({raw} LIKE '%delay%' OR {raw} LIKE '%opóźn%')"
        is_onway = "(" + " OR ".join(f"{raw} LIKE '%{term}%'" for term in
            ("reschedul", "przełożon", "on the way", "w drodze", "in transit")) + ")"
        where.append("state!='ready'")
        where.append(is_delayed if scope == "delayed"
                     else f"(NOT {is_delayed} AND {is_onway})")
    elif scope != "all":
        raise ValueError("Nieprawidłowy filtr.")
    if month and month != "all":
        if re.fullmatch(r"\d{4}", month):
            where.append("SUBSTR(COALESCE(NULLIF(ship_date,''),created_at),1,4)=?")
        elif re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            where.append("SUBSTR(COALESCE(NULLIF(ship_date,''),created_at),1,7)=?")
        else:
            raise ValueError("Miesiąc lub rok musi mieć postać RRRR albo RRRR-MM.")
        params.append(month)
    if search:
        where.append("(tracking LIKE ? OR shipment_id LIKE ? OR description LIKE ? OR error LIKE ?)")
        params.extend([f"%{search[:100]}%"] * 4)
    for column, value in (column_filters or {}).items():
        text = value.strip()
        if text:
            where.append(f"INSTR(LOWER({LIST_COLUMNS[column]}), LOWER(?)) > 0")
            params.append(text)
    condition = " AND ".join(where)
    order_field = SORT_COLUMNS[sort_by]
    order = (f"({order_field} IS NULL OR CAST({order_field} AS TEXT)='') ASC, "
             f"{order_field} {sort_dir.upper()}, created_at DESC, tracking ASC")
    with connection() as db:
        total = db.execute(f"SELECT COUNT(*) FROM shipments WHERE {condition}", params).fetchone()[0]
        rows = db.execute(
            "SELECT tracking,state,description,error,attempts,created_at,updated_at,"
            "last_attempt_at,next_attempt_at,delivered_at,pod_path,shipment_id,ship_date,"
            "ship_date_source,ups_status,ups_status_code,weight_value,weight_unit,"
            "delivery_time_type,delivery_time_start,delivery_time_end,delivery_date,"
             "delivery_date_type,received_by,photo_filename,ups_status_detail,"
            "ups_event_code,ups_tracking_code,tracking_extras,"
            "(SELECT sc.status FROM support_cases AS sc "
            "WHERE sc.tracking=shipments.tracking) AS support_status "
            "FROM shipments WHERE " + condition +
            " ORDER BY " + order + " LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    result_rows = []
    for record in rows:
        row = dict(record)
        row["ups_status_pl"], row["ups_detail_pl"] = ups_localization.presentation(row)
        try:
            raw = json.loads(row.pop("tracking_extras") or "{}")
            row["ups_extra"] = {key: str(raw.get(key, ""))[:180]
                                for key in UPS_EXTRA_COLUMNS if raw.get(key)}
        except (TypeError, ValueError, AttributeError):
            row["ups_extra"] = {}
        result_rows.append(row)
    return {"total": total, "rows": result_rows,
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
    detail_text = safe_description(detail.get("status_detail") or "")[:450]
    event_code = str(detail.get("event_code") or "")[:30]
    tracking_code = str(detail.get("status_code") or "")[:30]
    date = str(detail.get("ship_date") or "")
    source = str(detail.get("ship_date_source") or "")
    with connection() as db:
        previous = db.execute("SELECT ups_status,ups_status_code FROM shipments "
                              "WHERE tracking=?", (tracking,)).fetchone()
        was_late = bool(previous and (
            "delay" in (previous["ups_status"] or "").lower()
            or "opóźn" in (previous["ups_status"] or "").lower()))
        is_delivered = code == "D" or status.strip().lower() in {"delivered", "doręczono"}
        db.execute(
            """UPDATE shipments SET ups_status=?,ups_status_code=?,
               ups_status_detail=?,ups_event_code=?,ups_tracking_code=?,
               ship_date=CASE WHEN ?='' THEN ship_date ELSE ? END,
               ship_date_source=CASE WHEN ?='' THEN ship_date_source ELSE ? END,
               updated_at=? WHERE tracking=?""",
            (status, code, detail_text, event_code, tracking_code,
             date, date, date, source, utcnow(), tracking),
        )
        if was_late and is_delivered:
            db.execute("INSERT OR IGNORE INTO mail_notifications"
                       "(tracking,event,state,created_at) VALUES(?, 'delayed_to_delivered',"
                       "'queued',?)", (tracking, utcnow()))
    sync_named_pdf(tracking)



def save_tracking_payload(tracking: str, payload: dict, http_status: int = 200) -> None:
    """Persist the *complete* nested Tracking API response in lossless SQLite BLOB.

    All keys, arrays, nulls, and values are preserved as UTF-8 JSON compressed
    with zlib. Private fields (addresses, POD content, signatures) are never
    returned by dashboard APIs. A response must have the requested package.
    """
    if not NUMBER.fullmatch(tracking) or not isinstance(payload, dict):
        raise ValueError("Nieprawidłowa odpowiedź Tracking API.")
    if not _first_package(payload, tracking):
        raise ValueError("Brak numeru przesyłki w odpowiedzi Tracking API.")
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
    if len(encoded) > 24 * 1024 * 1024:
        raise ValueError("Odpowiedź UPS przekracza limit 24 MiB.")
    compressed = zlib.compress(encoded, level=6)
    with connection() as db:
        db.execute("""INSERT INTO ups_tracking_payloads
            (tracking,response_json_zlib,response_sha256,raw_bytes,captured_at,http_status)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(tracking) DO UPDATE SET
                response_json_zlib=excluded.response_json_zlib,
                response_sha256=excluded.response_sha256,
                raw_bytes=excluded.raw_bytes,
                captured_at=excluded.captured_at,
                http_status=excluded.http_status""",
            (tracking, sqlite3.Binary(compressed), hashlib.sha256(encoded).hexdigest(),
             len(encoded), utcnow(), http_status))


def pending_backfill_count() -> dict:
    with connection() as db:
        counts = db.execute("SELECT state,COUNT(*) n FROM ups_backfill "
                            "GROUP BY state").fetchall()
    result = {r["state"]: r["n"] for r in counts}
    return {"queued": result.get("queued", 0), "done": result.get("done", 0),
            "error": result.get("error", 0)}


def enqueue_missing_payloads() -> int:
    """Idempotent: enqueue missing raw responses only, without modifying POD state."""
    with connection() as db:
        count = db.execute("""INSERT OR IGNORE INTO ups_backfill(tracking)
            SELECT s.tracking FROM shipments s
            LEFT JOIN ups_tracking_payloads p ON p.tracking=s.tracking
            WHERE p.tracking IS NULL""").rowcount
    return count


def next_backfill() -> str | None:
    with connection() as db:
        row = db.execute("""SELECT tracking FROM ups_backfill
            WHERE state IN ('queued','error') AND attempts<3
              AND (next_try='' OR next_try<=?)
            ORDER BY CASE state WHEN 'queued' THEN 0 ELSE 1 END,
                     attempts,tracking LIMIT 1""", (utcnow(),)).fetchone()
    return row["tracking"] if row else None


def finish_backfill(tracking: str, *, http_status: int, error: str = "") -> None:
    with connection() as db:
        if error:
            db.execute("""UPDATE ups_backfill SET
                state='error',attempts=attempts+1,last_http=?,
                last_error=?,next_try=?,finished_at=''
                WHERE tracking=?""",
                (http_status, error[:150],
                 (dt.datetime.now(dt.timezone.utc) +
                  dt.timedelta(hours=3)).isoformat(timespec="seconds"), tracking))
        else:
            db.execute("""UPDATE ups_backfill SET
                state='done',attempts=attempts+1,last_http=?,
                last_error='',next_try='',finished_at=?
                WHERE tracking=?""", (http_status, utcnow(), tracking))


def tracking_payload_info() -> dict:
    with connection() as db:
        total = db.execute("SELECT COUNT(*) FROM ups_tracking_payloads").fetchone()[0]
    return {"stored": total, **pending_backfill_count()}


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
    shipments = (payload.get("trackResponse") or {}).get("shipment") or []
    shipment = next((sh for sh in shipments if isinstance(sh, dict)
                     and any(pkg.get("trackingNumber", "").upper() == tracking.upper()
                             for pkg in sh.get("package") or []
                             if isinstance(pkg, dict))), shipments[0] if shipments else {})
    status = p.get("currentStatus") or {}
    event = next((evt for evt in (p.get("activity") or [])
                  if isinstance(evt, dict)), {})
    event_status = event.get("status") or {}
    first_date = next((date for date in (p.get("deliveryDate") or [])
                       if isinstance(date, dict)), {})
    delivery_window = p.get("deliveryTime") or {}
    def safe_field(value):
        if isinstance(value, bool):
            return "Tak" if value else "Nie"
        if not isinstance(value, (str, int, float)):
            return ""
        return safe_description(str(value))[:180]
    raw_extra = {
        "ups.service": (shipment.get("service") or {}).get("description"),
        "ups.pickup_date": shipment.get("pickupDate"),
        "ups.package_count": shipment.get("packageCount"),
        "ups.status_detail": status.get("simplifiedTextDescription"),
        "ups.status_code": status.get("code"),
        "ups.status_type": event_status.get("type"),
        "ups.event_code": event_status.get("code"),
        "ups.event_status_code": event_status.get("statusCode"),
        "ups.activity_description": event_status.get("description"),
        "ups.activity_date": event.get("date"),
        "ups.activity_time": event.get("time"),
        "ups.delivery_date_type": first_date.get("type"),
        "ups.delivery_time_type": delivery_window.get("type"),
        "ups.delivery_start": delivery_window.get("startTime"),
        "ups.delivery_end": delivery_window.get("endTime"),
        "ups.weight_unit": (p.get("weight") or {}).get("unitOfMeasurement"),
        "ups.weight_value": (p.get("weight") or {}).get("weight"),
        "ups.service_code": (shipment.get("service") or {}).get("code"),
        "ups.package_service_code": (p.get("service") or {}).get("code"),
        "ups.package_service_desc": (p.get("service") or {}).get("description"),
        "ups.service_level": (p.get("service") or {}).get("levelCode"),
        "ups.shipment_type": shipment.get("shipmentType"),
        "ups.length": (p.get("dimension") or {}).get("length"),
        "ups.width": (p.get("dimension") or {}).get("width"),
        "ups.height": (p.get("dimension") or {}).get("height"),
        "ups.dimension_unit": (p.get("dimension") or {}).get("unitOfDimension"),
        "ups.activity_gmt_date": event.get("gmtDate"),
        "ups.activity_gmt_time": event.get("gmtTime"),
        "ups.activity_gmt_offset": event.get("gmtOffset"),
        "ups.activity_country": event.get("destCountryCode"),
        "ups.activity_location": (event.get("location") or {}).get("code"),
        "ups.activity_slic": (event.get("location") or {}).get("slic"),
        "ups.activity_logical": event.get("logicalScan"),
        "ups.exception_reason": (event.get("xpldDetails") or {}).get("exceptionReasonCode"),
        "ups.exception_resolution": (event.get("xpldDetails") or {}).get("exceptionResolutionCode"),
        "ups.exception_status": (event.get("xpldDetails") or {}).get("exceptionStatusCode"),
        "ups.healthcare": p.get("isHealthCarePackage"),
        "ups.smart_package": p.get("isSmartPackage"),
        "ups.insurance": p.get("isInsureShieldProtected"),
        "ups.current_description": status.get("description"),
        "ups.delivery_date_value": first_date.get("date"),
        "ups.package_count_detail": p.get("packageCount"),
        "ups.activity_dest_slic": event.get("destSLIC"),
        "ups.ulo_number": event.get("uloNumber"),
        "ups.ulo_type": event.get("uloTypeCode"),
        "ups.tax_indicator": p.get("taxIndicator"),
        "ups.return_eligible": (p.get("preAuthorizedReturnInfo") or {}).get("returnEligibilityIndicator"),
    }
    extras = {key: text for key, value in raw_extra.items()
              if key in UPS_EXTRA_COLUMNS and (text := safe_field(value))}
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
             "tracking_extras=?,updated_at=? WHERE tracking=?",
            (value(weight, "weight", 30),value(weight,"unitOfMeasurement",12),
             value(time_data,"type",12),value(time_data,"startTime",12),
             value(time_data,"endTime",12),date_yyyymmdd,value(selected_date,"type",12),
             value(delivered,"receivedBy",120),photo_filename or existing_photo,
             utcnow(),json.dumps(extras, ensure_ascii=False),utcnow(),tracking),
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


def reindex_archived_tracking_columns() -> int:
    """Rebuild safe UI columns from already archived, complete UPS JSON.

    Metadata only: never change state, POD, recipient, document or delivery date.
    Previous Tracking API responses were stored losslessly.
    """
    marker = DATA_DIR / ".ups_extras_schema_20261107_v3"
    if marker.exists():
        return 0
    count = 0

    def get_path(obj, path):
        for part in path.split("."):
            index = 0 if part.endswith("[0]") else None
            part = part.removesuffix("[0]")
            if not isinstance(obj, dict):
                return None
            obj = obj.get(part)
            if index is not None:
                if not isinstance(obj, list) or not obj:
                    return None
                obj = obj[0]
        return obj

    with connection() as db:
        saved = db.execute(
            "SELECT tracking,response_json_zlib FROM ups_tracking_payloads"
        ).fetchall()
    for record in saved:
        try:
            document = json.loads(zlib.decompress(record["response_json_zlib"]))
            packages = (document.get("trackResponse") or {}).get("shipment") or []
            matched = next(((ship, pkg) for ship in packages if isinstance(ship, dict)
                            for pkg in ship.get("package") or []
                            if isinstance(pkg, dict) and
                            str(pkg.get("trackingNumber", "")).upper() ==
                            record["tracking"].upper()), None)
            if matched is None:
                continue
            shipment, package = matched
            extras = {}
            for key, (_label, path) in UPS_EXTRA_COLUMNS.items():
                root, _, rest = path.partition(".")
                value = get_path(shipment if root == "shipment" else package, rest)
                if isinstance(value, bool):
                    text = "Tak" if value else "Nie"
                elif isinstance(value, (str, int, float)):
                    text = safe_description(str(value))[:180]
                else:
                    text = ""
                if text:
                    extras[key] = text
            with connection() as db:
                old = db.execute("SELECT tracking_extras FROM shipments WHERE tracking=?",
                                 (record["tracking"],)).fetchone()
                if not old:
                    continue
                try:
                    existing = json.loads(old["tracking_extras"] or "{}")
                except (ValueError, TypeError):
                    existing = {}
                current = dict(existing)
                current.update(extras)
                if current != existing:
                    db.execute("UPDATE shipments SET tracking_extras=? WHERE tracking=?",
                               (json.dumps(current, ensure_ascii=False), record["tracking"]))
                    count += 1
        except (ValueError, TypeError, KeyError, zlib.error):
            continue
    marker.write_text("safe additional keys only\n", encoding="utf-8")
    return count


def tracking_field_paths() -> list[str]:
    with connection() as db:
        return [r[0] for r in db.execute("SELECT path FROM tracking_fields ORDER BY path")]

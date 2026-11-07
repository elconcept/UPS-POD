"""Persistent SQLite tracking queue shared by the web UI and a single worker."""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
import shutil
import sqlite3
from pathlib import Path

DATA_DIR = Path(os.getenv("UPS_POD_HOME", "/data"))
DB_PATH = DATA_DIR / "ups_pod.sqlite3"
DOCUMENTS_DIR = DATA_DIR / "pod"
SOURCES_DIR = DATA_DIR / "source"
EXPORTS_DIR = DATA_DIR / "fba_pod"
MAX_BATCH = 5000
FBA_ID = re.compile(r"^FBA[A-Z0-9]{6,30}$")
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
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
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


def add_numbers(raw: str) -> dict:
    numbers, rejected = parse_tracking_numbers(raw)
    if not numbers:
        raise ValueError("Nie znaleziono poprawnych numerów UPS.")
    if len(numbers) > MAX_BATCH:
        raise ValueError(f"Limit jednorazowego importu: {MAX_BATCH} numerów.")
    inserted = 0
    now = utcnow()
    with connection() as db:
        for number in numbers:
            cursor = db.execute(
                "INSERT OR IGNORE INTO shipments(tracking,state,created_at,updated_at,next_attempt_at) "
                "VALUES(?, 'queued', ?, ?, ?)", (number, now, now, now)
            )
            inserted += cursor.rowcount
    audit("user", "import", description=f"Dodano {inserted}; istniejące {len(numbers)-inserted}.")
    return {
        "added": inserted,
        "existing": len(numbers) - inserted,
        "rejected": rejected[:100],
        "submitted_unique": len(numbers),
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


def list_numbers(search: str = "", scope: str = "all", limit: int = 200, offset: int = 0) -> dict:
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
    if search:
        where.append("(tracking LIKE ? OR shipment_id LIKE ? OR description LIKE ? OR error LIKE ?)")
        params.extend([f"%{search[:100]}%"] * 4)
    condition = " AND ".join(where)
    with connection() as db:
        total = db.execute(f"SELECT COUNT(*) FROM shipments WHERE {condition}", params).fetchone()[0]
        rows = db.execute(
            f"SELECT tracking,state,description,error,attempts,created_at,updated_at,"
            f"last_attempt_at,next_attempt_at,delivered_at,pod_path,shipment_id,ship_date,"
            f"ship_date_source,ups_status,ups_status_code FROM shipments WHERE {condition} "
            "ORDER BY created_at DESC, tracking ASC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    return {"total": total, "rows": [dict(r) for r in rows]}


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

    audit("worker", "retry_scheduled", number, description=f"{error}; kolejna próba {next_time}")


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
    if operation in {"ups_network_error", "credentials_missing"} or (http_status or 0) >= 500:
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


def audit_rows(after: int = 0, before: int = 0, levels: list[str] | None = None) -> list[dict]:
    levels = levels or []
    if any(x not in {"INFO", "SUCCESS", "WARNING", "ERROR"} for x in levels):
        raise ValueError("Nieprawidłowy poziom logów.")
    extra = " AND level IN (" + ",".join("?" for _ in levels) + ")" if levels else ""
    with connection() as db:
        return [dict(row) for row in db.execute(
            "SELECT * FROM audit WHERE id>? AND (?=0 OR id<?)" + extra +
            " ORDER BY id DESC LIMIT 200", (after, before, before, *levels))]


def parse_fba_text(raw: str) -> list[dict]:
    """Paste two or three Excel columns: FBA | UPS list, or date | FBA | UPS list."""
    assignments: list[dict] = []
    for i, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        cells = [x.strip() for x in line.split("\t")]
        if len(cells) < 2:
            cells = [x.strip() for x in re.split(r"\s{2,}", line.strip())]
        if len(cells) < 2:
            raise ValueError(f"Wiersz {i}: wymagane przynajmniej dwie kolumny (FBA i trackingi).")
        if cells[0].lower() in {"data", "date", "ship date", "shipment id", "fba", "fba id"}:
            continue
        if len(cells) == 2:
            shipment_id, raw_trackings = cells
            ship_date = ""
        elif cells[0].upper().startswith("FBA"):
            shipment_id, ship_date = cells[:2]
            raw_trackings = " ".join(cells[2:])
        else:
            ship_date, shipment_id = cells[:2]
            raw_trackings = " ".join(cells[2:])
        numbers, _ = parse_tracking_numbers(raw_trackings)
        if not numbers:
            raise ValueError(f"Wiersz {i}: brak poprawnych numerów UPS.")
        for number in numbers:
            assignments.append({"tracking": number, "shipment_id": shipment_id.upper(),
                                "ship_date": ship_date})
    if not assignments:
        raise ValueError("Brak danych FBA do importu.")
    return assignments


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
        if not NUMBER.fullmatch(tracking) or not FBA_ID.fullmatch(fba):
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
                    and row["shipment_id"] and not replace):
                raise ValueError(f"Konflikt daty dla {tracking}: istnieje {row['ship_date']}.")
            if not row:
                db.execute("INSERT INTO shipments(tracking,created_at,updated_at,next_attempt_at) "
                           "VALUES(?,?,?,?)", (tracking, now, now, now))
            final_date = date or (row["ship_date"] if row else "")
            final_source = "manual" if date else (row["ship_date_source"] if row else "")
            if not row or row["shipment_id"] != fba or row["ship_date"] != final_date:
                changed += 1
            db.execute("UPDATE shipments SET shipment_id=?,ship_date=?,ship_date_source=?,"
                       "updated_at=? WHERE tracking=?", (fba, final_date, final_source, now, tracking))
    for number in checked:
        sync_named_pdf(number)
    audit("user", "fba_mapping", description=f"Przypisano {len(checked)} numerów; zmieniono {changed}.")
    return {"assigned": len(checked), "changed": changed}


def save_tracking_detail(tracking: str, detail: dict) -> None:
    """Never overwrite a shipment date supplied by an operator."""
    status = str(detail.get("status") or "")[:150]
    code = str(detail.get("status_typ") or "")[:20]
    date = str(detail.get("ship_date") or "")
    source = str(detail.get("ship_date_source") or "")
    with connection() as db:
        db.execute(
            """UPDATE shipments SET ups_status=?,ups_status_code=?,
               ship_date=CASE WHEN ship_date_source='manual' OR ?='' THEN ship_date ELSE ? END,
               ship_date_source=CASE WHEN ship_date_source='manual' OR ?='' THEN ship_date_source ELSE ? END,
               updated_at=? WHERE tracking=?""",
            (status, code, date, date, date, source, utcnow(), tracking),
        )
    sync_named_pdf(tracking)


def export_filename(tracking: str) -> str:
    with connection() as db:
        row = db.execute("SELECT shipment_id,ship_date FROM shipments WHERE tracking=?",
                         (tracking,)).fetchone()
    if not row or not row["shipment_id"] or not row["ship_date"]:
        raise ValueError(f"Brak FBA Shipment ID albo daty nadania dla {tracking}.")
    return f"{row['ship_date']}-{row['shipment_id']}-{tracking}.pdf"



def sync_named_pdf(tracking: str) -> Path | None:
    """Maintain policy-named on-disk archive alongside untouched UPS source and tracking cache."""
    with connection() as db:
        row = db.execute(
            "SELECT shipment_id,ship_date,pod_path,state FROM shipments WHERE tracking=?", (tracking,)
        ).fetchone()
    if not row or not row["shipment_id"] or not row["ship_date"] or row["state"] != "ready":
        return None
    filename = export_filename(tracking)
    source = Path(row["pod_path"] or "").resolve()
    if not source.is_relative_to(DOCUMENTS_DIR.resolve()) or not source.is_file():
        return None
    with source.open("rb") as stream:
        signature = stream.read(4)
    if signature != b"%PDF":
        raise ValueError("Nieprawidłowy dokument PDF w archiwum.")
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    target = EXPORTS_DIR / filename
    if target.exists() and target.stat().st_size == source.stat().st_size:
        if hashlib.sha256(target.read_bytes()).digest() == hashlib.sha256(source.read_bytes()).digest():
            return target
    tmp = target.with_suffix(".pdf.tmp")
    try:
        shutil.copy2(source, tmp)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    for old in EXPORTS_DIR.glob(f"*-{tracking}.pdf"):
        if old != target:
            old.unlink(missing_ok=True)
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

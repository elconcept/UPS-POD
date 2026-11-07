"""Persistent SQLite tracking queue shared by the web UI and a single worker."""
from __future__ import annotations

import datetime as dt
import os
import re
import sqlite3
from pathlib import Path

DATA_DIR = Path(os.getenv("UPS_POD_HOME", "/data"))
DB_PATH = DATA_DIR / "ups_pod.sqlite3"
DOCUMENTS_DIR = DATA_DIR / "pod"
SOURCES_DIR = DATA_DIR / "source"
MAX_BATCH = 5000
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
        db.execute("CREATE INDEX IF NOT EXISTS ix_shipments_due ON shipments(state, next_attempt_at)")
        db.execute("CREATE INDEX IF NOT EXISTS ix_shipments_created ON shipments(created_at)")
        # Recover an interrupted job after a worker restart.
        db.execute(
            "UPDATE shipments SET state='retry', next_attempt_at=?, updated_at=? WHERE state='processing'",
            (utcnow(), utcnow()),
        )


def parse_tracking_numbers(raw: str) -> tuple[list[str], list[str]]:
    """Spaces, tabs, line breaks, comma and semicolon are equivalent delimiters."""
    numbers = []
    rejected = []
    seen = set()
    for raw_token in re.split(r"[\s,;]+", raw or ""):
        token = raw_token.strip().upper()
        if not token:
            continue
        if not NUMBER.fullmatch(token) or (token.startswith("1Z") and len(token) != 18):
            rejected.append(token[:80])
        elif token not in seen:
            seen.add(token)
            numbers.append(token)
    return numbers, rejected


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
        where.append("(tracking LIKE ? OR description LIKE ? OR error LIKE ?)")
        params.extend([f"%{search[:100]}%"] * 3)
    condition = " AND ".join(where)
    with connection() as db:
        total = db.execute(f"SELECT COUNT(*) FROM shipments WHERE {condition}", params).fetchone()[0]
        rows = db.execute(
            f"SELECT tracking,state,description,error,attempts,created_at,updated_at,"
            f"last_attempt_at,next_attempt_at,delivered_at,pod_path FROM shipments WHERE {condition} "
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


def mark_retry(number: str, error: str, description: str = "", delay_hours: float = 3) -> None:
    next_time = (
        dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=max(0.001, delay_hours))
    ).isoformat(timespec="seconds")
    with connection() as db:
        db.execute(
            "UPDATE shipments SET state='retry',description=?,error=?,updated_at=?,"
            "next_attempt_at=? WHERE tracking=?",
            (description[:500], error[:800], utcnow(), next_time, number),
        )


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

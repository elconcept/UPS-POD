"""Sequential UPS POD worker. Supervised alongside the web process in one container."""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import time
from pathlib import Path

import service
import ups_pod as core

logger = logging.getLogger("ups_pod.worker")
POLL_SECONDS = max(2, int(os.getenv("UPS_POD_POLL_SECONDS", "10")))
RETRY_HOURS = max(0.01, float(os.getenv("UPS_POD_RETRY_HOURS", "3")))


def credentials() -> tuple[str, str]:
    # The original company UPS file uses lowercase names; both spellings work.
    cid = os.getenv("UPS_CLIENT_ID") or os.getenv("client_id") or ""
    secret = os.getenv("UPS_CLIENT_SECRET") or os.getenv("client_secret") or ""
    return cid.strip(), secret.strip()


def attach_audit(client: core.KlientUPS) -> None:
    """Record every HTTP attempt, including OAuth and internal retries, without bodies."""
    if getattr(client, "_audit_attached", False):
        return
    client._audit_attached = True

    def response_audit(response, *args, **kwargs):
        is_tracking = "/api/track/" in response.url
        number = response.url.split("/details/")[-1].split("?")[0] if is_tracking else ""
        description = "Odpowiedź UPS OAuth" if not is_tracking else "Odpowiedź UPS Tracking"
        try:
            body = response.json()
            errors = (body.get("response") or {}).get("errors") or []
            codes = [str(e.get("code", "")) for e in errors if isinstance(e, dict)]
            # Only bounded machine codes are accepted from arbitrary error bodies.
            codes = [c for c in codes if c.isalnum() and len(c) <= 30]
            if codes:
                description += "; kody UPS: " + ", ".join(codes[:10])
        except (ValueError, AttributeError, TypeError):
            description += "; odpowiedź nie jest poprawnym JSON"
        service.audit("worker", "ups_http", number, response.status_code, description)

    client.sesja.hooks["response"].append(response_audit)
    original_send = client.sesja.send

    def audited_send(request, **kwargs):
        tracking = request.url.split("/details/")[-1].split("?")[0] if "/details/" in request.url else ""
        service.audit("worker", "ups_request", tracking,
                      description="UPS Tracking" if tracking else "UPS OAuth")
        try:
            return original_send(request, **kwargs)
        except client.requests.RequestException as exc:
            service.audit("worker", "ups_network_error", tracking,
                          description=type(exc).__name__)
            raise

    client.sesja.send = audited_send


def save_original(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def is_pdf(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 5:
        return False
    with path.open("rb") as stream:
        return stream.read(4) == b"%PDF"


def process_one(number: str, client: core.KlientUPS) -> str:
    """Check one package; preserve UPS original and a PDF in durable cache."""
    with service.connection() as db:
        existing = db.execute("SELECT pod_path FROM shipments WHERE tracking=?",
                              (number,)).fetchone()
    target = Path(existing["pod_path"]).resolve() if existing and existing["pod_path"] else None
    if target and target.is_relative_to(service.DOCUMENTS_DIR.resolve()) and is_pdf(target):
        service.mark_ready(number, "POD w lokalnej pamięci podręcznej", "", str(target), "",
                           "pdf", hashlib.sha256(target.read_bytes()).hexdigest())
        return "ready"
    try:
        attach_audit(client)
        service.audit("worker", "tracking_start", number)
        code, json_body, text = client.sledz(number)
        detail = core.analizuj(code, json_body, text)
        if isinstance(json_body, dict):
            service.save_tracking_fields(json_body)
        if detail['ok']:
            service.save_tracking_detail(number, detail)
        service.audit("worker", "tracking_result", number, code,
                      detail.get("opis", "") or detail.get("blad", ""))
        if not detail["ok"]:
            service.mark_retry(number, detail["blad"] or f"UPS HTTP {code}",
                               delay_hours=RETRY_HOURS)
            return "retry"
        if not detail["doreczono"]:
            service.mark_retry(number, "Przesyłka jeszcze niedoręczona.",
                               description=detail["opis"], delay_hours=RETRY_HOURS)
            return "retry"
        if not detail["pod"]:
            service.mark_retry(
                number, "Doręczono, lecz UPS Tracking API nie zwróciło Proof of Delivery. "
                "Sprawdź uprawnienia firmowego konta do tej przesyłki.",
                description=detail["opis"], delay_hours=RETRY_HOURS
            )
            return "retry"

        target = service.document_target(number)
        raw = detail["pod"]
        typ = detail["pod_typ"]
        suffix = {"pdf": ".pdf", "html": ".html", "gif": ".gif",
                  "jpeg": ".jpg", "png": ".png", "tekst": ".txt"}.get(typ, ".bin")
        original = service.SOURCES_DIR / (number + suffix)
        save_original(original, raw)
        converter = core.KonwerterPDF()
        try:
            core.zapisz_pod(raw, typ, target, converter)
        finally:
            converter.zamknij()
        if not is_pdf(target):
            raise ValueError("Zapisany dokument nie ma nagłówka PDF.")
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        delivered_at = " ".join(x for x in (detail["data_dor"], detail["godz_dor"]) if x)
        service.mark_ready(number, detail["opis"], delivered_at, str(target),
                           str(original), typ, digest)
        return "ready"
    except Exception as exc:
        logger.error("UPS POD failure for %s: %s", number, type(exc).__name__)
        service.mark_retry(number, f"Błąd przetwarzania: {type(exc).__name__}",
                           delay_hours=RETRY_HOURS)
        return "retry"



def rebuild_legacy_pods_once() -> int:
    """Repair PDFs whose archived UPS HTML references unavailable GIFs; keep originals."""
    marker = service.DATA_DIR / ".pod_branding_v2"
    if marker.exists():
        return 0
    backup_dir = service.DATA_DIR / "pdf_originals_before_branding_fix"
    with service.connection() as db:
        rows = db.execute(
            "SELECT tracking,pod_path,source_path FROM shipments "
            "WHERE state='ready' AND source_type='html' AND source_path IS NOT NULL"
        ).fetchall()
    regenerated = 0
    converter = core.KonwerterPDF()
    try:
        for row in rows:
            target = Path(row["pod_path"] or "").resolve()
            source = Path(row["source_path"] or "").resolve()
            if not (target.is_relative_to(service.DOCUMENTS_DIR.resolve())
                    and source.is_relative_to(service.SOURCES_DIR.resolve())
                    and target.is_file() and source.is_file()):
                continue
            html = source.read_bytes()
            if b"ups_logo.gif" not in html.lower() and b"ups_banner.gif" not in html.lower():
                continue
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup = backup_dir / (row["tracking"] + ".pdf")
            if not backup.is_file():
                shutil.copy2(target, backup)
            try:
                core.zapisz_pod(html, "html", target, converter)
                with service.connection() as db:
                    db.execute("UPDATE shipments SET sha256=?,updated_at=? WHERE tracking=?",
                               (hashlib.sha256(target.read_bytes()).hexdigest(),
                                service.utcnow(), row["tracking"]))
                service.sync_named_pdf(row["tracking"])
                service.audit("worker", "pod_graphics_rebuilt", row["tracking"],
                              description="Zachowano kopię oryginalnego PDF przed korektą.")
                regenerated += 1
            except Exception as exc:
                service.audit("worker", "pod_graphics_rebuild_failed", row["tracking"],
                              description=type(exc).__name__)
                raise
    finally:
        converter.zamknij()
    marker.write_text("POD branding v2; preserved archived originals.\n", encoding="utf-8")
    return regenerated


def run_once(client: core.KlientUPS | None = None) -> str | None:
    number = service.claim_next()
    if not number:
        return None
    if client is None:
        cid, secret = credentials()
        if not cid or not secret:
            service.mark_retry(number, "Brak Client ID lub Client Secret w .env.",
                               delay_hours=RETRY_HOURS)
            return "retry"
        client = core.KlientUPS(
            os.getenv("UPS_POD_ADRES_API", "https://onlinetools.ups.com"),
            cid, secret, jezyk="pl_PL", numer_konta=os.getenv("UPS_SHIPPER_NUMBER", ""),
        )
    result = process_one(number, client)
    service.audit("worker", result, number)
    return result


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    service.init_db()
    # Only the dedicated worker recovers interrupted items, not the web process.
    recovered = service.recover_in_progress()
    try:
        fixed = rebuild_legacy_pods_once()
        service.audit("worker", "pod_graphics_migration", description=f"Poprawiono PDF: {fixed}.")
    except Exception:
        logger.exception("Legacy POD graphics repair failed; original PDFs remain backed up.")
    service.audit("worker", "startup", description=f"Odzyskano zadań {recovered}.")
    logger.info("UPS POD worker started. Sequential processing, retry every %s hours.", RETRY_HOURS)
    client = None
    backfill_attempted = False
    while True:
        try:
            cid, secret = credentials()
            if cid and secret:
                if client is None:
                    client = core.KlientUPS(
                        os.getenv("UPS_POD_ADRES_API", "https://onlinetools.ups.com"),
                        cid, secret, jezyk="pl_PL", numer_konta=os.getenv("UPS_SHIPPER_NUMBER", ""),
                    )
                if not backfill_attempted:
                    backfill_attempted = True
                    with service.connection() as db:
                        pending = [r[0] for r in db.execute(
                            "SELECT tracking FROM shipments WHERE state='ready' AND ups_status='' LIMIT 100"
                        )]
                    for tracking in pending:
                        try:
                            code, payload, raw_text = client.sledz(tracking)
                            detail = core.analizuj(code, payload, raw_text)
                            if isinstance(payload, dict):
                                service.save_tracking_fields(payload)
                            if detail['ok']:
                                service.save_tracking_detail(tracking, detail)
                                service.audit("worker", "metadata_backfill", tracking, code)
                            else:
                                service.audit("worker", "metadata_backfill_failed", tracking, code,
                                              detail.get("blad", ""))
                        except Exception as exc:
                            service.audit("worker", "metadata_backfill_failed", tracking,
                                          description=type(exc).__name__)
                result = run_once(client)
                if result is not None:
                    continue
            else:
                service.audit("worker", "credentials_missing", description="Brak konfiguracji UPS.")
                client = None
        except Exception:
            logger.exception("Unexpected UPS queue worker error")
            client = None
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()

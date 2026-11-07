"""Sequential UPS POD worker. Runs separately from the HTTP server."""
from __future__ import annotations

import hashlib
import logging
import os
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
    target = service.DOCUMENTS_DIR / (number + ".pdf")
    if is_pdf(target):
        service.mark_ready(number, "POD w lokalnej pamięci podręcznej", "", str(target), "",
                           "pdf", hashlib.sha256(target.read_bytes()).hexdigest())
        return "ready"
    try:
        code, json_body, text = client.sledz(number)
        detail = core.analizuj(code, json_body, text)
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
        logger.exception("UPS POD failure for %s", number)
        service.mark_retry(number, f"{type(exc).__name__}: {exc}",
                           delay_hours=RETRY_HOURS)
        return "retry"


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
            cid, secret, numer_konta=os.getenv("UPS_SHIPPER_NUMBER", ""),
        )
    return process_one(number, client)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    service.init_db()
    # Only the dedicated worker recovers interrupted items, not the web process.
    service.recover_in_progress()
    logger.info("UPS POD worker started. Sequential processing, retry every %s hours.", RETRY_HOURS)
    client = None
    while True:
        try:
            cid, secret = credentials()
            if cid and secret:
                if client is None:
                    client = core.KlientUPS(
                        os.getenv("UPS_POD_ADRES_API", "https://onlinetools.ups.com"),
                        cid, secret, numer_konta=os.getenv("UPS_SHIPPER_NUMBER", ""),
                    )
                result = run_once(client)
                if result is not None:
                    continue
            else:
                client = None
        except Exception:
            logger.exception("Unexpected UPS queue worker error")
            client = None
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()

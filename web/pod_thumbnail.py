"""Cached, on-demand first-page thumbnails for archived UPS POD PDFs."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pymupdf

import service


THUMBNAIL_DIR = service.DATA_DIR / "pod_thumbnails"
MAX_PDF_BYTES = 24 * 1024 * 1024


def thumbnail(tracking: str) -> Path | None:
    if not service.NUMBER.fullmatch(tracking):
        return None
    files = service.ready_paths([tracking])
    if not files:
        return None
    pdf = files[0][1]
    if pdf.stat().st_size > MAX_PDF_BYTES:
        return None
    # Metadata key invalidates the image when a POD PDF is replaced.
    meta = pdf.stat()
    digest = hashlib.sha256(
        f"{tracking}|{meta.st_mtime_ns}|{meta.st_size}".encode("ascii")
    ).hexdigest()[:16]
    THUMBNAIL_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = THUMBNAIL_DIR / f"{tracking}-{digest}.png"
    if target.is_file():
        return target
    try:
        with pymupdf.open(pdf) as document:
            if not document.is_pdf or document.page_count < 1:
                return None
            page = document.load_page(0)
            # Limit memory: small preview only, but keep legibility.
            scale = min(1.1, 440.0 / max(1, page.rect.width))
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale),
                                    colorspace=pymupdf.csRGB, alpha=False)
            blob = pixmap.tobytes("png")
        if len(blob) > 2 * 1024 * 1024:
            return None
        temp = target.with_suffix(".tmp")
        try:
            with temp.open("wb") as out:
                os.chmod(temp, 0o600)
                out.write(blob)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
        return target
    except (ValueError, RuntimeError, OSError):
        # A corrupted historical PDF may be downloadable but not renderable.
        return None

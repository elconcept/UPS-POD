"""Cached, on-demand first-page thumbnails for archived UPS POD PDFs."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pymupdf

import service


THUMBNAIL_DIR = service.DATA_DIR / "pod_thumbnails"
MAX_PDF_BYTES = 24 * 1024 * 1024


def thumbnail(tracking: str, *, large: bool = False) -> Path | None:
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
        f"{tracking}|{meta.st_mtime_ns}|{meta.st_size}|{'large-v2' if large else 'small-v1'}".encode("ascii")
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
            # Separate cached high-resolution preview: never upscale the tiny
            # 440px table thumbnail into an unreadable large hover panel.
            scale = min(2.5 if large else 1.1,
                        (1120.0 if large else 440.0) / max(1, page.rect.width))
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale),
                                    colorspace=pymupdf.csRGB, alpha=False)
            blob = pixmap.tobytes("png")
        if len(blob) > (7 if large else 2) * 1024 * 1024:
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

"""Persist selected runtime credentials in the same Compose .env.

The NAS installer makes /volume2/docker/ups-pod/.env a symlink to
/volume2/docker/ups-pod/data/.env (seen in the app as /data/.env).
No mounted Docker socket or write access to the application repository is needed.
"""
from __future__ import annotations

import fcntl
import os
import re
import stat
import tempfile
from pathlib import Path

import service

KEYS = {
    "UPS_CLIENT_ID": "UPS Client ID",
    "UPS_CLIENT_SECRET": "UPS Client Secret",
    "GOOGLE_OAUTH_CLIENT_ID": "Google OAuth Client ID",
    "GOOGLE_OAUTH_CLIENT_SECRET": "Google OAuth Client Secret",
}
ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SAFE_VALUE = re.compile(r"^[A-Za-z0-9._~!+:/=@%-]{1,512}$")


def path() -> Path:
    return service.DATA_DIR / ".env"


def _read() -> tuple[list[str], dict[str, str]]:
    file = path()
    if not file.is_file() or file.is_symlink():
        return [], {}
    lines = file.read_text(encoding="utf-8").splitlines()
    result = {}
    for line in lines:
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if match:
            val = match.group(2).strip()
            if val.startswith(("'", '"')) and len(val) >= 2 and val[0] == val[-1]:
                val = val[1:-1]
            result[match.group(1)] = val
    return lines, result


def get(key: str) -> str:
    if key not in KEYS and key not in {"client_id", "client_secret"}:
        return ""
    _, cfg = _read()
    value = cfg.get(key)
    if value:
        return value
    return os.getenv(key, "") or ""


def ups_credentials() -> tuple[str, str]:
    return get("UPS_CLIENT_ID") or get("client_id"), get("UPS_CLIENT_SECRET") or get("client_secret")


def masked(value: str, *, identifier: bool = False) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "•" * len(value)
    return (value[:4] if identifier and len(value) > 9 else "") + "•" * min(12, len(value) - 4) + value[-4:]


def masked_status() -> dict:
    return {
        "ups_client_id": masked(ups_credentials()[0], identifier=True),
        "ups_client_secret": masked(ups_credentials()[1]),
        "google_client_id": masked(get("GOOGLE_OAUTH_CLIENT_ID"), identifier=True),
        "google_client_secret": masked(get("GOOGLE_OAUTH_CLIENT_SECRET")),
    }


def update(values: dict[str, str]) -> list[str]:
    if not isinstance(values, dict) or not values or set(values) - set(KEYS):
        raise ValueError("Nieobsługiwane pola konfiguracji.")
    updates = {}
    for key, value in values.items():
        if not ENV_KEY.fullmatch(key) or not isinstance(value, str):
            raise ValueError("Nieprawidłowy klucz lub wartość konfiguracji.")
        if not value:
            continue  # blank form input means keep old secret
        if not SAFE_VALUE.fullmatch(value) or "\r" in value or "\n" in value:
            raise ValueError("Wartość zawiera niedozwolone znaki.")
        updates[key] = value
    if not updates:
        return []
    file = path()
    if file.is_symlink():
        raise ValueError("Plik konfiguracji nie może być dowiązaniem.")
    file.parent.mkdir(parents=True, exist_ok=True)
    lock = file.parent / ".env-edit.lock"
    with lock.open("a+", encoding="utf-8") as handle:
        os.chmod(lock, 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX)
        lines, _ = _read()
        pending = dict(updates)
        result = []
        for line in lines:
            match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
            if match and match.group(1) in pending:
                result.append(match.group(1) + "=" + pending.pop(match.group(1)))
            else:
                result.append(line)
        for key, val in pending.items():
            result.append(key + "=" + val)
        fd, temp = tempfile.mkstemp(prefix=".env-write-", dir=file.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.chmod(temp, stat.S_IRUSR | stat.S_IWUSR)
                stream.write("\n".join(result).rstrip("\n") + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, file)
            directory = os.open(file.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
    return list(updates)

"""Google OAuth web-server flow for explicit operator-authorized Gmail sending.

OAuth implementation uses Google's maintained open-source google-auth-oauthlib:
https://github.com/googleapis/google-auth-library-python-oauthlib

Only gmail.send + OpenID email scopes. Refresh token is stored in /data,
mode 0600, separate from Compose's environment file. No Gmail inbox access.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import tempfile
import time
from email.message import EmailMessage
from pathlib import Path

from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

import config_store
import service

GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
SCOPES = [GMAIL_SEND_SCOPE, "openid", "email"]
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"


class GoogleConfigError(ValueError):
    pass


def token_file() -> Path:
    return service.DATA_DIR / ".gmail_oauth.json"


def redirect_uri() -> str:
    return "https://" + os.getenv("PUBLIC_HOST", "ups.xlap.pl") + "/api/config/gmail/callback"


def client_config() -> dict:
    client_id = config_store.get("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = config_store.get("GOOGLE_OAUTH_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise GoogleConfigError("Wprowadź Google OAuth Client ID i Client Secret w Konfiguracji.")
    if not client_id.endswith(".apps.googleusercontent.com"):
        raise GoogleConfigError("Google OAuth Client ID ma nieprawidłowy format.")
    return {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri()],
        }
    }


def fingerprint(client_id: str) -> str:
    return hashlib.sha256(client_id.encode("utf-8")).hexdigest()


def read_token() -> dict:
    file = token_file()
    if not file.is_file() or file.is_symlink():
        return {}
    try:
        stored = json.loads(file.read_text(encoding="utf-8"))
        expected = fingerprint(client_config()["web"]["client_id"])
        if stored.get("client_hash") != expected:
            return {}
        if not stored.get("refresh_token") or not stored.get("email"):
            return {}
        return stored
    except (OSError, ValueError, TypeError, KeyError, GoogleConfigError):
        return {}


def status() -> dict:
    token = read_token()
    return {"configured": bool(token), "email": token.get("email", ""),
            "oauth_ready": bool(config_store.get("GOOGLE_OAUTH_CLIENT_ID")
                                and config_store.get("GOOGLE_OAUTH_CLIENT_SECRET")),
            "redirect_uri": redirect_uri()}


def flow(*, state: str | None = None) -> Flow:
    instance = Flow.from_client_config(client_config(), scopes=SCOPES, state=state)
    instance.redirect_uri = redirect_uri()
    return instance


def authorization_url() -> tuple[str, str]:
    try:
        url, state = flow().authorization_url(
            access_type="offline", prompt="consent",
            include_granted_scopes="true",
        )
        return url, state
    except (ValueError, KeyError) as exc:
        raise GoogleConfigError("Nie można rozpocząć autoryzacji Google.") from exc


def _atomic_json(target: Path, content: dict) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise GoogleConfigError("Niedozwolone dowiązanie tokenu Gmail.")
    fd, tmp = tempfile.mkstemp(prefix=".gmail-tmp-", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
            json.dump(content, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def complete(authorization_response: str, state: str) -> str:
    try:
        current_flow = flow(state=state)
        current_flow.fetch_token(authorization_response=authorization_response)
        credentials = current_flow.credentials
        if not credentials.refresh_token:
            raise GoogleConfigError(
                "Google nie zwrócił tokenu odświeżania; ponów zgodę na dostęp offline."
            )
        with AuthorizedSession(credentials) as client:
            response = client.get(USERINFO_URL, timeout=20)
            response.raise_for_status()
            identity = response.json()
        email = str(identity.get("email", "")).strip().lower()
        if not identity.get("email_verified") or not email or len(email) > 254:
            raise GoogleConfigError("Google nie potwierdził adresu e-mail nadawcy.")
        _atomic_json(token_file(), {
            "email": email,
            "client_hash": fingerprint(client_config()["web"]["client_id"]),
            "refresh_token": credentials.refresh_token,
            "saved_at": int(time.time()),
        })
        return email
    except GoogleConfigError:
        raise
    except Exception as exc:
        # Never log OAuth tokens, redirect query, response bodies or error URLs.
        raise GoogleConfigError(
            "Autoryzacja Google nie powiodła się. Zweryfikuj klienta OAuth i URI przekierowania."
        ) from exc


def disconnect() -> None:
    # Deletes the local token. Google permission can also be revoked in the
    # Google Account security settings.
    file = token_file()
    if file.is_symlink():
        raise GoogleConfigError("Niedozwolony plik tokenu Gmail.")
    file.unlink(missing_ok=True)


def send(message: EmailMessage) -> dict:
    stored = read_token()
    if not stored:
        raise GoogleConfigError("Połącz konto Gmail z aplikacją w Konfiguracji.")
    client = client_config()["web"]
    credentials = Credentials(
        token=None,
        refresh_token=stored["refresh_token"],
        token_uri=client["token_uri"],
        client_id=client["client_id"],
        client_secret=client["client_secret"],
        scopes=SCOPES,
    )
    if "From" in message:
        message.replace_header("From", stored["email"])
    else:
        message["From"] = stored["email"]
    wire = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    try:
        with AuthorizedSession(credentials) as session:
            response = session.post(SEND_URL, json={"raw": wire}, timeout=25)
            response.raise_for_status()
            result = response.json()
    except Exception as exc:
        # Gmail might have accepted the message before a network timeout.
        raise GoogleConfigError(
            "Gmail API nie potwierdziło wysłania. Sprawdź folder Wysłane przed ponowieniem."
        ) from exc
    if not result.get("id"):
        raise GoogleConfigError("Gmail API nie zwróciło identyfikatora wiadomości.")
    return {"id": result["id"], "email": stored["email"]}

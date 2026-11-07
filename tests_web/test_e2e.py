"""End-to-end HTTP/API/SQLite/UPS-mock/PDF/ZIP tests (no real UPS credentials)."""
from __future__ import annotations

import base64
import io
import json
import os
import sys
import tempfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "UPS_POD"))
sys.path.insert(0, str(PROJECT / "web"))
os.environ["UPS_POD_HOME"] = tempfile.mkdtemp(prefix="ups-pod-ci-")
os.environ["PUBLIC_HOST"] = "ups.xlap.pl"

import app as webapp  # noqa: E402
import service  # noqa: E402
import worker  # noqa: E402

NUM_A = "1Z1234567890123456"
NUM_B = "1Z9876543210987654"
NUM_C = "1Z1111111111111111"
NUM_D = "1Z2222222222222222"
SAMPLE_PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "DATA_DIR", tmp_path)
    monkeypatch.setattr(service, "DB_PATH", tmp_path / "queue.sqlite3")
    monkeypatch.setattr(service, "DOCUMENTS_DIR", tmp_path / "pod")
    monkeypatch.setattr(service, "SOURCES_DIR", tmp_path / "source")
    monkeypatch.setattr(webapp, "SESSION_KEY", tmp_path / "web_session_key")
    flask_app = webapp.create_app(testing=True)
    with flask_app.test_client() as web_client:
        page = web_client.get("/")
        assert page.status_code == 200
        with web_client.session_transaction() as session:
            csrf = session["csrf"]
        yield web_client, csrf


def post(client, csrf, url, obj):
    return client.post(url, json=obj, headers={"X-CSRF-Token": csrf})


@pytest.fixture
def fake_ups(monkeypatch):
    state = {"scenarios": {}, "queries": [], "tokens": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def reply(self, status, payload):
            contents = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(contents)))
            self.end_headers()
            self.wfile.write(contents)

        def do_POST(self):
            if self.path != "/security/v1/oauth/token":
                self.reply(404, {})
                return
            expected = base64.b64encode(b"FAKE_ID:FAKE_SECRET").decode()
            if self.headers.get("Authorization") != "Basic " + expected:
                self.reply(401, {"response": {"errors": [{"code": "10401", "message": "Invalid client"}]}})
                return
            state["tokens"] += 1
            self.reply(200, {"access_token": "fake-token", "expires_in": 3600})

        def do_GET(self):
            parts = urlsplit(self.path)
            if not parts.path.startswith("/api/track/v1/details/"):
                self.reply(404, {})
                return
            number = parts.path.rsplit("/", 1)[-1]
            params = parse_qs(parts.query)
            assert params.get("returnPOD") == ["true"]
            assert params.get("returnSignature") == ["true"]
            assert self.headers.get("Authorization") == "Bearer fake-token"
            state["queries"].append(number)
            scenario = state["scenarios"].get(number, "not_delivered")
            if scenario == "not_found":
                self.reply(404, {"response": {"errors": [{"code": "151044", "message": "Not found"}]}})
                return
            delivered = scenario != "not_delivered"
            status = {"type": "D" if delivered else "I",
                      "description": "Delivered" if delivered else "In transit"}
            if scenario == "pdf":
                content = SAMPLE_PDF
            elif scenario == "html":
                content = (f"<html><body><h1>UPS POD</h1><p>Tracking: {number}</p>"
                           "<p>Odebrał: ŻÓŁĆ Gęślą</p></body></html>").encode()
            else:
                content = None
            di = {"receivedBy": "Odbiorca", "location": "WAREHOUSE"}
            if content is not None:
                di["pod"] = {"content": base64.b64encode(content).decode()}
            package = {
                "currentStatus": status,
                "activity": [{"status": status, "date": "20261027"}],
                "deliveryInformation": di,
                "deliveryDate": [{"type": "DEL", "date": "20261027"}] if delivered else [],
            }
            self.reply(200, {"trackResponse": {"shipment": [
                {"userRelation": ["SHIPPER"], "package": [package]}
            ]}})

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("UPS_POD_ADRES_API", f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setenv("client_id", "FAKE_ID")
    monkeypatch.setenv("client_secret", "FAKE_SECRET")
    monkeypatch.delenv("UPS_CLIENT_ID", raising=False)
    monkeypatch.delenv("UPS_CLIENT_SECRET", raising=False)
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("separator", [" ", "  ", "\t", "\n", "\r\n", ",", " ; "])
def test_parse_all_separators(separator):
    numbers, invalid = service.parse_tracking_numbers(f"{NUM_A}{separator}{NUM_B}")
    assert numbers == [NUM_A, NUM_B]
    assert invalid == []


def test_normalization_duplicates_and_invalid_numbers():
    numbers, invalid = service.parse_tracking_numbers(f"{NUM_A.lower()}, {NUM_A}; XX \n{NUM_B}")
    assert numbers == [NUM_A, NUM_B]
    assert invalid == ["XX"]


def test_import_idempotent_and_persistent(client):
    browser, csrf = client
    first = post(browser, csrf, "/api/shipments",
                 {"trackings": f"{NUM_A}  {NUM_B}, {NUM_A}"})
    assert first.status_code == 201
    assert first.json["added"] == 2
    second = post(browser, csrf, "/api/shipments", {"trackings": f"{NUM_A} \n {NUM_C}"})
    assert second.status_code == 201
    assert second.json["added"] == 1
    assert second.json["existing"] == 1
    third = post(browser, csrf, "/api/shipments", {"trackings": NUM_C})
    assert third.status_code == 200
    assert third.json["added"] == 0
    assert service.stats()["total"] == 3
    assert webapp.create_app(testing=True).test_client().get("/healthz").json["database"]


def test_request_returns_queued_before_background_work(client):
    browser, csrf = client
    reply = post(browser, csrf, "/api/shipments", {"trackings": NUM_A})
    assert reply.status_code == 201
    row = service.list_numbers()["rows"][0]
    assert row["state"] == "queued" and row["attempts"] == 0
    assert service.claim_next() == NUM_A
    assert service.claim_next() is None
    assert service.list_numbers()["rows"][0]["state"] == "processing"


def test_csrf_and_bad_data_protection(client):
    browser, csrf = client
    assert browser.post("/api/shipments", json={"trackings": NUM_A}).status_code == 403
    assert post(browser, csrf + "bad", "/api/shipments", {"trackings": NUM_A}).status_code == 403
    assert post(browser, csrf, "/api/shipments", {"trackings": "@@@"}).status_code == 400
    assert post(browser, csrf, "/api/shipments", {"trackings": ["bad"]}).status_code == 400
    assert browser.get("/api/shipments?scope=invalid").status_code == 400
    assert browser.get("/api/shipments/INVALID/pdf").status_code == 404


def test_full_ups_oauth_to_source_pdf_and_download(client, fake_ups):
    browser, csrf = client
    fake_ups["scenarios"][NUM_A] = "pdf"
    assert post(browser, csrf, "/api/shipments", {"trackings": NUM_A}).status_code == 201
    assert worker.run_once() == "ready"
    assert fake_ups["tokens"] == 1
    assert fake_ups["queries"] == [NUM_A]
    row = service.list_numbers()["rows"][0]
    assert row["state"] == "ready"
    assert row["attempts"] == 1
    assert row["delivered_at"].startswith("2026-10-27")
    assert (service.SOURCES_DIR / f"{NUM_A}.pdf").read_bytes() == SAMPLE_PDF
    assert (service.DOCUMENTS_DIR / f"{NUM_A}.pdf").read_bytes() == SAMPLE_PDF
    response = browser.get(f"/api/shipments/{NUM_A}/pdf")
    assert response.status_code == 200
    assert response.data == SAMPLE_PDF
    assert "attachment" in response.headers["Content-Disposition"]


def test_real_chromium_html_to_pdf_and_preserve_original(client, fake_ups):
    browser, csrf = client
    fake_ups["scenarios"][NUM_B] = "html"
    post(browser, csrf, "/api/shipments", {"trackings": NUM_B})
    assert worker.run_once() == "ready"
    original = (service.SOURCES_DIR / f"{NUM_B}.html").read_bytes()
    assert b"<html>" in original and NUM_B.encode() in original
    converted = (service.DOCUMENTS_DIR / f"{NUM_B}.pdf").read_bytes()
    assert converted.startswith(b"%PDF") and len(converted) > 500
    assert browser.get(f"/api/shipments/{NUM_B}/pdf").data == converted


def test_delivered_without_pod_retry_then_manual_success(client, fake_ups):
    browser, csrf = client
    fake_ups["scenarios"][NUM_A] = "no_pod"
    post(browser, csrf, "/api/shipments", {"trackings": NUM_A})
    assert worker.run_once() == "retry"
    row = service.list_numbers()["rows"][0]
    assert row["state"] == "retry"
    assert "nie zwróciło" in row["error"]
    assert row["next_attempt_at"] > row["updated_at"]
    assert worker.run_once() is None  # scheduled next attempt is not due
    fake_ups["scenarios"][NUM_A] = "pdf"
    assert post(browser, csrf, "/api/retry", {"trackings": [NUM_A]}).json["requeued"] == 1
    assert worker.run_once() == "ready"
    assert service.stats()["ready"] == 1


def test_undelivered_and_ups_404_get_separate_retry_states(client, fake_ups):
    browser, csrf = client
    fake_ups["scenarios"][NUM_A] = "not_delivered"
    fake_ups["scenarios"][NUM_B] = "not_found"
    post(browser, csrf, "/api/shipments", {"trackings": f"{NUM_A} {NUM_B}"})
    assert worker.run_once() == "retry"
    assert worker.run_once() == "retry"
    errors = {r["tracking"]: r["error"] for r in service.list_numbers()["rows"]}
    assert "jeszcze niedoręczona" in errors[NUM_A]
    assert "404" in errors[NUM_B] or "Not found" in errors[NUM_B]


def test_batch_zip_select_only_and_all(client, fake_ups):
    browser, csrf = client
    fake_ups["scenarios"].update({NUM_A: "pdf", NUM_B: "pdf", NUM_C: "not_delivered"})
    post(browser, csrf, "/api/shipments", {"trackings": f"{NUM_A} {NUM_B} {NUM_C}"})
    for _ in range(3):
        worker.run_once()
    one = post(browser, csrf, "/api/zip", {"trackings": [NUM_A, NUM_C]})
    assert one.status_code == 200
    with zipfile.ZipFile(io.BytesIO(one.data)) as package:
        assert package.namelist() == [f"{NUM_A}_POD.pdf"]
        assert package.read(package.namelist()[0]) == SAMPLE_PDF
    all_ready = post(browser, csrf, "/api/zip", {"all": True})
    with zipfile.ZipFile(io.BytesIO(all_ready.data)) as package:
        assert set(package.namelist()) == {f"{NUM_A}_POD.pdf", f"{NUM_B}_POD.pdf"}


def test_filter_pagination_and_persistence(client):
    browser, csrf = client
    post(browser, csrf, "/api/shipments", {"trackings": f"{NUM_A} {NUM_B} {NUM_C}"})
    service.mark_retry(NUM_A, "No POD yet")
    result = browser.get("/api/shipments?scope=missing&q=No%20POD").json
    assert result["total"] == 1
    assert result["rows"][0]["tracking"] == NUM_A
    assert browser.get("/api/shipments?limit=1&offset=1").json["total"] == 3
    # Creating another Flask instance must not destroy the database.
    next_app = webapp.create_app(testing=True)
    assert next_app.test_client().get("/api/shipments").json["stats"]["total"] == 3


def test_cached_pdf_skips_network_and_recovery(client, fake_ups):
    browser, csrf = client
    post(browser, csrf, "/api/shipments", {"trackings": NUM_A})
    service.DOCUMENTS_DIR.mkdir(exist_ok=True)
    (service.DOCUMENTS_DIR / f"{NUM_A}.pdf").write_bytes(SAMPLE_PDF)
    assert worker.run_once() == "ready"
    assert fake_ups["queries"] == []
    service.add_numbers(NUM_B)
    assert service.claim_next() == NUM_B
    current = {r["tracking"]: r for r in service.list_numbers()["rows"]}
    assert current[NUM_B]["state"] == "processing"
    assert service.recover_in_progress() == 1
    assert worker.run_once() == "retry"

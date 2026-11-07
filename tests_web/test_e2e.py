"""Production UPS API E2E. No simulator, no mocked UPS responses."""
import io
import os
import sys
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "web"), str(ROOT / "UPS_POD")]


@pytest.fixture
def application(tmp_path, monkeypatch):
    monkeypatch.setenv("UPS_POD_HOME", str(tmp_path))
    import service
    monkeypatch.setattr(service, "DATA_DIR", tmp_path)
    monkeypatch.setattr(service, "DB_PATH", tmp_path / "test.sqlite3")
    monkeypatch.setattr(service, "DOCUMENTS_DIR", tmp_path / "pod")
    monkeypatch.setattr(service, "SOURCES_DIR", tmp_path / "source")
    monkeypatch.setattr(service, "LEGACY_DOCUMENTS_DIR", tmp_path / "legacy" / "pod")
    monkeypatch.setattr(service, "LEGACY_EXPORTS_DIR", tmp_path / "legacy" / "fba_pod")
    import app
    monkeypatch.setattr(app, "SESSION_KEY", tmp_path / ".session")
    return app.create_app(testing=True)


def test_excel_parser_and_concurrent_dedup(application):
    import service
    numbers = ["1Z1234567890123456", "1Z9876543210987654", "1Z1111111111111111"]
    raw = 'Excel: "' + numbers[0].lower() + '"\r\n' + numbers[1] + '\t(' + numbers[2] + "); " + numbers[0]
    assert service.parse_tracking_numbers(raw)[0] == numbers
    assert service.parse_tracking_numbers("invoice ABCDEFG 1ZTOOSHORT")[0] == []
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(service.add_numbers, [raw] * 8))
    assert sum(r["added"] for r in results) == 3
    with service.connection() as db:
        assert db.execute("SELECT count(*) FROM shipments").fetchone()[0] == 3
    assert len(service.audit_rows()) == 8


def test_real_ups_browser_worker_pdf_zip_persistence(application):
    import service
    import worker
    number = os.environ.get("UPS_E2E_TRACKING", "").strip()
    assert number, "UPS_E2E_TRACKING is required; live E2E must not be skipped"
    cid, secret = worker.credentials()
    assert cid and secret, "Real UPS credentials are required"
    assert os.getenv("UPS_POD_ADRES_API", "https://onlinetools.ups.com") == "https://onlinetools.ups.com"
    server = make_server("127.0.0.1", 0, application, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            base = f"http://127.0.0.1:{server.server_port}"
            page.goto(base)
            document_id = page.evaluate("window.documentMarker = Math.random()")
            page.locator("#trackings").fill(f'Excel: "{number}"\n{number.lower()}')
            page.locator("#add-btn").click()
            expect(page.locator("#count-total")).to_have_text("1")
            with ThreadPoolExecutor(max_workers=1) as pool:
                assert pool.submit(worker.run_once).result(timeout=180) == "ready"
            expect(page.locator("#count-ready")).to_have_text("1", timeout=20000)
            assert page.evaluate("window.documentMarker") == document_id
            csrf = page.locator('meta[name="csrf-token"]').get_attribute("content")
            initial_pdf = page.request.get(base + f"/api/shipments/{number}/pdf")
            assert initial_pdf.status == 200
            assert "FBA0000000" in initial_pdf.headers["content-disposition"]
            assignment = page.request.post(base + "/api/fba",
                                           data={"entries": [{"tracking": number,
                                                               "shipment_id": "FBA15MSX7FKL",
                                                               "ship_date": "2026-10-30"}]},
                                           headers={"X-CSRF-Token": csrf})
            assert assignment.status == 200
            response = page.request.get(base + f"/api/shipments/{number}/pdf")
            assert response.status == 200 and response.body().startswith(b"%PDF")
            assert f"2026-10-30-FBA15MSX7FKL-{number}.pdf" in response.headers["content-disposition"]
            response = page.request.post(base + "/api/zip", data={"all": True}, headers={"X-CSRF-Token": csrf})
            assert response.status == 200
            with zipfile.ZipFile(io.BytesIO(response.body())) as archive:
                assert archive.namelist() == [f"2026-10-30-FBA15MSX7FKL-{number}.pdf"]
                assert archive.read(archive.namelist()[0]).startswith(b"%PDF")
            page.locator("#tab-logs").click()
            expect(page.locator("#log-rows")).to_contain_text("ups_http")
            assert page.evaluate("window.documentMarker") == document_id
            service.init_db()
            logs = service.audit_rows()
            assert any(r["http_status"] == 200 and r["operation"] == "ups_http" for r in logs)
            assert all(secret not in str(r) and cid not in str(r) for r in logs)
            assert service.add_numbers(number)["added"] == 0
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.mark.parametrize("raw", [
    "1Z1234567890123456\n1Z9876543210987654\n1Z1111111111111111",
    '"1z1234567890123456"; (1Z9876543210987654),[1Z1111111111111111]',
    "A\t1Z1234567890123456\r\nB\t1Z9876543210987654\r\nC\t1Z1111111111111111",
])
def test_three_excel_rows(application, raw):
    with application.test_client() as client:
        client.get("/")
        with client.session_transaction() as session:
            csrf = session["csrf"]
        response = client.post("/api/shipments", json={"trackings": raw}, headers={"X-CSRF-Token": csrf})
        assert response.status_code == 201 and response.json["added"] == 3
        response = client.post("/api/shipments", json={"trackings": raw}, headers={"X-CSRF-Token": csrf})
        assert response.status_code == 200 and response.json["added"] == 0
        assert client.get("/api/shipments").json["total"] == 3


def test_audit_persistence_pagination_and_secret_redaction(application, monkeypatch):
    import service
    monkeypatch.setenv("UPS_CLIENT_SECRET", "private-secret-example")
    for i in range(205):
        service.audit("worker", "event", description=f"{i} private-secret-example\n<script>example</script>")
    service.init_db()
    first = service.audit_rows()
    second = service.audit_rows(before=first[-1]["id"])
    assert len(first) == 200 and len(second) == 5
    assert not ({r["id"] for r in first} & {r["id"] for r in second})
    assert "private-secret-example" not in str(first)
    with application.test_client() as client:
        response = client.get("/api/logs")
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert client.get("/api/logs?before=bad").status_code == 400


def test_mutations_require_csrf_and_missing_pdf_is_404(application):
    with application.test_client() as client:
        assert client.post("/api/shipments", json={"trackings": "1Z1234567890123456"}).status_code == 403
        assert client.post("/api/retry", json={"trackings": []}).status_code == 403
        assert client.post("/api/zip", json={"all": True}).status_code == 403
        assert client.post("/api/fba", json={"text": "x"}).status_code == 403
        assert client.get("/api/shipments/1Z1234567890123456/pdf").status_code == 404


def test_queue_retry_and_recovery(application):
    import service
    number = "1Z1234567890123456"
    service.add_numbers(number)
    assert service.claim_next() == number
    assert service.claim_next() is None
    assert service.recover_in_progress() == 1
    assert service.claim_next() == number
    service.mark_retry(number, "UPS niedostępne", delay_hours=3)
    assert service.claim_next() is None
    assert service.retry_now([number]) == 1
    assert service.claim_next() == number
    assert any(r["operation"] == "retry_scheduled" for r in service.audit_rows())



def test_fba_paste_many_trackings_filename_and_atomic_conflict(application):
    import service
    a, b = "1Z004R316807294692", "1Z004R316805636772"
    entries = service.parse_fba_text(f"DATA\tShipment ID\tList UPS\n"
                                     f"2026-10-30\tFBA15MSX7FKL\t{a}; {b}")
    assert len(entries) == 2
    assert service.assign_fba_rows(entries) == {"assigned": 2, "changed": 2}
    assert service.export_filename(a) == f"2026-10-30-FBA15MSX7FKL-{a}.pdf"
    assert service.assign_fba_rows(entries)["changed"] == 0
    with pytest.raises(ValueError, match="Konflikt FBA"):
        service.assign_fba_rows([
            {"tracking": b, "shipment_id": "FBA999999999", "ship_date": "2026-10-30"},
            {"tracking": a, "shipment_id": "FBA999999999", "ship_date": "2026-10-30"},
        ])
    assert service.export_filename(a) == f"2026-10-30-FBA15MSX7FKL-{a}.pdf"
    with application.test_client() as client:
        client.get("/")
        with client.session_transaction() as session:
            csrf = session["csrf"]
        response = client.post(
            "/api/fba",
            json={"entries": [{"tracking": a, "shipment_id": "FBA15MSX7FKL",
                               "ship_date": "2026-10-31"}]},
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 400
        response = client.post(
            "/api/fba",
            json={"entries": [{"tracking": a, "shipment_id": "FBA15MSX7FKL",
                               "ship_date": "2026-10-31"}], "replace": True},
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 200
        assert service.export_filename(a).startswith("2026-10-31-")


def test_tracking_fields_only_keys_and_log_severity_filter(application):
    import service
    service.save_tracking_fields({"trackResponse": {"shipment": [
        {"package": [{"currentStatus": {"type": "D", "description": "Sensitive"},
                      "deliveryInformation": {"pod": {"content": "PRIVATE"}}}]}]}})
    paths = service.tracking_field_paths()
    assert "trackResponse.shipment[].package[].currentStatus.type" in paths
    assert "trackResponse.shipment[].package[].deliveryInformation.pod.content" in paths
    assert "PRIVATE" not in str(paths)
    service.audit("worker", "ready", "1Z004R316807294692")
    service.audit("worker", "retry", "1Z004R316807294692")
    service.audit("worker", "ups_network_error", "1Z004R316807294692")
    assert {r["level"] for r in service.audit_rows(levels=["WARNING", "ERROR"])} == {"WARNING", "ERROR"}
    with application.test_client() as client:
        assert client.get("/api/tracking-fields").status_code == 200
        assert client.get("/api/logs?levels=WARNING,ERROR").status_code == 200
        assert len(client.get("/api/logs?levels=WARNING,ERROR").json["items"]) == 2
        assert client.get("/api/logs?levels=invalid").status_code == 400


def test_pdf_ups_relative_graphics_are_resolved(application):
    import ups_pod as core
    document = '<IMG src="./ups_logo.gif" ALT="UPS Logo"><IMG src="./ups_banner.gif" ALT="UPS Banner">'
    rendered = core.uzupelnij_grafiki_pod(document)
    assert "./ups_logo.gif" not in rendered
    assert "./ups_banner.gif" not in rendered
    assert "United Parcel Service" in rendered



def test_fba_two_columns_use_ups_pickup_date_and_named_disk_copy(application):
    import service
    import ups_pod as core
    number = "1Z004R316836426182"
    entries = service.parse_fba_text(f"FBA15MSX7FKL\t{number}\n")
    assert entries == [{"tracking": number, "shipment_id": "FBA15MSX7FKL", "ship_date": ""}]
    assert service.assign_fba_rows(entries) == {"assigned": 1, "changed": 1}
    payload = {"trackResponse": {"shipment": [{
        "pickupDate": "20261102",
        "package": [{"currentStatus": {"description": "Doręczono", "code": "011"},
                     "activity": [{"date": "20261104", "status": {"type": "D",
                                  "description": "Doręczono", "code": "FS"}}]}],
    }]}}
    detail = core.analizuj(200, payload, "")
    assert detail["ship_date"] == "2026-11-02"
    assert detail["ship_date_source"] == "ups_pickup_date"
    assert detail["status"] == "Doręczono"
    service.save_tracking_detail(number, detail)
    with service.connection() as db:
        row = db.execute("SELECT ship_date,ship_date_source,ups_status FROM shipments "
                         "WHERE tracking=?", (number,)).fetchone()
    assert tuple(row) == ("2026-11-02", "ups_pickup_date", "Doręczono")
    target = service.document_target(number)
    target.write_bytes(b"%PDF-1.4\nmock binary bytes\n")
    service.mark_ready(number, "Doręczono", "2026-11-04", str(target), "", "pdf", "hash")
    assert service.export_filename(number) == f"2026-11-02-FBA15MSX7FKL-{number}.pdf"
    named = service.document_target(number)
    assert named == target
    assert named.read_bytes().startswith(b"%PDF")
    assert len(list(service.DOCUMENTS_DIR.glob("*.pdf"))) == 1
    # Existing pickup date is left untouched by a two-column FBA reimport.
    service.assign_fba_rows(entries)
    assert named.is_file()



def test_unassigned_placeholder_and_physical_rename_on_fba_or_date_change(application):
    import service
    number = "1Z004R316811897667"
    service.add_numbers(number)
    # Without a verified UPS ship date, explicitly mark the date unknown.
    unknown = service.document_target(number)
    assert unknown.name == f"BRAK_DATY-FBA0000000-{number}.pdf"
    unknown.write_bytes(b"%PDF-1.4\nvalid sample")
    service.mark_ready(number, "Doręczono", "", str(unknown), "", "pdf", "hash")
    assert unknown.exists()
    assert service.ready_paths([number])[0][1] == unknown

    service.save_tracking_detail(number, {"ship_date": "2026-11-02",
                                          "ship_date_source": "ups_pickup_date",
                                          "status": "Doręczono"})
    from_ups = service.DOCUMENTS_DIR / f"2026-11-02-FBA0000000-{number}.pdf"
    assert from_ups.exists() and not unknown.exists()
    assert service.export_filename(number) == from_ups.name

    service.assign_fba_rows([{"tracking": number, "shipment_id": "FBA15MSX7FKL"}])
    assigned = service.DOCUMENTS_DIR / f"2026-11-02-FBA15MSX7FKL-{number}.pdf"
    assert assigned.exists() and not from_ups.exists()
    assert len(list(service.DOCUMENTS_DIR.glob("*.pdf"))) == 1
    assert service.ready_paths([number])[0][1] == assigned

    service.assign_fba_rows([{"tracking": number, "shipment_id": "FBA15MSX7FKL",
                             "ship_date": "2026-11-03"}], replace=True)
    renamed = service.DOCUMENTS_DIR / f"2026-11-03-FBA15MSX7FKL-{number}.pdf"
    assert renamed.exists() and not assigned.exists()
    with service.connection() as db:
        saved = db.execute("SELECT pod_path FROM shipments WHERE tracking=?", (number,)).fetchone()
    assert saved["pod_path"] == str(renamed)
    assert len(list(service.DOCUMENTS_DIR.glob("*.pdf"))) == 1


def test_migrate_legacy_pdf_to_mounted_archive_and_keep_backup(application):
    import service
    number = "1Z004R316805636772"
    service.add_numbers(number)
    service.save_tracking_detail(number, {"ship_date": "2026-10-30",
                                          "ship_date_source": "ups_pickup_date"})
    old = service.LEGACY_DOCUMENTS_DIR / (number + ".pdf")
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(b"%PDF-1.4\nlegacy content")
    # Old production DB's ready record contains the legacy /data/pod/1Z... path.
    with service.connection() as db:
        db.execute("UPDATE shipments SET state='ready',pod_path=? WHERE tracking=?",
                   (str(old), number))
    service.init_db()
    new = service.DOCUMENTS_DIR / f"2026-10-30-FBA0000000-{number}.pdf"
    assert new.read_bytes() == old.read_bytes()
    assert old.exists(), "Legacy PDF backup must be preserved"
    assert service.ready_paths([number])[0][1] == new
    # Repeated migrations do not create duplicates.
    service.init_db()
    assert len(list(service.DOCUMENTS_DIR.glob("*.pdf"))) == 1

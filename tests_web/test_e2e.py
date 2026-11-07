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
    assert len([r for r in service.audit_rows() if r["operation"] == "import"]) == 8


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
            page.locator("#intake-toggle").click()
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
                                                               }]},
                                           headers={"X-CSRF-Token": csrf})
            assert assignment.status == 200
            response = page.request.get(base + f"/api/shipments/{number}/pdf")
            assert response.status == 200 and response.body().startswith(b"%PDF")
            expected_name = service.export_filename(number)
            assert expected_name in response.headers["content-disposition"]
            response = page.request.post(base + "/api/zip", data={"all": True}, headers={"X-CSRF-Token": csrf})
            assert response.status == 200
            with zipfile.ZipFile(io.BytesIO(response.body())) as archive:
                assert archive.namelist() == [expected_name]
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
    assert any(r["operation"] == "pod_retry_error" for r in service.audit_rows())



def test_fba_paste_many_trackings_filename_and_atomic_conflict(application):
    import service
    a, b = "1Z004R316807294692", "1Z004R316805636772"
    entries = service.parse_fba_text(f"DATA\tShipment ID\tList UPS\n"
                                     f"2026-10-30\tFBA15MSX7FKL\t{a}; {b}")
    assert len(entries) == 2
    assert service.assign_fba_rows(entries) == {"assigned": 2, "changed": 2}
    # Date supplied in pasted third column is ignored: UPS pickupDate is the source.
    assert all(row["ship_date"] == "" for row in entries)
    service.save_tracking_detail(a, {"ship_date": "2026-10-30", "ship_date_source": "ups_pickup_date"})
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
        assert response.status_code == 200
        # Manual or imported date cannot override verified UPS pickupDate.
        assert service.export_filename(a).startswith("2026-10-30-")


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

    service.save_tracking_detail(number, {"ship_date": "2026-11-03",
                                          "ship_date_source": "ups_pickup_date",
                                          "status": "Doręczono"})
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



def test_ordered_mixed_fba_ups_import_with_garbage_and_excel_cell(application):
    import service
    a, b, c, d = (
        "1Z004R316807294692", "1Z004R316805636772",
        "1Z004R316836426182", "1Z004R316834147988",
    )
    source = ("noise FBA1234567 . " + a + " " + b
              + "\n?? FBA15MSX7FKL\t" + c + ";" + d + " ; nonsense")
    records, warning = service.parse_mixed_text(source)
    assert [(r["tracking"], r["shipment_id"]) for r in records] == [
        (a, "FBA1234567"), (b, "FBA1234567"),
        (c, "FBA15MSX7FKL"), (d, "FBA15MSX7FKL")]
    assert "NOISE" in warning and "NONSENSE" in warning
    result = service.add_numbers(source)
    assert result["added"] == 4
    assert result["assigned"] == 4
    assert result["rejected_count"] >= 2
    with service.connection() as db:
        mappings = dict(db.execute("SELECT tracking,shipment_id FROM shipments"))
    assert mappings[a] == mappings[b] == "FBA1234567"
    assert mappings[c] == mappings[d] == "FBA15MSX7FKL"


def test_ups_without_fba_then_same_tracking_with_fba_is_updated(application):
    import service
    tracking = "1Z004R316805636772"
    assert service.add_numbers(tracking)["added"] == 1
    result = service.add_numbers("FBA1234567;;;; " + tracking)
    assert result["added"] == 0 and result["existing"] == 1
    assert result["assigned"] == 1 and result["changed"] == 1
    assert service.export_filename(tracking) == f"BRAK_DATY-FBA1234567-{tracking}.pdf"
    with pytest.raises(ValueError, match="Konflikt FBA"):
        service.add_numbers("FBA7654321 " + tracking)
    with service.connection() as db:
        assert db.execute("SELECT shipment_id FROM shipments WHERE tracking=?",
                          (tracking,)).fetchone()[0] == "FBA1234567"



def test_repeated_import_of_same_fba_tracking_is_idempotent(application):
    import service
    tracking = "1Z004R316805636772"
    # FBA suffixes can have different lengths; a repeat must not be an error.
    for fba in ("FBA1", "FBA15MSX7FKL", "FBA123456789012345"):
        parsed, rejected = service.parse_mixed_text(f"{fba} {tracking}")
        assert rejected == []
        assert parsed[0]["shipment_id"] == fba
    payload = "FBA15MSX7FKL " + tracking
    first = service.add_numbers(payload)
    second = service.add_numbers(payload)
    assert first["added"] == 1 and first["changed"] == 1
    assert second["added"] == 0 and second["existing"] == 1
    assert second["changed"] == 0 and second["rejected_count"] == 0
    with service.connection() as db:
        assert db.execute("SELECT shipment_id FROM shipments WHERE tracking=?",
                          (tracking,)).fetchone()[0] == "FBA15MSX7FKL"



def test_waiting_tracking_gets_audit_history_and_no_false_error(application):
    import service
    tracking = "1Z004R316834147988"
    service.add_numbers(tracking)
    with service.connection() as db:
        db.execute("DELETE FROM audit WHERE tracking=?", (tracking,))
        db.execute("UPDATE shipments SET state='retry',error=?,description=?,attempts=1 "
                   "WHERE tracking=?",
                   ("Przesyłka jeszcze niedoręczona.", "W drodze", tracking))
    service.init_db()
    logs = service.audit_rows(tracking=tracking)
    assert len(logs) == 1
    assert logs[0]["operation"] == "awaiting_delivery"
    assert logs[0]["level"] == "INFO"
    with application.test_client() as client:
        rows = client.get("/api/logs?tracking=" + tracking).get_json()["items"]
        assert len(rows) == 1 and rows[0]["tracking"] == tracking
        assert client.get("/api/logs?tracking=garbage!").status_code == 400


def test_browser_shift_checkbox_bulk_fba_branding_and_status_pills(application):
    import service
    nums = [f"1Z{i:016d}" for i in range(11, 19)]
    service.add_numbers(" ".join(nums))
    with service.connection() as db:
        db.execute("UPDATE shipments SET ups_status='W drodze' WHERE tracking=?", (nums[0],))
    server = make_server("127.0.0.1", 0, application, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{server.server_port}")
            assert "ElectroSmart UPS-POD" in page.title()
            assert "WDT" not in page.content()
            expect(page.locator("#rows input[type=checkbox]")).to_have_count(8)
            page.locator("#rows input[type=checkbox]").nth(0).check()
            page.locator("#rows input[type=checkbox]").nth(5).click(modifiers=["Shift"])
            expect(page.locator("#rows input[type=checkbox]:checked")).to_have_count(6)
            expect(page.locator("#fba-selected")).to_have_text("FBA")
            expect(page.locator("#fba-selected")).to_have_attribute(
                "title", "Przypisz Shipment ID dla 6 wybranych przesyłek.")
            page.locator("#fba-selected").click()
            expect(page.locator("#fba-dialog")).to_be_visible()
            page.locator("#fba-dialog-id").fill("FBA1234567")
            page.locator("#fba-save").click()
            expect(page.locator("#fba-dialog")).to_be_hidden()
            expect(page.locator("#rows .fba-id")).to_have_count(6)
            with service.connection() as db:
                assert db.execute("SELECT count(*) FROM shipments WHERE shipment_id=?",
                                  ("FBA1234567",)).fetchone()[0] == 6
            assert page.locator("#rows .ups-status.status").count() == 8
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)



def test_month_filter_sorting_and_persisted_ups_delivery_metadata(application):
    import service
    import base64

    a, b = "1Z004R316836426182", "1Z004R316834147988"
    service.add_numbers(a + " " + b)
    photo = base64.b64encode(
        base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/VpAAAAAASUVORK5CYII=")
    ).decode()
    service.save_tracking_detail(a, {"status": "Doręczono", "ship_date": "2026-11-02",
                                     "ship_date_source": "ups_pickup_date"})
    service.save_tracking_metadata(a, {"trackResponse": {"shipment": [{"package": [{
        "trackingNumber": a,
        "weight": {"weight": "19.00", "unitOfMeasurement": "KGS"},
        "deliveryTime": {"type": "DEL", "endTime": "090000"},
        "deliveryDate": [{"type": "DEL", "date": "20261105"}],
        "deliveryInformation": {"receivedBy": "DRIVER RELEASE",
                                "deliveryPhoto": {"photoCaptureInd": "Y", "photo": photo}},
    }]}]}})
    service.save_tracking_detail(b, {"status": "W drodze", "ship_date": "2026-11-05",
                                     "ship_date_source": "ups_pickup_date"})
    service.save_tracking_metadata(b, {"trackResponse": {"shipment": [{"package": [{
        "trackingNumber": b,
        "weight": {"weight": "5.50", "unitOfMeasurement": "KGS"},
        "deliveryTime": {"type": "EDW", "startTime": "101500", "endTime": "131500"},
        "deliveryDate": [{"type": "SDD", "date": "20261109"}],
    }]}]}})
    nov = service.list_numbers(month="2026-11")
    assert [x["tracking"] for x in nov["rows"]][:2] == [b, a]
    assert len(service.list_numbers(month="2026-10")["rows"]) == 0
    assert service.current_month() in service.available_months()
    with pytest.raises(ValueError, match="Miesiąc"):
        service.list_numbers(month="2026-13")
    meta = {x["tracking"]: x for x in nov["rows"]}
    assert meta[a]["weight_value"] == "19.00"
    assert meta[a]["delivery_date"] == "20261105"
    assert meta[a]["delivery_time_type"] == "DEL"
    assert meta[a]["received_by"] == "DRIVER RELEASE"
    assert meta[a]["photo_filename"].endswith(".png")
    assert meta[b]["delivery_date_type"] == "SDD"
    assert meta[b]["delivery_time_start"] == "101500"
    assert not meta[b]["photo_filename"]
    with application.test_client() as client:
        assert client.get(f"/api/shipments/{a}/photo").status_code == 200
        assert client.get(f"/api/shipments/{b}/photo").status_code == 404
        assert len(client.get("/api/shipments?month=2026-11").json["rows"]) == 2


def test_photo_flag_without_actual_image_does_not_fabricate_thumbnail(application):
    import service
    tracking = "1Z004R316836426182"
    service.add_numbers(tracking)
    service.save_tracking_metadata(tracking, {"trackResponse": {"shipment": [{"package": [{
        "trackingNumber": tracking, "deliveryInformation": {
            "deliveryPhoto": {"photoCaptureInd": "N",
                              "isProximityMapViewable": False}}
    }]}]}})
    assert service.photo_path(tracking) is None


def test_variable_length_fba_ids_and_idempotent_reimport(application):
    import service
    tracking = "1Z004R316805636772"
    for fba in ("FBA1", "FBA12345678", "FBA15MSX7FKL", "FBA123456789012345678901234567890123456"):
        assert service.FBA_ID.fullmatch(fba)
        parsed, rejected = service.parse_mixed_text(f"{fba} {tracking}")
        assert rejected == []
        assert parsed[0]["shipment_id"] == fba
    first = service.add_numbers(f"FBA12345678 {tracking}")
    assert first["added"] == 1 and first["changed"] == 1
    again = service.add_numbers(f"FBA12345678 {tracking}")
    assert again["added"] == 0 and again["existing"] == 1
    assert again["changed"] == 0 and again["rejected"] == []
    with pytest.raises(ValueError, match="Konflikt FBA"):
        service.add_numbers(f"FBA987654321 {tracking}")


def test_removed_delivery_columns_from_web_table(application):
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    template = (root / "web/templates/index.html").read_text()
    script = (root / "web/static/app.js").read_text()
    for name in ("<th>Zdjęcie</th>", "<th>Godzina doręczenia</th>", "<th>Odebrał</th>"):
        assert name not in template
    assert 'colspan="9"' in template
    assert "tr.append(photoCell)" not in script
    assert 'addCell(tr, row.received_by' not in script


def test_dashboard_links_tooltip_return_collapsed_import_and_502(application):
    """Browser regression for UI state; UPS API is not replaced by a simulator."""
    import service

    exception, waiting, ready = (
        "1Z004R316807294692", "1Z004R316805636772", "1Z004R316836426182"
    )
    service.add_numbers(" ".join((exception, waiting, ready)))
    with service.connection() as db:
        db.execute(
            "UPDATE shipments SET state='retry', ups_status=?, ups_status_code=?,"
            " description=?, error=?, shipment_id=? WHERE tracking=?",
            ("Wyjątek w doręczeniu", "X", "Nieprawidłowy adres odbiorcy",
             "Brak potwierdzenia", "FBA15MSX7FKL", exception),
        )
        db.execute(
            "UPDATE shipments SET state='retry', ups_status=?, description=?, error=?"
            " WHERE tracking=?",
            ("W drodze", "W drodze", "Przesyłka jeszcze niedoręczona.", waiting),
        )
    pdf_path = service.document_target(ready)
    pdf_path.write_bytes(b"%PDF-1.4\nregression fixture\n")
    service.mark_ready(ready, "Doręczono", "", str(pdf_path), "", "pdf", "fixture")

    server = make_server("127.0.0.1", 0, application, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 920, "height": 720})
            page.goto(f"http://127.0.0.1:{server.server_port}")
            expect(page.locator("#rows tr")).to_have_count(3)
            headings = page.locator(
                "#shipments-table thead tr:first-child .sort-header"
            ).evaluate_all("(nodes) => nodes.map((n) => n.firstChild.textContent)")
            assert headings[:8] == [
                "Shipment ID", "Numer UPS", "Data nadania",
                "Data doręczenia", "Waga", "Status UPS", "API", "Dokument",
            ]
            assert len(headings) == 24
            expect(page.locator('.column-header[data-col="weight"]')).to_be_hidden()
            link = page.locator(f'#rows tr[data-tracking="{exception}"] td.track a')
            assert link.get_attribute("href") == (
                "https://www.ups.com/track?tracknum=" + exception
                + "&loc=pl_PL&requester=ST/trackdetails"
            )
            assert link.get_attribute("target") == "_blank"
            assert "noopener" in link.get_attribute("rel")
            pill = page.locator(f'#rows tr[data-tracking="{exception}"] .ups-status')
            pill.hover()
            expect(page.locator(".ups-tooltip")).to_be_visible()
            expect(page.locator(".ups-tooltip")).to_contain_text("Nieprawidłowy adres")
            expect(page.locator(".ups-tooltip")).not_to_contain_text("Typ UPS")
            assert page.locator(f'#rows tr[data-tracking="{ready}"] .document-bubble').inner_text() == "↓ PDF"

            expect(page.locator("#intake-content")).to_be_hidden()
            page.locator("#intake-toggle").click()
            expect(page.locator("#intake-content")).to_be_visible()
            assert page.evaluate('document.cookie.includes("ups_pod_intake_collapsed=0")')
            page.reload()
            expect(page.locator("#intake-content")).to_be_visible()
            page.locator("#intake-toggle").click()
            expect(page.locator("#intake-content")).to_be_hidden()

            page.locator("#month-filter").select_option("all")
            page.locator("#query").fill("1Z")
            expect(page.locator("#rows tr")).to_have_count(3)
            page.locator('[data-scope="missing"]').click()
            expect(page.locator("#rows tr")).to_have_count(2)
            page.reload()
            expect(page.locator('[data-scope="missing"]')).to_have_class("filter active")
            expect(page.locator("#query")).to_have_value("1Z")
            expect(page.locator("#month-filter")).to_have_value("all")
            page.locator(f'#rows tr[data-tracking="{waiting}"] .document-bubble').click()
            expect(page.locator("#logs-panel")).to_be_visible()
            expect(page.locator("#logs-tracking")).to_have_value(waiting)
            expect(page.locator("#logs-return")).to_be_visible()
            saved_position = page.evaluate("({ x: listReturnPoint.scrollX, y: listReturnPoint.scrollY })")
            page.locator("#logs-return").click()
            page.wait_for_function(
                "p => Math.abs(window.scrollY-p.y) < 5 && Math.abs(window.scrollX-p.x) < 5",
                arg=saved_position,
            )
            expect(page.locator("#shipments-panel")).to_be_visible()
            expect(page.locator("#logs-panel")).to_be_hidden()
            expect(page.locator("#query")).to_have_value("1Z")
            expect(page.locator("#month-filter")).to_have_value("all")
            expect(page.locator(f'#rows tr[data-tracking="{waiting}"]')).to_be_visible()

            # A gateway failure on automatic refresh must not render as an import error.
            page.route("**/api/shipments?*", lambda route: route.fulfill(
                status=502, content_type="text/html", body="Bad gateway"))
            page.evaluate("refresh()")
            expect(page.locator("#refresh-status")).to_contain_text("HTTP 502")
            expect(page.locator("#notice")).to_be_hidden()
            assert page.locator("#rows tr").count() == 2
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_server_side_filter_sort_all_rows_and_column_whitelist(application):
    import service

    entries = [f"1Z{i:016d}" for i in range(250)]
    service.add_numbers(" ".join(entries))
    with service.connection() as db:
        db.execute(
            "UPDATE shipments SET shipment_id=?,ship_date=?,weight_value=?,weight_unit=?,"
            " ups_status=?,delivery_date=?,delivery_date_type=? WHERE tracking=?",
            ("FBA999LONGID", "2026-11-02", "23.50", "KGS", "Wyjątek w doręczeniu",
             "20261101", "SDD", entries[234]),
        )
        db.execute(
            "UPDATE shipments SET shipment_id=?,ship_date=?,weight_value=?,weight_unit=?,"
            " ups_status=?,delivery_date=?,delivery_date_type=? WHERE tracking=?",
            ("FBA000111", "2026-11-03", "5.00", "KGS", "W drodze",
             "20261108", "SDD", entries[235]),
        )
    assert service.list_numbers(limit=200)["total"] == 250
    match = service.list_numbers(column_filters={"fba": "999long"}, limit=10)
    assert match["total"] == 1
    assert match["rows"][0]["tracking"] == entries[234]
    for key, needle in [
        ("tracking", entries[234]), ("ship_date", "2026-11-02"),
        ("delivery_date", "2026-11-01"), ("weight", "23.50 kg"),
        ("ups_status", "wyjątek"),
    ]:
        filtered = service.list_numbers(column_filters={key: needle})
        assert entries[234] in {r["tracking"] for r in filtered["rows"]}, key
    assert service.list_numbers(column_filters={"fba": "999", "weight": "5.00"})["total"] == 0
    assert service.list_numbers(column_filters={"queue": "W kolejce"})["total"] == 250
    assert service.list_numbers(column_filters={"document": "Oczekuje"})["total"] == 250
    ascending = service.list_numbers(sort_by="weight", sort_dir="asc")
    weights = [r["weight_value"] for r in ascending["rows"] if r["weight_value"]]
    assert weights == ["5.00", "23.50"]
    descending = service.list_numbers(sort_by="weight", sort_dir="desc")
    weights = [r["weight_value"] for r in descending["rows"] if r["weight_value"]]
    assert weights == ["23.50", "5.00"]
    with pytest.raises(ValueError, match="sortowanie"):
        service.list_numbers(sort_by="tracking; DROP TABLE shipments")
    with pytest.raises(ValueError, match="filtry"):
        service.list_numbers(column_filters={"track|DROP": "abc"})

    with application.test_client() as client:
        assert client.get("/api/shipments?sort=weight&direction=asc").status_code == 200
        api = client.get("/api/shipments?f_fba=999&f_weight=23.5")
        assert api.status_code == 200 and api.json["total"] == 1
        assert not {"pod_path", "received_by", "photo_filename", "delivered_at"} & set(api.json["rows"][0])
        assert client.get("/api/shipments?sort=DROP").status_code == 400


def test_browser_reorder_sort_filter_and_ups_case_draft(application):
    import service

    a, b, c = "1Z004R316807294692", "1Z004R316805636772", "1Z004R316836426182"
    service.add_numbers(" ".join((a, b, c)))
    with service.connection() as db:
        db.execute(
            "UPDATE shipments SET shipment_id='FBAONE',weight_value='22.00',"
            "weight_unit='KGS',ups_status='W drodze',"
            "delivery_date='20200101',delivery_date_type='SDD' WHERE tracking=?",
            (a,),
        )
        db.execute(
            "UPDATE shipments SET shipment_id='FBATWO',weight_value='8.00',"
            "weight_unit='KGS' WHERE tracking=?",
            (b,),
        )
    server = make_server("127.0.0.1", 0, application, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1460, "height": 900})
            page.goto(f"http://127.0.0.1:{server.server_port}")
            expect(page.locator("#rows tr")).to_have_count(3)
            header = page.locator("#shipments-table thead tr:first-child")
            page.locator("#columns-open").click()
            page.locator(".column-choice").filter(has_text="Waga").locator("input").check()
            page.locator("#columns-done").click()
            page.locator('.column-header[data-col="weight"] .sort-header').click()
            expect(page.locator('#rows tr').first).to_have_attribute("data-tracking", b)
            page.locator("#columns-open").click()
            page.locator(".column-choice").filter(has_text="Waga").locator("input").check()
            page.locator("#columns-done").click()
            page.locator('.column-header[data-col="weight"] .sort-header').click()
            expect(page.locator('#rows tr').first).to_have_attribute("data-tracking", a)

            page.locator('.column-header[data-col="weight"] .column-grip').drag_to(
                page.locator('.column-header[data-col="tracking"] .sort-header'))
            expect(header.locator("th").nth(0)).to_have_class("selection-header")
            expect(header.locator("th").nth(2)).to_have_attribute("data-col", "weight")
            expect(page.locator("#rows tr").first.locator("td").nth(2)).to_have_attribute(
                "data-col", "weight")
            assert "weight" in page.evaluate('document.cookie')
            # Accessible reorder without a mouse, from the current position.
            page.locator('.column-header[data-col="fba"] .column-grip').focus()
            page.keyboard.press("Alt+ArrowRight")
            expect(header.locator("th").nth(1)).to_have_attribute("data-col", "weight")

            page.locator("#column-filters-toggle").click()
            page.locator('.column-filter[data-col="fba"]').fill("FBATWO")
            expect(page.locator("#rows tr")).to_have_count(1)
            expect(page.locator("#rows tr").first).to_have_attribute("data-tracking", b)
            page.reload()
            expect(header.locator("th").nth(1)).to_have_attribute("data-col", "weight")
            expect(header.locator("th").nth(2)).to_have_attribute("data-col", "fba")
            expect(page.locator('.column-filter[data-col="fba"]')).to_have_value("FBATWO")
            expect(page.locator("#rows tr")).to_have_count(1)
            page.locator("#clear-column-filters").click()
            expect(page.locator("#rows tr")).to_have_count(3)

            page.locator(f'#rows tr[data-tracking="{a}"] .fba-edit-link').click()
            expect(page.locator("#fba-dialog")).to_be_visible()
            page.locator("#fba-dialog-id").fill("FBANEWID")
            page.locator("#fba-save").click()
            expect(page.locator("#fba-dialog-error")).to_contain_text("Zaznacz zgodę")
            page.locator("#fba-replace").check()
            page.locator("#fba-save").click()
            expect(page.locator("#fba-dialog")).to_be_hidden()
            expect(page.locator(f'#rows tr[data-tracking="{a}"] .fba-id')).to_have_text("FBANEWID")

            page.locator(f'#rows tr[data-tracking="{a}"] .support-send').click()
            expect(page.locator("#table-notice")).to_contain_text("SMTP")
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_startup_skips_repeated_hash_and_glob_for_canonical_pdf(application, monkeypatch):
    import service

    number = "1Z004R316834147988"
    service.add_numbers(number)
    target = service.document_target(number)
    target.write_bytes(b"%PDF-1.4\nvalid fixture\n")
    service.mark_ready(number, "Doręczono", "", str(target), "", "pdf", "abc")

    def refuse_repeated_migration(_tracking):
        raise AssertionError("Canonical PDF must not be rehashed on each startup")

    monkeypatch.setattr(service, "sync_named_pdf", refuse_repeated_migration)
    service.init_db()
    assert target.exists()


def test_ups_specific_status_and_safe_tracking_columns(application):
    import service
    import ups_pod as core

    tracking = "1Z004R316805433991"
    service.add_numbers(tracking)
    payload = {"trackResponse": {"shipment": [{
        "service": {"description": "UPS Express Saver"},
        "pickupDate": "20261105",
        "packageCount": 1,
        "package": [{
            "trackingNumber": tracking,
            "currentStatus": {
                "description": "Delivery Rescheduled", "code": "019",
                "simplifiedTextDescription": "Your delivery will be rescheduled.",
            },
            "activity": [{"status": {"type": "X", "code": "MF",
                                     "description": "Your delivery will be rescheduled.",
                                     "statusCode": "019"},
                          "date": "20261107", "time": "084351"}],
            "deliveryDate": [{"type": "RDD", "date": "20261119"}],
            "packageAddress": [{"address": {"addressLine": ["SENSITIVE PRIVATE ADDRESS"]}}],
            "deliveryInformation": {"receivedBy": "SENSITIVE PERSON"},
        }],
    }]}}
    detail = core.analizuj(200, payload, "")
    assert detail["status"] == "Delivery Rescheduled"
    assert detail["status_typ"] == "X"
    assert detail["event_code"] == "MF"
    assert detail["status_detail"] == "Your delivery will be rescheduled."
    service.save_tracking_metadata(tracking, payload)
    service.save_tracking_detail(tracking, detail)
    row = service.list_numbers(column_filters={"ups.event_code": "MF"})["rows"][0]
    assert row["ups_status"] == "Delivery Rescheduled"
    assert row["ups_status_detail"] == "Your delivery will be rescheduled."
    assert row["ups_tracking_code"] == "019"
    assert row["ups_extra"]["ups.status_detail"] == "Your delivery will be rescheduled."
    assert row["ups_extra"]["ups.service"] == "UPS Express Saver"
    assert "SENSITIVE" not in repr(row["ups_extra"])
    assert service.list_numbers(sort_by="ups.activity_date", sort_dir="desc")["total"] == 1
    with application.test_client() as client:
        data = client.get("/api/shipments?f_ups.event_code=MF").json
        assert data["total"] == 1
        assert len(data["columns"]) >= 10
        assert data["support_configured"] is False
        assert "SENSITIVE" not in repr(data)


def test_one_click_support_sends_once_without_claiming_delivery(application, monkeypatch):
    import service
    import support_mail

    tracking = "1Z004R316805433991"
    service.add_numbers(tracking)
    with service.connection() as db:
        db.execute("UPDATE shipments SET state='retry',delivery_date='20200101',"
                   "delivery_date_type='SDD',ups_status='Delivery Rescheduled',"
                   "ups_status_code='X' WHERE tracking=?", (tracking,))
    for key in ("UPS_SUPPORT_SMTP_HOST", "UPS_SUPPORT_SMTP_FROM"):
        monkeypatch.delenv(key, raising=False)

    with application.test_client() as client:
        client.get("/")
        with client.session_transaction() as session:
            csrf = session["csrf"]
        headers = {"X-CSRF-Token": csrf}
        missing = client.post(f"/api/support/{tracking}/send", json={}, headers=headers)
        assert missing.status_code == 503
        assert missing.json["code"] == "SMTP_NOT_CONFIGURED"

        sent = []
        class FakeSMTP:
            def __init__(self, *args, **kwargs):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return False
            def ehlo(self):
                pass
            def starttls(self, **kwargs):
                pass
            def send_message(self, message):
                sent.append(message)

        monkeypatch.setenv("UPS_SUPPORT_SMTP_HOST", "smtp.example.test")
        monkeypatch.setenv("UPS_SUPPORT_SMTP_FROM", "operator@example.test")
        monkeypatch.setenv("UPS_SUPPORT_TO", "customer.service@ups.com")
        monkeypatch.setattr(support_mail.smtplib, "SMTP", FakeSMTP)
        delivered = client.post(f"/api/support/{tracking}/send", json={}, headers=headers)
        assert delivered.status_code == 200, delivered.json
        assert delivered.json["sent"]
        assert delivered.json["recipient"] == "customer.service@ups.com"
        assert len(sent) == 1
        assert tracking in sent[0].get_content()
        assert "We do not assert that delivery has occurred." in sent[0].get_content()
        assert not sent[0].get_all("Bcc")
        # No second physical email even on repeated button presses.
        repeated = client.post(f"/api/support/{tracking}/send", json={}, headers=headers)
        assert repeated.status_code == 409
        assert len(sent) == 1
        rows = client.get("/api/shipments").json["rows"]
        assert rows[0]["support_status"] == "sent"

        monkeypatch.setenv("UPS_SUPPORT_TO", "attacker@example.test")
        assert client.get("/api/shipments").json["support_configured"] is False
        with service.connection() as db:
            record = db.execute(
                "SELECT status,message_id FROM support_cases WHERE tracking=?", (tracking,)
            ).fetchone()
        assert record["status"] == "sent" and record["message_id"].startswith("<")


def test_theme_and_configurable_columns_browser(application):
    import service

    service.add_numbers("1Z004R316805433991")
    server = make_server("127.0.0.1", 0, application, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.goto(f"http://127.0.0.1:{server.server_port}")
            expect(page.locator("#rows tr")).to_have_count(1)
            assert page.evaluate("document.documentElement.dataset.theme") == "light"
            assert page.locator(".topbar #tab-shipments").count() == 1
            assert page.locator(".topbar #tab-logs").count() == 1
            assert page.locator(".topbar .top-status").count() == 0
            assert page.locator('link[rel="icon"]').get_attribute("href") == "/static/favicon.svg"
            assert page.request.get(f"http://127.0.0.1:{server.server_port}/static/favicon.svg").status == 200
            assert page.locator("#rows .queue-icon").count() == 1

            page.locator("#theme-toggle").click()
            assert page.evaluate("document.documentElement.dataset.theme") == "dark"
            assert page.evaluate('document.cookie.includes("ups_pod_theme=dark")')
            dark_background = page.evaluate(
                "getComputedStyle(document.body).backgroundColor"
            )
            panel = page.evaluate(
                "getComputedStyle(document.querySelector('.panel')).backgroundColor"
            )
            assert dark_background != panel
            page.reload()
            assert page.evaluate("document.documentElement.dataset.theme") == "dark"
            page.locator("#theme-toggle").click()
            assert page.evaluate("document.documentElement.dataset.theme") == "light"

            expect(page.locator('.column-header[data-col="weight"]')).to_be_hidden()
            expect(page.locator('.column-header[data-col="ups.service"]')).to_be_hidden()
            page.locator("#columns-open").click()
            expect(page.locator("#columns-dialog")).to_be_visible()
            # Match checkbox by row label: available Tracking API keys.
            service_field = page.locator('.column-choice').filter(
                has_text="Rodzaj usługi UPS"
            ).locator('input[type="checkbox"]')
            service_field.check()
            page.locator("#columns-done").click()
            expect(page.locator('.column-header[data-col="ups.service"]')).to_be_visible()
            assert page.evaluate('document.cookie.includes("ups_pod_columns_visible=")')

            width_before = page.locator('.column-header[data-col="ups_status"]').evaluate(
                "(node) => parseFloat(node.style.width)"
            )
            handle = page.locator('.column-header[data-col="ups_status"] .column-resize')
            handle.focus()
            page.keyboard.press("ArrowRight")
            width_after = page.locator('.column-header[data-col="ups_status"]').evaluate(
                "(node) => parseFloat(node.style.width)"
            )
            # Wider user preference reallocates some spare pixels from other
            # columns; the entire table must keep filling the available width.
            assert width_before < width_after <= width_before + 10
            page.reload()
            expect(page.locator('.column-header[data-col="ups.service"]')).to_be_visible()
            persisted = page.locator('.column-header[data-col="ups_status"]').evaluate(
                "(node) => parseFloat(node.style.width)"
            )
            assert persisted == width_after
            page.locator("#rows .fba-edit-link").click()
            expect(page.locator("#fba-dialog")).to_be_visible()
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_configuration_masks_credentials_and_persists_env(application, monkeypatch):
    import config_store
    import service
    import worker

    path = service.DATA_DIR / ".env"
    path.write_text("# preserved comment\nUPS_POD_RETRY_HOURS=3\nclient_id=legacyvalue\n", encoding="utf-8")
    path.chmod(0o600)
    assert config_store.get("client_id") == "legacyvalue"
    with application.test_client() as client:
        client.get("/")
        with client.session_transaction() as session:
            csrf = session["csrf"]
        headers = {"X-CSRF-Token": csrf}
        initial = client.get("/api/config")
        assert initial.status_code == 200
        assert initial.json["ups_client_id"] != "legacyvalue"
        saved = client.post("/api/config/ups", json={
            "client_id": "NewClientID1234567890",
            "client_secret": "NewSecretUPS12345678"
        }, headers=headers)
        assert saved.status_code == 200, saved.json
        assert "NewSecretUPS12345678" not in saved.get_data(as_text=True)
        assert "NewClientID1234567890" not in saved.get_data(as_text=True)
        assert saved.json["ups_client_id"].endswith("7890")
        assert "•" in saved.json["ups_client_secret"]
        assert worker.credentials() == ("NewClientID1234567890", "NewSecretUPS12345678")
        assert (path.stat().st_mode & 0o777) == 0o600
        text = path.read_text(encoding="utf-8")
        assert "# preserved comment" in text
        assert "UPS_POD_RETRY_HOURS=3" in text
        assert "UPS_CLIENT_SECRET=NewSecretUPS12345678" in text

        # Blank fields preserve existing values rather than resetting credentials.
        blank = client.post("/api/config/ups", json={
            "client_id": "", "client_secret": ""
        }, headers=headers)
        assert blank.status_code == 200
        assert worker.credentials()[1] == "NewSecretUPS12345678"
        invalid = client.post("/api/config/ups", json={
            "client_id": "good\nMALICIOUS=YES", "client_secret": ""
        }, headers=headers)
        assert invalid.status_code == 400
        assert "MALICIOUS" not in path.read_text(encoding="utf-8")
        blocked = client.post("/api/config/ups", json={
            "client_id": "another", "client_secret": ""
        })
        assert blocked.status_code == 403

        oauth = client.post("/api/config/google", json={
            "client_id": "12345-abcdef.apps.googleusercontent.com",
            "client_secret": "GOCSPX-0123456789abcdef",
        }, headers=headers)
        assert oauth.status_code == 200
        assert oauth.json["gmail"]["oauth_ready"] is True
        assert oauth.json["gmail"]["configured"] is False
        assert "GOCSPX-0123456789abcdef" not in oauth.get_data(as_text=True)
        assert "GOCSPX-0123456789abcdef" in path.read_text(encoding="utf-8")
        initiated = client.post("/api/config/gmail/start", json={}, headers=headers)
        assert initiated.status_code == 200, initiated.json
        from urllib.parse import parse_qs, urlparse
        url = urlparse(initiated.json["url"])
        assert url.hostname == "accounts.google.com"
        query = parse_qs(url.query)
        assert "gmail.send" in query["scope"][0]
        assert "gmail.readonly" not in query["scope"][0]
        assert query["redirect_uri"] == ["https://ups.xlap.pl/api/config/gmail/callback"]
        with client.session_transaction() as session:
            expected_state = session["gmail_oauth_state"]
        assert query["state"] == [expected_state]
        rejected = client.get("/api/config/gmail/callback?state=wrong&code=unused")
        assert rejected.status_code == 403

        started = client.post("/api/config/gmail/start", json={}, headers=headers)
        state = parse_qs(urlparse(started.json["url"]).query)["state"][0]
        denied = client.get("/api/config/gmail/callback", query_string={
            "state": state, "error": "access_denied"
        })
        assert denied.status_code == 302
        assert denied.location.endswith("/?config=gmail-denied")
    # No network OAuth requests or real Gmail messages in this test.


def test_support_can_send_via_google_without_smtp(application, monkeypatch):
    import config_store
    import gmail_oauth
    import service
    import support_mail

    config_store.update({
        "GOOGLE_OAUTH_CLIENT_ID": "12345-abcdef.apps.googleusercontent.com",
        "GOOGLE_OAUTH_CLIENT_SECRET": "GOCSPX-0123456789abcdef",
    })
    gmail_oauth._atomic_json(gmail_oauth.token_file(), {
        "email": "verified-gmail-user@gmail.com",
        "client_hash": gmail_oauth.fingerprint(
            config_store.get("GOOGLE_OAUTH_CLIENT_ID")),
        "refresh_token": "fake-refresh-token",
    })
    assert gmail_oauth.status()["configured"] is True
    captured = []
    def fake_send(message):
        captured.append(message)
        return {"id": "gmail-msg-123", "email": "verified-gmail-user@gmail.com"}
    monkeypatch.setattr(gmail_oauth, "send", fake_send)
    monkeypatch.delenv("UPS_SUPPORT_SMTP_HOST", raising=False)

    tracking = "1Z004R316805433991"
    service.add_numbers(tracking)
    with service.connection() as db:
        db.execute("UPDATE shipments SET state='retry',delivery_date='20200101',"
                   "delivery_date_type='RDD',ups_status='Delivery Rescheduled',"
                   "ups_status_code='X' WHERE tracking=?", (tracking,))
    sent = support_mail.send_support(tracking)
    assert sent["sent"] is True and sent["channel"] == "gmail"
    assert len(captured) == 1
    assert captured[0]["From"] == "verified-gmail-user@gmail.com"
    assert captured[0]["To"] == support_mail.OFFICIAL_CONTACT
    assert "We do not assert that delivery has occurred." in captured[0].get_content()
    with pytest.raises(support_mail.SupportAlreadySent):
        support_mail.send_support(tracking)
    gmail_oauth.disconnect()
    assert gmail_oauth.status()["configured"] is False


def test_config_tab_plain_dates_and_mixed_zip_browser(application):
    import service

    ready_number = "1Z004R316807294692"
    missing_number = "1Z004R316805636772"
    service.add_numbers(ready_number + " " + missing_number)
    target = service.document_target(ready_number)
    target.write_bytes(b"%PDF-1.4\nfile with no personal data\n")
    service.mark_ready(ready_number, "Doręczono", "", str(target), "", "pdf", "sha")
    with service.connection() as db:
        db.execute("UPDATE shipments SET ship_date='2026-11-01',"
                   "delivery_date='20261108',delivery_date_type='SDD' "
                   "WHERE tracking IN (?,?)", (ready_number, missing_number))
        db.execute("UPDATE shipments SET state='retry' WHERE tracking=?", (missing_number,))
    server = make_server("127.0.0.1", 0, application, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1360, "height": 900})
            page.goto(f"http://127.0.0.1:{server.server_port}")
            expect(page.locator("#rows tr")).to_have_count(2)
            date_text = page.locator(f'#rows tr[data-tracking="{missing_number}"]'
                                     ' [data-col="delivery_date"]').text_content()
            assert date_text == "2026-11-08"

            # A missing-only selection must remain usable for FBA/retry but
            # cannot trigger an empty ZIP download.
            page.locator(f'#rows tr[data-tracking="{missing_number}"] input[type=checkbox]').check()
            expect(page.locator("#download-selected")).to_be_disabled()
            expect(page.locator("#fba-selected")).to_be_enabled()
            expect(page.locator("#retry-selected")).to_be_enabled()

            page.locator(f'#rows tr[data-tracking="{ready_number}"] input[type=checkbox]').check()
            expect(page.locator("#download-selected")).to_be_enabled()
            expect(page.locator("#download-selected")).to_have_text("ZIP")
            expect(page.locator("#download-selected")).to_have_attribute(
                "title", "Pobierz 1 gotowych POD w ZIP (z 2 wybranych).")
            with page.expect_download() as download_info:
                page.locator("#download-selected").click()
            download = download_info.value
            assert download.suggested_filename == "ups-pod-potwierdzenia.zip"
            expect(page.locator("#table-notice")).to_contain_text(
                "Pominięto 1 zaznaczonych przesyłek bez POD")

            page.locator("#tab-config").click()
            expect(page.locator("#config-panel")).to_be_visible()
            expect(page.locator("#shipments-panel")).to_be_hidden()
            expect(page.locator("#logs-panel")).to_be_hidden()
            expect(page.locator("#ups-config-id")).to_be_visible()
            expect(page.locator("#gmail-app-email")).to_be_visible()
            expect(page.locator("#gmail-config-id")).to_be_hidden()
            expect(page.locator("#gmail-connect")).to_be_disabled()
            assert page.locator("#tab-config").get_attribute("aria-selected") == "true"
            page.locator("#tab-shipments").click()
            expect(page.locator("#rows tr")).to_have_count(2)
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_tracking_table_fills_viewport_and_uses_page_vertical_scroll(application):
    """Regressions: no 72vh iframe-like scrollbox and no 1002px fixed-width grid."""
    import service

    numbers = " ".join("1Z" + f"{index:016d}" for index in range(1, 45))
    service.add_numbers(numbers)
    server = make_server("127.0.0.1", 0, application, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            for width, height in [(1440, 900), (1920, 1080), (2560, 1440), (390, 844)]:
                context = browser.new_context(viewport={"width": width, "height": height})
                context.add_cookies([{
                    "name": "ups_pod_month", "value": "all",
                    "url": f"http://127.0.0.1:{server.server_port}",
                }])
                page = context.new_page()
                page.goto(f"http://127.0.0.1:{server.server_port}")
                expect(page.locator("#rows tr")).to_have_count(44)
                def metrics(active_page=page):
                    return active_page.evaluate("""() => {
                      const wrap = document.querySelector('#shipments-panel .table-wrap');
                      const table = document.getElementById('shipments-table');
                      const panel = document.getElementById('shipments-panel');
                      return {
                        panelWidth: panel.getBoundingClientRect().width,
                        wrapperWidth: wrap.clientWidth,
                        tableWidth: table.getBoundingClientRect().width,
                        wrapperHeight: wrap.clientHeight,
                        contentHeight: wrap.scrollHeight,
                        pageHeight: document.documentElement.scrollHeight,
                        docWidth: document.documentElement.scrollWidth,
                      };
                    }""")
                m = metrics()
                assert m["docWidth"] <= width + 1
                assert m["panelWidth"] > width * .90
                assert m["pageHeight"] > height
                # The entire list participates in the page's vertical scroll.
                assert abs(m["contentHeight"] - m["wrapperHeight"]) <= 1
                if width >= 1200:
                    assert abs(m["tableWidth"] - m["wrapperWidth"]) <= 2
                else:
                    assert abs(m["tableWidth"] - m["wrapperWidth"]) <= 2

                if width == 1440:
                    page.set_viewport_size({"width": 1920, "height": 1080})
                    page.wait_for_function("""() => {
                      const w=document.querySelector('#shipments-panel .table-wrap');
                      const t=document.getElementById('shipments-table');
                      return Math.abs(t.getBoundingClientRect().width-w.clientWidth)<=2;
                    }""")
                    expect(page.locator("#rows tr")).to_have_count(44)
                    page.locator("#columns-open").click()
                    # Adding an eighth visible column still occupies full grid.
                    page.locator(".column-choice").filter(has_text="Waga").locator("input").check()
                    page.locator("#columns-done").click()
                    assert abs(metrics()["tableWidth"] - metrics()["wrapperWidth"]) <= 2
                    # Switching tabs should not leave the hidden grid at its
                    # smallest base width when returning to the page.
                    page.locator("#tab-config").click()
                    page.locator("#tab-shipments").click()
                    page.wait_for_function("""() => {
                      const w=document.querySelector('#shipments-panel .table-wrap');
                      const t=document.getElementById('shipments-table');
                      return Math.abs(t.getBoundingClientRect().width-w.clientWidth)<=2;
                    }""")
                    # Any number of visible fields must fit the viewport without
                    # a horizontal scrollbar. Long text gets truncated.
                    page.locator("#columns-open").click()
                    for item in page.locator("#columns-picker .column-choice input").all():
                        if not item.is_checked():
                            item.check()
                    page.locator("#columns-done").click()
                    full = metrics()
                    assert abs(full["tableWidth"] - full["wrapperWidth"]) <= 2
                    assert abs(full["contentHeight"] - full["wrapperHeight"]) <= 1
                context.close()
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_polish_ups_statuses_and_monthly_pending_statistics(application):
    import datetime as dt
    import service
    import ups_localization
    from zoneinfo import ZoneInfo

    current = dt.datetime.now(ZoneInfo("Europe/Warsaw"))
    current_month = current.strftime("%Y-%m")
    previous = (current.replace(day=1) - dt.timedelta(days=1)).strftime("%Y-%m")
    numbers = [
        "1Z004R316837162143", "1Z004R316839216224",
        "1Z004R316805433991", "1Z004R316807294692",
        "1Z004R316805636772",
    ]
    service.add_numbers(" ".join(numbers))
    with service.connection() as db:
        data = [
            ("On the Way: Delayed", "X", "Delivery will be delayed by one business day.", "retry", current_month, numbers[0]),
            ("Delayed", "I", "We’re sorry, delivery is taking longer than expected. Your package has been temporarily delayed.", "retry", previous, numbers[1]),
            ("Delivery Rescheduled", "X", "Your delivery will be rescheduled.", "retry", current_month, numbers[2]),
            ("On the Way", "I", "Departed from Facility", "retry", previous, numbers[3]),
            ("Doręczono", "D", "", "ready", current_month, numbers[4]),
        ]
        db.executemany(
            "UPDATE shipments SET ups_status=?,ups_status_code=?,"
            "ups_status_detail=?,state=?,ship_date=? WHERE tracking=?",
            [(s, kind, text, state, month + "-04", tracking)
             for s, kind, text, state, month, tracking in data],
        )
    stats = service.stats()
    assert stats["total"] == 5
    assert stats["pending"] == 4
    assert stats["ready"] == 1
    assert stats["pending_year"] == 4
    assert stats["delayed"] == 2
    assert stats["on_way"] == 1
    assert stats["rescheduled"] == 1
    assert stats["on_way_or_rescheduled"] == 2
    buckets = {x["month"]: x["pending"] for x in stats["pending_months"]}
    assert buckets[current_month] == 2 and buckets[previous] == 2
    assert len(buckets) == 12
    a = service.list_numbers(column_filters={"ups_status": "opóźniona"})
    assert a["total"] == 2
    labels = {r["tracking"]: r for r in a["rows"]}
    assert labels[numbers[0]]["ups_status_pl"] == "W drodze — opóźniona"
    assert labels[numbers[1]]["ups_status_pl"] == "Opóźniona"
    assert labels[numbers[0]]["ups_detail_pl"] == "Doręczenie opóźni się o jeden dzień roboczy."
    assert "Typ UPS:" not in repr(labels)
    assert ups_localization.status_pl("Reschedule Delivery Requested", "X") == (
        "Zlecono zmianę terminu doręczenia"
    )
    assert ups_localization.detail_pl("Processing at UPS Facility", "W drodze") == (
        "Przesyłka jest przetwarzana w sortowni UPS."
    )


def test_selection_column_fixed_icons_polish_and_full_width(application):
    import datetime as dt
    import service
    from zoneinfo import ZoneInfo

    a, b, c = (
        "1Z004R316837162143", "1Z004R316839216224", "1Z004R316807294692"
    )
    service.add_numbers(" ".join((a, b, c)))
    current = dt.datetime.now(ZoneInfo("Europe/Warsaw")).strftime("%Y-%m-%d")
    with service.connection() as db:
        db.execute(
            "UPDATE shipments SET state='retry',ups_status='On the Way: Delayed',"
            "ups_status_code='X',ups_status_detail='Delivery will be delayed by one business day.',"
            "ship_date=? WHERE tracking=?", (current, a),
        )
        db.execute(
            "UPDATE shipments SET state='retry',ups_status='Delayed',"
            "ups_status_code='I',"
            "ups_status_detail='We’re sorry, delivery is taking longer than expected. "
            "Your package has been temporarily delayed.',"
            "ship_date=? WHERE tracking=?", (current, b),
        )
    server = make_server("127.0.0.1", 0, application, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            for width in [1440, 390]:
                page = browser.new_page(viewport={"width": width, "height": 900})
                page.goto(f"http://127.0.0.1:{server.server_port}")
                page.locator("#month-filter").select_option("all")
                expect(page.locator("#rows tr")).to_have_count(3)
                first_header = page.locator("#shipments-table thead tr:first-child th").first
                expect(first_header).to_have_class("selection-header")
                expect(page.locator("#shipments-table thead .column-filters th").first).to_have_class("selection-filter")
                expect(page.locator("#rows tr .selection-cell")).to_have_count(3)
                assert page.locator("#rows tr .fba-cell input[type=checkbox]").count() == 0
                expect(page.locator('.column-header[data-col="fba"] .sort-header')).to_contain_text("Shipment ID")
                expect(page.locator('.column-header[data-col="queue"] .sort-header')).to_contain_text("API")

                row_a = page.locator(f'#rows tr[data-tracking="{a}"]')
                row_b = page.locator(f'#rows tr[data-tracking="{b}"]')
                expect(row_a.locator(".ups-status")).to_have_text("W drodze — opóźniona")
                expect(row_b.locator(".ups-status")).to_have_text("Opóźniona")
                row_a.locator(".ups-status").hover()
                expect(page.locator(".ups-tooltip")).to_contain_text(
                    "Doręczenie opóźni się o jeden dzień roboczy."
                )
                for forbidden in ("Typ UPS:", "Status: 048", "Delivery will be delayed"):
                    assert forbidden not in page.locator(".ups-tooltip").text_content()

                assert page.locator("#rows .queue-icon svg").count() == 3
                expect(page.locator(f'#rows tr[data-tracking="{c}"] .queue-icon')).to_have_attribute(
                    "aria-label", "Oczekuje na kolejne sprawdzenie w API UPS")
                pending_doc = page.locator(f'#rows tr[data-tracking="{c}"] .document-waiting-icon')
                expect(pending_doc).to_be_visible()
                assert pending_doc.text_content() == ""
                assert pending_doc.locator("svg").count() == 1
                # Numerical date columns share exactly the same typography.
                fonts = row_a.evaluate("""node => {
                    const ship = getComputedStyle(node.querySelector('[data-col="ship_date"]'));
                    const delivery = getComputedStyle(node.querySelector('[data-col="delivery_date"]'));
                    return [ship.fontFamily, delivery.fontFamily, ship.fontWeight, delivery.fontWeight];
                }""")
                assert fonts[0] == fonts[1] and fonts[2] == fonts[3]

                page.locator(".column-header[data-col='fba'] .column-grip").drag_to(
                    page.locator(".column-header[data-col='document'] .sort-header"))
                expect(first_header).to_have_class("selection-header")
                expect(page.locator("#rows tr").first.locator("td").first).to_have_class(
                    "selection-cell")
                page.locator("#select-page").check()
                expect(page.locator("#rows tr .selection-cell input:checked")).to_have_count(3)
                assert page.locator("#rows tr .fba-cell input").count() == 0
                assert page.locator("#count-delayed").text_content() == "2"
                assert page.locator("#count-pending-year").text_content() == "3"
                assert page.locator("#pending-months .month-count").count() == 13
                if width >= 1200:
                    dimensions = page.evaluate("""() => {
                        const wrap = document.querySelector('#shipments-panel .table-wrap');
                        return [wrap.clientWidth,
                          document.querySelector('#shipments-table').getBoundingClientRect().width]
                    }""")
                    assert abs(dimensions[0] - dimensions[1]) <= 2
                page.close()
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_full_tracking_payload_roundtrip_and_backfill_queue(application):
    import hashlib
    import json
    import zlib
    import service

    tracking = "1Z004R316837162143"
    service.add_numbers(tracking)
    payload = {"trackResponse": {"shipment": [{
        "pickupDate": "20261102", "package": [{
            "trackingNumber": tracking,
            "currentStatus": {"type": "X", "description": "On the Way: Delayed"},
            "activity": [{"status": {"description": "Delayed"}, "date": "20261107"}],
            "nestedExtra": {"string": "zażółć", "null": None, "array": [1, True, {"key": "value"}]},
        }]
    }]}}
    assert service.enqueue_missing_payloads() == 1
    assert service.next_backfill() == tracking
    service.save_tracking_payload(tracking, payload)
    with service.connection() as db:
        record = db.execute("SELECT * FROM ups_tracking_payloads WHERE tracking=?",
                            (tracking,)).fetchone()
    decoded = zlib.decompress(record["response_json_zlib"])
    assert json.loads(decoded) == payload
    assert hashlib.sha256(decoded).hexdigest() == record["response_sha256"]
    assert record["raw_bytes"] == len(decoded)
    service.finish_backfill(tracking, http_status=200)
    assert service.enqueue_missing_payloads() == 0
    assert service.next_backfill() is None
    assert service.tracking_payload_info()["stored"] == 1
    # Sensitive full JSON never leaks into standard shipment list.
    content = json.dumps(service.list_numbers(), ensure_ascii=False)
    assert "nestedExtra" not in content


def test_xlsx_report_selected_tracking_links_and_counters(application):
    import io
    import zipfile
    import service
    import reports

    a, b = "1Z004R316837162143", "1Z004R316839216224"
    service.add_numbers(a + " " + b)
    with service.connection() as db:
        db.execute("UPDATE shipments SET state='retry',ups_status='On the Way: Delayed',"
                   "ups_status_code='X',ship_date='2026-11-02' WHERE tracking=?", (a,))
        db.execute("UPDATE shipments SET state='ready',ups_status='Doręczono',"
                   "ups_status_code='D',ship_date='2026-11-03' WHERE tracking=?", (b,))
    buf, count = reports.build_report([a, b])
    assert count == 2
    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as book:
        assert book.testzip() is None
        workbook = book.read("xl/workbook.xml").decode()
        assert 'name="Przesyłki"' in workbook
        assert 'name="Statystyki"' in workbook
        rels = book.read("xl/worksheets/_rels/sheet1.xml.rels").decode()
        assert "loc=pl_PL" in rels
        assert a in rels and b in rels
        summary = book.read("xl/worksheets/sheet2.xml").decode()
        assert "<v>2</v>" in summary
    with application.test_client() as client:
        client.get("/")
        with client.session_transaction() as session:
            token = session["csrf"]
        response = client.post("/api/report/xlsx",
                               json={"trackings": [a,b]}, headers={"X-CSRF-Token":token})
        assert response.status_code == 200
        assert response.data.startswith(b"PK")
        assert "attachment" in response.headers["Content-Disposition"]
        assert response.headers["Content-Type"].startswith(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        assert response.headers["X-UPS-POD-Count"] == "2"
        assert client.post("/api/report/xlsx", json={"trackings":[a]}).status_code == 403
        bad = client.post("/api/report/xlsx",
                          json={"trackings":["1Z0000000000000000"]},
                          headers={"X-CSRF-Token":token})
        assert bad.status_code == 400


def test_pdf_thumbnail_first_page_cached_and_missing(application):
    import pymupdf
    import service
    import pod_thumbnail

    number = "1Z004R316807294692"
    service.add_numbers(number)
    document = pymupdf.open()
    page = document.new_page(width=350, height=495)
    page.insert_text((24, 42), "UPS Proof of Delivery – sample", fontsize=15)
    pdf = service.document_target(number)
    pdf.write_bytes(document.tobytes())
    document.close()
    service.mark_ready(number, "Doręczono", "", str(pdf), "", "pdf", "thumb-test")
    img = pod_thumbnail.thumbnail(number)
    assert img is not None and img.read_bytes().startswith(b"\x89PNG")
    assert pod_thumbnail.thumbnail(number) == img
    with application.test_client() as client:
        response = client.get(f"/api/shipments/{number}/thumbnail")
        assert response.status_code == 200
        assert response.mimetype == "image/png"
        assert response.data.startswith(b"\x89PNG")
        missing = client.get("/api/shipments/1Z0000000000000000/thumbnail")
        assert missing.status_code == 404


def test_gmail_app_password_config_and_login_without_gcp(application, monkeypatch):
    import service
    import support_mail

    logins = []
    class FakeSMTP:
        def __init__(self, host, port, timeout=None, context=None):
            assert host == "smtp.gmail.com" and port == 465
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def login(self, email, password): logins.append((email,password))
    monkeypatch.setattr(support_mail.smtplib, "SMTP_SSL", FakeSMTP)
    with application.test_client() as client:
        client.get("/")
        with client.session_transaction() as session:
            token = session["csrf"]
        endpoint = "/api/config/gmail-app"
        payload = {"email": "office@electrosmart.pl",
                   "app_password": "abcdefghijklmnop"}
        response = client.post(endpoint, json=payload, headers={"X-CSRF-Token":token})
        assert response.status_code == 200
        assert "abcdefghijklmnop" not in response.get_data(as_text=True)
        stored = (service.DATA_DIR / ".env").read_text()
        assert "GMAIL_SMTP_APP_PASSWORD=abcdefghijklmnop" in stored
        status = client.get("/api/config").json["gmail_app"]
        assert status["configured"] and status["email"] == payload["email"]
        verified = client.post("/api/config/gmail-app/check",
                               json={}, headers={"X-CSRF-Token":token})
        assert verified.status_code == 200
        assert verified.json["email"] == payload["email"]
        assert logins == [(payload["email"], payload["app_password"])]
        assert client.post(endpoint, json={"email":"bad","app_password":""},
                           headers={"X-CSRF-Token":token}).status_code == 400
        assert (service.DATA_DIR / ".env").stat().st_mode & 0o777 == 0o600


def test_full_width_24_columns_filter_visibility_resizer_and_xlsx_browser(application):
    import pymupdf
    import service

    numbers = ["1Z" + f"{i:016d}" for i in range(1, 202)]
    service.add_numbers(" ".join(numbers))
    first = numbers[0]
    document = pymupdf.open()
    page = document.new_page(width=320, height=460)
    page.insert_text((30, 50), "UPS POD Test")
    target = service.document_target(first)
    target.write_bytes(document.tobytes())
    document.close()
    service.mark_ready(first, "Doręczono", "", str(target), "", "pdf", "test")
    server = make_server("127.0.0.1", 0, application, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            for width in (390, 1440, 1920):
                ctx = browser.new_context(viewport={"width": width, "height": 900},
                                          accept_downloads=True)
                ctx.add_cookies([{
                    "name": "ups_pod_month", "value": "all",
                    "url": f"http://127.0.0.1:{server.server_port}"
                }])
                page = ctx.new_page()
                page.goto(f"http://127.0.0.1:{server.server_port}")
                expect(page.locator("#rows tr")).to_have_count(200)
                expect(page.locator("#column-filter-row")).to_be_hidden()
                page.locator("#column-filters-toggle").click()
                expect(page.locator("#column-filter-row")).to_be_visible()
                page.locator("#column-filters-toggle").click()
                expect(page.locator("#column-filter-row")).to_be_hidden()

                page.locator("#columns-open").click()
                for checkbox in page.locator("#columns-picker input[type=checkbox]").all():
                    if not checkbox.is_checked():
                        checkbox.check()
                page.locator("#columns-done").click()
                def table_metrics(active_page=page):
                    return active_page.evaluate("""() => {
                        const wrap = document.querySelector('#shipments-panel .table-wrap');
                        const table = document.querySelector('#shipments-table');
                        return {wrap:wrap.clientWidth,table:table.getBoundingClientRect().width,
                            scrollWidth:wrap.scrollWidth,body:document.documentElement.scrollWidth,
                            overflow:getComputedStyle(wrap).overflowX};
                    }""")
                dims = table_metrics()
                assert dims["body"] <= width + 1, dims
                assert abs(dims["wrap"] - dims["table"]) < 3, dims
                assert dims["scrollWidth"] <= dims["wrap"] + 3, dims
                assert dims["overflow"] == "hidden", dims

                page.locator("#columns-reset").count()  # no dialog reopened
                if width == 1920:
                    page.locator("#columns-open").click()
                    page.locator("#columns-reset").click()
                    page.locator("#columns-done").click()
                    th = page.locator('.column-header[data-col="fba"]')
                    handle = th.locator(".column-resize")
                    original = float(th.evaluate("(el)=>el.style.width.replace('px','')"))
                    bounds = handle.bounding_box()
                    assert bounds
                    page.mouse.move(bounds["x"] + bounds["width"]/2,
                                    bounds["y"] + bounds["height"]/2)
                    page.mouse.down()
                    page.mouse.move(bounds["x"] + bounds["width"]/2 + 145,
                                    bounds["y"] + bounds["height"]/2, steps=50)
                    page.mouse.up()
                    current = float(th.evaluate("(el)=>el.style.width.replace('px','')"))
                    assert current > original
                    assert abs(table_metrics()["wrap"]-table_metrics()["table"]) <= 2
                    page.locator(f'#rows tr[data-tracking="{first}"] .document-bubble.ready').hover()
                    expect(page.locator(".pod-preview")).to_be_visible()
                    assert page.locator(".pod-preview img").get_attribute("src").endswith(
                        f"/api/shipments/{first}/thumbnail")
                    img = page.request.get(
                        f"http://127.0.0.1:{server.server_port}/api/shipments/{first}/thumbnail")
                    assert img.status == 200 and img.body().startswith(b"\x89PNG")
                    page.locator(f'#rows tr[data-tracking="{first}"] input[type=checkbox]').check()
                    expect(page.locator("#export-selected")).to_be_enabled()
                    with page.expect_download() as download_info:
                        page.locator("#export-selected").click()
                    assert download_info.value.suggested_filename.endswith(".xlsx")
                ctx.close()
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_year_month_filter_with_twelve_month_pending_counts(application):
    import datetime as dt
    import service
    from zoneinfo import ZoneInfo

    now = dt.datetime.now(ZoneInfo("Europe/Warsaw"))
    year = now.year
    month_now = now.strftime("%Y-%m")
    month_prior = (now.replace(day=1) - dt.timedelta(days=1)).strftime("%Y-%m")
    prior_year = f"{year - 1}-12"
    a, b, c, d = ("1Z004R316807294692", "1Z004R316805636772",
                  "1Z004R316805433991", "1Z004R316837162143")
    service.add_numbers(" ".join([a,b,c,d]))
    with service.connection() as db:
        db.execute("UPDATE shipments SET state='retry',ship_date=? WHERE tracking=?",
                   (month_now + "-04", a))
        db.execute("UPDATE shipments SET state='retry',ship_date=? WHERE tracking=?",
                   (month_prior + "-08", b))
        db.execute("UPDATE shipments SET state='retry',ship_date=? WHERE tracking=?",
                   (prior_year + "-05", c))
        db.execute("UPDATE shipments SET state='ready',ship_date=? WHERE tracking=?",
                   (month_now + "-03", d))
    stats = service.stats()
    assert len(stats["pending_months"]) == 12
    assert stats["pending_months"][-1]["month"] == month_now
    assert stats["pending_months"][-1]["pending"] == 1
    assert stats["pending_year"] == 2
    current_year = service.list_numbers(month=str(year), scope="missing")
    assert current_year["total"] == 2
    assert {r["tracking"] for r in current_year["rows"]} == {a,b}
    assert service.list_numbers(month=month_now, scope="missing")["total"] == 1
    assert service.list_numbers(month=month_now, scope="all")["total"] == 2
    with application.test_client() as client:
        response = client.get(f"/api/shipments?month={year}&scope=missing")
        assert response.status_code == 200
        assert response.json["total"] == 2
        assert len(response.json["stats"]["pending_months"]) == 12
        assert client.get("/api/shipments?month=2026-13").status_code == 400


def test_month_buttons_tool_groups_reset_and_column_picker(application):
    import datetime as dt
    import service
    from zoneinfo import ZoneInfo

    now = dt.datetime.now(ZoneInfo("Europe/Warsaw"))
    month = now.strftime("%Y-%m")
    year = str(now.year)
    numbers = ["1Z" + f"{index:016d}" for index in range(5, 8)]
    service.add_numbers(" ".join(numbers))
    with service.connection() as db:
        db.execute("UPDATE shipments SET state='retry',ship_date=?,ups_status='On the Way', "
                   "ups_status_code='I' WHERE tracking=?", (month + "-07", numbers[0]))
        db.execute("UPDATE shipments SET state='ready',ship_date=?,ups_status='Delivered' "
                   "WHERE tracking=?", (month + "-08", numbers[1]))
        db.execute("UPDATE shipments SET state='retry',ship_date=?,ups_status='Delayed' "
                   "WHERE tracking=?", (f"{now.year-1}-12-10", numbers[2]))
    server = make_server("127.0.0.1", 0, application, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            for width in [1440, 390]:
                context = browser.new_context(viewport={"width":width,"height":850})
                context.add_cookies([{"name":"ups_pod_month", "value":"all",
                    "url":f"http://127.0.0.1:{server.server_port}"}])
                page = context.new_page()
                page.goto(f"http://127.0.0.1:{server.server_port}")
                expect(page.locator("#rows tr")).to_have_count(3)
                month_buttons = page.locator("#pending-months button.month-count")
                expect(month_buttons).to_have_count(13)
                expect(page.locator("#pending-months button").last).to_have_attribute(
                    "data-month", year)
                assert page.locator("#pending-months").evaluate(
                    "(x)=> x.scrollWidth <= x.clientWidth + 2")
                assert page.locator("#download-all").count() == 0
                labels = [page.locator("#"+name).text_content() for name in
                    ("download-selected","export-selected","fba-selected",
                     "retry-selected","clear-column-filters","default-view","columns-open")]
                assert labels == ["ZIP","XLSX","FBA","PONÓW API",
                    "WYCZYŚĆ FILTRY","DOMYŚLNY WIDOK","KOLUMNY"]
                assert page.locator(".action-secondary .action-group").count() == 3
                assert page.locator(".action-secondary .action-group-retry button").count() == 1
                expect(page.locator("#column-filter-row")).to_be_hidden()
                expect(page.locator(f'#rows tr[data-tracking="{numbers[0]}"] .document-waiting-icon')).to_have_attribute(
                    "aria-label", "Brak PDF. Otwórz dziennik operacji przesyłki " + numbers[0])

                page.locator(f'#pending-months button[data-month="{month}"]').click()
                expect(page.locator("#rows tr")).to_have_count(1)
                expect(page.locator("#month-filter")).to_have_value(month)
                expect(page.locator('[data-scope="missing"]')).to_have_class("filter active")
                expect(page.locator(f'#pending-months button[data-month="{month}"]')).to_have_attribute("aria-pressed","true")
                page.reload()
                expect(page.locator("#month-filter")).to_have_value(month)
                expect(page.locator("#rows tr")).to_have_count(1)
                page.locator(f'#pending-months button[data-month="{year}"]').click()
                expect(page.locator("#month-filter")).to_have_value(year)
                expect(page.locator("#rows tr")).to_have_count(1)
                page.locator("#clear-column-filters").click()
                expect(page.locator("#month-filter")).to_have_value("all")
                expect(page.locator("#rows tr")).to_have_count(3)
                expect(page.locator('[data-scope="all"]')).to_have_class("filter active")
                page.locator("#columns-open").click()
                picker = page.locator("#columns-picker")
                assert picker.locator("small").count() == 0
                assert picker.locator("h3").all_text_contents() == [
                    "Podstawowe","Dodatkowe dane UPS Tracking API"]
                choice = picker.locator(".column-choice").filter(has_text="Koniec przedziału doręczenia")
                expect(choice).to_have_attribute("title",
                    "Źródło danych w UPS Tracking API: package.deliveryTime.endTime")
                weight = picker.locator(".column-choice").filter(has_text="Waga")
                assert weight.count() == 1
                assert weight.evaluate("(el)=> {let n=el.previousElementSibling; while(n && n.tagName!=='H3') n=n.previousElementSibling; return n?.textContent?.trim() }") == "Dodatkowe dane UPS Tracking API"
                page.locator("#columns-done").click()
                page.locator('.column-header[data-col="fba"] .column-grip').focus()
                page.keyboard.press("Alt+ArrowRight")
                expect(page.locator("#shipments-table thead tr:first-child th").nth(2)).to_have_attribute("data-col","fba")
                page.locator("#default-view").click()
                expect(page.locator("#shipments-table thead tr:first-child th").nth(1)).to_have_attribute("data-col","fba")
                expect(page.locator('.column-header[data-col="weight"]')).to_be_hidden()
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= window.innerWidth + 1")
                context.close()
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)

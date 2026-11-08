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
    assert 'colspan="11"' in template
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
            assert headings[:9] == [
                "Shipment ID", "Numer UPS", "Nadanie",
                "Doręczenie", "W transporcie", "Waga", "Status UPS", "Dokument", "Podgląd POD",
            ]
            assert len(headings) == len(service.UPS_EXTRA_COLUMNS) + 9
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
            expect(header.locator("th").nth(3)).to_have_attribute("data-col", "weight")
            expect(page.locator("#rows tr").first.locator("td").nth(3)).to_have_attribute(
                "data-col", "weight")
            assert "weight" in page.evaluate('document.cookie')
            # Accessible reorder without a mouse, from the current position.
            page.locator('.column-header[data-col="fba"] .column-grip').focus()
            page.keyboard.press("Alt+ArrowRight")
            expect(header.locator("th").nth(2)).to_have_attribute("data-col", "weight")

            page.locator("#column-filters-toggle").click()
            page.locator('.column-filter[data-col="fba"]').fill("FBATWO")
            expect(page.locator("#rows tr")).to_have_count(1)
            expect(page.locator("#rows tr").first).to_have_attribute("data-tracking", b)
            page.reload()
            expect(header.locator("th").nth(2)).to_have_attribute("data-col", "weight")
            expect(header.locator("th").nth(3)).to_have_attribute("data-col", "fba")
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
            expect(page.locator("#table-notice")).to_contain_text("Zapisz adres Gmail")
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
    import gmail_smtp

    tracking = "1Z004R316805433991"
    service.add_numbers(tracking)
    with service.connection() as db:
        db.execute("UPDATE shipments SET state='retry',delivery_date='20200101',"
                   "delivery_date_type='SDD',ups_status='Delivery Rescheduled',"
                   "ups_status_code='X' WHERE tracking=?", (tracking,))

    with application.test_client() as client:
        client.get("/")
        with client.session_transaction() as session:
            csrf = session["csrf"]
        headers = {"X-CSRF-Token": csrf}
        missing = client.post(f"/api/support/{tracking}/send", json={}, headers=headers)
        assert missing.status_code == 503
        assert missing.json["code"] == "GMAIL_APP_PASSWORD_REQUIRED"

        sent = []
        monkeypatch.setattr(gmail_smtp, "status", lambda: {
            "configured": True, "verified": True, "email": "worker@gmail.com",
        })
        monkeypatch.setattr(gmail_smtp, "send", lambda msg:
            (sent.append(msg), {"id": "gmail-confirmed-17"})[1])
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

        with service.connection() as db:
            record = db.execute(
                "SELECT status,message_id FROM support_cases WHERE tracking=?", (tracking,)
            ).fetchone()
        assert record["status"] == "sent" and record["message_id"] == "gmail-confirmed-17"


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

        # Legacy OAuth routes are intentionally disabled after SMTP migration.
        assert client.post("/api/config/google",json={},headers=headers).status_code == 404
        assert client.post("/api/config/gmail/start",json={},headers=headers).status_code == 404
        assert client.get("/api/config").json["gmail_app"]["configured"] is False
        assert client.post("/api/config/gmail-app",json={
            "email":"invalid","app_password":""},headers=headers).status_code == 400
        assert client.post("/api/config/gmail-app/check",json={}).status_code == 403


def test_support_can_send_via_gmail_smtp(application, monkeypatch):
    import gmail_smtp
    import service
    import support_mail

    captured = []
    monkeypatch.setattr(gmail_smtp, "status", lambda: {
        "configured": True, "verified": True, "email": "sender@example.test"})
    monkeypatch.setattr(gmail_smtp, "send", lambda message: (
        captured.append(message), {"id": "smtp-test-id"})[1])

    tracking = "1Z004R316805433991"
    service.add_numbers(tracking)
    with service.connection() as db:
        db.execute("UPDATE shipments SET state='retry',delivery_date='20200101',"
                   "delivery_date_type='RDD',ups_status='Delivery Rescheduled',"
                   "ups_status_code='X' WHERE tracking=?", (tracking,))
    sent = support_mail.send_support(tracking)
    assert sent["sent"] is True and sent["channel"] == "gmail_smtp"
    assert len(captured) == 1
    assert captured[0]["From"] == "sender@example.test"
    assert captured[0]["To"] == support_mail.OFFICIAL_CONTACT
    assert "We do not assert that delivery has occurred." in captured[0].get_content()
    with pytest.raises(support_mail.SupportAlreadySent):
        support_mail.send_support(tracking)

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
            expect(page.locator("#gmail-app-form")).to_be_visible()
            assert page.locator("#gmail-config-id").count() == 0
            expect(page.locator("#gmail-app-save")).to_be_disabled()
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
    assert labels[numbers[0]]["ups_status_pl"] == "Opóźniona"
    assert labels[numbers[1]]["ups_status_pl"] == "Opóźniona"
    assert labels[numbers[0]]["ups_detail_pl"] == "Doręczenie opóźni się o jeden dzień roboczy."
    assert "Typ UPS:" not in repr(labels)
    assert ups_localization.status_pl("Reschedule Delivery Requested", "X") == (
        "Zmiana terminu"
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
                expect(page.locator(".api-fixed-header")).to_have_text("API")

                row_a = page.locator(f'#rows tr[data-tracking="{a}"]')
                row_b = page.locator(f'#rows tr[data-tracking="{b}"]')
                expect(row_a.locator(".ups-status")).to_have_text("Opóźniona")
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

                if width >= 700:
                    page.locator(".column-header[data-col='fba'] .column-grip").drag_to(
                        page.locator(".column-header[data-col='document'] .sort-header"))
                else:
                    # Tiny touch layouts keep sorting available; column order
                    # remains keyboard-accessible even without pointer drag.
                    page.locator(".column-header[data-col='fba'] .column-grip").focus()
                    page.keyboard.press("Alt+ArrowRight")
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


def test_gmail_oauth_only_ui(application):
    with application.test_client() as client:
        html = client.get("/").get_data(as_text=True)
        assert 'id="gmail-app-form"' in html
        assert 'id="gmail-connect"' not in html
        assert "Hasło aplikacji Google" in html
        assert "Zapisz Gmail" in html
        assert "brand-home" in html
        with client.session_transaction() as session:
            headers = {"X-CSRF-Token": session["csrf"]}
        assert client.post("/api/config/gmail-app",
                           json={},headers=headers).status_code == 200
        assert client.post("/api/config/gmail-app/check",
                           json={},headers=headers).status_code == 422
        status = client.get("/api/config").json["gmail_app"]
        assert not status["configured"]
        assert not status["verified"]
        assert client.post("/api/config/gmail/start",
                           json={},headers=headers).status_code == 404


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
                expect(page.locator("#rows tr")).to_have_count(201)
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
                    page.locator(f'#rows tr[data-tracking="{first}"] .pod-thumb-button').hover()
                    expect(page.locator(".pod-preview")).to_be_visible()
                    assert page.locator(".pod-preview img").get_attribute("src").endswith(
                        f"/api/shipments/{first}/thumbnail?size=large")
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
                labels = [page.locator("#"+name+" .button-label").text_content() for name in
                    ("download-selected","export-selected","fba-selected",
                     "retry-selected","clear-column-filters","default-view","columns-open")]
                assert labels == ["ZIP","XLSX","FBA","UPS API",
                    "Wyczyść filtry","Domyślny widok","Kolumny"]
                assert page.locator(".action-secondary .action-group").count() == 2
                assert page.locator(".action-main .action-group-files").count() == 1
                assert page.locator(".action-secondary .action-group-filters #retry-selected").count() == 1
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
                expect(page.locator("#shipments-table thead tr:first-child th").nth(3)).to_have_attribute("data-col","fba")
                page.locator("#default-view").click()
                expect(page.locator("#shipments-table thead tr:first-child th").nth(2)).to_have_attribute("data-col","fba")
                expect(page.locator('.column-header[data-col="weight"]')).to_be_hidden()
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= window.innerWidth + 1")
                context.close()
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)

def test_backend_1500_default_page_and_global_kpi_filters(application):
    import datetime as dt
    from zoneinfo import ZoneInfo
    import service

    now = service.utcnow()
    year = dt.datetime.now(ZoneInfo("Europe/Warsaw")).year
    month = f"{year}-11"
    with service.connection() as db:
        db.executemany(
            "INSERT INTO shipments(tracking,state,created_at,updated_at,next_attempt_at,ship_date)"
            " VALUES(?,'queued',?,?,?,?)",
            [("1Z"+f"{i:016d}",now,now,now,month+"-04") for i in range(1605)]
        )
        db.execute("UPDATE shipments SET state='ready' WHERE tracking='1Z0000000000000000'")
        db.execute("UPDATE shipments SET state='retry',ups_status='Delayed' WHERE tracking='1Z0000000000000001'")
        db.execute("UPDATE shipments SET state='retry',ups_status='On the Way: Delayed' WHERE tracking='1Z0000000000000002'")
        db.execute("UPDATE shipments SET state='retry',ups_status='On the Way' WHERE tracking='1Z0000000000000003'")
        db.execute("UPDATE shipments SET state='retry',ups_status='Delivery Rescheduled' WHERE tracking='1Z0000000000000004'")
    assert len(service.list_numbers()["rows"]) == 1500
    assert len(service.list_numbers(offset=1500)["rows"]) == 105
    assert len(service.list_numbers(limit=2000)["rows"]) == 1605
    assert len(service.list_numbers(limit=5000)["rows"]) == 1605
    assert service.list_numbers(scope="ready")["total"] == 1
    assert service.list_numbers(scope="delayed")["total"] == 2
    assert service.list_numbers(scope="onway")["total"] == 2
    assert service.list_numbers(scope="missing")["total"] == 1604
    assert service.stats()["delayed"] == 2
    assert service.stats()["on_way_or_rescheduled"] == 2
    with application.test_client() as c:
        data = c.get("/api/shipments?month=all").json
        assert data["total"] == 1605
        assert len(data["rows"]) == 1500
        assert c.get("/api/shipments?scope=delayed&month=all").json["total"] == 2
        assert c.get("/api/shipments?scope=onway&month=all").json["total"] == 2
        assert len(c.get("/api/shipments?limit=5000&month=all").json["rows"]) == 1605


def test_browser_1500_rows_double_pager_kpi_fixed_api_and_pod_thumbnail(application):
    import datetime as dt
    import pymupdf
    import service
    from zoneinfo import ZoneInfo

    now = service.utcnow()
    year = dt.datetime.now(ZoneInfo("Europe/Warsaw")).year
    month = f"{year}-11"
    with service.connection() as db:
        db.executemany(
            "INSERT INTO shipments(tracking,state,created_at,updated_at,next_attempt_at,ship_date)"
            " VALUES(?,'queued',?,?,?,?)",
            [("1Z"+f"{i:016d}",now,now,now,month+"-04") for i in range(1510)]
        )
        for i,status in [(1,"Delayed"),(2,"On the Way: Delayed"),
                         (3,"On the Way"),(4,"Delivery Rescheduled")]:
            db.execute("UPDATE shipments SET state='retry',ups_status=? WHERE tracking=?",
                       (status, "1Z"+f"{i:016d}"))
    first="1Z0000000000000000"
    doc=pymupdf.open()
    page=doc.new_page(width=340,height=460)
    page.insert_text((25,35),"UPS POD: screenshot test")
    target=service.document_target(first)
    target.write_bytes(doc.tobytes())
    doc.close()
    service.mark_ready(first,"Doręczono","",str(target),"","pdf","test")
    server=make_server("127.0.0.1",0,application,threaded=True)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch()
            context=browser.new_context(viewport={"width":1440,"height":940})
            context.add_cookies([{"name":"ups_pod_month","value":"all",
                "url":f"http://127.0.0.1:{server.server_port}"}])
            page=context.new_page()
            page.goto(f"http://127.0.0.1:{server.server_port}")
            expect(page.locator("#rows tr")).to_have_count(1500,timeout=30000)
            expect(page.locator("#page-size-top")).to_have_value("1500")
            expect(page.locator("#page-size")).to_have_value("1500")
            assert "1500 z 1510" in page.locator("#page-info-top").inner_text()
            assert page.locator("#page-info-top").inner_text()==page.locator("#page-info").inner_text()
            assert page.locator("#shipments-table thead tr:first-child th").nth(1).inner_text()=="API"
            assert page.locator("#shipments-table #rows tr").first.locator("td").nth(1).get_attribute("data-fixed")=="api"
            assert page.locator("#columns-picker .column-choice").filter(has_text="API").count()==0
            assert page.locator('.api-fixed-header').evaluate(
                "(el)=>getComputedStyle(el).position")=="sticky"
            assert page.locator('.api-fixed-header').evaluate(
                "(el)=>Math.round(el.getBoundingClientRect().width)")<=48
            thumb=page.locator(f'#rows tr[data-tracking="{first}"] .pod-thumb-button')
            expect(thumb).to_be_visible()
            expect(thumb.locator("img")).to_have_attribute("loading","lazy")
            thumb.hover()
            expect(page.locator(".pod-preview")).to_be_visible()
            assert page.locator(".pod-preview img").get_attribute("src").endswith(
                "/api/shipments/"+first+"/thumbnail?size=large")
            page.locator("#next-page-top").click()
            expect(page.locator("#rows tr")).to_have_count(10)
            expect(page.locator("#next-page")).to_be_disabled()
            expect(page.locator("#prev-page-top")).to_be_enabled()
            page.locator("#prev-page").click()
            expect(page.locator("#rows tr")).to_have_count(1500)
            page.locator("#page-size-top").select_option("500")
            expect(page.locator("#rows tr")).to_have_count(500)
            expect(page.locator("#page-size")).to_have_value("500")
            assert "ups_pod_page_size=500" in page.evaluate("document.cookie")
            page.reload()
            expect(page.locator("#page-size-top")).to_have_value("500")
            expect(page.locator("#rows tr")).to_have_count(500)
            page.locator('.stat-action[data-kpi="delayed"]').click()
            expect(page.locator("#rows tr")).to_have_count(2)
            page.locator('.stat-action[data-kpi="onway"]').click()
            expect(page.locator("#rows tr")).to_have_count(2)
            page.locator('.stat-action[data-kpi="ready"]').click()
            expect(page.locator("#rows tr")).to_have_count(1)
            page.locator('.stat-action[data-kpi="pending-year"]').click()
            expect(page.locator("#rows tr")).to_have_count(500)
            page.locator('.stat-action[data-kpi="all"]').click()
            expect(page.locator("#rows tr")).to_have_count(500)
            context.close()
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_responsive_toolbar_no_button_wrapping_390_to_1920(application):
    import service
    service.add_numbers("1Z004R316837162143 1Z004R316839216224")
    server = make_server("127.0.0.1", 0, application, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            for width in (390, 640, 768, 1024, 1440, 1920):
                for theme in ("light", "dark"):
                    context = browser.new_context(viewport={"width":width, "height":900})
                    context.add_cookies([
                        {"name":"ups_pod_theme","value":theme,
                         "url":f"http://127.0.0.1:{server.server_port}"},
                        {"name":"ups_pod_month","value":"all",
                         "url":f"http://127.0.0.1:{server.server_port}"}
                    ])
                    page = context.new_page()
                    page.goto(f"http://127.0.0.1:{server.server_port}")
                    expect(page.locator("#rows tr")).to_have_count(2)
                    metrics=page.evaluate("""() => {
                        const rail=document.querySelector('.action-secondary');
                        const r=rail.getBoundingClientRect();
                        const buttons=[...rail.querySelectorAll('button')].map(e=>{
                          const b=e.getBoundingClientRect();
                          return {id:e.id,x:b.left,width:b.width,top:b.top,right:b.right,
                           height:b.height};
                        });
                        return {width:innerWidth, doc:document.documentElement.scrollWidth,
                          rail:{left:r.left,right:r.right,top:r.top,width:r.width},
                          buttons, first:document.querySelector('.action-main').getBoundingClientRect().top,
                          monthCount:document.querySelectorAll('#pending-months button').length,
                          apiLeft:document.querySelector('.api-fixed-header').getBoundingClientRect().left,
                          selectRight:document.querySelector('.selection-header').getBoundingClientRect().right};
                    }""")
                    assert metrics["doc"] <= width+2, metrics
                    assert metrics["monthCount"] == 13
                    assert max(b["top"]+b["height"]/2 for b in metrics["buttons"]) - min(
                        b["top"]+b["height"]/2 for b in metrics["buttons"]) <= 2, metrics
                    assert all(b["width"] >= 18 and b["height"] >= 25
                               for b in metrics["buttons"]), metrics
                    assert all(b["right"] <= metrics["rail"]["right"] + 2
                               and b["x"] >= metrics["rail"]["left"] - 2
                               for b in metrics["buttons"]), metrics
                    assert abs(metrics["apiLeft"]-metrics["selectRight"]) <= 2, metrics
                    assert page.locator('.stat-action').count() == 6
                    assert page.locator(".pending-history-title").inner_text() == "Brak POD"
                    assert page.locator("#column-filters-toggle").get_attribute("aria-expanded") == "false"
                    context.close()
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_staff_gmail_and_notification_outbox(application, monkeypatch):
    import datetime as dt
    import json
    from zoneinfo import ZoneInfo
    import notifications
    import service
    import gmail_smtp

    numbers = ["1Z" + f"{i:016d}" for i in (411, 412, 413)]
    service.add_numbers(" ".join(numbers))
    old = (dt.datetime.now(ZoneInfo("Europe/Warsaw")) -
           dt.timedelta(days=3)).strftime("%Y%m%d")
    with service.connection() as db:
        for number in numbers:
            db.execute("UPDATE shipments SET state='retry',attempts=2,"
                       "ups_status='Out for Delivery',tracking_extras=? WHERE tracking=?",
                       (json.dumps({"ups.activity_date":old}), number))
        db.execute("UPDATE shipments SET ups_status='Delayed',tracking_extras='{}' "
                   "WHERE tracking=?", (numbers[2],))
    with application.test_client() as client:
        client.get("/")
        with client.session_transaction() as sess:
            headers = {"X-CSRF-Token":sess["csrf"]}
        employee={"name":"Anna Kowalska","email":"worker@example.org"}
        assert client.post("/api/config/employee",json=employee,headers=headers).status_code==200
        assert client.get("/api/config").json["employee"]==employee
        assert not notifications.available()
        monkeypatch.setattr(gmail_smtp,"verified", lambda: True)
        monkeypatch.setattr(gmail_smtp,"status", lambda: {
            "configured":True,"verified":True,"email":"office@example.org"})
        assert notifications.available()
        rows=client.get("/api/shipments?month=all").json["rows"]
        assert next(r for r in rows if r["tracking"]==numbers[0])["support_eligible"]
    service.save_tracking_detail(numbers[2],{"status":"Delivered","status_typ":"D"})
    assert notifications.queue_overdue()==2
    sent=[]
    monkeypatch.setattr(gmail_smtp,"send",
                        lambda msg:(sent.append(msg),{"id":"test-id"})[1])
    assert notifications.tick()==3
    assert len(sent)==1
    assert "Anna Kowalska" in sent[0].get_content()
    assert all(n in sent[0].get_content() for n in numbers)
    assert sent[0]["To"]==employee["email"]
    assert notifications.tick()==0
    with service.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM mail_notifications "
                          "WHERE state='sent'").fetchone()[0]==3
    monkeypatch.setattr(gmail_smtp,"verified", lambda: False)
    assert not notifications.available()



def test_ups_raw_reindex_adds_observed_safe_columns_only(application):
    import json
    import service

    number = "1Z004R316837162143"
    service.add_numbers(number)
    with service.connection() as db:
        db.execute("UPDATE shipments SET ups_status='Unchanged',"
                   "delivery_date='20261101',tracking_extras='{}' WHERE tracking=?",
                   (number,))
    response = {"trackResponse": {"shipment": [
        {"package": [{"trackingNumber": number,
                      "dimension": {"length": "72", "height": "44", "width": "33",
                                    "unitOfDimension": "CM"},
                      "currentStatus": {"description": "On the Way"},
                      "activity": [{"destSLIC": "ABCD", "xpldDetails": {
                          "exceptionReasonCode": "99"}}],
                      "referenceNumber": [{"number": "HIDDEN_PRIVATE_REFERENCE"}]}]}]}}
    service.save_tracking_payload(number, response)
    assert service.reindex_archived_tracking_columns() == 1
    assert service.reindex_archived_tracking_columns() == 0
    with service.connection() as db:
        record = db.execute("SELECT * FROM shipments WHERE tracking=?",
                            (number,)).fetchone()
        extras = json.loads(record["tracking_extras"])
    assert extras["ups.length"] == "72"
    assert extras["ups.height"] == "44"
    assert extras["ups.current_description"] == "On the Way"
    assert extras["ups.exception_reason"] == "99"
    assert record["ups_status"] == "Unchanged"
    assert record["delivery_date"] == "20261101"
    assert "HIDDEN_PRIVATE_REFERENCE" not in json.dumps(service.list_numbers())



def test_browser_mail_icon_employee_home_and_autofit_responsive(application):
    import datetime as dt
    import json
    import service
    from zoneinfo import ZoneInfo

    tracked = "1Z004R316837162143"
    service.add_numbers("FBA1234567890123456 " + tracked)
    old = (dt.datetime.now(ZoneInfo("Europe/Warsaw")) -
           dt.timedelta(days=3)).strftime("%Y%m%d")
    with service.connection() as db:
        db.execute("UPDATE shipments SET state='retry', attempts=2,"
                   "ups_status='Out for Delivery',delivery_date='',"
                   "delivery_date_type='',tracking_extras=? WHERE tracking=?",
                   (json.dumps({"ups.activity_date": old,
                                "ups.length": "44",
                                "ups.exception_reason": "98"}),tracked))
    server = make_server("127.0.0.1",0,application,threaded=True)
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            for width in (390, 768, 1440, 1920):
                context=browser.new_context(viewport={"width":width,"height":940})
                context.add_cookies([{"name":"ups_pod_month","value":"all",
                    "url":f"http://127.0.0.1:{server.server_port}"}])
                page=context.new_page()
                page.goto(f"http://127.0.0.1:{server.server_port}")
                expect(page.locator("#rows tr")).to_have_count(1)
                assert page.locator(".brand-home").get_attribute("href")=="/"
                assert page.locator(".brand-home").inner_text()=="ElectroSmart UPS-POD"
                expect(page.locator(".support-mail-icon")).to_be_visible()
                assert page.locator(".support-mail-icon svg").count()==1
                assert page.locator(".support-mail-icon").get_attribute("aria-label").startswith(
                    "Wyślij do UPS prośbę")
                dims=page.evaluate("""() => {
                  const c=document.querySelector("#rows .selection-cell").getBoundingClientRect();
                  const f=document.querySelector("#rows .fba-cell").getBoundingClientRect();
                  const t=document.querySelector("#shipments-table").getBoundingClientRect();
                  const w=document.querySelector("#shipments-panel .table-wrap").getBoundingClientRect();
                  const a=document.querySelector(".action-secondary").getBoundingClientRect();
                  const p=document.querySelector(".pagination-top").getBoundingClientRect();
                  return {selectEnd:c.right, fbaStart:f.left, fbaWidth:f.width,
                    tableWidth:t.width,wrapperWidth:w.width,actMiddle:a.top+a.height/2,
                    pagerMiddle:p.top+p.height/2,documentWidth:document.documentElement.scrollWidth};
                }""")
                assert abs(dims["tableWidth"]-dims["wrapperWidth"]) <= 2
                assert dims["documentWidth"]<=width+2
                assert dims["selectEnd"]<=dims["fbaStart"]+2
                if width>=1000:
                    assert abs(dims["actMiddle"]-dims["pagerMiddle"])<18, dims
                page.locator("#columns-open").click()
                field=page.locator(".column-choice").filter(has_text="Długość paczki")
                field.locator("input").check()
                page.locator("#columns-done").click()
                expect(page.locator('.column-header[data-col="ups.length"]')).to_be_visible()
                assert page.evaluate('columnWidths["ups.length"] >= 25')
                assert page.evaluate('document.cookie.includes("ups_pod_column_widths=")')
                page.locator("#tab-config").click()
                expect(page.locator("#employee-config-form")).to_be_visible()
                expect(page.locator("#gmail-app-note")).to_be_visible()
                assert page.locator(".gmail-advanced").count() == 0
                context.close()
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)

def test_v9_api_left_status_palette_large_pod_preview_and_oauth_ui(application):
    import datetime as dt
    import struct
    from zoneinfo import ZoneInfo
    import pymupdf
    import service

    numbers = ["1Z"+f"{i:016d}" for i in range(700,707)]
    statuses = [
        ("Delayed","X"), ("On the Way: Delayed","X"),
        ("Delivery Rescheduled","X"), ("Reschedule Delivery Requested","X"),
        ("Returned to Sender","X"), ("On the Way","I"), ("Delivered","D"),
    ]
    service.add_numbers(" ".join(numbers))
    today = dt.datetime.now(ZoneInfo("Europe/Warsaw")).strftime("%Y-%m-%d")
    with service.connection() as db:
        for tracking,(status,kind) in zip(numbers,statuses,strict=True):
            db.execute("UPDATE shipments SET state='retry',ups_status=?,ups_status_code=?,"
                       "ship_date=? WHERE tracking=?",(status,kind,today,tracking))
    doc=pymupdf.open()
    page=doc.new_page(width=595,height=842)
    page.insert_text((40,40),"UPS POD - first page zoom",fontsize=14)
    target=service.document_target(numbers[-1])
    target.write_bytes(doc.tobytes())
    doc.close()
    service.mark_ready(numbers[-1],"Doręczono","",str(target),"","pdf","checksum")
    from pod_thumbnail import thumbnail
    small = thumbnail(numbers[-1],large=False)
    big = thumbnail(numbers[-1],large=True)
    assert small and big
    small_size=struct.unpack(">II",small.read_bytes()[16:24])
    big_size=struct.unpack(">II",big.read_bytes()[16:24])
    assert big_size[0] > small_size[0]*1.9

    server=make_server("127.0.0.1",0,application,threaded=True)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch()
            for theme in ("light","dark"):
                context=browser.new_context(viewport={"width":1440,"height":940})
                context.add_cookies([
                    {"name":"ups_pod_month","value":"all",
                     "url":f"http://127.0.0.1:{server.server_port}"},
                    {"name":"ups_pod_theme","value":theme,
                     "url":f"http://127.0.0.1:{server.server_port}"},
                ])
                page=context.new_page()
                page.goto(f"http://127.0.0.1:{server.server_port}")
                expect(page.locator("#rows tr")).to_have_count(7)
                headers=page.locator("#shipments-table thead tr:first-child th")
                expect(headers.nth(0)).to_have_class("selection-header")
                expect(headers.nth(1)).to_have_class("api-fixed-header")
                expect(page.locator("#rows tr").first.locator("td").nth(1)).to_have_attribute("data-fixed","api")
                expect(page.locator("#rows tr").first.locator("td").nth(2)).to_have_attribute("data-col","fba")
                expect(page.locator("#rows .ups-status")).to_have_count(7)
                classes=[]
                for i,(_raw,_kind) in enumerate(statuses):
                    status=page.locator(f'#rows tr[data-tracking="{numbers[i]}"] .ups-status')
                    expect(status).to_have_text(["Opóźniona","Opóźniona","Zmiana terminu",
                                                  "Zmiana terminu","Zwrot","W drodze","Doręczono"][i])
                    classes.append(status.evaluate("(e)=>getComputedStyle(e).backgroundColor"))
                assert classes[0]==classes[1]==classes[4]
                assert classes[2]==classes[3]
                assert len(set((classes[0],classes[2],classes[5],classes[6])))==4
                ready=page.locator(f'#rows tr[data-tracking="{numbers[-1]}"] .document-bubble.ready')
                expect(ready).to_be_visible()
                doc_bg=ready.evaluate("(e)=>getComputedStyle(e).backgroundColor")
                assert doc_bg not in set(classes)
                waiting=page.locator(f'#rows tr[data-tracking="{numbers[0]}"] .document-waiting-icon')
                wait_bg=waiting.evaluate("(e)=>getComputedStyle(e).backgroundColor")
                assert wait_bg not in set(classes) and wait_bg != doc_bg
                thumb=page.locator(f'#rows tr[data-tracking="{numbers[-1]}"] .pod-thumb-button')
                thumb.hover()
                panel=page.locator(".pod-preview")
                expect(panel).to_be_visible()
                assert panel.locator("img").get_attribute("src").endswith("thumbnail?size=large")
                panel_rect=panel.bounding_box()
                link_rect=thumb.bounding_box()
                assert panel_rect and link_rect and panel_rect["width"]>550
                cx=link_rect["x"]+link_rect["width"]/2
                cy=link_rect["y"]+link_rect["height"]/2
                assert not (panel_rect["x"]<=cx<=panel_rect["x"]+panel_rect["width"]
                            and panel_rect["y"]<=cy<=panel_rect["y"]+panel_rect["height"])
                assert page.locator(".brand-home").get_attribute("href")=="/"
                page.locator("#columns-open").click()
                assert page.locator(".column-choice").filter(has_text="API").count()==0
                page.locator("#columns-done").click()
                page.locator('.column-header[data-col="fba"] .column-grip').focus()
                page.keyboard.press("Alt+ArrowRight")
                expect(headers.nth(1)).to_have_class("api-fixed-header")
                page.locator("#tab-config").click()
                expect(page.locator("#gmail-app-form")).to_be_visible()
                expect(page.locator("#gmail-app-save")).to_be_disabled()
                assert page.locator("#gmail-connect").count()==0
                page.locator("#ups-config-form button[type=submit]").click()
                expect(page.locator("#config-notice")).to_have_class("table-notice warning")
                context.close()
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)

def test_v10_transit_days_numeric_sort_filter_and_excel(application):
    import datetime as dt
    import zipfile
    import service
    import reports
    from zoneinfo import ZoneInfo

    today = dt.datetime.now(ZoneInfo("Europe/Warsaw")).date()
    ten = (today - dt.timedelta(days=10)).isoformat()
    four = (today - dt.timedelta(days=4)).strftime("%Y%m%d")
    planned = (today - dt.timedelta(days=1)).strftime("%Y%m%d")
    nums = ["1Z" + f"{i:016d}" for i in range(1000,1004)]
    service.add_numbers(" ".join(nums))
    with service.connection() as db:
        db.execute("UPDATE shipments SET ship_date=?,state='retry',ups_status='On the Way' WHERE tracking=?",
                   (ten,nums[0]))
        db.execute("UPDATE shipments SET ship_date=?,state='ready',ups_status='Delivered',ups_status_code='D',"
                   "delivery_date=?,delivery_date_type='DEL' WHERE tracking=?",
                   (ten,four,nums[1]))
        db.execute("UPDATE shipments SET ship_date=?,state='ready',ups_status='Delivered',"
                   "delivery_date=?,delivery_date_type='SDD',delivered_at='' WHERE tracking=?",
                   (ten,planned,nums[2]))
        db.execute("UPDATE shipments SET ship_date='' WHERE tracking=?", (nums[3],))
    details={row["tracking"]:row for row in service.list_numbers(month="all")["rows"]}
    assert details[nums[0]]["transit_days"]==10
    assert details[nums[1]]["transit_days"]==6
    assert details[nums[2]]["transit_days"] is None  # forecast is NOT delivered
    assert details[nums[3]]["transit_days"] is None  # unknown shipment date
    by_sort=service.list_numbers(month="all",sort_by="transit_days",sort_dir="desc")
    assert [row["tracking"] for row in by_sort["rows"]][:2]==nums[:2]
    filtered=service.list_numbers(month="all",column_filters={"transit_days":"6 dni"})
    assert filtered["total"]==1 and filtered["rows"][0]["tracking"]==nums[1]
    neg=service.transit_days_expression(today)
    with service.connection() as db:
        assert db.execute("SELECT "+neg+" FROM shipments WHERE tracking=?",(nums[1],)).fetchone()[0]==6
    output,count=reports.build_report(nums)
    assert count==4
    with zipfile.ZipFile(output) as book:
        names=book.namelist()
        strings=book.read("xl/sharedStrings.xml") if "xl/sharedStrings.xml" in names else b""
        # Strings_to_numbers=False, but durations are written as actual XLSX numbers.
        xml=book.read("xl/worksheets/sheet1.xml")
        assert b"W transporcie" in xml or b"W transporcie" in strings
        assert b'numFmtId=' in book.read("xl/styles.xml")
        assert b"6" in xml and b"10" in xml
        assert b"hyperlink" in xml


def test_v10_toolbar_led_header_and_resize_visuals(application):
    import datetime as dt
    from zoneinfo import ZoneInfo
    import service
    now=dt.datetime.now(ZoneInfo("Europe/Warsaw")).date().isoformat()
    nums=["1Z"+f"{i:016d}" for i in (2701,2702)]
    service.add_numbers(" ".join(nums))
    with service.connection() as db:
        db.execute("UPDATE shipments SET ship_date=? WHERE tracking=?",(now,nums[0]))
        db.execute("UPDATE shipments SET ship_date=?,state='retry' WHERE tracking=?",(now,nums[1]))
    server=make_server("127.0.0.1",0,application,threaded=True)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch()
            for width in (390,640,1024,1440,1920):
                for theme in ("light","dark"):
                    context=browser.new_context(viewport={"width":width,"height":900})
                    context.add_cookies([
                        {"name":"ups_pod_theme","value":theme,
                         "url":f"http://127.0.0.1:{server.server_port}"},
                        {"name":"ups_pod_month","value":"all",
                         "url":f"http://127.0.0.1:{server.server_port}"},
                    ])
                    page=context.new_page()
                    page.goto(f"http://127.0.0.1:{server.server_port}")
                    expect(page.locator("#rows tr")).to_have_count(2)
                    expect(page.locator('.column-header[data-col="transit_days"]')).to_be_visible()
                    expect(page.locator('#rows [data-col="transit_days"]').first).to_have_text("0 dni")
                    assert page.locator('#shipments-panel h2').filter(has_text="Potwierdzenia i statusy").count()==0
                    assert page.locator("#refresh-status").evaluate(
                        "(el)=>el.closest('header')?.classList.contains('topbar')") is True
                    expect(page.locator("#refresh-status")).to_contain_text("Aktualizacja:")
                    loc=page.evaluate("""()=>{
                        const g=id=>document.querySelector(id).getBoundingClientRect();
                        const f=g('.actions .action-main .action-group-files');
                        const q=g('#query'), p=g('.actions .pagination-top');
                        return {fileLeft:f.left,fileRight:f.right,queryLeft:q.left,
                          pagerLeft:p.left, width:document.documentElement.scrollWidth,
                          ledWidth:g('.pending-months .led-number').width,
                          ledBg:getComputedStyle(document.querySelector('.led-number')).backgroundColor,
                          resWidth:g('.column-header[data-col="tracking"] .column-resize').width,
                          resBg:getComputedStyle(document.querySelector(
                            '.column-header[data-col="tracking"] .column-resize'),'::after').backgroundColor};
                    }""")
                    assert loc["width"]<=width+2,loc
                    assert loc["fileRight"]<=loc["queryLeft"]+2,loc
                    assert loc["ledWidth"]>=21 and loc["resWidth"]>=5,loc
                    assert loc["resBg"]!="rgba(0, 0, 0, 0)",loc
                    page.locator(".column-header[data-col='transit_days'] .sort-header").click()
                    expect(page.locator('#rows [data-col="transit_days"]').first).to_have_text("0 dni")
                    context.close()
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)

def test_gmail_app_password_smtp_only_secure_config_and_delivery(application, monkeypatch):
    from email.message import EmailMessage
    import config_store
    import gmail_smtp
    import service

    password = "abcd" * 4
    handshakes = []
    delivered = []
    class FakeSMTP:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def login(self, address, supplied):
            handshakes.append(address)
            assert supplied == password
        def send_message(self, message):
            delivered.append(message)
            return {}
    monkeypatch.setattr(gmail_smtp, "_session", FakeSMTP)
    with application.test_client() as client:
        client.get("/")
        with client.session_transaction() as sess:
            headers = {"X-CSRF-Token": sess["csrf"]}
        result = client.post("/api/config/gmail-app", json={
            "email":"smtp@example.test", "app_password":"abcd abcd abcd abcd",
        }, headers=headers)
        assert result.status_code == 200
        assert password not in result.get_data(as_text=True)
        assert result.json["gmail_app"]["configured"]
        assert not result.json["gmail_app"]["verified"]
        assert not gmail_smtp.verified()
        assert (service.DATA_DIR / ".env").stat().st_mode & 0o777 == 0o600
        assert client.post("/api/config/gmail-app/check",json={}).status_code == 403
        checked = client.post("/api/config/gmail-app/check",json={},headers=headers)
        assert checked.status_code == 200 and checked.json["verified"]
        assert not delivered
        assert handshakes == ["smtp@example.test"]
        assert gmail_smtp.verified()
        assert client.get("/api/config").json["gmail_app"]["email"] == "smtp@example.test"
        assert password not in str(client.get("/api/config").json)

    note = EmailMessage()
    note["From"] = "smtp@example.test"
    note["To"] = "recipient@example.test"
    note["Message-ID"] = "<test-mail-id@example.test>"
    note["Subject"] = "Kontrola lokalna"
    note.set_content("Wiadomość w pamięci, brak wysyłki przez sieć")
    assert gmail_smtp.send(note)["channel"] == "gmail_smtp"
    assert len(delivered) == 1
    assert handshakes == ["smtp@example.test"] * 2
    with pytest.raises(gmail_smtp.GmailConfigError):
        note.replace_header("From", "other@example.test")
        gmail_smtp.send(note)
    config_store.update({"GMAIL_SMTP_APP_PASSWORD":"wxyz"*4})
    assert not gmail_smtp.verified()

def test_ups_optional_date_columns_iso_projection_hidden_by_default(application):
    import json
    import service

    a,b,c = ["1Z" + f"{i:016d}" for i in (3101,3102,3103)]
    service.add_numbers(" ".join((a,b,c)))
    with service.connection() as db:
        data = [
            (a,{"ups.pickup_date":"20261101","ups.activity_date":"20261107"}),
            (b,{"ups.pickup_date":"2026-11-03","ups.activity_date":"2026-11-08"}),
            (c,{"ups.pickup_date":"20260230","ups.activity_date":""}),
        ]
        for tracking,extras in data:
            db.execute("UPDATE shipments SET tracking_extras=? WHERE tracking=?",
                       (json.dumps(extras),tracking))
    found=service.list_numbers(month="all",
                               column_filters={"ups.pickup_date":"2026-11-01"})
    assert found["total"] == 1 and found["rows"][0]["tracking"] == a
    desc=service.list_numbers(month="all",sort_by="ups.activity_date",sort_dir="desc")
    assert [r["tracking"] for r in desc["rows"]][:2]==[b,a]
    server=make_server("127.0.0.1",0,application,threaded=True)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch()
            for mode in ("light","dark"):
                page=browser.new_page(viewport={"width":1440,"height":920})
                page.goto(f"http://127.0.0.1:{server.server_port}")
                page.evaluate("(theme) => { document.documentElement.dataset.theme = theme; }", mode)
                page.locator("#month-filter").select_option("all")
                expect(page.locator("#rows tr")).to_have_count(3)
                expect(page.locator('.column-header[data-col="ship_date"] .sort-header')).to_contain_text("Nadanie")
                expect(page.locator('.column-header[data-col="delivery_date"] .sort-header')).to_contain_text("Doręczenie")
                for key in ("ups.pickup_date","ups.activity_date"):
                    expect(page.locator(f'.column-header[data-col="{key}"]')).to_be_hidden()
                page.locator("#columns-open").click()
                for label in ("Data przejęcia przez UPS", "Ostatni skan"):
                    page.locator(".column-choice").filter(has_text=label).first.locator("input").check()
                page.locator("#columns-done").click()
                expect(page.locator('.column-header[data-col="ups.activity_date"] .sort-header')).to_contain_text("Ostatni skan")
                for tracking, pickup, scanned in [
                    (a,"2026-11-01","2026-11-07"),
                    (b,"2026-11-03","2026-11-08"),
                    (c,"—","—"),
                ]:
                    selector=f'#rows tr[data-tracking="{tracking}"]'
                    expect(page.locator(selector+' [data-col="ups.pickup_date"]')).to_have_text(pickup)
                    expect(page.locator(selector+' [data-col="ups.activity_date"]')).to_have_text(scanned)
                assert page.locator(f'#rows tr[data-tracking="{a}"] [data-col="ups.pickup_date"]').evaluate(
                    '(el)=>getComputedStyle(el).fontFamily') == page.locator(
                    f'#rows tr[data-tracking="{a}"] [data-col="ship_date"]').evaluate(
                    '(el)=>getComputedStyle(el).fontFamily')
                page.close()
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)

def test_gmail_app_form_disabled_until_valid_credentials_browser(application):
    server = make_server("127.0.0.1", 0, application, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 850})
            page.goto(f"http://127.0.0.1:{server.server_port}")
            page.locator("#tab-config").click()
            expect(page.locator("#gmail-app-save")).to_be_disabled()
            expect(page.locator("#gmail-app-check")).to_be_disabled()
            page.locator("#gmail-app-email").fill("demo@example.test")
            expect(page.locator("#gmail-app-save")).to_be_disabled()
            page.locator("#gmail-app-password").fill("a" * 15)
            expect(page.locator("#gmail-app-save")).to_be_disabled()
            page.locator("#gmail-app-password").fill("a" * 16)
            expect(page.locator("#gmail-app-save")).to_be_enabled()
            page.locator("#gmail-app-save").click()
            expect(page.locator("#config-notice")).to_contain_text("Dane Gmail zapisano")
            expect(page.locator("#gmail-app-save")).to_be_disabled()
            expect(page.locator("#gmail-app-check")).to_be_enabled()
            assert "a" * 16 not in page.locator("body").inner_text()
            # Do not click the live SMTP login check in a browser test.
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)

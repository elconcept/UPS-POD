#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
revision=$(git rev-parse HEAD)
docker compose -p ups-pod -f compose.yaml build --build-arg "REVISION=$revision"
# Synology CI fallback: real UPS API + SQLite + PDF route smoke test must
# succeed BEFORE Docker replaces production. Runs with isolated temporary data.
docker run --rm -i \
  --network ups-pod_ups_outbound \
  --user 1031:100 \
  --env-file /volume2/docker/ups-pod/.env \
  -e UPS_POD_HOME=/tmp/ups-pod-ci \
  -e UPS_POD_PDF_DIR=/tmp/ups-pod-ci/pod \
  -e HOME=/tmp \
  --tmpfs /tmp:size=256m,mode=1777 \
  --entrypoint python \
  ups-pod-web:local - <<'PY'
import os
from pathlib import Path
import ups_pod
import service
import app
service.init_db()
test_number = os.getenv("UPS_E2E_TRACKING") or "1Z004R316836426182"
client_id = os.getenv("UPS_CLIENT_ID") or os.getenv("client_id")
client_secret = os.getenv("UPS_CLIENT_SECRET") or os.getenv("client_secret")
assert client_id and client_secret, "UPS API credentials missing"
api = ups_pod.KlientUPS(
    os.getenv("UPS_POD_ADRES_API", "https://onlinetools.ups.com"),
    client_id, client_secret, jezyk="pl_PL",
    numer_konta=os.getenv("UPS_SHIPPER_NUMBER", ""))
status, payload, raw = api.sledz(test_number)
assert status == 200 and isinstance(payload, dict), "Real UPS API failed"
shipment = (payload.get("trackResponse") or {}).get("shipment") or []
assert shipment, "Missing UPS shipment"
pickup = shipment[0].get("pickupDate", "")
assert len(pickup) == 8 and pickup.isdigit(), "Missing actual UPS pickupDate"
service.add_numbers("FBA1234567 " + test_number)
service.save_tracking_metadata(test_number, payload)
with service.connection() as db:
    db.execute("UPDATE shipments SET ship_date=?,ship_date_source=? WHERE tracking=?",
               (pickup[:4] + "-" + pickup[4:6] + "-" + pickup[6:],
                "ups_pickup_date", test_number))
assert service.list_numbers(month=pickup[:4] + "-" + pickup[4:6])["rows"]
web = app.create_app(testing=True)
with web.test_client() as c:
    assert c.get("/healthz").status_code == 200
    assert c.get("/api/shipments?month=" + pickup[:4] + "-" + pickup[4:6]).status_code == 200
print("CI_LIVE_UPS_AND_SQLITE_PASS")
PY
# Account mapping on Synology: upspodsvc UID 1031, users GID 100.
# A one-time safe ownership migration is required for persistent SQLite and source files.
# Complete image build before stopping running application processes.
migration_marker=/volume2/docker/ups-pod/data/.uid_1031_migrated
if [ ! -f "$migration_marker" ]; then
  docker stop ups-pod-worker ups-pod-web 2>/dev/null || true
  docker run --rm --user 0:0 --entrypoint /bin/sh \
    -v /volume2/docker/ups-pod/data:/data \
    ups-pod-web:local -c 'chown -R 1031:100 /data && chmod 700 /data'
  touch "$migration_marker"
fi
docker compose -p ups-pod -f compose.yaml up -d --no-build --remove-orphans --wait --wait-timeout 150
printf 'UPS-POD deployed %s\n' "$revision"

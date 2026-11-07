#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
revision=$(git rev-parse HEAD)
docker compose -p ups-pod -f compose.yaml build --build-arg "REVISION=$revision"
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

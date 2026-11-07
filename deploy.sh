#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
revision=$(git rev-parse HEAD)
docker compose -p ups-pod -f compose.yaml build --build-arg "REVISION=$revision"
docker compose -p ups-pod -f compose.yaml up -d --no-build --remove-orphans --wait --wait-timeout 150
printf 'UPS-POD deployed %s\n' "$revision"

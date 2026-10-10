#!/usr/bin/env bash
# Read-only deployment check. Never print credentials or account response bodies.
set -euo pipefail
source /etc/zukunft-worker.env
printf 'Authenticated platform response: '
curl --silent --show-error --output /dev/null --write-out '%{http_code}\n' --header "x-worker-token: ${WORKER_TOKEN}" "${API_BASE_URL}/worker/accounts"

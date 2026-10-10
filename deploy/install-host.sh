#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run with sudo: sudo bash deploy/install-host.sh" >&2
  exit 1
fi

id -u zukunft-worker >/dev/null 2>&1 || useradd --system --home /var/lib/zukunft-worker --shell /usr/sbin/nologin zukunft-worker
install -d -m 0755 /opt/zukunft-worker/releases
install -d -o zukunft-worker -g zukunft-worker -m 0700 /var/lib/zukunft-worker/state
python3 -m venv /opt/zukunft-worker/venv
install -m 0644 deploy/zukunft-worker.service /etc/systemd/system/zukunft-worker.service

install -d -o root -g zukunft-worker -m 0750 /etc/zukunft-worker
if [[ ! -f /etc/zukunft-worker/private.pem ]]; then
  openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out /etc/zukunft-worker/private.pem
  openssl pkey -in /etc/zukunft-worker/private.pem -pubout -out /etc/zukunft-worker/public.pem
  chown root:zukunft-worker /etc/zukunft-worker/private.pem
  chmod 0640 /etc/zukunft-worker/private.pem
fi

if [[ ! -f /etc/zukunft-worker.env ]]; then
  install -m 0640 /dev/null /etc/zukunft-worker.env
  chown root:zukunft-worker /etc/zukunft-worker.env
  cat > /etc/zukunft-worker.env <<'EOF'
API_BASE_URL=https://zukunft-trading-backend.onrender.com
WORKER_TOKEN=REPLACE_ME
WORKER_ID=zukunft-customers-01
PRIVATE_KEY_PATH=/etc/zukunft-worker/private.pem
STATE_DIR=/var/lib/zukunft-worker/state
POLL_SECONDS=30
EXECUTION_ENABLED=false
EOF
fi

systemctl daemon-reload
echo "Host installed. Configure /etc/zukunft-worker.env before starting the service."
echo "Public key: /etc/zukunft-worker/public.pem"


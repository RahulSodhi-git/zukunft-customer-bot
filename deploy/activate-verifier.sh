#!/usr/bin/env bash
set -euo pipefail
archive="${1:?release archive required}"
release_id="${2:?release id required}"
[[ "$release_id" =~ ^[a-f0-9]{40}$ ]] || exit 1
root=/opt/zukunft-worker
release="$root/releases/$release_id"
sudo install -d -m 0755 "$release"
sudo tar -xzf "$archive" -C "$release"
sudo "$root/venv/bin/pip" install --no-deps --disable-pip-version-check "$release"
sudo ln -sfn "$release" "$root/current"
sudo install -m 0644 "$release/deploy/zukunft-verifier.service" /etc/systemd/system/zukunft-verifier.service
sudo chown root:zukunft-worker /etc/zukunft-worker
sudo chmod 0750 /etc/zukunft-worker
sudo systemctl daemon-reload
sudo systemctl enable --now zukunft-verifier
sudo systemctl restart zukunft-verifier
sudo systemctl is-active --quiet zukunft-verifier
# This deployment deliberately does NOT start/restart the trading worker.

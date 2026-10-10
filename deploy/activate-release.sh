#!/usr/bin/env bash
set -euo pipefail

archive="${1:?release archive is required}"
release_id="${2:?release id is required}"
root=/opt/zukunft-worker
release="$root/releases/$release_id"

sudo install -d -m 0755 "$release"
sudo tar -xzf "$archive" -C "$release"
sudo "$root/venv/bin/pip" install --disable-pip-version-check --no-cache-dir "$release"
sudo ln -sfn "$release" "$root/current"
sudo systemctl daemon-reload
sudo systemctl restart zukunft-worker
sudo systemctl is-active --quiet zukunft-worker
sudo find "$root/releases" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' | sort -nr | tail -n +6 | cut -d' ' -f2- | xargs -r sudo rm -rf


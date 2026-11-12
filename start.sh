#!/usr/bin/env bash
# Builds and starts the whole stack. Safe to run again at any time.
set -euo pipefail
cd -- "$(dirname -- "$0")"

if [ ! -f .env ]; then
  echo 'First run: cp .env.example .env and set MEDIA_DOMAIN (see README).' >&2
  exit 1
fi
python3 scripts/setup.py
docker compose up -d --build
domain=$(sed -n 's/^MEDIA_DOMAIN=//p' .env | tr -d "\"'")
echo "Done. Open https://$domain/admin to connect Surfshark and Jellyfin and see the status of every app."

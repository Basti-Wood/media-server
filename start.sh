#!/usr/bin/env bash
# Builds and starts the whole stack. Safe to run again at any time.
set -euo pipefail
cd -- "$(dirname -- "$0")"
# Compose plugin (docker compose) or the standalone tool (docker-compose), whichever is installed.
compose() { if docker compose version >/dev/null 2>&1; then docker compose "$@"; else docker-compose "$@"; fi; }

if [ ! -f .env ]; then
  echo 'First run: cp .env.example .env and set MEDIA_DOMAIN (see README).' >&2
  exit 1
fi
python3 scripts/setup.py
compose up -d --build
# Compose does not notice edited config files, so load the current login and address settings.
compose restart authelia caddy
python3 scripts/connect_apps.py
domain=$(sed -n 's/^MEDIA_DOMAIN=//p' .env | tr -d "\"'")
echo "Done. Open https://$domain/admin for the status of every app, Surfshark and the Jellyfin connection."

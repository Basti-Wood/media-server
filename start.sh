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
grep -qs media_session provision/authelia/configuration.yml && new_login= || new_login=1
python3 scripts/setup.py
compose up -d --build
# A login created just now must be loaded by the already running login service.
if [ -n "$new_login" ]; then compose restart authelia; fi
domain=$(sed -n 's/^MEDIA_DOMAIN=//p' .env | tr -d "\"'")
echo "Done. Open https://$domain/admin to connect Surfshark and Jellyfin and see the status of every app."

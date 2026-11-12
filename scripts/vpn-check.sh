#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")/.."
# Compose plugin (docker compose) or the standalone tool (docker-compose), whichever is installed.
compose() { if docker compose version >/dev/null 2>&1; then docker compose "$@"; else docker-compose "$@"; fi; }
echo 'VPN container health:'
compose ps gluetun qbittorrent
echo 'Public IP seen inside the downloader network namespace:'
compose exec -T qbittorrent curl --fail --max-time 15 https://api.ipify.org
echo
echo 'Public IP seen by indexer searches through the Gluetun proxy:'
compose exec -T prowlarr curl --fail --max-time 15 --proxy http://gluetun:8888 https://api.ipify.org
echo
echo 'Confirm this is a Surfshark exit IP, not your home IP. See README for the outage test.'

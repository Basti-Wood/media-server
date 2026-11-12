#!/usr/bin/env bash
# Consistent local backup. Briefly stops only services that were already running.
set -euo pipefail
cd -- "$(dirname -- "$0")/.."
umask 077
# Compose plugin (docker compose) or the standalone tool (docker-compose), whichever is installed.
compose() { if docker compose version >/dev/null 2>&1; then docker compose "$@"; else docker-compose "$@"; fi; }
mkdir -p backups
running_output=$(compose ps --services --status running)
running=()
if [[ -n "$running_output" ]]; then mapfile -t running <<< "$running_output"; fi
resume() {
  if ((${#running[@]})); then compose start "${running[@]}"; fi
}
trap resume EXIT
if ((${#running[@]})); then compose stop "${running[@]}"; fi
archive=$(mktemp "backups/config-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX.tar.gz")
tar --exclude='*/Cache/*' --exclude='*/Logs/*' --exclude='*.log' \
  -czf "$archive" .env docker-compose.yml deploy provision
sha256sum "$archive" > "$archive.sha256"
printf 'Backup created: %s\n' "$archive"

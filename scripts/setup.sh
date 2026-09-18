#!/usr/bin/env bash
# Linux/macOS: create .env from .env.example with generated passwords. Safe to re-run: it only
# fills values that are still empty. (Windows: scripts/setup.ps1 does the same.)
set -euo pipefail
cd "$(dirname "$0")/.."

command -v docker >/dev/null || { echo "Install Docker first: https://docs.docker.com/get-docker/" >&2; exit 1; }

random() { LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c "${1:-24}"; }
get_var() { grep -E "^$1=" .env | tail -n1 | cut -d= -f2-; }
set_var() {  # set KEY=VALUE in .env without sed's platform differences
  local tmp; tmp="$(mktemp)"
  awk -v k="$1" -v v="$2" 'BEGIN{done=0} $0 ~ "^"k"=" {print k"="v; done=1; next} {print} END{if(!done) print k"="v}' .env >"$tmp"
  mv "$tmp" .env
}

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env"
fi
chmod 600 .env
[ -n "$(get_var MQTT_PASSWORD)" ] || set_var MQTT_PASSWORD "$(random 24)"
if [ -z "$(get_var DASHBOARD_PASSWORD)" ]; then
  pw="$(random 16)"
  set_var DASHBOARD_PASSWORD "$pw"
  echo "Generated DASHBOARD_PASSWORD: $pw   (change it in .env if you like)"
fi

cat <<'EOF'

Next (docs/RUNBOOK.md has the details):
  1. In .env set NVR_HOST, NVR_RTSP_USER, NVR_RTSP_PASSWORD
  2. docker compose run --rm --no-deps dashboard python -m app.probe    # finds the stream URLs
  3. Paste the NVR_MAIN_URL / NVR_SUB_URL / CAMERAS lines it prints into .env
  4. docker compose up -d --build
EOF

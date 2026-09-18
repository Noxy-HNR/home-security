#!/usr/bin/env bash
# One-time (and safe to re-run) setup on the Linux host:
#   - creates .env from .env.example with generated secrets
#   - creates the data/ directories and the starting Frigate config
#   - (re)writes the MQTT password file from MQTT_USER / MQTT_PASSWORD in .env
set -euo pipefail
cd "$(dirname "$0")/.."

say() { printf '\033[1m%s\033[0m\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null || die "Docker is not installed (https://docs.docker.com/engine/install/)"
docker compose version >/dev/null 2>&1 || die "the Docker Compose plugin is missing (apt install docker-compose-plugin)"

random() { openssl rand -hex "${1:-24}" 2>/dev/null || head -c "${1:-24}" /dev/urandom | od -An -tx1 | tr -d ' \n'; }
set_var() {  # set_var KEY VALUE: replace KEY=... in .env (or append it)
  if grep -q "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else echo "$1=$2" >> .env; fi
}
get_var() { grep -E "^$1=" .env | tail -n1 | cut -d= -f2-; }

if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  say "Created .env"
fi
[ -n "$(get_var SESSION_SECRET)" ] || set_var SESSION_SECRET "$(random 32)"
[ -n "$(get_var MQTT_PASSWORD)" ] || set_var MQTT_PASSWORD "$(random 18)"
set_var DASHBOARD_UID "$(id -u)"
set_var DASHBOARD_GID "$(id -g)"

media_dir="$(get_var FRIGATE_MEDIA_DIR)"
mkdir -p data/dashboard data/homeassistant data/mosquitto data/frigate/config "${media_dir:-data/frigate/media}"

if [ ! -f data/frigate/config/config.yml ]; then
  cp config/frigate/config.example.yml data/frigate/config/config.yml
  say "Created data/frigate/config/config.yml from the template: edit the cameras section"
fi

mqtt_user="$(get_var MQTT_USER)"
mqtt_pass="$(get_var MQTT_PASSWORD)"
[ -n "$mqtt_user" ] || die "MQTT_USER is empty in .env"
docker compose run --rm --no-deps --entrypoint sh mosquitto -c \
  'mosquitto_passwd -b -c /mosquitto/data/passwd "$0" "$1" && chown mosquitto:mosquitto /mosquitto/data/passwd && chmod 0700 /mosquitto/data/passwd' \
  "$mqtt_user" "$mqtt_pass" >/dev/null
say "Wrote the MQTT password file for user '$mqtt_user'"

warn=0
[ "$(get_var DASHBOARD_PASSWORD)" = "change-me" ] && { echo "  - set DASHBOARD_PASSWORD in .env"; warn=1; }
[ -z "$(get_var NVR_RTSP_PASSWORD)" ] && { echo "  - set NVR_HOST / NVR_RTSP_USER / NVR_RTSP_PASSWORD in .env"; warn=1; }
[ -z "$(get_var HA_TOKEN)" ] && { echo "  - HA_TOKEN is empty: expected until Phase 1 (docs/RUNBOOK.md)"; warn=1; }
[ "$warn" = 1 ] && say "Still to do:"

cat <<'EOF'

Next:
  1. python3 scripts/probe_nvr.py --host <NVR IP> --user <user> --channels <n>   # find the RTSP URLs
  2. edit data/frigate/config/config.yml (cameras)
  3. docker compose up -d homeassistant mosquitto frigate
  4. follow docs/RUNBOOK.md Phase 1 for Home Assistant + Total Connect, put the token in HA_TOKEN
  5. docker compose up -d --build dashboard
  6. docker compose run --rm dashboard python -m app.check        # verify every integration
EOF

# Runbook

The plan's phases as concrete steps on the dedicated Linux box. Each phase ends with a
check; don't start the next one until it passes.

## Phase 0: Host and infrastructure

1. **OS.** Ubuntu Server 24.04 LTS or Debian 12. Give it a static DHCP lease.
2. **Docker Engine** (not Docker Desktop):
   ```bash
   curl -fsSL https://get.docker.com | sudo sh
   sudo usermod -aG docker "$USER"   # log out and back in
   sudo apt install -y ffmpeg git    # ffprobe for the NVR probe
   ```
3. **Intel iGPU?** `ls /dev/dri` showing `renderD128` means Frigate can decode video and run
   OpenVINO on the iGPU. Uncomment the `devices:` block in `docker-compose.yml` and set
   `device: GPU` + `hwaccel_args: preset-vaapi` in the Frigate config. On a CPU-only box
   this is the biggest free speedup there is.
4. **Storage.** Put `FRIGATE_MEDIA_DIR` on the big disk. Rough sizing: a 4 MP H.264 main
   stream records at ~4–8 GB/day of motion-only footage per camera.
5. **Clone and set up:**
   ```bash
   git clone <this repo> ~/home-security && cd ~/home-security
   ./scripts/setup.sh
   ```
   Edit `.env`: `DASHBOARD_PASSWORD`, `TZ`, `NVR_HOST`, `NVR_RTSP_USER`, `NVR_RTSP_PASSWORD`
   (URL-encode special characters), `FRIGATE_MEDIA_DIR`, `FRIGATE_PUBLIC_URL`.
6. **Find the RTSP URLs** (the plan's "confirm in VLC first", automated):
   ```bash
   python3 scripts/probe_nvr.py --host "$NVR_HOST" --user admin --channels 8
   ```
   It finds the URL layout, and prints each channel's main/sub codec, resolution and fps.
   Note any H.265 main streams (switch them to H.264 in the NVR if you want browser live
   video) and each sub stream's resolution.

**Check:** the probe lists a main and sub stream for every camera.

## Phase 1: Alarm integration

1. **In the Total Connect app/portal** (as the master user):
   - Create a **standard (non-admin) user** for this system, give it access to the location
     and a user code for the panel.
   - Enable **Sensor Activities** (Notifications → Sensor Activities). Without it no zone
     states reach Home Assistant at all.
2. **Start Home Assistant:** `docker compose up -d homeassistant`, open
   `http://<box>:8123`, create the owner account.
3. **Add the integration:** Settings → Devices & services → Add integration → *Total
   Connect*. Sign in as the standard user from step 1 and enter its user code.
4. **Integration options** (Configure on the Total Connect card):
   - **Require code: on.** Arming/disarming then needs the panel code; the dashboard shows
     a keypad and HA checks the code.
   - *Auto bypass low battery*: your call.
5. **Verify arm/disarm through HA before any custom code touches it** (the plan's Phase 1
   exit). Create a token first: your HA profile → Security → Long-lived access tokens.
   ```bash
   export HA_TOKEN=...               # the token you just made
   curl -s -H "Authorization: Bearer $HA_TOKEN" http://localhost:8123/api/states \
     | python3 -c 'import json,sys; [print(s["entity_id"], s["state"]) for s in json.load(sys.stdin) if s["entity_id"].startswith(("alarm_control_panel","binary_sensor"))]'
   # Arm Stay, then disarm (replace the entity id and code):
   curl -s -X POST -H "Authorization: Bearer $HA_TOKEN" -H "Content-Type: application/json" \
     -d '{"entity_id": "alarm_control_panel.home", "code": "XXXX"}' \
     http://localhost:8123/api/services/alarm_control_panel/alarm_arm_home
   curl -s -X POST -H "Authorization: Bearer $HA_TOKEN" -H "Content-Type: application/json" \
     -d '{"entity_id": "alarm_control_panel.home", "code": "XXXX"}' \
     http://localhost:8123/api/services/alarm_control_panel/alarm_disarm
   ```
   Watch the keypad or the TC2 app confirm each change (allow ~30 s).
6. Put the token in `.env` as `HA_TOKEN`.

**Check:** zones appear and change state when you open a door (within ~30 s), and the curl
arm/disarm works.

## Phase 2: Cameras and detection

1. Edit `data/frigate/config/config.yml`: one `go2rtc.streams` pair and one `cameras` entry
   per channel, using the layout and sub-stream resolutions from the probe.
2. `docker compose up -d mosquitto frigate`
3. Frigate prints its generated admin password once:
   ```bash
   docker compose logs frigate | grep -i -A2 password
   ```
   Open `https://<box>:8971` (self-signed certificate), sign in, change the password.
4. **Masks and zones first** (highest-leverage tuning): per camera, Settings → Masks /
   Zones. Mask the street, sky, trees and timestamps; draw zones for the areas you care
   about (porch, driveway). Put the alert-worthy zone names in `ALERT_ZONES` in `.env`.
5. Watch the stats on Frigate's System page. On a CPU-only box aim for detector inference
   under ~30 ms and zero skipped fps. If it can't keep up: lower `detect.fps` (5 → 3),
   use smaller sub streams, iGPU (Phase 0 step 3), or a Coral USB (swap the `detectors`
   block only).

**Check:** walking past a camera shows a tracked person in Frigate's UI, and this prints
an event (user and password are `MQTT_USER` / `MQTT_PASSWORD` from `.env`):
```bash
docker compose exec mosquitto mosquitto_sub -u security -P '<MQTT_PASSWORD>' -t frigate/events -C 1
```

## Phase 3: Backend

```bash
docker compose up -d --build dashboard
docker compose run --rm dashboard python -m app.check
```

`app.check` verifies, from inside the dashboard container: the HA token, the Total Connect
panel and zones (and warns if *Require code* is off), Frigate's API and cameras, each
camera's go2rtc live stream, and that Frigate publishes over MQTT with the configured
credentials. Fix anything marked `FAIL` before moving on.

Raw verification without the UI (the plan's "curl / raw WebSocket client"): put a random
token in `API_TOKENS` in `.env` (`openssl rand -hex 24`), `docker compose up -d dashboard`, then
```bash
curl -s -H "Authorization: Bearer <token>" http://localhost:8080/api/state | head -c 600
curl -s -H "Authorization: Bearer <token>" 'http://localhost:8080/api/events?limit=5'
```

## Phase 4–5: Dashboard

Open `http://<box>:8080`. Everything from the plan's Phase 4 (live grid, alarm panel,
event list) and Phase 5 (unified searchable log, per-zone sensor grid, alerts, notification
delivery) is in place. To finish alert delivery:

- **Phones:** install the ntfy app, subscribe to a long random topic, set `NTFY_TOPIC`
  (and `PUBLIC_URL` so a tap opens the event). Self-host ntfy if you'd rather not use ntfy.sh.
- **Desktop notifications** need HTTPS (browser rule), so they arrive with Phase 6. Until
  then the dashboard shows toasts and plays a tone when it's open.
- Tune `ALERT_LABELS`, `ALERT_CAMERAS`, `ALERT_ZONES`, `ALERT_ONLY_WHEN_ARMED`,
  `ALERT_COOLDOWN_SECONDS`, then `docker compose up -d dashboard`.

## Phase 6: Hardening

**LAN only (recommended to start):**
- Remove the `8123:8123` mapping from `docker-compose.yml` once Total Connect is set up.
- Consider binding Frigate's 8971 to the LAN only, or remove it and re-add when tuning.
- Back up `.env` and `data/` (excluding recordings): `data/dashboard` holds the event log
  and archived snapshots, `data/homeassistant` the Total Connect setup, `data/frigate/config`
  the camera config.

**Remote access:** prefer a VPN (Tailscale/WireGuard) over opening ports. The dashboard then
stays LAN-only while your phone reaches it through the tunnel. If you do put it behind a
reverse proxy with a real certificate (Caddy is simplest):
1. Set `DASHBOARD_BIND=127.0.0.1` so only the proxy reaches the dashboard.
2. `COOKIE_SECURE=true`, `PUBLIC_URL=https://…`.
3. Add `FORWARDED_ALLOW_IPS=<proxy address>` to the dashboard's environment so login rate
   limiting sees real client addresses.
4. Proxy WebSockets (`/api/ws`), pass the `Host` header through unchanged (the dashboard
   rejects WebSockets whose Origin doesn't match it), and don't buffer responses (camera streams).
5. HTTPS also enables HTTP/2 and desktop notifications.

**Retention:** Frigate's `record` / `snapshots` retention bounds recordings (watch the
System page's storage meter). The dashboard keeps its own snapshot copies for
`SNAPSHOT_RETENTION_DAYS` and event rows for `EVENT_RETENTION_DAYS` (0 = forever; rows are
tiny).

## Plan open questions, and where each one lands

| Question | Where it plugs in |
|---|---|
| Server RAM / disk / other workloads | `FRIGATE_SHM_SIZE`, `FRIGATE_MEDIA_DIR`, `detect.fps`, Frigate retention |
| Camera count, sub/main streams | `scripts/probe_nvr.py` output → Frigate `go2rtc` + `cameras` |
| Remote access vs LAN-only | Phase 6 above; nothing else changes |
| Clip/snapshot retention | Frigate `record`/`snapshots`; `SNAPSHOT_RETENTION_DAYS` |
| Notification channel | ntfy built in; toasts/sound always; desktop notifications with HTTPS |

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| System page: Home Assistant "Access token rejected" | `HA_TOKEN` wrong or revoked |
| No zones on the Alarm page | Sensor Activities not enabled in TC2; reload the integration after enabling |
| "Invalid user code" | wrong code, or the code isn't valid for the TC2 user HA signs in as |
| Arming refused with zone names | those zones are open: close or bypass them |
| Camera tile "Camera unavailable" | camera missing from Frigate, or Frigate can't read the stream (`docker compose logs frigate`) |
| Live video falls back to detections | go2rtc stream missing (`app.check`), or the main stream is H.265 |
| MQTT "Disconnected: … not authorised" | re-run `scripts/setup.sh` after changing `MQTT_USER`/`MQTT_PASSWORD` |
| Frigate exits with "Bus error" | raise `FRIGATE_SHM_SIZE` |

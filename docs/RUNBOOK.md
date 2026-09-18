# Runbook

The plan's phases as concrete steps. Everything runs in Docker, on whichever machine you
pick: a Windows or Mac with Docker Desktop, or a Linux box with Docker Engine. Each phase ends
with a check; don't start the next one until it passes.

Commands are the same on every platform (PowerShell, Terminal, or a Linux shell), run from the
project folder.

## Phase 0: Docker, `.env`, and finding the NVR's streams

### 1. Install Docker

| Host | Install | Notes |
|---|---|---|
| **Windows 10/11** | [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/) with the WSL 2 backend (the default) | Settings → Resources: give it at least 4 GB RAM and 4 CPUs. Turn off sleep, or recording stops. Allow the firewall prompt for port 8080. |
| **macOS** | [Docker Desktop](https://docs.docker.com/desktop/setup/install/mac-install/) | Same resource advice. Apple Silicon uses Frigate's CPU detector automatically (OpenVINO is x86 only). |
| **Linux** | `curl -fsSL https://get.docker.com \| sudo sh`, then `sudo usermod -aG docker $USER` and log in again | The only host where an Intel iGPU can be passed to Frigate (step 5). |

Docker Desktop can't pass a GPU or a USB Coral through, so detection runs on the CPU there.
That matches the plan's CPU-only constraint; a Linux host is only needed if you later want
the iGPU or a Coral.

### 2. Create `.env`

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1     # Windows
```
```bash
./scripts/setup.sh                                               # macOS / Linux
```
Both copy `.env.example` to `.env`, generate `MQTT_PASSWORD` and a `DASHBOARD_PASSWORD`, and
never overwrite values you've set. Then edit `.env`: `TZ`, `NVR_HOST`, `NVR_RTSP_USER`,
`NVR_RTSP_PASSWORD` (URL-encode special characters: `@` → `%40`, `#` → `%23`).

### 3. Find the NVR (Foscam FN9108HE)

Confirmed from the FN9108HE's own firmware (V6.2.154), not guessed:

- **No web interface.** This NVR refuses browser access on purpose ("this product does not
  support web access. Please download the Foscam APP&VMS software"). So there's no settings
  page to read a stream URL from — the probe, ONVIF, or the Foscam app are the only ways in.
- **It speaks ONVIF and RTSP.** The firmware runs a gSOAP **ONVIF server** and a live555
  **RTSP server**, so the probe's ONVIF path can ask it for its exact per-channel stream URLs
  (the RTSP paths are assigned dynamically, which is exactly why they can't be hardcoded).
- **H.264 only** (no H.265), so every stream plays in a browser — the dashboard's live video
  works for all channels.
- The Foscam app itself uses a proprietary protocol on ports 6001–6003; that's not usable by
  Frigate, which is why we go through ONVIF/RTSP.

Steps:
- **Find the NVR's address:** Foscam's [Equipment Search Tool](https://www.foscam.com/downloads/app_software.html)
  (Windows/macOS) lists every Foscam device with its IP. Give the NVR a fixed address (a DHCP
  reservation in your router). The Foscam app shows it too, under the device's network info.
- **Are the cameras reachable directly?** If the search tool also lists the cameras with
  addresses on your home network, Frigate can read each camera instead of going through the
  NVR (step 4). Cameras plugged into the NVR's PoE ports usually sit on the NVR's own subnet
  and aren't reachable; then the NVR's streams are the way in.

### 4. Probe for the stream URLs

Foscam doesn't publish the FN9108HE's RTSP address format, so don't guess it:

```bash
docker compose run --rm --no-deps dashboard python -m app.probe
```

It uses `NVR_HOST` / `NVR_RTSP_USER` / `NVR_RTSP_PASSWORD` from `.env` and:
1. asks the NVR over **ONVIF** for its exact stream addresses (ports 80, 88, 8000, 8080, 8899);
2. otherwise tries the common NVR URL layouts over **RTSP** (ports 554 and 88), reading codec
   and resolution from each answer;
3. stops at the first rejected password, so a typo can't lock the NVR account.

It ends with lines to paste into `.env`, for example:
```
NVR_MAIN_URL=rtsp://{user}:{password}@{host}:554/...{ch}...
NVR_SUB_URL=rtsp://{user}:{password}@{host}:554/...{ch}...
CAMERAS=camera_1:1,camera_2:2,camera_3:3
DETECT_SIZE=640x360
```
Rename the cameras (`CAMERAS=front_door:1,driveway:2,backyard:3`). Names become Frigate
camera names: letters, digits, `_` and `-`.

**Probe finds nothing?**
- Probe a camera instead of the NVR: `docker compose run --rm --no-deps dashboard python -m app.probe --host <camera-ip>`.
  Foscam cameras answer on `/videoMain` and `/videoSub` (port 88 or 554), and the probe will
  say so. List them in `CAMERAS` as `name@ip`, e.g. `CAMERAS=porch@192.168.1.21`; direct
  cameras default to Foscam's paths, so no URL settings are needed. When the NVR added the
  cameras at factory defaults it gave them the NVR's username and password.
- The FN9108HE has no web page to copy a URL from. If the probe can't reach it over ONVIF or
  RTSP, check that `NVR_HOST` and the credentials are right and that the NVR is on the same
  network as Docker, then try `--host <a camera's IP>` as above.

**Check:** the probe prints a main and a sub stream for every camera.

### 5. Linux + Intel iGPU only (optional)

If `ls /dev/dri` shows `renderD128`: uncomment the `devices:` block under `frigate` in
`docker-compose.yml`, and after the first start set `device: GPU` under `detectors` and add
`ffmpeg: {hwaccel_args: preset-vaapi}` in `frigate/config.yml`. It's the biggest free
speedup for detection.

## Phase 1: Alarm integration

1. **In the Total Connect app/portal** (as the master user):
   - Create a **standard (non-admin) user** for this system, give it access to the location
     and a user code for the panel.
   - Enable **Sensor Activities** (Notifications → Sensor Activities). Without it no zone
     states reach Home Assistant at all.
2. **Start Home Assistant:** `docker compose up -d homeassistant`, open
   `http://localhost:8123` (or `http://<host-ip>:8123`), create the owner account.
3. **Add the integration:** Settings → Devices & services → Add integration → *Total
   Connect*. Sign in as the standard user from step 1 and enter its user code.
4. **Integration options** (Configure on the Total Connect card):
   - **Require code: on.** Arming/disarming then needs the panel code; the dashboard shows
     a keypad and HA checks the code.
   - *Auto bypass low battery*: your call.
5. **Create a token:** your HA profile → Security → Long-lived access tokens. Put it in
   `.env` as `HA_TOKEN`.
6. **Verify arm/disarm in Home Assistant before any custom code touches it** (the plan's
   Phase 1 exit): Settings → Devices & services → Total Connect → the panel device → *Arm
   home*, then *Disarm*. Watch the keypad or the TC2 app confirm each change (allow ~30 s).

**Check:** zones appear on the device page and change state when you open a door (within
~30 s), and arm/disarm from HA works.

## Phase 2: Cameras and detection

1. Start the stack:
   ```bash
   docker compose up -d --build
   ```
   The `frigate-config` step writes `frigate/config.yml` from `CAMERAS` and the URLs before
   Frigate starts. If it fails, `docker compose up` stops with its message (e.g. `CAMERAS is
   empty`). Fix `.env` and run the command again.
2. Frigate prints a generated admin password once:
   ```bash
   docker compose logs frigate
   ```
   Open `https://localhost:8971` (self-signed certificate; accept the warning), sign in, and
   change the password. Put this address in `FRIGATE_PUBLIC_URL` so the dashboard links to it.
3. **Masks and zones first** (highest-leverage tuning): per camera, Settings → Masks /
   Zones. Mask the street, sky, trees and timestamps; draw zones for the areas you care
   about (porch, driveway). Put the alert-worthy zone names in `ALERT_ZONES` in `.env`.
4. Watch Frigate's System page (or the dashboard's). On CPU detection aim for inference
   under ~30 ms and zero skipped fps. If it can't keep up: lower `DETECT_FPS` (5 → 3) or the
   NVR's sub-stream resolution, or use the iGPU on a Linux host.

**Adding a camera later:** add it to `CAMERAS` and run `docker compose up -d`. New cameras
are appended to `frigate/config.yml`; existing ones, with their zones and masks, are never
touched. To regenerate from scratch (loses UI edits):
`docker compose run --rm frigate-config python -m app.frigate_config --config /config/config.yml --force`.

**Check:** walking past a camera shows a tracked person in Frigate's UI.

## Phase 3: Backend

```bash
docker compose run --rm dashboard python -m app.check
```

`app.check` verifies, from inside the dashboard container: the HA token, the Total Connect
panel and zones (and warns if *Require code* is off), Frigate's API and cameras, each
camera's live stream, and that Frigate publishes over MQTT. Fix anything marked `FAIL`.

Raw verification without the UI (the plan's "curl / raw WebSocket client"): put a random
token in `API_TOKENS` in `.env`, run `docker compose up -d`, then
```bash
curl -H "Authorization: Bearer <token>" http://localhost:8080/api/state
```
(PowerShell: use `curl.exe`, since `curl` there is an alias for `Invoke-WebRequest`.)

## Phase 4–5: Dashboard

Open `http://localhost:8080` on the host, or `http://<host-ip>:8080` from other devices.
Everything from the plan's Phase 4 (live grid, alarm panel, event list) and Phase 5 (unified
searchable log, per-zone sensor grid, alerts, notification delivery) is in place. To finish
alert delivery:

- **Phones:** install the ntfy app, subscribe to a long random topic, set `NTFY_TOPIC`
  (and `PUBLIC_URL` so a tap opens the event).
- **Desktop notifications** need HTTPS (browser rule), so they arrive with Phase 6. Until
  then the dashboard shows toasts and plays a tone when it's open.
- Tune `ALERT_LABELS`, `ALERT_CAMERAS`, `ALERT_ZONES`, `ALERT_ONLY_WHEN_ARMED`,
  `ALERT_COOLDOWN_SECONDS`, then `docker compose up -d`.

## Phase 6: Hardening

**LAN only (recommended to start):**
- Remove the `8123` port mapping from `docker-compose.yml` once Total Connect is set up.
- Consider removing Frigate's `8971` mapping too, and re-adding it when tuning.
- **Backups:** everything lives in Docker volumes plus two files. Keep `.env` and the
  `frigate/` folder, and back up the volumes that matter:
  ```bash
  docker run --rm -v home-security_dashboard-data:/v -v "${PWD}:/b" alpine tar czf /b/dashboard-data.tgz -C /v .
  docker run --rm -v home-security_homeassistant-config:/v -v "${PWD}:/b" alpine tar czf /b/homeassistant.tgz -C /v .
  ```
  (`dashboard-data` holds the event log and archived snapshots; `homeassistant-config` the
  Total Connect setup. Recordings are in `frigate-media`, usually not worth backing up.)

**Remote access:** prefer a VPN (Tailscale/WireGuard) over opening ports. The dashboard then
stays LAN-only while your phone reaches it through the tunnel. If you do put it behind a
reverse proxy with a real certificate (Caddy is simplest):
1. Set `DASHBOARD_BIND=127.0.0.1` so only the proxy reaches the dashboard.
2. `COOKIE_SECURE=true`, `PUBLIC_URL=https://…`.
3. Add `FORWARDED_ALLOW_IPS=<proxy address>` to `.env` so login rate limiting sees real
   client addresses.
4. Proxy WebSockets (`/api/ws`), pass the `Host` header through unchanged (the dashboard
   rejects WebSockets whose Origin doesn't match it), and don't buffer responses (camera streams).
5. HTTPS also enables HTTP/2 and desktop notifications.

**Retention:** Frigate's `record` / `snapshots` retention bounds recordings (watch the
System page's storage meter; on Docker Desktop recordings count against its disk image,
see Settings → Resources). The dashboard keeps its own snapshot copies for
`SNAPSHOT_RETENTION_DAYS` and event rows for `EVENT_RETENTION_DAYS` (0 = forever).

## Plan open questions, and where each one lands

| Question | Where it plugs in |
|---|---|
| Which machine / RAM / disk | Any Docker host; Docker Desktop resources; `FRIGATE_SHM_SIZE`, `FRIGATE_MEDIA`, `DETECT_FPS` |
| Camera count, sub/main streams | `app.probe` output → `CAMERAS`, `NVR_MAIN_URL`, `NVR_SUB_URL`, `DETECT_SIZE` |
| Remote access vs LAN-only | Phase 6 above; nothing else changes |
| Clip/snapshot retention | Frigate `record`/`snapshots` in `frigate/config.yml`; `SNAPSHOT_RETENTION_DAYS` |
| Notification channel | ntfy built in; toasts/sound always; desktop notifications with HTTPS |

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `docker compose` says `Set MQTT_PASSWORD in .env` (or `DASHBOARD_PASSWORD`) | run the setup script, or fill it in |
| `frigate-config` exits with `CAMERAS is empty` / `NVR_MAIN_URL is empty` | Phase 0 step 4 isn't done yet |
| Probe: `rejected the username/password` | wrong `NVR_RTSP_USER`/`NVR_RTSP_PASSWORD`; it stopped after one try on purpose |
| System page: Home Assistant "Access token rejected" | `HA_TOKEN` wrong or revoked |
| No zones on the Alarm page | Sensor Activities not enabled in TC2; reload the integration after enabling |
| "Invalid user code" | wrong code, or the code isn't valid for the TC2 user HA signs in as |
| Arming refused with zone names | those zones are open: close or bypass them |
| Camera tile "Camera unavailable" | Frigate can't read the stream: `docker compose logs frigate` |
| Live video falls back to detections | go2rtc stream missing (`app.check`) |
| MQTT "not authorised" | after changing `MQTT_USER`/`MQTT_PASSWORD`, run `docker compose up -d` so every service restarts with it |
| Frigate exits with "Bus error" | raise `FRIGATE_SHM_SIZE` |
| Nothing records overnight (Windows/macOS) | the computer slept; Docker Desktop pauses with it |

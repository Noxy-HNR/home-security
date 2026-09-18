# Home Security Dashboard

One self-hosted dashboard for a Honeywell ProSeries panel (via Resideo Total Connect 2.0)
and the cameras on an ONVIF/RTSP NVR: live views with detection overlays, arm/disarm,
every zone's state, alerts, and a searchable event log you own.

Home Assistant and Frigate run headless as integration engines; the dashboard is the only
UI you use day to day, and the only service the browser talks to.

```
NVR (RTSP) ──► Frigate ──MQTT──► Mosquitto ──► dashboard backend (FastAPI) ──► browser
                 │  └─ REST / MJPEG / go2rtc ───────────▲        │                (WebSocket
                 ▼                                       │        ▼                 + REST)
           recordings                  Home Assistant ◄──┘   event log
                                       (Total Connect)  WS    (SQLite / Postgres)
                                             ▲                + archived snapshots
                                  TC2 cloud ─┘ (polled ~30 s)
```

## Try it now (no hardware)

Demo mode simulates Home Assistant, Frigate and MQTT through the same code paths the real
integrations use: people and cars walking past four cameras, doors opening, a panel you can
arm (code `1234`), and a "simulate a break-in" button.

```bash
cd backend
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt        # Windows: .venv\Scripts\pip
DEMO_MODE=true .venv/bin/uvicorn app.main:create_app --factory --port 8090
```

Open http://localhost:8090. Without a `DASHBOARD_PASSWORD`, demo mode needs no login; set
one to try the sign-in screen. On Windows PowerShell use `$env:DEMO_MODE="true"` first.

## Deploy

On the dedicated Linux box (Debian/Ubuntu Server + Docker Engine), follow
**[docs/RUNBOOK.md](docs/RUNBOOK.md)**. It is the plan's Phase 0–6, as steps:

```bash
git clone <this repo> home-security && cd home-security
./scripts/setup.sh                                   # .env, secrets, data dirs, MQTT password
python3 scripts/probe_nvr.py --host <NVR IP> --channels <n>   # find RTSP URLs + resolutions
# edit data/frigate/config/config.yml, then Phase 1 (Home Assistant + Total Connect)
docker compose up -d --build
docker compose run --rm dashboard python -m app.check   # verifies HA, Frigate, go2rtc, MQTT
```

## What's in the dashboard

| View | What it does |
|---|---|
| **Live** | Every camera with Frigate's boxes drawn, badges for people/cars moving now, alarm summary, live activity feed |
| **Camera** | Full-quality live video (go2rtc, main stream) or the detection view; that camera's events; link to Frigate for zones/masks |
| **Alarm** | Arm Stay/Away/Night (optionally instant), disarm on a keypad, bypass zones, AC/battery/tamper, every zone's state |
| **Events** | Detections, arm/disarm, zone activity, low battery/tamper, and every command sent (who, when, result) in one log; filter by source, camera/zone, object, time, alerts-only, text; snapshot + clip playback |
| **System** | Connection health for HA/Frigate/MQTT, detector inference time, per-camera fps and skipped frames, recording storage |

Alerts (people by default, optionally only in chosen Frigate zones or while armed; alarm
triggered; AC loss; tamper; low battery) show as toasts with sound in the dashboard, as
desktop notifications (HTTPS only), and as phone pushes through [ntfy](https://ntfy.sh)
with the snapshot attached.

## Decisions that differ from the plan

- **Linux host throughout.** The plan's host section says a dedicated Linux box, but §3.1
  and Phase 0 still describe Docker Desktop + WSL2 on Windows. Everything here targets the
  Linux box.
- **Home Assistant is read over its WebSocket API, not MQTT.** HA's MQTT integration doesn't
  publish entity states by itself (that needs `mqtt_statestream`), and the WebSocket gives
  attributes, registry ids and commands with real error messages ("Invalid PIN") over one
  authenticated connection. MQTT still carries Frigate.
- **Zones are identified by registry `unique_id`, not entity names**, so renaming entities in
  HA can't break the dashboard.
- **Total Connect doesn't expose battery level or signal strength.** HA's integration gives
  low-battery and tamper as on/off only. The history table keeps `battery_level` and
  `signal_strength` columns (empty) for a future direct `total_connect_client` source.
- **Zone changes lag up to ~30 s.** HA polls the TC2 cloud; nothing local can do better
  with this panel. The UI says so where it matters.
- **No-build frontend.** Plain ES modules served by the backend instead of React/Next: no
  Node toolchain on the box, one container fewer, strict CSP (`script-src 'self'`).
- **The live grid polls snapshots instead of holding an MJPEG stream per camera.** Browsers
  allow six HTTP/1.1 connections per host; eight endless streams would stall everything
  else. Polling pauses off-screen and in background tabs, which also saves CPU on the box.
- **No `cameras` table.** Frigate's config is the single source of truth for cameras (and
  the only place RTSP credentials live).
- **SQLite by default,** Postgres via `DATABASE_URL` and the optional compose profile.

## Security notes

- Only the dashboard (8080), Frigate's authenticated UI (8971) and HA (8123, needed for
  setup only) are published. MQTT, Frigate's unauthenticated API and go2rtc stay internal.
- One household password → signed, HttpOnly, SameSite=Strict session cookie; login is
  rate-limited; bearer tokens (`API_TOKENS`) for scripts; WebSocket origin checked.
- Camera names are whitelisted against Frigate's camera list before they reach an upstream
  URL (otherwise `/api/cameras/config/...` would proxy Frigate's config, credentials included).
- Every arm/disarm/bypass request is written to the event log with the client address and
  the result, including rejected codes.
- Turn on **Require code** in the Total Connect integration options so disarming needs the
  panel code, not just a dashboard session. `app.check` warns if it's off.
- Before exposing anything beyond the LAN: Phase 6 in the runbook (HTTPS, `COOKIE_SECURE`).

## Development

```bash
cd backend && .venv/bin/python -m pytest      # 48 tests: HA protocol, Total Connect model, ingest, API, auth
```

```
backend/app/
  main.py          app factory, security headers        api.py        REST + WebSocket routes
  services.py      wiring + background tasks            auth.py       password/session/bearer auth
  totalconnect.py  HA Total Connect entities -> model   alarm.py      history, events, alerts, commands
  ha_client.py     HA WebSocket client                  detections.py Frigate MQTT -> events, archive
  media.py         Frigate/go2rtc media proxy           alerts.py     alert rules + ntfy
  db.py            schema + queries                     demo.py       simulators for DEMO_MODE
  check.py         `python -m app.check` integration verifier
frontend/          index.html, css/app.css, js/ (app shell, store, views/)
config/            Mosquitto, Frigate (template), Home Assistant
scripts/           setup.sh, probe_nvr.py
```

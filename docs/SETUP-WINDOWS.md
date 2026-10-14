# Setup day: Windows + Docker Desktop

The single path from a bare Windows PC to a working dashboard. Follow it top to bottom — each
step ends with a **Check** that must pass before you go on.

[RUNBOOK.md](RUNBOOK.md) is the reference: every phase, every setting, all three platforms.
This page is the mechanical version for one machine, and links back where detail lives.

Two steps have lead time and don't need the PC. Do them first, today:

- **A.** In the Total Connect app: turn on **Notifications → Sensor Activities**.
- **B.** In the Total Connect app: create a **standard (non-admin) user** with a user code.

Both are in [Step 4](#step-4-alarm-total-connect--home-assistant). Without **A** no zone ever
reaches the dashboard, and the setting sometimes takes a while to apply across the account.

---

## What you'll need

| | |
|---|---|
| Time | ~90 minutes, plus waiting on Total Connect |
| Windows | 10 21H2+ or 11, 64-bit, admin rights |
| Free disk | 20 GB for the stack, plus recordings (see [Step 7](#step-7-keep-it-running)) |
| RAM | 8 GB free for Docker is comfortable for 8 channels; 4 GB is the floor |
| On hand | Total Connect master login, the NVR's admin password, your router's admin page |

---

## Step 1: Docker Desktop

1. Install [Docker Desktop for Windows](https://docs.docker.com/desktop/setup/install/windows-install/).
   Keep the **WSL 2** backend (the default). Reboot when it asks.
2. Launch it and wait for the whale icon to stop animating.
3. Settings → **General** → tick **Start Docker Desktop when you sign in**.

**Cap what it may take.** With the WSL 2 backend, the memory and CPU sliders don't apply —
WSL reads `%UserProfile%\.wslconfig` instead. Create it (Notepad is fine) so Docker can't grow
into all your RAM:

```ini
[wsl2]
memory=8GB
processors=4
```

Then in PowerShell: `wsl --shutdown` (Docker Desktop restarts on its own).

**Check:** `docker run --rm hello-world` prints "Hello from Docker!".

```powershell
docker run --rm hello-world
```

## Step 2: Get the project and create `.env`

```powershell
cd $HOME
git clone https://github.com/Noxy-HNR/home-security.git
cd home-security
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
```

`setup.ps1` copies `.env.example` to `.env` and generates `MQTT_PASSWORD` and
`DASHBOARD_PASSWORD`. It never overwrites a value you've already set, so it's safe to re-run.

Open `.env` in an editor and set:

| Key | Value |
|---|---|
| `TZ` | your zone, e.g. `America/Chicago` — wrong TZ means wrong timestamps on every clip |
| `NVR_HOST` | the NVR's IP (Step 3) |
| `NVR_RTSP_USER` | usually `admin` |
| `NVR_RTSP_PASSWORD` | URL-encode specials: `@`→`%40`, `#`→`%23`, `:`→`%3A`, `/`→`%2F` |

Write down `DASHBOARD_PASSWORD` — it's how you sign in later.

**Check:** `.env` exists and `MQTT_PASSWORD` / `DASHBOARD_PASSWORD` are not empty.

### Want to see the UI before any hardware works?

```powershell
docker compose run --rm --no-deps -p 8080:8080 -e DEMO_MODE=true dashboard
```

The first run builds the image, so give it a few minutes. Then open <http://localhost:8080> —
four simulated cameras, a panel you can arm with code `1234`, and a "simulate a break-in"
button. Sign in with `DASHBOARD_PASSWORD` from `.env`. Ctrl-C to stop.

`--no-deps` keeps Home Assistant and Frigate out of it: demo mode simulates both, through the
same code paths the real integrations use. Nothing real is touched, and it's the fastest way to
confirm Docker itself is healthy before any hardware is involved.

## Step 3: Find the NVR

The FN9108HE **has no web interface** — it refuses browsers on purpose. So you can't read a
stream URL off a settings page; ONVIF is the way in.

1. Install Foscam's [Equipment Search Tool](https://www.foscam.com/downloads/app_software.html)
   and run it. It lists the NVR with its IP.
2. In your router, give that MAC a **DHCP reservation** so the IP never moves. A changed NVR IP
   breaks every camera at once.
3. Put the IP in `NVR_HOST`.

> Run the search tool **on Windows, not in a container.** It finds devices by broadcasting on
> the local network, and that broadcast doesn't cross WSL 2's NAT. Containers reach the NVR
> fine once they know its IP — they just can't discover it.

While you're there: does the tool also list the **cameras** with their own LAN addresses? If so
Frigate can read them directly, which is one less hop. Cameras on the NVR's PoE ports usually
sit on the NVR's private subnet and aren't reachable — then NVR streams are the way, and that's
normal.

**Check:** `ping <NVR_HOST>` answers.

## Step 4: Alarm (Total Connect → Home Assistant)

Prerequisites **A** and **B** from the top of this page must be done first.

```powershell
docker compose up -d homeassistant
```

Give it 2–3 minutes on first start, then open <http://localhost:8123> and create the owner
account (this is local to your PC, unrelated to Total Connect).

1. Settings → Devices & services → **Add integration** → *Total Connect*.
2. Sign in as the **standard user** from prerequisite **B**, and enter its user code.
3. On the Total Connect card → **Configure**: turn **Require code** *on*. The dashboard then
   shows a keypad and the panel code is checked on every arm/disarm.
4. Your profile (bottom left) → Security → **Long-lived access tokens** → create one.
   Paste it into `.env` as `HA_TOKEN`.

**Verify arming in Home Assistant before the dashboard exists.** Settings → Devices & services →
Total Connect → the panel device → **Arm home**, then **Disarm**. Watch the physical keypad or
the TC2 app confirm each one. Allow ~30 s; Home Assistant polls Total Connect and the cloud is
not instant.

**Check:** zones are listed on the device page and change when you open a door (within ~30 s),
and arm/disarm works from Home Assistant.

## Step 5: Find the camera streams

Foscam doesn't publish this NVR's RTSP address format, so don't guess it — ask the NVR:

```powershell
docker compose run --rm --no-deps dashboard python -m app.probe
```

It reads `NVR_HOST` / `NVR_RTSP_USER` / `NVR_RTSP_PASSWORD` from `.env`, asks over ONVIF
(ports 80, 88, 8000, 8080, 8899), falls back to trying common RTSP layouts (554, 88), and
**stops at the first rejected password** so a typo can't lock the NVR account.

It ends with lines to paste into `.env`:

```
NVR_MAIN_URL=rtsp://{user}:{password}@{host}:554/...{ch}...
NVR_SUB_URL=rtsp://{user}:{password}@{host}:554/...{ch}...
CAMERAS=camera_1:1,camera_2:2,camera_3:3
DETECT_SIZE=640x360
```

Rename the cameras to real names — `CAMERAS=front_door:1,driveway:2,backyard:3`. These become
Frigate camera names, so: letters, digits, `_` and `-` only.

**If the probe finds nothing,** try a camera directly:

```powershell
docker compose run --rm --no-deps dashboard python -m app.probe --host <camera-ip>
```

Foscam cameras answer on `/videoMain` and `/videoSub`. List them as `name@ip`
(`CAMERAS=porch@192.168.1.21`); direct cameras use Foscam's defaults, so no URL settings are
needed. Cameras the NVR adopted usually inherit the NVR's username and password.

**Check:** the probe prints a main *and* a sub stream for every channel you expect.

## Step 6: Start everything

```powershell
docker compose up -d --build
```

First build takes a few minutes. The `frigate-config` step writes `frigate\config.yml` from
`.env` before Frigate starts; if something's missing it stops with the reason (e.g.
`CAMERAS is empty`) — fix `.env` and run the same command again.

Windows will pop a **Windows Defender Firewall** prompt. Allow it on **Private** networks, or
only this PC will be able to open the dashboard.

Then verify the plumbing from inside the container:

```powershell
docker compose run --rm dashboard python -m app.check
```

`app.check` tests the HA token, the Total Connect panel and zones (warning if *Require code* is
off), Frigate's API and cameras, every camera's live stream, and that Frigate is publishing over
MQTT. Fix anything marked `FAIL` before moving on.

Now open the dashboard: <http://localhost:8080>, user password = `DASHBOARD_PASSWORD`.

From your phone or another PC, use this machine's LAN address:

```powershell
(Get-NetIPAddress -AddressFamily IPv4 -PrefixOrigin Dhcp,Manual).IPAddress
```

→ `http://<that-ip>:8080`. Put the same base URL in `PUBLIC_URL` in `.env` so notification taps
open the right event.

**Check:** every camera shows live video, and the Alarm page shows your real zones.

### Draw masks and zones — don't skip this

This is the highest-leverage tuning there is. Frigate's own UI is at
`https://localhost:8971` (self-signed certificate, accept the warning). It prints a one-time
admin password on first start:

```powershell
docker compose logs frigate
```

Sign in, change the password, then per camera: **Settings → Masks / Zones**. Mask the street,
sky, moving trees and the timestamp overlay; draw zones for what you actually care about
(porch, driveway). Put the alert-worthy zone names in `ALERT_ZONES` in `.env`.

Untuned cameras are the #1 cause of "it notifies me about nothing".

**Check:** walking past a camera shows a tracked person in Frigate, and no boxes appear on
empty footage.

## Step 7: Keep it running

Three things bite a PC-hosted NVR. Handle them now, not after you lose a night of recording.

### Sleep stops everything

Docker Desktop pauses when Windows sleeps. No recording, no alerts, no camera history. In an
**admin** PowerShell:

```powershell
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /change monitor-timeout-ac 10   # the screen may still sleep; the PC must not
```

### After a reboot, someone has to sign in

Docker Desktop starts at *sign-in*, not at boot. A PC that reboots overnight (Windows Update)
sits at the lock screen with nothing running until someone logs in. Either keep reboots manual,
or set Windows to sign in automatically — and understand that means the machine boots unlocked.

Containers themselves are `restart: unless-stopped`, so once Docker is up they come back on
their own.

### Recordings grow inside a virtual disk

`FRIGATE_MEDIA` defaults to a Docker volume, which lives in Docker Desktop's WSL disk image.
That image **grows as recordings accumulate and does not shrink when files are deleted**.

- Watch the storage meter on the dashboard's System page.
- Docker Desktop → Settings → Resources → **Disk image location** shows the current size and
  lets you cap it or move it to a bigger drive.
- Bound the recordings themselves with Frigate's `record` / `snapshots` retention in
  `frigate\config.yml` — that's the real fix; the rest is just where the bytes sit.

**Check:** reboot the PC, sign in, wait two minutes, and the dashboard answers on
`http://localhost:8080` with live cameras.

## Step 8: Notifications and backups

**Phone push** — install the [ntfy](https://ntfy.sh) app, subscribe to a long random topic
(treat it as a password; anyone who knows it sees your alerts), then set `NTFY_TOPIC` and
`PUBLIC_URL` in `.env` and run `docker compose up -d`.

Desktop browser notifications need HTTPS, so they come with the reverse-proxy work in
[RUNBOOK.md](RUNBOOK.md) Phase 6. Until then an open dashboard shows toasts and plays a tone.

**Back up** `.env` and the `frigate\` folder (config, zones, masks), plus the two volumes that
hold state you can't regenerate:

```powershell
docker run --rm -v home-security_dashboard-data:/v -v "${PWD}:/b" alpine tar czf /b/dashboard-data.tgz -C /v .
docker run --rm -v home-security_homeassistant-config:/v -v "${PWD}:/b" alpine tar czf /b/homeassistant.tgz -C /v .
```

Recordings (`frigate-media`) are usually not worth backing up.

**Lock it down** once Total Connect is set up: delete the `8123` port mapping from
`docker-compose.yml` so Home Assistant is no longer reachable from the LAN. For access from
outside the house, use a VPN (Tailscale or WireGuard) rather than forwarding a port — see
Phase 6 in [RUNBOOK.md](RUNBOOK.md).

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `docker compose` says `Set MQTT_PASSWORD in .env` | run `scripts\setup.ps1`, or fill the value in |
| `frigate-config` exits `CAMERAS is empty` / `NVR_MAIN_URL is empty` | Step 5 isn't done |
| Probe: `rejected the username/password` | wrong `NVR_RTSP_USER`/`NVR_RTSP_PASSWORD`. It stopped after one attempt deliberately — don't retry blindly, you'll lock the account |
| Probe reaches nothing at all | wrong `NVR_HOST`, or the password needs URL-encoding. `ping` it first |
| Search tool finds no devices | run it on Windows, not in a container (WSL 2 blocks the discovery broadcast) |
| System page: "Access token rejected" | `HA_TOKEN` is wrong or was revoked — make a new one |
| No zones on the Alarm page | Sensor Activities not enabled in TC2; enable it, then reload the integration |
| "Invalid user code" | wrong code, or the code isn't valid for the TC2 user HA signs in as |
| Arming refused, naming zones | those zones are open — close or bypass them |
| Tile says "Camera unavailable" | Frigate can't read that stream: `docker compose logs frigate` |
| Live video falls back to stills | go2rtc stream missing — `docker compose run --rm dashboard python -m app.check` |
| MQTT "not authorised" | after changing `MQTT_USER`/`MQTT_PASSWORD`, run `docker compose up -d` so *every* service restarts with it |
| Frigate exits with "Bus error" | raise `FRIGATE_SHM_SIZE` in `.env` |
| Skipped frames / detector can't keep up | lower `DETECT_FPS` (5 → 3), or the sub-stream resolution. See below |
| `curl` behaves strangely in PowerShell | `curl` is an alias for `Invoke-WebRequest`; use `curl.exe` |
| Nothing recorded overnight | the PC slept, or rebooted and nobody signed in — Step 7 |
| Dashboard works locally, not from your phone | firewall: allow Docker on **Private** networks; check `DASHBOARD_BIND=0.0.0.0` |

### If detection can't keep up

Detection here runs on the **CPU** — Docker Desktop cannot pass a GPU or a Coral USB stick
through to a Linux container, so the iGPU and the Coral option in `docker-compose.yml` are
Linux-host-only. Aim for inference under ~30 ms and zero skipped fps on the System page. If
you're over: lower `DETECT_FPS`, shrink `DETECT_SIZE`, or mask more aggressively.

If this PC has an NVIDIA GPU, Frigate's TensorRT detector is worth investigating later — it
needs the `-tensorrt` image tag rather than the one pinned here, so it's a deliberate change,
not a toggle. Not set up in this repo.

### Adding a camera later

Add it to `CAMERAS` in `.env` and run `docker compose up -d`. New cameras are appended to
`frigate\config.yml`; existing ones keep their zones and masks untouched.

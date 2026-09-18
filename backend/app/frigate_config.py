"""Write Frigate's config.yml from .env, then keep adding cameras listed in CAMERAS.

Runs as the one-shot `frigate-config` service before Frigate on every `docker compose up`:

- no config.yml yet: writes a complete one (MQTT, detector, retention, cameras)
- config.yml exists: adds any camera in CAMERAS that it doesn't have, and touches nothing
  else, so zones, masks and tuning done in Frigate's UI survive

CAMERAS is a comma-separated list of
    name:channel[:WxH]   a channel on the NVR, via NVR_MAIN_URL / NVR_SUB_URL
    name@ip[:WxH]        a camera addressed directly, via CAMERA_MAIN_URL / CAMERA_SUB_URL
URL templates use {user} {password} {host} {ch} {ch2} (01) {chz} (00); `python -m app.probe`
prints the right ones. Credentials never land in config.yml: they become Frigate's
{FRIGATE_RTSP_USER}/{FRIGATE_RTSP_PASSWORD} placeholders, filled from .env at runtime.
"""

from __future__ import annotations

import argparse
import os
import platform
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CAMERA_MAIN = "rtsp://{user}:{password}@{host}:88/videoMain"  # Foscam cameras
DEFAULT_CAMERA_SUB = "rtsp://{user}:{password}@{host}:88/videoSub"
NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
SIZE_RE = re.compile(r"^(\d{2,5})x(\d{2,5})$")
HEADER = """\
# Frigate config for the home security dashboard.
# Written by the frigate-config service from .env; cameras listed in CAMERAS are added
# automatically. Edit freely (or use Frigate's UI): existing cameras are never overwritten.
"""


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Camera:
    name: str
    channel: int | None  # NVR channel, or None for a directly addressed camera
    host: str | None  # camera IP when addressed directly
    width: int
    height: int


def parse_cameras(spec: str, default_size: tuple[int, int]) -> list[Camera]:
    cameras: list[Camera] = []
    for raw in (item.strip() for item in spec.split(",")):
        if not raw:
            continue
        size = default_size
        head, _, tail = raw.rpartition(":")
        if head and SIZE_RE.match(tail):
            raw, size = head, tuple(int(v) for v in SIZE_RE.match(tail).groups())  # type: ignore[assignment]
        if "@" in raw:
            name, _, host = raw.partition("@")
            channel = None
            if not re.match(r"^[A-Za-z0-9.-]+$", host):
                raise ConfigError(f"'{raw}': expected name@ip, e.g. porch@192.168.1.21")
        else:
            name, _, ch = raw.partition(":")
            host = None
            if not ch.isdigit() or not 1 <= int(ch) <= 64:
                raise ConfigError(f"'{raw}': expected name:channel (1-64), e.g. front_door:1")
            channel = int(ch)
        if not NAME_RE.match(name):
            raise ConfigError(f"'{name}': camera names may only use letters, digits, _ and -")
        if any(c.name == name for c in cameras):
            raise ConfigError(f"camera '{name}' is listed twice")
        cameras.append(Camera(name, channel, host, size[0], size[1]))
    return cameras


def stream_url(template: str, camera: Camera) -> str:
    """Fill a URL template for one camera, leaving credentials as Frigate env placeholders."""
    ch = camera.channel or 1
    url = (
        template.replace("{user}", "{FRIGATE_RTSP_USER}")
        .replace("{password}", "{FRIGATE_RTSP_PASSWORD}")
        .replace("{host}", camera.host or "{FRIGATE_NVR_HOST}")
        .replace("{ch2}", f"{ch:02d}")
        .replace("{chz}", f"{ch - 1:02d}")
        .replace("{ch}", str(ch))
    )
    leftover = [p for p in re.findall(r"\{([^}]*)\}", url) if not p.startswith("FRIGATE_")]
    if leftover:
        raise ConfigError(f"unknown placeholder {{{leftover[0]}}} in {template}")
    return url


def pick_detector(choice: str) -> str:
    if choice in ("openvino", "cpu"):
        return choice
    # OpenVINO runs on x86 CPUs (Intel and AMD); Apple Silicon / ARM hosts use the CPU detector.
    return "openvino" if platform.machine().lower() in ("x86_64", "amd64") else "cpu"


def base_config(detector: str, detect_fps: int) -> dict[str, Any]:
    config: dict[str, Any] = {
        "mqtt": {
            "enabled": True,
            "host": "mosquitto",
            "user": "{FRIGATE_MQTT_USER}",
            "password": "{FRIGATE_MQTT_PASSWORD}",
            "stats_interval": 15,
        },
        # Frigate's own database lives on a Docker volume, not the bind-mounted config folder:
        # SQLite on Docker Desktop's Windows/macOS file sharing is slow and can lock.
        "database": {"path": "/db/frigate.db"},
    }
    if detector == "openvino":
        config["detectors"] = {"ov": {"type": "openvino", "device": "CPU"}}
        config["model"] = {
            "width": 300,
            "height": 300,
            "input_tensor": "nhwc",
            "input_pixel_format": "bgr",
            "path": "/openvino-model/ssdlite_mobilenet_v2.xml",
            "labelmap_path": "/openvino-model/coco_91cl_bkgr.txt",
        }
    else:
        config["detectors"] = {"cpu": {"type": "cpu", "num_threads": 3}}
    config.update({
        "detect": {"enabled": True, "fps": detect_fps},
        "objects": {"track": ["person", "car", "dog", "cat"],
                    "filters": {"person": {"min_score": 0.5, "threshold": 0.7}}},
        "record": {
            "enabled": True,
            "continuous": {"days": 0},
            "motion": {"days": 3},
            "alerts": {"retain": {"days": 30, "mode": "motion"}},
            "detections": {"retain": {"days": 14, "mode": "motion"}},
        },
        "snapshots": {"enabled": True, "bounding_box": True, "retain": {"default": 30}},
        "review": {"alerts": {"labels": ["person"]}, "detections": {"labels": ["person", "car", "dog", "cat"]}},
        "go2rtc": {"streams": {}},
        "cameras": {},
    })
    return config


def add_cameras(config: dict[str, Any], cameras: list[Camera], env: dict[str, str]) -> list[str]:
    """Add cameras missing from config; returns the names added."""
    streams = config.setdefault("go2rtc", {}).setdefault("streams", {})
    existing = config.setdefault("cameras", {})
    if existing is None:
        existing = config["cameras"] = {}
    if streams is None:
        streams = config["go2rtc"]["streams"] = {}
    added = []
    for camera in cameras:
        if camera.name in existing:
            continue
        if camera.channel is not None:
            main_t, sub_t = env.get("NVR_MAIN_URL", ""), env.get("NVR_SUB_URL", "")
            if not main_t:
                raise ConfigError("NVR_MAIN_URL is empty: run the probe (RUNBOOK Phase 0) and put its lines in .env")
        else:
            main_t = env.get("CAMERA_MAIN_URL") or DEFAULT_CAMERA_MAIN
            sub_t = env.get("CAMERA_SUB_URL") or (DEFAULT_CAMERA_SUB if not env.get("CAMERA_MAIN_URL") else "")
        streams[camera.name] = [stream_url(main_t, camera)]
        inputs = [{"path": f"rtsp://127.0.0.1:8554/{camera.name}", "input_args": "preset-rtsp-restream",
                   "roles": ["record"] if sub_t else ["record", "detect"]}]
        live = {"Main": camera.name}
        if sub_t:
            streams[f"{camera.name}_sub"] = [stream_url(sub_t, camera)]
            inputs.append({"path": f"rtsp://127.0.0.1:8554/{camera.name}_sub",
                           "input_args": "preset-rtsp-restream", "roles": ["detect"]})
            live["Sub"] = f"{camera.name}_sub"
        existing[camera.name] = {
            "ffmpeg": {"output_args": {"record": "preset-record-generic-audio-copy"}, "inputs": inputs},
            "detect": {"width": camera.width, "height": camera.height},
            "live": {"streams": live},
            "ui": {"order": len(existing) + 1},
        }
        added.append(camera.name)
    return added


def render(config: dict[str, Any]) -> str:
    return HEADER + yaml.safe_dump(config, sort_keys=False, default_flow_style=False, width=4096)


def run(path: Path, env: dict[str, str], force: bool = False) -> str:
    size = SIZE_RE.match(env.get("DETECT_SIZE") or "640x360")
    if not size:
        raise ConfigError(f"DETECT_SIZE '{env.get('DETECT_SIZE')}' should look like 640x360")
    cameras = parse_cameras(env.get("CAMERAS", ""), (int(size.group(1)), int(size.group(2))))
    if path.exists() and not force:
        config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(config, dict):
            raise ConfigError(f"{path} isn't a YAML mapping; fix or delete it")
        added = add_cameras(config, cameras, env)
        if not added:
            return f"{path}: up to date ({len(config.get('cameras') or {})} cameras)"
        _write(path, render(config))
        return f"{path}: added {', '.join(added)}"
    if not cameras:
        raise ConfigError(
            "CAMERAS is empty. Run the probe to find your NVR's stream URLs, then set CAMERAS and "
            "NVR_MAIN_URL/NVR_SUB_URL in .env (RUNBOOK Phase 0)."
        )
    detector = pick_detector(env.get("DETECTOR", "auto"))
    config = base_config(detector, int(env.get("DETECT_FPS") or 5))
    add_cameras(config, cameras, env)
    _write(path, render(config))
    return f"{path}: wrote {len(cameras)} cameras ({detector} detector)"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    try:
        path.chmod(0o666)  # written as root inside Docker; keep it editable from the host
    except OSError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate or extend Frigate's config.yml from .env")
    parser.add_argument("--config", default="/config/config.yml")
    parser.add_argument("--force", action="store_true", help="rewrite from scratch (loses UI-made zones/masks)")
    parser.add_argument("--stdout", action="store_true", help="print a fresh config instead of writing it")
    args = parser.parse_args()
    env = dict(os.environ)
    try:
        if args.stdout:
            size = SIZE_RE.match(env.get("DETECT_SIZE") or "640x360") or SIZE_RE.match("640x360")
            config = base_config(pick_detector(env.get("DETECTOR", "auto")), int(env.get("DETECT_FPS") or 5))
            add_cameras(config, parse_cameras(env.get("CAMERAS", ""), (int(size.group(1)), int(size.group(2)))), env)
            print(render(config))
            return 0
        print(run(Path(args.config), env, force=args.force))
        return 0
    except ConfigError as exc:
        print(f"frigate-config: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

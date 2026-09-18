"""Runtime configuration, read from environment variables (and a local .env in development)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_DIR = Path(__file__).resolve().parent.parent


def csv_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Demo mode swaps Home Assistant, Frigate and MQTT for in-process simulators so the
    # whole dashboard can be exercised without any hardware.
    demo_mode: bool = False
    demo_autogen: bool = True  # tests turn this off to get a quiet, deterministic demo
    demo_time_scale: float = 1.0  # tests shrink the simulated cloud latency and arming delays
    log_level: str = "info"

    data_dir: Path = _BACKEND_DIR / "data"
    database_url: str = ""
    static_dir: Path = _BACKEND_DIR.parent / "frontend"

    # Dashboard auth
    dashboard_password: str = ""
    session_secret: str = ""
    session_hours: int = 12
    api_tokens: str = ""  # comma-separated bearer tokens for scripts
    cookie_secure: bool = False  # set true once the dashboard is served over HTTPS

    # Home Assistant (Total Connect integration)
    ha_url: str = "http://homeassistant:8123"
    ha_token: str = ""

    # Frigate (internal unauthenticated port, only reachable on the compose network)
    frigate_url: str = "http://frigate:5000"
    frigate_public_url: str = ""  # Frigate's own UI, linked from the dashboard for zone/mask editing
    go2rtc_url: str = "http://frigate:1984"

    mqtt_host: str = "mosquitto"
    mqtt_port: int = 1883
    mqtt_user: str = ""
    mqtt_password: str = ""
    frigate_topic_prefix: str = "frigate"

    # Live grid (Frigate's MJPEG feed of the detect stream, with boxes drawn)
    live_fps: int = 5
    live_height: int = 360

    # Alerts
    alert_labels: str = "person"
    alert_cameras: str = ""  # empty = every camera
    alert_zones: str = ""  # empty = anywhere in frame; otherwise the object must enter one of these
    alert_only_when_armed: bool = False
    alert_cooldown_seconds: int = 60
    notify_arm_changes: bool = False

    ntfy_url: str = "https://ntfy.sh"
    ntfy_topic: str = ""
    ntfy_token: str = ""
    public_url: str = ""  # used for click-through links in push notifications

    # Retention
    archive_snapshots: bool = True
    snapshot_retention_days: int = 90
    event_retention_days: int = 0  # 0 keeps the event log forever

    @property
    def db_url(self) -> str:
        if self.database_url:
            return self.database_url
        name = "demo.db" if self.demo_mode else "security.db"
        return f"sqlite+aiosqlite:///{(self.data_dir / name).as_posix()}"

    @property
    def snapshot_dir(self) -> Path:
        return self.data_dir / ("demo-snapshots" if self.demo_mode else "snapshots")

    @property
    def auth_open(self) -> bool:
        """Demo mode without a password needs no login: there is nothing real to protect."""
        return self.demo_mode and not self.dashboard_password

    def validate_runtime(self) -> None:
        if not self.dashboard_password and not self.demo_mode:
            raise RuntimeError("DASHBOARD_PASSWORD must be set (or run with DEMO_MODE=true)")
        if not self.demo_mode and not self.ha_token:
            raise RuntimeError("HA_TOKEN must be set to a Home Assistant long-lived access token")


@lru_cache
def get_settings() -> Settings:
    return Settings()

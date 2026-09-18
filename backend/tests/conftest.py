from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.alerts import AlertEngine, Notifier
from app.config import Settings
from app.db import Store
from app.hub import Hub
from app.main import create_app

PASSWORD = "correct horse"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        demo_mode=True,
        demo_autogen=False,
        demo_time_scale=0.01,
        data_dir=tmp_path,
        static_dir=tmp_path / "no-frontend",
        dashboard_password=PASSWORD,
        session_secret="x" * 64,
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def authed(client: TestClient) -> TestClient:
    response = client.post("/api/auth/login", json={"password": PASSWORD})
    assert response.status_code == 200
    return client


@pytest.fixture
async def store(tmp_path: Path) -> Store:
    db = Store(f"sqlite+aiosqlite:///{(tmp_path / 'unit.db').as_posix()}")
    await db.init()
    yield db
    await db.close()


class RecordingHub(Hub):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[dict] = []

    def publish(self, message: dict) -> None:
        self.messages.append(message)
        super().publish(message)

    def of_type(self, kind: str) -> list[dict]:
        return [m for m in self.messages if m["type"] == kind]


@pytest.fixture
def hub() -> RecordingHub:
    return RecordingHub()


def make_alerts(settings: Settings, hub: Hub, armed: bool = False) -> AlertEngine:
    return AlertEngine(settings, hub, Notifier(settings), is_armed=lambda: armed)

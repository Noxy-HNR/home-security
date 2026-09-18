import asyncio

import pytest

from app.db import EventFilter
from app.detections import DetectionService, summarize_stats
from app.live import LiveState

from .conftest import make_alerts


class FakeMedia:
    def __init__(self):
        self.snapshots: list[str] = []

    async def list_cameras(self):
        return [{"name": "driveway", "label": "Driveway", "zones": ["driveway", "street"]}]

    async def event_snapshot_bytes(self, frigate_id, height=None):
        self.snapshots.append(frigate_id)
        return b"\xff\xd8jpeg", "image/jpeg"


def frigate_obj(**overrides):
    obj = {
        "id": "1726650000.123456-abc123", "camera": "driveway", "label": "person", "sub_label": None,
        "top_score": 0.82, "score": 0.8, "false_positive": False, "start_time": 1726650000.1,
        "end_time": None, "entered_zones": [], "current_zones": [], "has_snapshot": False,
        "has_clip": False, "stationary": False,
    }
    obj.update(overrides)
    return obj


@pytest.fixture
async def detections(settings, store, hub):
    settings.alert_zones = "driveway"
    live = LiveState()
    svc = DetectionService(settings, store, hub, live, make_alerts(settings, hub), FakeMedia())
    await svc.refresh_cameras()
    return svc


async def rows(store):
    result, _ = await store.query_events(EventFilter(limit=200))
    return result


async def test_lifecycle_is_one_row(detections, store, hub):
    new = frigate_obj()
    await detections.handle_event({"type": "new", "before": new, "after": new})
    assert list(detections.live.active_objects) == [new["id"]]

    upd = frigate_obj(entered_zones=["street"], has_snapshot=True, top_score=0.9)
    await detections.handle_event({"type": "update", "before": new, "after": upd})
    end = frigate_obj(entered_zones=["street"], has_snapshot=True, has_clip=True, top_score=0.9, end_time=1726650030.0)
    await detections.handle_event({"type": "end", "before": upd, "after": end})

    (row,) = await rows(store)
    assert (row.subject, row.subject_id, row.label, row.confidence) == ("Driveway", "driveway", "person", 0.9)
    assert row.zones == ["street"] and row.has_clip and row.end_time is not None
    assert row.severity == "info"  # never entered the alert zone
    assert detections.live.active_objects == {}
    assert len(hub.of_type("event")) == 3
    assert hub.of_type("alert") == []


async def test_alert_once_when_object_enters_alert_zone(detections, hub):
    a = frigate_obj()
    await detections.handle_event({"type": "new", "before": a, "after": a})
    b = frigate_obj(entered_zones=["driveway"], has_snapshot=True)
    await detections.handle_event({"type": "update", "before": a, "after": b})
    c = frigate_obj(entered_zones=["driveway"], has_snapshot=True, top_score=0.95)
    await detections.handle_event({"type": "update", "before": b, "after": c})
    (alert,) = hub.of_type("alert")
    assert alert["alert"]["title"] == "Person at Driveway"
    assert "driveway" in alert["alert"]["body"]


async def test_non_alert_labels_and_false_positives(detections, store, hub):
    car = frigate_obj(id="1.0-car", label="car", entered_zones=["driveway"])
    await detections.handle_event({"type": "new", "before": car, "after": car})
    ghost = frigate_obj(id="2.0-ghost", false_positive=True)
    await detections.handle_event({"type": "new", "before": ghost, "after": ghost})
    assert [r.label for r in await rows(store)] == ["car"]
    assert hub.of_type("alert") == []


async def test_snapshot_is_archived_when_object_ends(detections, store, settings):
    end = frigate_obj(has_snapshot=True, end_time=1726650030.0)
    await detections.handle_event({"type": "end", "before": end, "after": end})
    for _ in range(50):
        await asyncio.sleep(0.01)
        (row,) = await rows(store)
        if row.snapshot_path:
            break
    assert row.snapshot_path == "2024/09/18/1726650000.123456-abc123.jpg"
    assert (settings.snapshot_dir / row.snapshot_path).read_bytes() == b"\xff\xd8jpeg"


async def test_dispatch_topics(detections, hub):
    await detections.dispatch("available", b"offline")
    assert detections.live.sources["frigate"].connected is False
    await detections.dispatch("stats", b'{"detectors": {"ov": {"inference_speed": 12.5}}, "service": {"version": "0.18.0"}}')
    assert detections.live.frigate_stats["detectors"] == {"ov": {"inference_ms": 12.5}}


def test_summarize_stats_keeps_media_storage_only():
    summary = summarize_stats({
        "service": {"storage": {"/media/frigate/recordings": {"total": 10, "used": 4, "free": 6, "mount_type": "ext4"},
                                "/dev/shm": {"total": 1, "used": 0, "free": 1}}},
        "cpu_usages": {"frigate.full_system": {"cpu": "12.0"}},
    })
    assert summary["storage"] == {"/media/frigate/recordings": {"total": 10, "used": 4, "free": 6}}
    assert summary["cpu_percent"] == "12.0"

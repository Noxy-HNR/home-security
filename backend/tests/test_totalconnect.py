from app.totalconnect import Role, TotalConnectModel, classify

LOC = "123456"


def registry() -> list[dict]:
    rows = [
        ("alarm_control_panel.home", LOC),
        ("binary_sensor.front_door", f"{LOC}_1_zone"),
        ("binary_sensor.front_door_battery", f"{LOC}_1_low_battery"),
        ("binary_sensor.front_door_tamper", f"{LOC}_1_tamper"),
        ("button.front_door_bypass", f"{LOC}_1_bypass"),
        ("binary_sensor.smoke", f"{LOC}_9_zone"),
        ("binary_sensor.home_power", f"{LOC}_power"),
        ("button.home_bypass_all", f"{LOC}_bypass_all"),
    ]
    entries = [{"entity_id": e, "unique_id": u, "platform": "totalconnect", "disabled_by": None} for e, u in rows]
    entries.append({"entity_id": "sun.sun", "unique_id": "sun", "platform": "sun"})
    return entries


def state(entity_id: str, value: str, **attrs) -> dict:
    return {"entity_id": entity_id, "state": value, "attributes": attrs, "last_changed": "2026-09-18T10:00:00Z"}


def loaded_model() -> TotalConnectModel:
    model = TotalConnectModel()
    model.load_registry(registry())
    for s in (
        state("alarm_control_panel.home", "disarmed", friendly_name="Home", code_format="number", supported_features=7),
        state("binary_sensor.front_door", "off", friendly_name="Front Door", device_class="door", zone_id="1", partition="1"),
        state("binary_sensor.front_door_battery", "on", device_class="battery"),
        state("binary_sensor.front_door_tamper", "off", device_class="tamper"),
        state("binary_sensor.smoke", "off", friendly_name="Upstairs Smoke", device_class="smoke", zone_id="9"),
        state("binary_sensor.home_power", "off"),
    ):
        model.apply(s["entity_id"], s)
    return model


def test_classify_unique_ids():
    assert classify("alarm_control_panel.home", LOC) == Role("panel", LOC, partition="1")
    assert classify("alarm_control_panel.home_2", f"{LOC}_2") == Role("panel", LOC, partition="2")
    assert classify("binary_sensor.x", f"{LOC}_5_zone") == Role("zone", LOC, zone="5")
    assert classify("binary_sensor.x", f"{LOC}_5_low_battery") == Role("zone_low_battery", LOC, zone="5")
    assert classify("button.x", f"{LOC}_5_bypass") == Role("zone_bypass", LOC, zone="5")
    assert classify("binary_sensor.x", f"{LOC}_low_battery") == Role("location_low_battery", LOC)
    assert classify("button.x", f"{LOC}_bypass_all") == Role("location_bypass_all", LOC)
    assert classify("button.x", f"{LOC}_clear_bypass") == Role("location_clear_bypass", LOC)
    # domain must agree with the kind of entity
    assert classify("binary_sensor.x", f"{LOC}_5_bypass") is None
    assert classify("button.x", f"{LOC}_5_zone") is None
    assert classify("sensor.x", f"{LOC}_5_zone") is None


def test_snapshot_shape():
    snap = loaded_model().snapshot()
    (panel,) = snap["panels"]
    assert panel["state"] == "disarmed"
    assert panel["code_required"] is True
    assert panel["modes"] == ["stay", "away", "night"]
    assert panel["instant_modes"] == ["stay", "away"]
    assert panel["ac_loss"] is False
    assert panel["can_bypass_all"] is True and panel["can_clear_bypass"] is False

    door, smoke = snap["zones"]  # sorted by zone number
    assert door == {
        "id": f"{LOC}-1", "name": "Front Door", "number": "1", "partition": "1", "device_class": "door",
        "available": True, "faulted": False, "low_battery": True, "tampered": False, "status": "Closed",
        "can_bypass": True, "changed_at": "2026-09-18T10:00:00Z",
    }
    assert smoke["status"] == "Clear" and smoke["can_bypass"] is False and smoke["low_battery"] is None


def test_changes_are_reported_once_and_first_sighting_has_no_old_value():
    model = TotalConnectModel()
    model.load_registry(registry())
    (first,) = model.apply("binary_sensor.front_door", state("binary_sensor.front_door", "off", device_class="door"))
    assert (first.old, first.new) == (None, False)

    (opened,) = model.apply("binary_sensor.front_door", state("binary_sensor.front_door", "on", device_class="door"))
    assert (opened.target, opened.field, opened.old, opened.new) == (f"{LOC}-1", "faulted", False, True)
    assert model.apply("binary_sensor.front_door", state("binary_sensor.front_door", "on", device_class="door")) == []


def test_unavailable_keeps_last_known_and_does_not_flap():
    model = loaded_model()
    assert model.apply("binary_sensor.front_door", state("binary_sensor.front_door", "unavailable")) == []
    zone = model.snapshot()["zones"][0]
    assert zone["available"] is False and zone["status"] == "Unavailable"
    # Coming back with the same value is not a change
    assert model.apply("binary_sensor.front_door", state("binary_sensor.front_door", "off", device_class="door")) == []
    assert model.apply("alarm_control_panel.home", state("alarm_control_panel.home", "unavailable")) == []
    assert model.snapshot()["panels"][0]["state"] == "unavailable"


def test_seed_suppresses_phantom_changes_after_restart():
    model = TotalConnectModel()
    model.seed({"alarm_control_panel.home": "armed_away"}, {f"{LOC}-1": {"faulted": False, "low_battery": True}})
    model.load_registry(registry())
    assert model.apply("alarm_control_panel.home", state("alarm_control_panel.home", "armed_away")) == []
    (change,) = model.apply("binary_sensor.front_door_battery", state("binary_sensor.front_door_battery", "off"))
    assert (change.old, change.new) == (True, False)


def test_location_power_sensor_means_ac_loss():
    model = loaded_model()
    (change,) = model.apply("binary_sensor.home_power", state("binary_sensor.home_power", "on"))
    assert (change.target, change.field, change.new) == (LOC, "ac_loss", True)
    assert model.snapshot()["panels"][0]["ac_loss"] is True


def test_is_armed_and_faulted_zone_names():
    model = loaded_model()
    assert not model.is_armed()
    model.apply("alarm_control_panel.home", state("alarm_control_panel.home", "armed_night", supported_features=7))
    model.apply("binary_sensor.front_door", state("binary_sensor.front_door", "on", friendly_name="Front Door"))
    assert model.is_armed()
    assert model.faulted_zone_names(LOC) == ["Front Door"]


def test_registry_reload_drops_removed_entities():
    model = loaded_model()
    model.load_registry([r for r in registry() if not r["unique_id"].startswith(f"{LOC}_9")])
    assert [z["name"] for z in model.snapshot()["zones"]] == ["Front Door"]
    assert not model.is_tracked("binary_sensor.smoke")

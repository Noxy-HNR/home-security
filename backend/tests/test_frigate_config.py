import pytest
import yaml

from app.frigate_config import ConfigError, parse_cameras, run, stream_url

NVR_ENV = {
    "CAMERAS": "front_door:1,driveway:2:704x480",
    "NVR_MAIN_URL": "rtsp://{user}:{password}@{host}:554/chID={ch}&streamType=main&linkType=tcp",
    "NVR_SUB_URL": "rtsp://{user}:{password}@{host}:554/chID={ch}&streamType=sub&linkType=tcp",
    "DETECTOR": "openvino",
}


def load(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_parse_cameras():
    cams = parse_cameras("front_door:1, porch@192.168.1.21:1280x720 ,garage:12", (640, 360))
    assert [(c.name, c.channel, c.host, c.width, c.height) for c in cams] == [
        ("front_door", 1, None, 640, 360),
        ("porch", None, "192.168.1.21", 1280, 720),
        ("garage", 12, None, 640, 360),
    ]
    assert parse_cameras("", (640, 360)) == []
    for bad in ("front door:1", "cam:0", "cam", "cam:1,cam:2", "cam@bad host"):
        with pytest.raises(ConfigError):
            parse_cameras(bad, (640, 360))


def test_stream_url_keeps_credentials_out_of_config():
    cam = parse_cameras("a:3", (640, 360))[0]
    assert stream_url("rtsp://{user}:{password}@{host}:554/ch{ch2}/{chz}/{ch}", cam) == (
        "rtsp://{FRIGATE_RTSP_USER}:{FRIGATE_RTSP_PASSWORD}@{FRIGATE_NVR_HOST}:554/ch03/02/3")
    direct = parse_cameras("porch@192.168.1.21", (640, 360))[0]
    assert stream_url("rtsp://{user}:{password}@{host}:88/videoMain", direct) == (
        "rtsp://{FRIGATE_RTSP_USER}:{FRIGATE_RTSP_PASSWORD}@192.168.1.21:88/videoMain")
    with pytest.raises(ConfigError, match="placeholder"):
        stream_url("rtsp://{nvr}/x", cam)


def test_fresh_config(tmp_path):
    path = tmp_path / "config.yml"
    assert "wrote 2 cameras (openvino detector)" in run(path, NVR_ENV)
    config = load(path)
    assert config["database"] == {"path": "/db/frigate.db"}
    assert config["detectors"] == {"ov": {"type": "openvino", "device": "CPU"}}
    assert config["mqtt"]["password"] == "{FRIGATE_MQTT_PASSWORD}"
    streams = config["go2rtc"]["streams"]
    assert streams["front_door"] == ["rtsp://{FRIGATE_RTSP_USER}:{FRIGATE_RTSP_PASSWORD}@{FRIGATE_NVR_HOST}:554/chID=1&streamType=main&linkType=tcp"]
    assert streams["driveway_sub"][0].endswith("chID=2&streamType=sub&linkType=tcp")
    for name, cam in config["cameras"].items():
        for inp in cam["ffmpeg"]["inputs"]:
            assert inp["path"].rsplit("/", 1)[1] in streams
        assert set(cam["live"]["streams"].values()) <= set(streams)
    assert config["cameras"]["driveway"]["detect"] == {"width": 704, "height": 480}
    assert [c["ffmpeg"]["inputs"][1]["roles"] for c in config["cameras"].values()] == [["detect"], ["detect"]]
    assert "admin" not in path.read_text() and "{user}" not in path.read_text()


def test_existing_cameras_are_never_touched(tmp_path):
    path = tmp_path / "config.yml"
    run(path, NVR_ENV)
    config = load(path)
    config["cameras"]["front_door"]["zones"] = {"porch": {"coordinates": "0,0,1,0,1,1"}}
    config["cameras"]["front_door"]["detect"]["fps"] = 3
    path.write_text(yaml.safe_dump(config), encoding="utf-8")

    env = {**NVR_ENV, "CAMERAS": NVR_ENV["CAMERAS"] + ",porch@192.168.1.21"}
    assert run(path, env).endswith("added porch")
    config = load(path)
    assert config["cameras"]["front_door"]["zones"] == {"porch": {"coordinates": "0,0,1,0,1,1"}}
    assert config["cameras"]["front_door"]["detect"]["fps"] == 3
    # a directly addressed camera uses Foscam's documented camera paths by default
    assert config["go2rtc"]["streams"]["porch"] == ["rtsp://{FRIGATE_RTSP_USER}:{FRIGATE_RTSP_PASSWORD}@192.168.1.21:88/videoMain"]
    assert "up to date (3 cameras)" in run(path, env)


def test_no_sub_stream_means_detect_on_main(tmp_path):
    path = tmp_path / "config.yml"
    run(path, {"CAMERAS": "a:1", "NVR_MAIN_URL": "rtsp://{host}/live/{ch}", "DETECTOR": "cpu"})
    config = load(path)
    assert config["detectors"] == {"cpu": {"type": "cpu", "num_threads": 3}} and "model" not in config
    assert config["cameras"]["a"]["ffmpeg"]["inputs"] == [
        {"path": "rtsp://127.0.0.1:8554/a", "input_args": "preset-rtsp-restream", "roles": ["record", "detect"]}]


def test_missing_settings_fail_clearly(tmp_path):
    with pytest.raises(ConfigError, match="CAMERAS is empty"):
        run(tmp_path / "c.yml", {})
    with pytest.raises(ConfigError, match="NVR_MAIN_URL"):
        run(tmp_path / "c.yml", {"CAMERAS": "a:1"})
    with pytest.raises(ConfigError, match="DETECT_SIZE"):
        run(tmp_path / "c.yml", {"CAMERAS": "a:1", "DETECT_SIZE": "big"})

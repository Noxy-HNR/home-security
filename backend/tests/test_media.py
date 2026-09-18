import httpx

from app.media import cameras_from_config, proxy


def client_for(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def body_of(response):
    chunks = [chunk async for chunk in response.body_iterator]
    return b"".join(c if isinstance(c, bytes) else c.encode() for c in chunks)


async def test_proxy_streams_upstream_and_forwards_range():
    seen = {}

    def handler(request):
        seen["range"] = request.headers.get("range")
        seen["params"] = dict(request.url.params)
        # an unread stream, like a real upstream (content= would be pre-read by httpx)
        return httpx.Response(206, stream=httpx.ByteStream(b"mp4data"),
                              headers={"content-type": "video/mp4", "content-range": "bytes 0-6/7"})

    async with client_for(handler) as client:
        response = await proxy(client, "http://frigate:5000/api/events/x/clip.mp4", params={"bbox": 1}, headers={"Range": "bytes=0-"})
        assert response.status_code == 206
        assert response.headers["content-type"] == "video/mp4"
        assert response.headers["content-range"] == "bytes 0-6/7"
        assert response.headers["cache-control"] == "no-store"
        assert await body_of(response) == b"mp4data"
    assert seen == {"range": "bytes=0-", "params": {"bbox": "1"}}


async def test_proxy_error_mapping():
    async with client_for(lambda r: httpx.Response(404)) as client:
        assert (await proxy(client, "http://frigate/api/x")).status_code == 404
    async with client_for(lambda r: httpx.Response(500)) as client:
        assert (await proxy(client, "http://frigate/api/x")).status_code == 502

    def boom(request):
        raise httpx.ConnectError("refused")

    async with client_for(boom) as client:
        assert (await proxy(client, "http://frigate/api/x")).status_code == 502


def test_cameras_from_frigate_config():
    cams = cameras_from_config({"cameras": {
        "b_cam": {"ui": {"order": 2}, "zones": {"z2": {}, "z1": {}}, "live": {"streams": {"Main": "b_main", "Sub": "b_sub"}}},
        "a_cam": {"ui": {"order": 1}, "enabled": False, "detect": {"width": 640, "height": 360, "fps": 5}},
    }})
    assert [c["name"] for c in cams] == ["a_cam", "b_cam"]
    assert cams[0]["enabled"] is False and cams[0]["live_stream"] == "a_cam" and cams[0]["label"] == "A Cam"
    assert cams[1]["zones"] == ["z1", "z2"] and cams[1]["live_stream"] == "b_main"

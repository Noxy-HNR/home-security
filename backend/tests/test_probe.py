"""NVR probe: SPS/SDP parsing, RTSP digest auth against a fake server, ONVIF against a fake device."""

import asyncio
import base64
import hashlib
import re

import httpx
import pytest

from app.probe import (
    CredentialsRejected,
    _unescape_nal,
    h264_resolution,
    infer_template,
    onvif_profiles,
    parse_sdp,
    print_onvif,
    probe,
    rtsp_describe,
)

# --- building real SPS NAL units --------------------------------------------------------------


class BitWriter:
    def __init__(self):
        self.bits = []

    def u(self, n, value):
        self.bits += [(value >> (n - 1 - i)) & 1 for i in range(n)]

    def ue(self, value):
        value += 1
        length = value.bit_length()
        self.u(length - 1, 0)
        self.u(length, value)

    def bytes(self):
        bits = self.bits + [1]  # rbsp stop bit
        bits += [0] * (-len(bits) % 8)
        return bytes(int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8))


def make_sps(profile, width, height):
    """H.264 SPS for a 4:2:0 progressive stream, cropping to the exact size like real encoders."""
    w = BitWriter()
    w.u(8, 0x67)  # NAL header: SPS
    w.u(8, profile)
    w.u(8, 0)
    w.u(8, 40)  # level 4.0
    w.ue(0)  # sps id
    if profile == 100:
        w.ue(1)  # chroma 4:2:0
        w.ue(0)
        w.ue(0)
        w.u(1, 0)
        w.u(1, 0)  # no scaling matrix
    w.ue(0)  # log2_max_frame_num - 4
    w.ue(0)  # poc type 0
    w.ue(2)
    w.ue(1)  # ref frames
    w.u(1, 0)
    mbs_w, mbs_h = -(-width // 16), -(-height // 16)
    w.ue(mbs_w - 1)
    w.ue(mbs_h - 1)
    w.u(1, 1)  # frame_mbs_only
    w.u(1, 1)  # direct_8x8
    crop_right, crop_bottom = (mbs_w * 16 - width) // 2, (mbs_h * 16 - height) // 2
    if crop_right or crop_bottom:
        w.u(1, 1)
        for v in (0, crop_right, 0, crop_bottom):
            w.ue(v)
    else:
        w.u(1, 0)
    w.u(1, 0)  # no VUI
    return w.bytes()


@pytest.mark.parametrize("profile,size", [(66, (640, 360)), (100, (1920, 1080)), (100, (2560, 1440)), (77, (704, 480))])
def test_h264_resolution(profile, size):
    assert h264_resolution(make_sps(profile, *size)) == size


def test_emulation_prevention_bytes_are_removed():
    assert _unescape_nal(b"\x00\x00\x03\x01\x00\x00\x03\x00") == b"\x00\x00\x01\x00\x00\x00"


def sdp_for(width, height, codec="H264"):
    sps = base64.b64encode(make_sps(100, width, height)).decode()
    return (
        "v=0\r\no=- 0 0 IN IP4 0.0.0.0\r\ns=Session\r\nt=0 0\r\n"
        "m=audio 0 RTP/AVP 8\r\na=rtpmap:8 PCMA/8000\r\n"
        f"m=video 0 RTP/AVP 96\r\na=rtpmap:96 {codec}/90000\r\n"
        f"a=fmtp:96 packetization-mode=1;profile-level-id=640028;sprop-parameter-sets={sps},aO48sA==\r\n"
    )


def test_parse_sdp():
    info = parse_sdp(sdp_for(2560, 1440))
    assert (info.codec, info.width, info.height) == ("H264", 2560, 1440)
    assert parse_sdp("v=0\r\nm=audio 0 RTP/AVP 8\r\n") is None
    hevc = parse_sdp("m=video 0 RTP/AVP 98\r\na=rtpmap:98 H265/90000\r\na=x-dimensions:1920,1080\r\n")
    assert (hevc.codec, hevc.width, hevc.height) == ("H265", 1920, 1080)


# --- fake RTSP NVR (TVT-style paths, digest auth) ---------------------------------------------------

USER, PASSWORD, REALM, NONCE = "admin", "s3cret!", "NVR", "abc123"


class FakeNvr:
    def __init__(self, channels=2):
        self.streams = {}
        for ch in range(1, channels + 1):
            self.streams[f"/chID={ch}&streamType=main&linkType=tcp"] = sdp_for(2560, 1440)
            self.streams[f"/chID={ch}&streamType=sub&linkType=tcp"] = sdp_for(640, 360)
        self.auth_attempts = 0

    async def handle(self, reader, writer):
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                lines = head.decode().split("\r\n")
                method, uri, _ = lines[0].split(" ")
                headers = {k.lower(): v.strip() for k, _, v in (l.partition(":") for l in lines[1:] if ":" in l)}
                cseq = headers.get("cseq", "0")
                path = "/" + uri.split("/", 3)[3]
                auth = headers.get("authorization")
                if not auth:
                    reply = (f"RTSP/1.0 401 Unauthorized\r\nCSeq: {cseq}\r\n"
                             f'WWW-Authenticate: Digest realm="{REALM}", nonce="{NONCE}"\r\n\r\n')
                else:
                    self.auth_attempts += 1
                    fields = dict(re.findall(r'(\w+)="([^"]*)"', auth))
                    ha1 = hashlib.md5(f"{USER}:{REALM}:{PASSWORD}".encode()).hexdigest()
                    ha2 = hashlib.md5(f"{method}:{fields['uri']}".encode()).hexdigest()
                    good = fields.get("response") == hashlib.md5(f"{ha1}:{NONCE}:{ha2}".encode()).hexdigest()
                    if not good:
                        reply = f"RTSP/1.0 401 Unauthorized\r\nCSeq: {cseq}\r\n\r\n"
                    elif path in self.streams:
                        body = self.streams[path]
                        reply = (f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nContent-Type: application/sdp\r\n"
                                 f"Content-Length: {len(body)}\r\n\r\n{body}")
                    else:
                        reply = f"RTSP/1.0 404 Not Found\r\nCSeq: {cseq}\r\n\r\n"
                writer.write(reply.encode())
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


@pytest.fixture
async def nvr():
    fake = FakeNvr()
    server = await asyncio.start_server(fake.handle, "127.0.0.1", 0)
    fake.port = server.sockets[0].getsockname()[1]
    yield fake
    server.close()


async def test_describe_with_digest_auth(nvr):
    answer = await rtsp_describe("127.0.0.1", nvr.port, "/chID=1&streamType=sub&linkType=tcp", USER, PASSWORD)
    assert answer.status == 200 and (answer.info.width, answer.info.height) == (640, 360)
    missing = await rtsp_describe("127.0.0.1", nvr.port, "/Streaming/Channels/101", USER, PASSWORD)
    assert missing.status == 404 and missing.info is None


async def test_wrong_password_stops_after_one_attempt(nvr, capsys):
    with pytest.raises(CredentialsRejected):
        await rtsp_describe("127.0.0.1", nvr.port, "/whatever", USER, "wrong")
    nvr.auth_attempts = 0
    assert await probe("127.0.0.1", USER, "wrong", 8, 2, rtsp_ports=(nvr.port,), onvif_ports=()) == 1
    assert nvr.auth_attempts == 1  # never keeps guessing: NVRs lock accounts
    assert "rejected the username/password" in capsys.readouterr().out


async def test_probe_finds_layout_and_prints_env(nvr, capsys):
    assert await probe("127.0.0.1", USER, PASSWORD, 4, 2, rtsp_ports=(nvr.port,), onvif_ports=()) == 0
    out = capsys.readouterr().out
    assert "Layout: tvt" in out
    assert f"NVR_MAIN_URL=rtsp://{{user}}:{{password}}@{{host}}:{nvr.port}/chID={{ch}}&streamType=main&linkType=tcp" in out
    assert f"NVR_SUB_URL=rtsp://{{user}}:{{password}}@{{host}}:{nvr.port}/chID={{ch}}&streamType=sub&linkType=tcp" in out
    assert "CAMERAS=camera_1:1,camera_2:2" in out  # channels 3-4 have nothing
    assert "DETECT_SIZE=640x360" in out


async def test_probe_reports_closed_port(capsys):
    assert await probe("127.0.0.1", USER, PASSWORD, 1, 1, rtsp_ports=(9,), onvif_ports=()) == 1
    assert "no RTSP service" in capsys.readouterr().out


# --- fake ONVIF device ---------------------------------------------------------------------------------

PROFILES = [("prof1m", "src1", 2560, 1440), ("prof1s", "src1", 640, 360), ("prof2m", "src2", 1920, 1080), ("prof2s", "src2", 640, 360)]
URIS = {"prof1m": "101", "prof1s": "102", "prof2m": "201", "prof2s": "202"}


def soap(body):
    return ('<?xml version="1.0"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
            'xmlns:tt="http://www.onvif.org/ver10/schema" xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
            f'xmlns:tds="http://www.onvif.org/ver10/device/wsdl"><s:Body>{body}</s:Body></s:Envelope>')


def onvif_handler(request: httpx.Request) -> httpx.Response:
    body = request.content.decode()
    if "GetSystemDateAndTime" in body:
        return httpx.Response(200, text=soap(
            "<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime><tt:UTCDateTime>"
            "<tt:Date><tt:Year>2026</tt:Year><tt:Month>9</tt:Month><tt:Day>18</tt:Day></tt:Date>"
            "<tt:Time><tt:Hour>12</tt:Hour><tt:Minute>0</tt:Minute><tt:Second>0</tt:Second></tt:Time>"
            "</tt:UTCDateTime></tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>"))
    # Every other call must carry a valid WS-Security digest for the right password
    nonce = base64.b64decode(re.search(r"<wsse:Nonce[^>]*>([^<]+)<", body).group(1))
    created = re.search(r"<wsu:Created>([^<]+)<", body).group(1)
    digest = re.search(r"<wsse:Password[^>]*>([^<]+)<", body).group(1)
    expected = base64.b64encode(hashlib.sha1(nonce + created.encode() + PASSWORD.encode()).digest()).decode()
    if digest != expected:
        return httpx.Response(400, text=soap(
            "<s:Fault><s:Reason><s:Text>Sender not Authorized</s:Text></s:Reason></s:Fault>"))
    if "GetCapabilities" in body:
        return httpx.Response(200, text=soap(
            "<tds:GetCapabilitiesResponse><tds:Capabilities><tt:Media>"
            "<tt:XAddr>http://10.0.0.99/onvif/Media</tt:XAddr></tt:Media></tds:Capabilities></tds:GetCapabilitiesResponse>"))
    if "GetProfiles" in body:
        profiles = "".join(
            f'<trt:Profiles token="{t}"><tt:Name>{t}</tt:Name><tt:VideoSourceConfiguration token="v{s}">'
            f"<tt:SourceToken>{s}</tt:SourceToken></tt:VideoSourceConfiguration><tt:VideoEncoderConfiguration>"
            f"<tt:Encoding>H264</tt:Encoding><tt:Resolution><tt:Width>{w}</tt:Width><tt:Height>{h}</tt:Height>"
            "</tt:Resolution></tt:VideoEncoderConfiguration></trt:Profiles>"
            for t, s, w, h in PROFILES)
        return httpx.Response(200, text=soap(f"<trt:GetProfilesResponse>{profiles}</trt:GetProfilesResponse>"))
    if "GetStreamUri" in body:
        token = re.search(r"<trt:ProfileToken>([^<]+)<", body).group(1)
        # the device advertised an internal address; the probe must use the one it reached
        assert (request.url.host, request.url.path) == ("192.168.1.50", "/onvif/Media")
        return httpx.Response(200, text=soap(
            f"<trt:GetStreamUriResponse><trt:MediaUri><tt:Uri>rtsp://10.0.0.99:554/Streaming/Channels/{URIS[token]}"
            "</tt:Uri></trt:MediaUri></trt:GetStreamUriResponse>"))
    return httpx.Response(404)


async def test_onvif_profiles_and_env_output(capsys):
    async with httpx.AsyncClient(transport=httpx.MockTransport(onvif_handler)) as client:
        result = await onvif_profiles(client, "192.168.1.50", 80, USER, PASSWORD)
    assert [(p.source, p.width, p.uri.rsplit("/", 1)[1]) for p in result.profiles] == [
        ("src1", 2560, "101"), ("src1", 640, "102"), ("src2", 1920, "201"), ("src2", 640, "202")]
    print_onvif(result, "192.168.1.50")
    out = capsys.readouterr().out
    assert "NVR_MAIN_URL=rtsp://{user}:{password}@{host}:554/Streaming/Channels/{ch}01" in out
    assert "NVR_SUB_URL=rtsp://{user}:{password}@{host}:554/Streaming/Channels/{ch}02" in out
    assert "CAMERAS=camera_1:1,camera_2:2" in out and "DETECT_SIZE=640x360" in out


async def test_onvif_wrong_password_is_reported():
    from app.probe import OnvifError

    async with httpx.AsyncClient(transport=httpx.MockTransport(onvif_handler)) as client:
        with pytest.raises(OnvifError, match="not Authorized"):
            await onvif_profiles(client, "192.168.1.50", 80, USER, "wrong")


def test_infer_template_variants():
    tvt = [f"rtsp://nvr:554/chID={c}&streamType=main&linkType=tcp" for c in (1, 2, 3)]
    assert infer_template(tvt) == "rtsp://{user}:{password}@{host}:554/chID={ch}&streamType=main&linkType=tcp"
    padded = [f"rtsp://nvr/ch{c:02d}/0" for c in (1, 2)]
    assert infer_template(padded) == "rtsp://{user}:{password}@{host}:554/ch{ch2}/0"
    assert infer_template(["rtsp://nvr/a", "rtsp://nvr/b"]) is None
    assert infer_template(["rtsp://cam:88/videoMain"]) == "rtsp://{user}:{password}@{host}:88/videoMain"

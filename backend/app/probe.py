"""Find the NVR's stream URLs (Phase 0), from inside Docker:

    docker compose run --rm --no-deps dashboard python -m app.probe

Reads NVR_HOST / NVR_RTSP_USER / NVR_RTSP_PASSWORD from .env (flags override them).

1. ONVIF: asks the NVR for its media profiles and their exact RTSP URIs.
2. RTSP: tries the common NVR URL layouts with DESCRIBE, reading codec and resolution
   from the SDP. Stops at the first rejected password so the NVR doesn't lock the account.

Prints the NVR_MAIN_URL / NVR_SUB_URL / CAMERAS lines to put in .env. Standard library +
httpx only; no ffmpeg needed.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import getpass
import hashlib
import os
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from html import escape
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx

# --- H.264 SPS: width/height from the SDP's sprop-parameter-sets --------------------------------


class _Bits:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def u(self, n: int) -> int:
        value = 0
        for _ in range(n):
            byte = self.data[self.pos >> 3]
            value = (value << 1) | ((byte >> (7 - (self.pos & 7))) & 1)
            self.pos += 1
        return value

    def ue(self) -> int:
        zeros = 0
        while self.u(1) == 0:
            zeros += 1
        return (1 << zeros) - 1 + (self.u(zeros) if zeros else 0)

    def se(self) -> int:
        k = self.ue()
        return (k + 1) // 2 if k % 2 else -(k // 2)


def _unescape_nal(nal: bytes) -> bytes:
    """Drop emulation-prevention bytes (00 00 03 -> 00 00)."""
    out = bytearray()
    zeros = 0
    for b in nal:
        if zeros >= 2 and b == 3:
            zeros = 0
            continue
        out.append(b)
        zeros = zeros + 1 if b == 0 else 0
    return bytes(out)


_HIGH_PROFILES = {100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135}


def h264_resolution(sps: bytes) -> tuple[int, int] | None:
    try:
        r = _Bits(_unescape_nal(sps))
        r.u(8)  # NAL header
        profile = r.u(8)
        r.u(16)  # constraint flags + level
        r.ue()  # seq_parameter_set_id
        chroma = 1
        if profile in _HIGH_PROFILES:
            chroma = r.ue()
            if chroma == 3:
                r.u(1)
            r.ue()
            r.ue()
            r.u(1)
            if r.u(1):  # scaling matrices
                for i in range(12 if chroma == 3 else 8):
                    if r.u(1):
                        last = nxt = 8
                        for _ in range(16 if i < 6 else 64):
                            if nxt:
                                nxt = (last + r.se() + 256) % 256
                            last = nxt or last
        r.ue()  # log2_max_frame_num
        poc = r.ue()
        if poc == 0:
            r.ue()
        elif poc == 1:
            r.u(1)
            r.se()
            r.se()
            for _ in range(r.ue()):
                r.se()
        r.ue()  # max_num_ref_frames
        r.u(1)
        width_mbs = r.ue() + 1
        height_units = r.ue() + 1
        frame_mbs_only = r.u(1)
        if not frame_mbs_only:
            r.u(1)
        r.u(1)
        width = width_mbs * 16
        height = (2 - frame_mbs_only) * height_units * 16
        if r.u(1):  # frame cropping
            left, right, top, bottom = r.ue(), r.ue(), r.ue(), r.ue()
            unit_x = 1 if chroma in (0, 3) else 2
            unit_y = (2 - frame_mbs_only) * (2 if chroma == 1 else 1)
            width -= (left + right) * unit_x
            height -= (top + bottom) * unit_y
        return width, height
    except IndexError:
        return None


@dataclass
class StreamInfo:
    codec: str | None = None
    width: int | None = None
    height: int | None = None

    def describe(self) -> str:
        size = f"{self.width}x{self.height}" if self.width else "size ?"
        return f"{self.codec or '?':<5} {size}"


def parse_sdp(sdp: str) -> StreamInfo | None:
    """Codec and resolution of the first video track, or None if there isn't one."""
    video = None
    for section in re.split(r"\r?\n(?=m=)", sdp):
        if section.startswith("m=video"):
            video = section
            break
    if video is None:
        return None
    info = StreamInfo()
    if m := re.search(r"a=rtpmap:\d+\s+([A-Za-z0-9-]+)/", video):
        info.codec = m.group(1).upper().replace("HEVC", "H265")
    if m := re.search(r"sprop-parameter-sets=([A-Za-z0-9+/=]+)", video):
        try:
            size = h264_resolution(base64.b64decode(m.group(1) + "=" * (-len(m.group(1)) % 4)))
        except ValueError:
            size = None
        if size:
            info.width, info.height = size
    if not info.width and (m := re.search(r"a=x-dimensions:\s*(\d+)\s*,\s*(\d+)", video)):
        info.width, info.height = int(m.group(1)), int(m.group(2))
    return info


# --- RTSP DESCRIBE with Basic/Digest auth ---------------------------------------------------------


class CredentialsRejected(Exception):
    pass


@dataclass
class RtspAnswer:
    status: int | None
    detail: str
    info: StreamInfo | None = None


def _auth_header(challenges: list[str], method: str, uri: str, user: str, password: str) -> str | None:
    digest = next((c for c in challenges if c.lower().startswith("digest")), None)
    if digest:
        params = dict(re.findall(r'(\w+)="?([^",]*)"?', digest[6:]))
        realm, nonce = params.get("realm", ""), params.get("nonce", "")
        ha1 = hashlib.md5(f"{user}:{realm}:{password}".encode()).hexdigest()
        ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
        header = f'Digest username="{user}", realm="{realm}", nonce="{nonce}", uri="{uri}"'
        if "auth" in params.get("qop", "").split(","):
            cnonce = os.urandom(8).hex()
            response = hashlib.md5(f"{ha1}:{nonce}:00000001:{cnonce}:auth:{ha2}".encode()).hexdigest()
            header += f', qop=auth, nc=00000001, cnonce="{cnonce}"'
        else:
            response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
        if "opaque" in params:
            header += f', opaque="{params["opaque"]}"'
        return header + f', response="{response}"'
    if any(c.lower().startswith("basic") for c in challenges):
        return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
    return None


async def _exchange(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, lines: list[str], timeout: float):
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
    first, *rest = head.decode(errors="replace").split("\r\n")
    parts = first.split(" ", 2)
    status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    headers: dict[str, list[str]] = {}
    for line in rest:
        if ":" in line:
            key, _, value = line.partition(":")
            headers.setdefault(key.strip().lower(), []).append(value.strip())
    length = int((headers.get("content-length") or ["0"])[0] or 0)
    body = (await asyncio.wait_for(reader.readexactly(length), timeout)).decode(errors="replace") if length else ""
    return status, (parts[2] if len(parts) > 2 else ""), headers, body


async def rtsp_describe(host: str, port: int, path: str, user: str, password: str, timeout: float = 5.0) -> RtspAnswer:
    uri = f"rtsp://{host}:{port}{path}"
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, TimeoutError) as exc:
        return RtspAnswer(None, f"no RTSP service ({exc.__class__.__name__})")
    try:
        base = [f"DESCRIBE {uri} RTSP/1.0", "Accept: application/sdp", "User-Agent: home-security-probe"]
        status, reason, headers, body = await _exchange(reader, writer, [*base, "CSeq: 1"], timeout)
        if status == 401 and user:
            auth = _auth_header(headers.get("www-authenticate", []), "DESCRIBE", uri, user, password)
            if auth:
                status, reason, headers, body = await _exchange(
                    reader, writer, [*base, "CSeq: 2", f"Authorization: {auth}"], timeout
                )
                if status == 401:
                    raise CredentialsRejected(f"{host}:{port} rejected the username/password")
        if status == 200:
            info = parse_sdp(body)
            if info:
                return RtspAnswer(200, "ok", info)
            return RtspAnswer(200, "answered without a video track")
        return RtspAnswer(status, f"{status} {reason}".strip())
    except (OSError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
        return RtspAnswer(None, f"no answer ({exc.__class__.__name__})")
    finally:
        writer.close()


# URL layouts seen on NVRs. {ch} = 1, {ch2} = 01, {chz} = 00 for channel 1.
LAYOUTS = [
    ("hikvision", "/Streaming/Channels/{ch}01", "/Streaming/Channels/{ch}02"),
    ("dahua", "/cam/realmonitor?channel={ch}&subtype=0", "/cam/realmonitor?channel={ch}&subtype=1"),
    ("tvt", "/chID={ch}&streamType=main&linkType=tcp", "/chID={ch}&streamType=sub&linkType=tcp"),
    ("xmeye", "/user={user}&password={password}&channel={ch}&stream=0.sdp?real_stream",
     "/user={user}&password={password}&channel={ch}&stream=1.sdp?real_stream"),
    ("uniview", "/unicast/c{ch}/s0/live", "/unicast/c{ch}/s1/live"),
    ("ch-main", "/ch{ch}/main", "/ch{ch}/sub"),
    ("ch-main-2", "/ch{ch2}/main", "/ch{ch2}/sub"),
    ("ch-index", "/ch{ch2}/0", "/ch{ch2}/1"),
    ("h264-ch", "/h264/ch{ch}/main/av_stream", "/h264/ch{ch}/sub/av_stream"),
    ("live-ch", "/live/ch{chz}_0", "/live/ch{chz}_1"),
    ("reolink", "/h264Preview_{ch2}_main", "/h264Preview_{ch2}_sub"),
    ("foscam-camera", "/videoMain", "/videoSub"),  # single camera: channel isn't in the path
]
RTSP_PORTS = (554, 88)  # Foscam devices often serve RTSP on their HTTP port, 88


def fill(template: str, ch: int, user: str = "", password: str = "") -> str:
    return (template.replace("{ch2}", f"{ch:02d}").replace("{chz}", f"{ch - 1:02d}").replace("{ch}", str(ch))
            .replace("{user}", user).replace("{password}", password))


# --- ONVIF ----------------------------------------------------------------------------------------

WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
DIGEST_TYPE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
B64_TYPE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary"
ONVIF_PORTS = (80, 88, 8000, 8080, 8899)


class OnvifError(Exception):
    pass


def envelope(body: str, user: str | None = None, password: str = "", clock_offset: timedelta = timedelta()) -> str:
    header = ""
    if user:
        nonce = os.urandom(16)
        created = (datetime.now(UTC) + clock_offset).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + password.encode()).digest()).decode()
        header = (
            f'<s:Header><wsse:Security xmlns:wsse="{WSSE}" xmlns:wsu="{WSU}"><wsse:UsernameToken>'
            f"<wsse:Username>{escape(user)}</wsse:Username>"
            f'<wsse:Password Type="{DIGEST_TYPE}">{digest}</wsse:Password>'
            f'<wsse:Nonce EncodingType="{B64_TYPE}">{base64.b64encode(nonce).decode()}</wsse:Nonce>'
            f"<wsu:Created>{created}</wsu:Created></wsse:UsernameToken></wsse:Security></s:Header>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
        f'xmlns:tt="http://www.onvif.org/ver10/schema">{header}<s:Body>{body}</s:Body></s:Envelope>'
    )


def _text(node: ET.Element | None, path: str) -> str | None:
    found = node.find(path) if node is not None else None
    return found.text.strip() if found is not None and found.text else None


@dataclass
class OnvifProfile:
    token: str
    source: str
    encoding: str | None
    width: int | None
    height: int | None
    uri: str | None = None


@dataclass
class OnvifResult:
    port: int
    profiles: list[OnvifProfile] = field(default_factory=list)


async def _soap(client: httpx.AsyncClient, url: str, body: str, auth: tuple[str, str] | None, offset: timedelta) -> ET.Element:
    payload = envelope(body, *(auth or (None, "")), clock_offset=offset)
    response = await client.post(url, content=payload, headers={"Content-Type": "application/soap+xml; charset=utf-8"})
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as exc:
        raise OnvifError(f"not an ONVIF service (HTTP {response.status_code})") from exc
    fault = root.find(".//{*}Fault")
    if fault is not None:
        reason = _text(fault, ".//{*}Text") or _text(fault, ".//{*}Value") or "SOAP fault"
        raise OnvifError(reason)
    return root


def _rebase(xaddr: str, host: str, port: int) -> str:
    """Devices often advertise an internal address; keep the path, use the address we reached."""
    parts = urlsplit(xaddr)
    return urlunsplit((parts.scheme or "http", f"{host}:{port}", parts.path or "/onvif/media_service", parts.query, ""))


async def onvif_profiles(client: httpx.AsyncClient, host: str, port: int, user: str, password: str) -> OnvifResult:
    device = f"http://{host}:{port}/onvif/device_service"
    try:
        root = await _soap(client, device, "<tds:GetSystemDateAndTime/>", None, timedelta())
    except httpx.HTTPError as exc:
        raise OnvifError(f"no ONVIF service ({exc.__class__.__name__})") from exc
    offset = timedelta()
    utc = root.find(".//{*}UTCDateTime")
    if utc is not None:
        try:
            device_now = datetime(
                int(_text(utc, ".//{*}Year")), int(_text(utc, ".//{*}Month")), int(_text(utc, ".//{*}Day")),
                int(_text(utc, ".//{*}Hour")), int(_text(utc, ".//{*}Minute")), int(_text(utc, ".//{*}Second")), tzinfo=UTC,
            )
            offset = device_now - datetime.now(UTC)  # sign the token with the device's clock
        except (TypeError, ValueError):
            pass
    auth = (user, password)
    caps = await _soap(client, device, "<tds:GetCapabilities><tds:Category>Media</tds:Category></tds:GetCapabilities>", auth, offset)
    xaddr = _text(caps, ".//{*}Media/{*}XAddr")
    if not xaddr:
        raise OnvifError("device has no media service")
    media = _rebase(xaddr, host, port)
    profiles_root = await _soap(client, media, "<trt:GetProfiles/>", auth, offset)
    result = OnvifResult(port)
    for p in profiles_root.findall(".//{*}Profiles"):
        enc = p.find(".//{*}VideoEncoderConfiguration")
        width, height = _text(enc, ".//{*}Resolution/{*}Width"), _text(enc, ".//{*}Resolution/{*}Height")
        result.profiles.append(OnvifProfile(
            token=p.get("token", ""),
            source=_text(p, ".//{*}VideoSourceConfiguration/{*}SourceToken") or "",
            encoding=_text(enc, "{*}Encoding"),
            width=int(width) if width else None,
            height=int(height) if height else None,
        ))
    for profile in result.profiles:
        body = (
            "<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream><tt:Transport>"
            "<tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup>"
            f"<trt:ProfileToken>{escape(profile.token)}</trt:ProfileToken></trt:GetStreamUri>"
        )
        profile.uri = _text(await _soap(client, media, body, auth, offset), ".//{*}Uri")
    return result


def _path(uri: str) -> str:
    parts = urlsplit(uri)
    return parts.path + (f"?{parts.query}" if parts.query else "")


def infer_template(uris: list[str]) -> str | None:
    """Turn per-channel URIs (channel 1, 2, ...) into one {ch} template, if they follow a pattern."""
    if not uris:
        return None
    base = f"rtsp://{{user}}:{{password}}@{{host}}:{urlsplit(uris[0]).port or 554}"
    first = _path(uris[0])
    if len(uris) == 1:
        return base + first
    candidates = []
    for m in re.finditer(r"\d+", first):
        digits = m.group()
        for token, value in (("{ch}", "1"), ("{ch2}", "01"), ("{chz}", "00")):
            if digits == value:
                candidates.append(first[: m.start()] + token + first[m.end():])
        if len(digits) == 3 and digits[0] == "1":  # Hikvision-style 101 = channel 1, stream 01
            candidates.append(first[: m.start()] + "{ch}" + digits[1:] + first[m.end():])
    wanted = [_path(u) for u in uris]
    for candidate in candidates:
        if all(fill(candidate, i + 1) == w for i, w in enumerate(wanted)):
            return base + candidate
    return None


# --- CLI ------------------------------------------------------------------------------------------


async def probe(
    host: str, user: str, password: str, channels: int, timeout: float,
    rtsp_ports: tuple[int, ...] = RTSP_PORTS, onvif_ports: tuple[int, ...] = ONVIF_PORTS,
) -> int:
    print(f"Probing {host} as {user or '(no user)'}\n")
    print("ONVIF")
    async with httpx.AsyncClient(timeout=timeout) as client:
        for port in onvif_ports:
            try:
                result = await onvif_profiles(client, host, port, user, password)
            except OnvifError as exc:
                print(f"  port {port:<5} {exc}")
                if "not authorized" in str(exc).lower() or "sender not authorized" in str(exc).lower():
                    print("\nThe NVR rejected the username/password over ONVIF; stopping so it doesn't lock the account.")
                    return 1
                continue
            except httpx.HTTPError as exc:
                print(f"  port {port:<5} no answer ({exc.__class__.__name__})")
                continue
            print(f"  port {port:<5} ONVIF answered with {len(result.profiles)} profiles")
            if result.profiles:
                print_onvif(result, host)
                return 0

    print("\nRTSP layouts")
    found = None
    try:
        for port in rtsp_ports:
            for name, main, sub in LAYOUTS:
                answer = await rtsp_describe(host, port, fill(main, 1, user, password), user, password, timeout)
                if answer.status is None and "no RTSP service" in answer.detail:
                    print(f"  port {port:<5} {answer.detail}")
                    break
                print(f"  port {port:<5} {name:<14} {answer.info.describe() if answer.info else answer.detail}")
                if answer.info:
                    found = (port, name, main, sub)
                    break
            if found:
                break
        if not found:
            print("\nNo known layout answered. Open the NVR's web page (http://<nvr>:88) and look for the RTSP\n"
                  "settings, or point the probe straight at a camera's IP instead of the NVR.")
            return 1
        port, name, main, sub = found
        print(f"\nLayout: {name} on port {port}\n")
        print(f"{'ch':>3}  {'main':<18} {'sub':<18}")
        live = []
        for ch in range(1, channels + 1):
            row = []
            for template in (main, sub):
                a = await rtsp_describe(host, port, fill(template, ch, user, password), user, password, timeout)
                row.append(a.info)
            print(f"{ch:>3}  {row[0].describe() if row[0] else '-- none --':<18} {row[1].describe() if row[1] else '-- none --':<18}")
            if row[0]:
                live.append((ch, row[1] or row[0]))
            if "{ch" not in main:
                break
    except CredentialsRejected as exc:
        print(f"\n{exc}. Stopping so the NVR doesn't lock the account; check NVR_RTSP_USER/NVR_RTSP_PASSWORD.")
        return 1
    print_env(f"rtsp://{{user}}:{{password}}@{{host}}:{port}{main}", f"rtsp://{{user}}:{{password}}@{{host}}:{port}{sub}", live, host)
    return 0


def print_onvif(result: OnvifResult, host: str) -> None:
    by_source: dict[str, list[OnvifProfile]] = {}
    for p in result.profiles:
        by_source.setdefault(p.source, []).append(p)
    rows = []
    for source, profiles in by_source.items():
        ranked = sorted(profiles, key=lambda p: (p.width or 0) * (p.height or 0), reverse=True)
        main, sub = ranked[0], ranked[-1]
        rows.append((source, main, sub))
        print(f"    {source:<16} main {main.encoding or '?'} {main.width}x{main.height}  {main.uri}")
        if sub is not main:
            print(f"    {'':<16} sub  {sub.encoding or '?'} {sub.width}x{sub.height}  {sub.uri}")
    main_t = infer_template([r[1].uri for r in rows if r[1].uri])
    sub_t = infer_template([r[2].uri for r in rows if r[2].uri])
    if not main_t:
        print("\n  The URIs above don't share a channel pattern; set NVR_MAIN_URL from them by hand (RUNBOOK).")
        return
    print_env(main_t, sub_t, [(i + 1, _info(r[2])) for i, r in enumerate(rows)], host)


def _info(p: OnvifProfile) -> StreamInfo:
    return StreamInfo((p.encoding or "").upper() or None, p.width, p.height)


def print_env(main: str, sub: str | None, live: list[tuple[int, StreamInfo]], host: str) -> None:
    single = "{ch" not in main  # a single camera (e.g. Foscam /videoMain), not an NVR channel layout
    prefix = "CAMERA" if single else "NVR"
    print("\nPut these in .env (then rename the cameras):\n")
    print(f"  {prefix}_MAIN_URL={main}")
    if sub and sub != main:
        print(f"  {prefix}_SUB_URL={sub}")
    if single:
        print(f"  CAMERAS=camera_1@{host}      # add more as name@ip, comma-separated")
    else:
        print(f"  CAMERAS={','.join(f'camera_{ch}:{ch}' for ch, _ in live) or 'camera_1:1'}")
    sizes = {(i.width, i.height) for _, i in live if i and i.width}
    if len(sizes) == 1:
        w, h = sizes.pop()
        print(f"  DETECT_SIZE={w}x{h}")
    elif sizes:
        print("  # sub streams differ in size; give it per camera, e.g. CAMERAS=front_door:1:640x360,...")
    if any(i and i.codec == "H265" for _, i in live):
        print("\n  Some streams are H.265: browsers can't play those; switch them to H.264 for live view.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Find the NVR's RTSP stream URLs.")
    parser.add_argument("--host", default=os.environ.get("NVR_HOST"))
    parser.add_argument("--user", default=os.environ.get("NVR_RTSP_USER", "admin"))
    parser.add_argument("--channels", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args()
    if not args.host:
        parser.error("set NVR_HOST in .env or pass --host")
    # .env holds the password URL-encoded (for Frigate's URLs); the NVR wants the raw one.
    password = unquote(os.environ.get("NVR_RTSP_PASSWORD", "")) or getpass.getpass("NVR password: ")
    return asyncio.run(probe(args.host, args.user, password, args.channels, args.timeout))


if __name__ == "__main__":
    sys.exit(main())

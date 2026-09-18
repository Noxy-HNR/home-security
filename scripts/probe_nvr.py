#!/usr/bin/env python3
"""Find working RTSP URLs on the NVR before configuring Frigate (Phase 0).

Tries the common URL layouts for each channel's main and sub stream with ffprobe and prints
codec, resolution and frame rate: the numbers Frigate's config needs. Standard library only;
needs ffprobe on the PATH (apt install ffmpeg).

    python3 scripts/probe_nvr.py --host 192.168.1.64 --user admin --channels 8
    python3 scripts/probe_nvr.py --host 192.168.1.64 --user admin --pattern 'rtsp://{auth}{host}:554/live/ch{ch}/{stream}'

The password is read from NVR_RTSP_PASSWORD or prompted for; it is never printed.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
from urllib.parse import quote

# name -> (template, main stream token, sub stream token)
PATTERNS = {
    "hikvision": ("rtsp://{auth}{host}:{port}/Streaming/Channels/{ch}0{stream}", "1", "2"),
    "dahua": ("rtsp://{auth}{host}:{port}/cam/realmonitor?channel={ch}&subtype={stream}", "0", "1"),
    "onvif-generic": ("rtsp://{auth}{host}:{port}/ch{ch}/{stream}", "main", "sub"),
    "h264-generic": ("rtsp://{auth}{host}:{port}/h264/ch{ch}/{stream}/av_stream", "main", "sub"),
}


def probe(url: str, timeout: int) -> dict | None:
    cmd = [
        "ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-timeout", str(timeout * 1_000_000),
        "-select_streams", "v:0", "-show_entries", "stream=codec_name,width,height,avg_frame_rate",
        "-of", "json", url,
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
    except subprocess.TimeoutExpired:
        return None
    if out.returncode != 0:
        return None
    streams = json.loads(out.stdout or "{}").get("streams") or []
    return streams[0] if streams else None


def fps(rate: str | None) -> str:
    try:
        num, den = (rate or "0/1").split("/")
        return f"{float(num) / float(den):.0f}"
    except (ValueError, ZeroDivisionError):
        return "?"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", default="admin")
    parser.add_argument("--port", type=int, default=554)
    parser.add_argument("--channels", type=int, default=4, help="how many NVR channels to try")
    parser.add_argument("--pattern", help="custom template with {auth} {host} {port} {ch} {stream}")
    parser.add_argument("--main", default="main", help="{stream} value for main with --pattern")
    parser.add_argument("--sub", default="sub", help="{stream} value for sub with --pattern")
    parser.add_argument("--timeout", type=int, default=6)
    args = parser.parse_args()

    if not shutil.which("ffprobe"):
        print("ffprobe not found: sudo apt install ffmpeg", file=sys.stderr)
        return 2
    password = os.environ.get("NVR_RTSP_PASSWORD") or getpass.getpass(f"RTSP password for {args.user}@{args.host}: ")
    auth = f"{quote(args.user, safe='')}:{quote(password, safe='')}@"

    patterns = {"custom": (args.pattern, args.main, args.sub)} if args.pattern else PATTERNS
    found_layout = None
    for name, (template, main_tok, sub_tok) in patterns.items():
        url = template.format(auth=auth, host=args.host, port=args.port, ch=1, stream=main_tok)
        if probe(url, args.timeout):
            found_layout = name
            break
        print(f"  {name:<14} no answer on channel 1")
    if not found_layout:
        print("\nNo known layout answered. Check the credentials, that RTSP is enabled on the NVR,\n"
              "or find the URL with ONVIF Device Manager and pass it via --pattern.")
        return 1

    template, main_tok, sub_tok = patterns[found_layout]
    print(f"\nLayout: {found_layout}   {template.replace('{auth}', 'USER:PASS@')}\n")
    print(f"{'ch':>3}  {'stream':<5} {'codec':<6} {'resolution':<11} {'fps':>4}")
    subs = []
    for ch in range(1, args.channels + 1):
        for label, token in (("main", main_tok), ("sub", sub_tok)):
            info = probe(template.format(auth=auth, host=args.host, port=args.port, ch=ch, stream=token), args.timeout)
            if not info:
                print(f"{ch:>3}  {label:<5} -- no stream --")
                continue
            res = f"{info.get('width')}x{info.get('height')}"
            note = "  <- browsers can't play H.265; switch the NVR to H.264 for live view" if label == "main" and info.get("codec_name") in ("hevc", "h265") else ""
            print(f"{ch:>3}  {label:<5} {info.get('codec_name', '?'):<6} {res:<11} {fps(info.get('avg_frame_rate')):>4}{note}")
            if label == "sub":
                subs.append((ch, info))
    if subs:
        print("\nFor each camera in Frigate's config set detect.width/height to its sub-stream size, e.g.")
        ch, info = subs[0]
        print(f"  channel {ch}: detect: {{ width: {info.get('width')}, height: {info.get('height')} }}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

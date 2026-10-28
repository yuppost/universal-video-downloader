"""How a browser extension hands a video link to Universal Video Studio.

    browser extension --(Native Messaging, JSON)--> native host (uvs-native-host.exe) --(local pipe)--> running app

This module is deliberately standard-library only: the tiny native host exe imports it without Qt.
The app listens on a per-user local pipe/socket; a request is one JSON line ``{"type": "open", "url": "..."}``.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import socket
import struct
import sys
import tempfile
from typing import Optional

HOST_NAME = "com.universalvideostudio.host"
MAX_URL = 4096
_CONTROL = re.compile(r"[\x00-\x20\x7f]")


def pipe_name() -> str:
    """Per-user name, so two Windows users on one PC never talk to each other's app."""
    user = re.sub(r"[^A-Za-z0-9_.-]", "_", getpass.getuser() or "user")
    return f"UniversalVideoStudio-{user}"


def socket_path() -> str:
    """Where the local socket lives on non-Windows systems (only used by tests/dev; the app itself targets Windows)."""
    return os.path.join(tempfile.gettempdir(), pipe_name())


def clean_url(raw: object) -> Optional[str]:
    """The URL when it is a plain http(s) address, else None. Whitespace/control characters are rejected."""
    if not isinstance(raw, str):
        return None
    url = raw.strip()
    if not url or len(url) > MAX_URL or _CONTROL.search(url):
        return None
    if not re.match(r"^https?://[^/\s]+", url, re.I):
        return None
    return url


def encode_request(url: str) -> bytes:
    return (json.dumps({"type": "open", "url": url}, separators=(",", ":")) + "\n").encode("utf-8")


def parse_request(data: bytes) -> Optional[str]:
    """The URL inside one request line, or None when it is malformed or not an 'open' request."""
    try:
        msg = json.loads(data.decode("utf-8").strip())
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(msg, dict) or msg.get("type") != "open":
        return None
    return clean_url(msg.get("url"))


def send_to_running_app(url: str, timeout: float = 2.0) -> bool:
    """Deliver *url* to the app that is already running. False when no app is listening."""
    payload = encode_request(url)
    try:
        if sys.platform == "win32":
            with open(r"\\.\pipe\\" + pipe_name(), "r+b", buffering=0) as pipe:
                pipe.write(payload)
            return True
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.settimeout(timeout)
            s.connect(socket_path())
            s.sendall(payload)
        finally:
            s.close()
        return True
    except OSError:
        return False


# ---- Chrome/Edge/Brave Native Messaging framing: 4-byte little-endian length + UTF-8 JSON -----------------
def read_native_message(stream) -> Optional[dict]:
    head = stream.read(4)
    if len(head) < 4:
        return None
    (n,) = struct.unpack("<I", head)
    if n <= 0 or n > 1_000_000:
        return None
    body = stream.read(n)
    if len(body) < n:
        return None
    try:
        msg = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return msg if isinstance(msg, dict) else None


def write_native_message(stream, msg: dict) -> None:
    data = json.dumps(msg, separators=(",", ":")).encode("utf-8")
    stream.write(struct.pack("<I", len(data)) + data)
    stream.flush()

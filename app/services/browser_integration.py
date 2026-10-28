"""Connects the browser extension (Chrome, Brave, Edge) to this app: registers the Native Messaging host.

Windows only: a JSON manifest in the user's data folder plus one registry value per browser under HKCU (no
administrator rights). It is refreshed on every start, so moving the app folder or updating never breaks it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

from app.services.link_protocol import HOST_NAME
from app.utils.paths import app_data_dir

log = logging.getLogger("uvs.browser")

HOST_EXE = "uvs-native-host.exe"
# Public key of the extension in /browser_extension/manifest.json: it fixes the extension's ID for unpacked installs.
EXTENSION_KEY = (
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEApXuP10EGteGdxf157EZKRlub6a4dIxSMJ+Grieu0lnrVfuMvrkP2a5uE"
    "niqR5FsZgZK/xWzfAuYqzfiv/Dx+w+zF04zgEKlCDE28vOxXJag60bQWH9ryZ5HmxUBAsdTxnggFGbNbTDJiz4unow1oNSN2Vy35"
    "+ry0Ucw3M4UCA4+CvFc3py5d+coQ/LwCoDFuyMb7PKZYxBza5zYan6jpj691br73po8oKffENFizThF+JchQRvLxuUC4NqNfJNJ+"
    "G/N9fxjsKd/WwDYiSQym7z9XJZ0VeuCiowIXz2O2oQVDLCj37IBnYEmB0P7HkWcxaWIR5lGibDEB+fk0QP1emwIDAQAB")
# IDs the extension gets in the Chrome Web Store / Edge Add-ons after publishing: add them here or, without
# rebuilding, one per line in <data folder>/extension_ids.txt
STORE_EXTENSION_IDS: List[str] = []

BROWSER_KEYS = {
    "Chrome": r"Software\Google\Chrome\NativeMessagingHosts",
    "Edge": r"Software\Microsoft\Edge\NativeMessagingHosts",
    "Brave": r"Software\BraveSoftware\Brave-Browser\NativeMessagingHosts",
}


def extension_id_from_key(b64_key: str) -> str:
    """Chrome's rule: SHA-256 of the DER public key, first 16 bytes, hex digits mapped 0-f -> a-p."""
    digest = hashlib.sha256(base64.b64decode(b64_key)).hexdigest()[:32]
    return "".join(chr(ord("a") + int(c, 16)) for c in digest)


def dev_extension_id() -> str:
    return extension_id_from_key(EXTENSION_KEY)


def extension_ids() -> List[str]:
    ids = [dev_extension_id(), *STORE_EXTENSION_IDS]
    extra = app_data_dir() / "extension_ids.txt"
    try:
        for line in extra.read_text(encoding="utf-8").splitlines():
            line = line.strip().lower()
            if len(line) == 32 and line.isalpha() and line not in ids:
                ids.append(line)
    except OSError:
        pass
    return ids


def host_manifest(host_path: str, ids: Optional[List[str]] = None) -> Dict[str, object]:
    return {"name": HOST_NAME,
            "description": "Universal Video Studio - sends video links from the browser to the app",
            "path": host_path, "type": "stdio",
            "allowed_origins": [f"chrome-extension://{i}/" for i in (ids or extension_ids())]}


def host_exe_path() -> Optional[Path]:
    """What the browser should start: the native host exe next to the app exe; when running from source (run.bat),
    a small .bat that starts ``native_host.py`` with the same Python."""
    p = Path(sys.executable).parent / HOST_EXE
    if getattr(sys, "frozen", False):
        return p if p.is_file() else None
    script = Path(__file__).resolve().parents[2] / "native_host.py"
    if os.name != "nt" or not script.is_file():
        return None
    py = Path(sys.executable)
    if py.name.lower() == "pythonw.exe" and (py.with_name("python.exe")).is_file():
        py = py.with_name("python.exe")                    # the host needs stdin/stdout, pythonw has none
    bat = app_data_dir() / "native" / "uvs-native-host-dev.bat"
    try:
        bat.parent.mkdir(parents=True, exist_ok=True)
        bat.write_text(f'@echo off\r\n"{py}" "{script}" %*\r\n', encoding="ascii", errors="replace")
    except OSError:
        return None
    return bat


def manifest_path() -> Path:
    return app_data_dir() / "native" / f"{HOST_NAME}.json"


def register(host: Optional[Path] = None, winreg_module=None) -> bool:
    """Write the host manifest and point Chrome/Edge/Brave at it. True when at least one browser was registered."""
    if os.name != "nt" and winreg_module is None:
        return False
    host = host or host_exe_path()
    if host is None:
        return False
    mp = manifest_path()
    try:
        mp.parent.mkdir(parents=True, exist_ok=True)
        mp.write_text(json.dumps(host_manifest(str(host)), indent=2), encoding="utf-8")
        reg = winreg_module
        if reg is None:
            import winreg as reg  # type: ignore[no-redef]
        ok = 0
        for name, key in BROWSER_KEYS.items():
            try:
                with reg.CreateKey(reg.HKEY_CURRENT_USER, rf"{key}\{HOST_NAME}") as k:
                    reg.SetValueEx(k, "", 0, reg.REG_SZ, str(mp))
                ok += 1
            except OSError as exc:
                log.warning("could not register for %s: %s", name, exc)
        return ok > 0
    except OSError as exc:
        log.warning("browser integration failed: %s", exc)
        return False


def unregister(winreg_module=None) -> None:
    if os.name != "nt" and winreg_module is None:
        return
    reg = winreg_module
    if reg is None:
        import winreg as reg  # type: ignore[no-redef]
    for key in BROWSER_KEYS.values():
        try:
            reg.DeleteKey(reg.HKEY_CURRENT_USER, rf"{key}\{HOST_NAME}")
        except OSError:
            pass
    try:
        manifest_path().unlink()
    except OSError:
        pass


def extension_folder() -> Optional[Path]:
    """The folder with the unpacked extension (shipped next to the exe), for 'Load unpacked'."""
    for base in (Path(sys.executable).parent, Path(__file__).resolve().parents[2]):
        p = base / "browser_extension"
        if (p / "manifest.json").is_file():
            return p
    return None

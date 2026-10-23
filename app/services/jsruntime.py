"""Locate a JavaScript runtime for yt-dlp's YouTube challenge solver (Deno preferred, Node 22+ accepted)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Optional, Tuple

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_EXE = ".exe" if os.name == "nt" else ""


def managed_deno_dir() -> Path:
    from app.utils.paths import app_data_dir
    return app_data_dir() / "tools" / "deno"


def _version(path: str, flag: str = "--version") -> Optional[Tuple[int, ...]]:
    try:
        out = subprocess.run([path, flag], capture_output=True, text=True, timeout=15,
                             creationflags=_NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", out or "")
    return tuple(int(x) for x in m.groups()) if m else None


def find_js_runtime() -> Optional[Tuple[str, str, Tuple[int, ...]]]:
    """Return (name, path, version) of the best usable runtime, or None."""
    managed = managed_deno_dir() / f"deno{_EXE}"
    cands = []
    import sys
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[2]
    bundled = base / "deno" / f"deno{_EXE}"
    if bundled.is_file():
        cands.append(("deno", str(bundled), (2, 3, 0)))
    if managed.is_file():
        cands.append(("deno", str(managed), (2, 3, 0)))
    w = shutil.which("deno")
    if w:
        cands.append(("deno", w, (2, 3, 0)))
    w = shutil.which("node")
    if w:
        cands.append(("node", w, (22, 0, 0)))
    for name, path, minimum in cands:
        ver = _version(path)
        if ver and ver >= minimum:
            return name, path, ver
    return None


def js_runtimes_option() -> Dict[str, Dict[str, str]]:
    """Value for yt-dlp's ``js_runtimes`` parameter ({} lets yt-dlp use its own default)."""
    found = find_js_runtime()
    return {found[0]: {"path": found[1]}} if found else {}


def js_runtime_present() -> str:
    """Cheap check for startup: name of a runtime that exists on disk/PATH (nothing is executed)."""
    import sys
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[2]
    for p in (base / "deno" / f"deno{_EXE}", managed_deno_dir() / f"deno{_EXE}"):
        if p.is_file():
            return "deno"
    if shutil.which("deno"):
        return "deno"
    return "node" if shutil.which("node") else ""

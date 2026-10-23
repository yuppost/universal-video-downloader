"""Browser sign-in support: detect browsers, build/test the cookie source, classify sign-in errors.

Cookies are only ever read by yt-dlp at run time from the browser the user chose (or from a cookies.txt
file the user exported). They are never copied into settings, shown, or written to logs.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Tuple

BROWSER_LABELS: Dict[str, str] = {"firefox": "Firefox", "chrome": "Google Chrome", "edge": "Microsoft Edge",
                                  "brave": "Brave", "opera": "Opera", "vivaldi": "Vivaldi", "chromium": "Chromium"}
# Browsers whose cookie store yt-dlp can read reliably. Chrome-family browsers (Chrome/Edge/Brave...) encrypt
# cookies in a way that often fails while the browser is open or on newer versions -> Firefox / cookies.txt is safer.
RELIABLE = {"firefox"}


def _dirs() -> Dict[str, List[Path]]:
    local = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    roam = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
    home = Path.home()
    return {
        "firefox": [roam / "Mozilla" / "Firefox" / "Profiles", home / ".mozilla" / "firefox"],
        "chrome": [local / "Google" / "Chrome" / "User Data", home / ".config" / "google-chrome"],
        "edge": [local / "Microsoft" / "Edge" / "User Data", home / ".config" / "microsoft-edge"],
        "brave": [local / "BraveSoftware" / "Brave-Browser" / "User Data", home / ".config" / "BraveSoftware"],
        "opera": [roam / "Opera Software" / "Opera Stable", home / ".config" / "opera"],
        "vivaldi": [local / "Vivaldi" / "User Data", home / ".config" / "vivaldi"],
        "chromium": [local / "Chromium" / "User Data", home / ".config" / "chromium"],
    }


def detect_browsers() -> List[str]:
    """Names of browsers that have a profile folder on this PC (Firefox first: most reliable)."""
    found = [name for name, paths in _dirs().items() if any(p.exists() for p in paths)]
    return sorted(found, key=lambda n: (n not in RELIABLE, n))


def cookie_spec(browser: str, cookies_file: str) -> str:
    """Single string used by the yt-dlp layer: '' | 'firefox' | 'file:<path>'."""
    if cookies_file and Path(cookies_file).is_file():
        return "file:" + cookies_file
    return browser if browser in BROWSER_LABELS else ""


def describe(browser: str, cookies_file: str) -> str:
    spec = cookie_spec(browser, cookies_file)
    if spec.startswith("file:"):
        return "cookies.txt"
    return BROWSER_LABELS.get(spec, "Off")


SIGNED_IN_NAMES = {"SAPISID", "__Secure-3PSID", "__Secure-1PSID", "SID", "LOGIN_INFO"}


def check_cookies(spec: str) -> Tuple[bool, str]:
    """Try to read the cookie source and say whether a YouTube/Google sign-in appears present.

    Only cookie *names* are inspected; values are never read out or displayed.
    """
    if not spec:
        return False, "No sign-in source selected."
    try:
        if spec.startswith("file:"):
            from http.cookiejar import MozillaCookieJar
            jar = MozillaCookieJar(spec[5:])
            jar.load(ignore_discard=True, ignore_expires=True)
        else:
            from yt_dlp.cookies import extract_cookies_from_browser
            jar = extract_cookies_from_browser(spec)
    except Exception as exc:  # noqa: BLE001 - yt-dlp raises many types
        return False, explain_cookie_error(str(exc), spec)
    yt = [c for c in jar if "youtube.com" in (c.domain or "") or "google.com" in (c.domain or "")]
    if not yt:
        return False, "Cookies were read, but none for YouTube/Google. Open YouTube in that browser and sign in first."
    if any(c.name in SIGNED_IN_NAMES for c in yt):
        return True, f"OK - read {len(yt)} YouTube/Google cookies and a signed-in session was found."
    return False, (f"Read {len(yt)} cookies but no signed-in session was found. "
                   "Sign in to YouTube in that browser, then test again.")


def explain_cookie_error(raw: str, spec: str = "") -> str:
    t = raw.lower()
    if any(k in t for k in ("could not copy", "database is locked", "locked", "permission denied", "being used")):
        return ("The browser's cookie file is locked. Close that browser completely (also in the system tray) and try again, "
                "or use Firefox, or export a cookies.txt file.")
    if any(k in t for k in ("dpapi", "decrypt", "app-bound", "app bound", "keyring")):
        return ("This browser encrypts its cookies in a way that cannot be read (common with newer Chrome/Edge). "
                "Use Firefox, or export a cookies.txt file from your browser and choose it here.")
    if "could not find" in t and "cookie" in t:
        return "No cookie database was found for that browser. Is it installed and have you used it?"
    return "Could not read cookies: " + (raw.strip().splitlines()[0][:200] if raw.strip() else "unknown error")


# ---- error classification ------------------------------------------------------------------
def classify(raw: str) -> str:
    """Return a hint for what could fix a yt-dlp error: 'cookies' | 'js' | 'signin' | 'update' | ''."""
    t = raw.lower()
    if any(k in t for k in ("could not copy", "dpapi", "failed to decrypt", "cookie database", "cookies from browser")):
        return "cookies"
    if any(k in t for k in ("javascript runtime", "js runtime", "no supported javascript", "challenge solving",
                            "n challenge", "nsig", "signature solving", "ejs")):
        return "js"
    if any(k in t for k in ("sign in", "not a bot", "log in", "login", "age-restricted", "confirm your age",
                            "members-only", "members only", "private video", "video is private", "is private",
                            "login required", "requires login", "registered users", "authentication", "checkpoint",
                            "http error 403", "cookies")):
        return "signin"
    if any(k in t for k in ("needs to be reloaded", "unable to extract", "unsupported url", "video unavailable",
                            "nsig extraction", "http error 400")):
        return "update"
    return ""

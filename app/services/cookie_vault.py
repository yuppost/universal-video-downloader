"""Cookie vault: sign-in cookies collected per site, used automatically by yt-dlp.

* Each site has one *entry* (``cookies/<id>.txt`` in Netscape format + a line in ``cookies/index.json``).
* ``cookiefile_for(url)`` writes a temporary file holding only the cookies of the site that *url* belongs to,
  so a YouTube download never sends Facebook cookies and vice versa.
* Values are never logged or shown. Files are created with owner-only permissions where the OS supports it.
  They live in the app's data folder (``%APPDATA%\\UniversalVideoStudio\\cookies``); anyone with access to your
  Windows account can read them, exactly like the cookie store of a browser - remove an entry to delete it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit

log = logging.getLogger("uvs.cookies")

# site key -> hostnames whose cookies belong to the same account/sign-in
SITE_GROUPS: Dict[str, List[str]] = {
    "youtube.com": ["youtube.com", "youtu.be", "google.com", "googlevideo.com", "ytimg.com", "youtube-nocookie.com"],
    "facebook.com": ["facebook.com", "fb.com", "fb.watch", "fbcdn.net", "messenger.com"],
    "instagram.com": ["instagram.com", "cdninstagram.com"],
    "x.com": ["x.com", "twitter.com", "twimg.com"],
    "tiktok.com": ["tiktok.com", "tiktokcdn.com", "tiktokv.com"],
    "vimeo.com": ["vimeo.com", "vimeocdn.com"],
    "twitch.tv": ["twitch.tv"],
    "reddit.com": ["reddit.com", "redd.it", "redditmedia.com"],
}
SITE_LABELS: Dict[str, str] = {"youtube.com": "YouTube / Google", "facebook.com": "Facebook", "instagram.com": "Instagram",
                               "x.com": "X (Twitter)", "tiktok.com": "TikTok", "vimeo.com": "Vimeo",
                               "twitch.tv": "Twitch", "reddit.com": "Reddit"}
LOGIN_URLS: Dict[str, str] = {
    "youtube.com": "https://accounts.google.com/ServiceLogin?service=youtube&continue=https%3A%2F%2Fwww.youtube.com%2F",
    "facebook.com": "https://www.facebook.com/login",
    "instagram.com": "https://www.instagram.com/accounts/login/",
    "x.com": "https://x.com/i/flow/login",
    "tiktok.com": "https://www.tiktok.com/login",
    "vimeo.com": "https://vimeo.com/log_in",
    "twitch.tv": "https://www.twitch.tv/login",
    "reddit.com": "https://www.reddit.com/login/",
}
SESSION_LIFETIME = 30 * 86400          # session cookies get this expiry in the file (yt-dlp ignores expired lines)
_SECOND_LEVEL = {"co", "com", "org", "net", "gov", "ac", "edu", "or", "ne"}


@dataclass
class CookieRec:
    domain: str
    path: str
    name: str
    value: str
    secure: bool = False
    expires: int = 0          # unix seconds; 0 = session cookie
    http_only: bool = False


@dataclass
class VaultEntry:
    id: str
    site: str                 # site key, e.g. "youtube.com"
    label: str
    source: str               # "app-browser" | "browser:firefox" | "file"
    count: int = 0
    updated: float = 0.0
    expires: float = 0.0      # latest expiry among persistent cookies (0 = only session cookies)
    domains: List[str] = field(default_factory=list)
    signed_in: Optional[bool] = None   # known for YouTube, Facebook, Instagram, X, TikTok, Reddit, Twitch

    @property
    def expired(self) -> bool:
        return bool(self.expires) and self.expires < time.time()


# ---------------------------------------------------------------- site helpers
def host_of(url_or_host: str) -> str:
    s = (url_or_host or "").strip()
    host = urlsplit(s if "://" in s else "//" + s).hostname or ""
    return host.lower().rstrip(".")


def _root_domain(host: str) -> str:
    parts = host.split(".")
    if len(parts) <= 2 or re.fullmatch(r"[0-9.]+", host) or ":" in host:      # also IPs and "localhost"
        return host
    if len(parts[-1]) == 2 and parts[-2] in _SECOND_LEVEL and len(parts) >= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _in(host: str, domain: str) -> bool:
    domain = domain.lstrip(".").lower()
    return host == domain or host.endswith("." + domain)


def site_key(url_or_host: str) -> str:
    """Canonical key for the site a URL/host belongs to (aliases collapse, e.g. youtu.be -> youtube.com)."""
    host = host_of(url_or_host)
    if not host:
        return ""
    for key, hosts in SITE_GROUPS.items():
        if any(_in(host, h) for h in hosts):
            return key
    return _root_domain(host)


def site_domains(key: str) -> List[str]:
    return list(SITE_GROUPS.get(key, [key]))


def site_label(key: str) -> str:
    return SITE_LABELS.get(key, key)


def login_url_for(url_or_host: str) -> str:
    """Where the in-app browser should start so the user can sign in for this site."""
    key = site_key(url_or_host)
    if key in LOGIN_URLS:
        return LOGIN_URLS[key]
    s = (url_or_host or "").strip()
    if s.lower().startswith(("http://", "https://")):
        return s
    return "https://" + (host_of(s) or s)


# ---------------------------------------------------------------- Netscape format
_FORBIDDEN = re.compile(r"[\t\r\n]")


def write_netscape(path: Path, cookies: Iterable[CookieRec]) -> int:
    lines = ["# Netscape HTTP Cookie File", "# Written by Universal Video Studio. Keep this file private.", ""]
    n = 0
    for c in cookies:
        if _FORBIDDEN.search(c.name) or _FORBIDDEN.search(c.value) or not c.domain or not c.name:
            continue
        exp = c.expires if c.expires > 0 else int(time.time()) + SESSION_LIFETIME
        sub = "TRUE" if c.domain.startswith(".") else "FALSE"
        prefix = "#HttpOnly_" if c.http_only else ""
        lines.append("\t".join([prefix + c.domain, sub, c.path or "/", "TRUE" if c.secure else "FALSE",
                                str(int(exp)), c.name, c.value]))
        n += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    return n


def read_netscape(path: Path) -> List[CookieRec]:
    """Tolerant cookies.txt reader (also understands ``#HttpOnly_`` lines)."""
    out: List[CookieRec] = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.rstrip("\r\n")
            http_only = False
            if line.startswith("#HttpOnly_"):
                http_only, line = True, line[len("#HttpOnly_"):]
            elif not line.strip() or line.lstrip().startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) != 7:
                continue
            domain, _sub, cpath, secure, exp, name, value = parts
            try:
                expires = int(float(exp or 0))
            except ValueError:
                expires = 0
            out.append(CookieRec(domain, cpath or "/", name, value, secure.upper() == "TRUE", expires, http_only))
    return out


def filter_for_site(cookies: Iterable[CookieRec], key: str) -> List[CookieRec]:
    """Keep only the cookies that belong to the site's domains (drops trackers/ad domains)."""
    doms = site_domains(key)
    return [c for c in cookies if any(_in(c.domain.lstrip(".").lower(), d) for d in doms)]


SIGNED_IN_NAMES = {"SAPISID", "__Secure-3PSID", "__Secure-1PSID", "SID", "LOGIN_INFO"}

# Cookie names that only exist once the user is really signed in (names only - values are never inspected).
# Each tuple: every name in it must be present; the site counts as signed in if ANY tuple is satisfied.
LOGIN_COOKIES: Dict[str, List[Tuple[str, ...]]] = {
    "youtube.com": [("SAPISID",), ("__Secure-3PSID",), ("__Secure-1PSID",), ("SID",), ("LOGIN_INFO",)],
    "facebook.com": [("c_user", "xs")],
    "instagram.com": [("sessionid",)],
    "x.com": [("auth_token",)],
    "tiktok.com": [("sessionid",), ("sessionid_ss",), ("sid_tt",)],
    "reddit.com": [("reddit_session",), ("token_v2",)],
    "twitch.tv": [("auth-token",)],
}


def login_state(key: str, names: Iterable[str]) -> Optional[bool]:
    """True/False whether a sign-in is present for sites we can recognise, None for other sites."""
    rules = LOGIN_COOKIES.get(key)
    if not rules:
        return None
    have = set(names)
    return any(all(n in have for n in rule) for rule in rules)


def missing_login_cookies(key: str) -> str:
    rules = LOGIN_COOKIES.get(key)
    return " + ".join(rules[0]) if rules else ""


# ---------------------------------------------------------------- the vault
class CookieVault:
    def __init__(self, root: Optional[Path] = None) -> None:
        if root is None:
            from app.utils.paths import app_data_dir
            root = app_data_dir() / "cookies"
        self.root = Path(root)
        self._lock = threading.RLock()
        self.root.mkdir(parents=True, exist_ok=True)
        self.clean_runtime()

    # --- index
    @property
    def _index(self) -> Path:
        return self.root / "index.json"

    def _load_index(self) -> Dict[str, dict]:
        try:
            data = json.loads(self._index.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_index(self, data: Dict[str, dict]) -> None:
        tmp = self._index.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self._index)

    def _file(self, entry_id: str) -> Path:
        return self.root / f"{entry_id}.txt"

    @staticmethod
    def _entry_id(key: str) -> str:
        return re.sub(r"[^a-z0-9._-]+", "_", key.lower()).strip("._") or "site"

    # --- reading
    def entries(self) -> List[VaultEntry]:
        with self._lock:
            idx = self._load_index()
        out: List[VaultEntry] = []
        for eid, d in idx.items():
            if not self._file(eid).is_file():
                continue
            site = d.get("site", eid)
            signed = d.get("signed_in")
            if site in LOGIN_COOKIES:                      # recomputed from the file (older entries lack it)
                try:
                    signed = login_state(site, (c.name for c in read_netscape(self._file(eid))))
                except OSError:
                    pass
            out.append(VaultEntry(eid, site, d.get("label", eid), d.get("source", ""),
                                  int(d.get("count", 0)), float(d.get("updated", 0)), float(d.get("expires", 0)),
                                  list(d.get("domains", [])), signed))
        return sorted(out, key=lambda e: e.label.lower())

    def entry_for(self, url_or_host: str) -> Optional[VaultEntry]:
        key = site_key(url_or_host)
        for e in self.entries():
            if e.site == key:
                return e
        return None

    def has_cookies_for(self, url_or_host: str) -> bool:
        return self.entry_for(url_or_host) is not None

    def cookies(self, entry_id: str) -> List[CookieRec]:
        try:
            return read_netscape(self._file(entry_id))
        except OSError:
            return []

    # --- writing
    def save(self, key: str, cookies: Iterable[CookieRec], source: str, label: str = "") -> VaultEntry:
        """Create or replace the entry for site *key*. Only cookies of that site's domains are kept."""
        kept = filter_for_site(cookies, key) if key in SITE_GROUPS or "." in key else list(cookies)
        if not kept:
            raise ValueError("No cookies for this site were found - sign in first.")
        eid = self._entry_id(key)
        self._write_entry(eid, key, kept, source, label or site_label(key))
        log.info("cookie vault: saved %d cookies for %s (%s)", len(kept), key, source)
        return next(e for e in self.entries() if e.id == eid)

    def _write_entry(self, eid: str, key: str, kept: List[CookieRec], source: str, label: str) -> None:
        with self._lock:
            n = write_netscape(self._file(eid), kept)
            persistent = [c.expires for c in kept if c.expires > 0]
            names = {c.name for c in kept}
            idx = self._load_index()
            idx[eid] = {"site": key, "label": label, "source": source, "count": n,
                        "updated": time.time(), "expires": float(max(persistent)) if persistent else 0.0,
                        "domains": sorted({c.domain.lstrip(".") for c in kept})[:12],
                        "signed_in": login_state(key, names)}
            self._save_index(idx)

    def delete_cookies(self, entry_id: str, keys: Iterable[Tuple[str, str, str]]) -> int:
        """Delete single cookies, given as (domain, path, name). Removes the entry when nothing is left."""
        recs = self.cookies(entry_id)
        drop = set(keys)
        keep = [c for c in recs if (c.domain, c.path, c.name) not in drop]
        removed = len(recs) - len(keep)
        if not removed:
            return 0
        if not keep:
            self.remove(entry_id)
            return removed
        with self._lock:
            d = self._load_index().get(entry_id, {})
        self._write_entry(entry_id, d.get("site", entry_id), keep, d.get("source", "file"), d.get("label", entry_id))
        return removed

    def import_netscape(self, path: Path, key: str, source: str = "file") -> VaultEntry:
        recs = read_netscape(Path(path))
        if not recs:
            raise ValueError("That file contains no cookies (expected the Netscape cookies.txt format).")
        return self.save(key, recs, source)

    def import_from_browser(self, browser: str, key: str) -> VaultEntry:
        from yt_dlp.cookies import extract_cookies_from_browser
        jar = extract_cookies_from_browser(browser)
        recs = [CookieRec(c.domain, c.path or "/", c.name, c.value or "", bool(c.secure), int(c.expires or 0))
                for c in jar if c.name and c.value is not None]
        return self.save(key, recs, f"browser:{browser}")

    def remove(self, entry_id: str) -> None:
        with self._lock:
            idx = self._load_index()
            idx.pop(entry_id, None)
            self._save_index(idx)
            try:
                self._file(entry_id).unlink()
            except OSError:
                pass

    # --- use by yt-dlp
    def cookiefile_for(self, url: str) -> Optional[Path]:
        """A private temporary cookies.txt with only this site's cookies, or None if we have none."""
        e = self.entry_for(url)
        if not e:
            return None
        recs = self.cookies(e.id)
        if not recs:
            return None
        run = self.root / "_run"
        run.mkdir(exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f"{e.id}-", suffix=".txt", dir=run)
        os.close(fd)
        write_netscape(Path(name), recs)
        return Path(name)

    def clean_runtime(self) -> None:
        run = self.root / "_run"
        if run.is_dir():
            for f in run.glob("*.txt"):
                try:
                    f.unlink()
                except OSError:
                    pass


_vault: Optional[CookieVault] = None
_vault_lock = threading.Lock()


def get_vault() -> CookieVault:
    global _vault
    with _vault_lock:
        if _vault is None:
            _vault = CookieVault()
        return _vault


def set_vault(vault: Optional[CookieVault]) -> None:
    """Replace the shared vault (tests)."""
    global _vault
    with _vault_lock:
        _vault = vault

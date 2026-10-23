"""In-app browser window used to sign in to a site; the cookies it receives go into the cookie vault.

The window uses a private (off-the-record) Chromium profile: nothing is kept on disk by the browser, and
once the sign-in is saved the profile is thrown away, so the browser never refreshes the saved session.
The app never sees or stores your password - only the cookies the site sets after you log in.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget)

from app.services import cookie_vault as cv

log = logging.getLogger("uvs.login")
# Some sites (Google especially) refuse sign-in from anything that calls itself an embedded browser.
CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
             "Chrome/124.0.0.0 Safari/537.36")


def webengine_available() -> bool:
    try:
        from PySide6 import QtWebEngineCore, QtWebEngineWidgets  # noqa: F401
        return True
    except ImportError:
        return False


QUICK_SITES = [("YouTube", "youtube.com"), ("Facebook", "facebook.com"), ("Instagram", "instagram.com"),
               ("TikTok", "tiktok.com"), ("X", "x.com"), ("Vimeo", "vimeo.com")]


def normalize_address(text: str) -> str:
    """What the user typed in the address bar -> a URL ('' if nothing usable)."""
    t = (text or "").strip()
    if not t or " " in t:
        return ""
    if "://" not in t:
        if re.match(r"^[a-z][a-z0-9+.\-]*:(?!\d+(/|$))", t, re.I):      # javascript:, data:, mailto: ... are not websites
            return ""
        t = "https://" + t
    return t if t.lower().startswith(("http://", "https://")) else ""


class BrowserLoginDialog(QDialog):
    """A small browser: open any site, sign in, then press "Save sign-in" to store that site's cookies in the vault.

    *start_url* may be empty (blank page with quick links) or a site/login address."""

    def __init__(self, start_url: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
        from PySide6.QtWebEngineWidgets import QWebEngineView

        self.start_url = start_url
        self.entry: Optional[cv.VaultEntry] = None
        self._cookies: Dict[Tuple[str, str, str], cv.CookieRec] = {}
        self.setWindowTitle("Sign in with the browser window")
        self.resize(1000, 740)

        self.profile = QWebEngineProfile(self)               # no storage name -> private, nothing persisted
        self.profile.setHttpUserAgent(CHROME_UA)
        self.page = QWebEnginePage(self.profile, self)
        self.view = QWebEngineView(self)
        self.view.setPage(self.page)
        store = self.profile.cookieStore()
        store.cookieAdded.connect(self._added)
        store.cookieRemoved.connect(self._removed)
        store.loadAllCookies()
        self.restored = self._restore_saved_cookies(store)      # saved sign-ins are still signed in next time

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        hint = QLabel("Open any website below, sign in like in a normal browser (2-step verification, age confirmation, "
                      "etc.), then press <b>Save sign-in</b>. The app keeps only the login cookies of that site "
                      "(not your password) and reuses them for downloads.")
        hint.setTextFormat(Qt.RichText)
        hint.setWordWrap(True)
        lay.addWidget(hint)
        quick = QHBoxLayout()
        quick.addWidget(QLabel("Quick open:"))
        for label, key in QUICK_SITES:
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, k=key: self.open_address(cv.login_url_for(k)))
            quick.addWidget(b)
        quick.addStretch(1)
        lay.addLayout(quick)
        bar = QHBoxLayout()
        self.btn_back, self.btn_fwd = QPushButton("<"), QPushButton(">")
        self.btn_reload = QPushButton("Reload")
        self.addr = QLineEdit()
        self.addr.setPlaceholderText("Type any website address, for example facebook.com, and press Enter")
        for b in (self.btn_back, self.btn_fwd):
            b.setFixedWidth(34)
        bar.addWidget(self.btn_back)
        bar.addWidget(self.btn_fwd)
        bar.addWidget(self.btn_reload)
        bar.addWidget(self.addr, 1)
        lay.addLayout(bar)
        lay.addWidget(self.view, 1)
        foot = QHBoxLayout()
        self.status = QLabel("")
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        self.btn_save = QPushButton("Save sign-in")
        self.btn_save.setObjectName("primary")
        self.btn_cancel = QPushButton("Cancel")
        foot.addWidget(self.status, 1)
        foot.addWidget(self.btn_cancel)
        foot.addWidget(self.btn_save)
        lay.addLayout(foot)

        self.btn_back.clicked.connect(self.view.back)
        self.btn_fwd.clicked.connect(self.view.forward)
        self.btn_reload.clicked.connect(self.view.reload)
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_save.clicked.connect(self._save_clicked)
        self.addr.returnPressed.connect(lambda: self.open_address(self.addr.text()))
        self.view.urlChanged.connect(self._url_changed)
        self.view.loadFinished.connect(lambda _ok: self._refresh_status())
        if start_url:
            first = cv.login_url_for(start_url)
            # Give Chromium a moment to take the restored cookies before the first request is made.
            QTimer.singleShot(250 if self.restored else 0, lambda: self.open_address(first))
        self._refresh_status()

    # --- saved sign-ins -> browser
    @staticmethod
    def _to_qt_cookie(rec: cv.CookieRec):
        from PySide6.QtCore import QDateTime
        from PySide6.QtNetwork import QNetworkCookie
        c = QNetworkCookie(rec.name.encode("utf-8"), rec.value.encode("utf-8"))
        if rec.domain.startswith("."):
            c.setDomain(rec.domain)                  # domain cookie; host-only cookies get their host from the origin URL
        c.setPath(rec.path or "/")
        c.setSecure(rec.secure)
        c.setHttpOnly(rec.http_only)
        if rec.expires > 0:
            c.setExpirationDate(QDateTime.fromSecsSinceEpoch(int(rec.expires)))
        return c

    def _restore_saved_cookies(self, store) -> int:
        """Load every cookie saved in the vault into this private browser so earlier sign-ins are still active."""
        n = 0
        try:
            vault = cv.get_vault()
            for e in vault.entries():
                for rec in vault.cookies(e.id):
                    host = rec.domain.lstrip(".")
                    if not host or not rec.name:
                        continue
                    store.setCookie(self._to_qt_cookie(rec), QUrl(f"https://{host}{rec.path or '/'}"))
                    n += 1
        except Exception:  # noqa: BLE001 - a vault problem must never stop the browser from opening
            log.exception("could not restore saved cookies into the browser")
        return n

    # --- navigation
    def open_address(self, text: str) -> None:
        url = normalize_address(text)
        if url:
            self.view.load(QUrl(url))

    def _url_changed(self, u) -> None:
        self.addr.setText(u.toString())
        self._refresh_status()

    @property
    def site(self) -> str:
        """The site the browser is on right now: what "Save sign-in" would store."""
        return cv.site_key(self.view.url().toString()) or cv.site_key(self.start_url)

    # --- cookie bookkeeping
    def _rec(self, c) -> cv.CookieRec:
        exp = 0 if c.isSessionCookie() else int(c.expirationDate().toSecsSinceEpoch())
        return cv.CookieRec(str(c.domain()), c.path() or "/", bytes(c.name()).decode("utf-8", "replace"),
                            bytes(c.value()).decode("utf-8", "replace"), c.isSecure(), exp, c.isHttpOnly())

    def _added(self, c) -> None:
        r = self._rec(c)
        self._cookies[(r.domain, r.path, r.name)] = r

    def _removed(self, c) -> None:
        pass          # Chromium reports an overwritten cookie as removed+added in no fixed order, so removals are ignored

    def site_cookies(self) -> List[cv.CookieRec]:
        return cv.filter_for_site(self._cookies.values(), self.site) if self.site else []

    def signed_in(self) -> Optional[bool]:
        """True/False for sites with a known login cookie (YouTube, Facebook, Instagram, X, ...), else None."""
        return cv.login_state(self.site, (c.name for c in self.site_cookies())) if self.site else None

    def _refresh_status(self) -> None:
        if not self.site:
            self.status.setText("Open a website to sign in.")
            self.btn_save.setEnabled(False)
            self.btn_save.setText("Save sign-in")
            return
        n = len(self.site_cookies())
        si = self.signed_in()
        label = cv.site_label(self.site)
        if si:
            self.status.setText(f"Signed in to {label} detected ({n} cookies). Press Save sign-in.")
        elif si is False:
            self.status.setText(f"{label}: not signed in yet ({n} cookies) - finish signing in first "
                                f"(login cookie: {cv.missing_login_cookies(self.site)}).")
        else:
            self.status.setText(f"{label}: {n} cookies received. Sign in, then press Save sign-in.")
        self.btn_save.setText(f"Save sign-in for {label}")
        self.btn_save.setEnabled(n > 0)

    # --- finish
    def _save_clicked(self) -> None:
        """Button handler: warn before storing a session that has no login in it."""
        if self.signed_in() is False:
            from PySide6.QtWidgets import QMessageBox
            label = cv.site_label(self.site)
            if QMessageBox.question(
                    self, "Not signed in yet",
                    f"No {label} login was detected in this window, so downloads that need a login will still fail.\n\n"
                    "Finish signing in first (including 2-step verification or 'save browser' prompts).\n\n"
                    "Save anyway?", QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                return
        self.save()

    def save(self) -> None:
        try:
            self.entry = cv.get_vault().save(self.site, self.site_cookies(), "app-browser")
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        self.accept()

    def done(self, r: int) -> None:  # noqa: D401 - Qt override
        # Delete the page before its profile, otherwise Qt warns about a still-referenced profile.
        try:
            self.page.deleteLater()
        except RuntimeError:
            pass
        super().done(r)


def sign_in(url_or_site: str = "", parent: Optional[QWidget] = None) -> Optional[cv.VaultEntry]:
    """Open the sign-in window for the site of *url_or_site*. Returns the saved entry, or None if cancelled."""
    from PySide6.QtWidgets import QMessageBox
    if not webengine_available():
        QMessageBox.warning(parent, "Browser component missing",
                            "The built-in browser (QtWebEngine) is not installed.\n\nRun:  pip install PySide6-Addons\n"
                            "or use 'Import from browser' / 'Import cookies.txt' in the Cookie manager.")
        return None
    dlg = BrowserLoginDialog(url_or_site, parent)
    return dlg.entry if dlg.exec() == QDialog.Accepted else None

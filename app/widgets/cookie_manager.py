"""Cookie manager: every saved sign-in, how to add one for ANY website, and a viewer to inspect/delete cookies."""

from __future__ import annotations

import time
from pathlib import Path
from typing import List, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QFileDialog, QGridLayout, QHBoxLayout, QHeaderView,
                               QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from app.services import cookie_vault as cv
from app.services import signin_service as ss
from app.widgets import browser_login

QUICK = [("YouTube / Google", "youtube.com"), ("Facebook", "facebook.com"), ("Instagram", "instagram.com"),
         ("TikTok", "tiktok.com"), ("X (Twitter)", "x.com"), ("Vimeo", "vimeo.com"), ("Twitch", "twitch.tv"),
         ("Reddit", "reddit.com")]


def _when(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "-"


def ask_site(parent: QWidget, text: str) -> str:
    """Let the user pick a known site or type any address; returns the site key ('' if cancelled)."""
    items = [f"{label}  ({key})" for label, key in QUICK]
    pick, ok = QInputDialog.getItem(parent, "Which website?", text + "\n(choose one, or type any website address)",
                                    items, 0, True)
    if not ok or not pick.strip():
        return ""
    for (label, key), shown in zip(QUICK, items):
        if pick == shown:
            return key
    return cv.site_key(pick.strip())


class CookieViewDialog(QDialog):
    """Lists the cookies saved for one site; values are hidden unless you tick *Show values*. Cookies can be deleted."""

    COLS = ["Domain", "Name", "Value", "Expires", "Flags"]

    def __init__(self, entry_id: str, label: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.entry_id = entry_id
        self.vault = cv.get_vault()
        self.setWindowTitle(f"Saved cookies - {label}")
        self.resize(820, 480)
        lay = QVBoxLayout(self)
        note = QLabel("These are the cookies the app sends to this site while downloading. Treat the values like a password: "
                      "anyone who has them can use your login.")
        note.setWordWrap(True)
        note.setObjectName("muted")
        lay.addWidget(note)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        lay.addWidget(self.table, 1)
        row = QHBoxLayout()
        self.show_values = QCheckBox("Show values")
        self.btn_del = QPushButton("Delete selected cookies")
        self.btn_del.setObjectName("danger")
        self.btn_all = QPushButton("Delete all cookies of this site")
        self.btn_all.setObjectName("danger")
        btn_close = QPushButton("Close")
        row.addWidget(self.show_values)
        row.addStretch(1)
        row.addWidget(self.btn_del)
        row.addWidget(self.btn_all)
        row.addWidget(btn_close)
        lay.addLayout(row)
        self.show_values.toggled.connect(lambda _on: self.refresh())
        self.btn_del.clicked.connect(self._delete_selected)
        self.btn_all.clicked.connect(self._delete_all)
        btn_close.clicked.connect(self.accept)
        self.table.itemSelectionChanged.connect(lambda: self.btn_del.setEnabled(bool(self.table.selectedItems())))
        self.recs: List[cv.CookieRec] = []
        self.refresh()

    def refresh(self) -> None:
        self.recs = self.vault.cookies(self.entry_id)
        self.table.setRowCount(len(self.recs))
        for r, c in enumerate(self.recs):
            flags = ", ".join(f for f, on in (("Secure", c.secure), ("HttpOnly", c.http_only)) if on)
            exp = _when(c.expires) if c.expires else "Session"
            value = c.value if self.show_values.isChecked() else "*" * min(12, max(4, len(c.value)))
            for col, v in enumerate((c.domain, c.name, value, exp, flags)):
                self.table.setItem(r, col, QTableWidgetItem(v))
        self.btn_del.setEnabled(False)
        self.btn_all.setEnabled(bool(self.recs))

    def _delete_selected(self) -> None:
        rows = sorted({i.row() for i in self.table.selectedItems()})
        keys = [(self.recs[r].domain, self.recs[r].path, self.recs[r].name) for r in rows if r < len(self.recs)]
        if not keys:
            return
        if QMessageBox.question(self, "Delete cookies", f"Delete {len(keys)} cookie(s)?") != QMessageBox.Yes:
            return
        self.vault.delete_cookies(self.entry_id, keys)
        self.refresh()
        if not self.recs:
            self.accept()

    def _delete_all(self) -> None:
        if QMessageBox.question(self, "Delete all", "Delete every saved cookie of this site?") == QMessageBox.Yes:
            self.vault.remove(self.entry_id)
            self.accept()


class CookieManagerDialog(QDialog):
    COLS = ["Site", "Status", "Cookies", "Valid until", "Added", "Source"]

    def __init__(self, parent: Optional[QWidget] = None, preset_url: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("Cookies & sign-ins")
        self.resize(820, 600)
        self.vault = cv.get_vault()
        lay = QVBoxLayout(self)
        info = QLabel("Sign in once, and the app reuses the saved cookies for private videos, age-restricted videos and "
                      "'confirm you're not a bot' errors - without signing in again. Cookies are only sent to the site "
                      "they belong to, stay on this PC, and can be viewed and deleted below.")
        info.setWordWrap(True)
        info.setObjectName("muted")
        lay.addWidget(info)

        title = QLabel("1. Sign in to a website")
        title.setObjectName("panelTitle")
        lay.addWidget(title)
        grid = QGridLayout()
        grid.setSpacing(6)
        self.quick_buttons: List[QPushButton] = []
        for i, (label, key) in enumerate(QUICK):
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, k=key: self._sign_in(k))
            grid.addWidget(b, i // 4, i % 4)
            self.quick_buttons.append(b)
        lay.addLayout(grid)
        other = QHBoxLayout()
        self.addr = QLineEdit()
        self.addr.setPlaceholderText("Any other website, for example example.com")
        if preset_url:
            self.addr.setText(cv.host_of(preset_url) or preset_url)
        self.btn_other = QPushButton("Open browser window")
        self.btn_other.setObjectName("primary")
        other.addWidget(self.addr, 1)
        other.addWidget(self.btn_other)
        lay.addLayout(other)
        imp = QHBoxLayout()
        imp.addWidget(QLabel("Or import:"))
        self.btn_import = QPushButton("From my browser...")
        self.btn_file = QPushButton("From a cookies.txt file...")
        imp.addWidget(self.btn_import)
        imp.addWidget(self.btn_file)
        imp.addStretch(1)
        lay.addLayout(imp)

        title2 = QLabel("2. Saved sign-ins")
        title2.setObjectName("panelTitle")
        lay.addWidget(title2)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        lay.addWidget(self.table, 1)
        self.empty = QLabel("No sign-ins saved yet. Use a button above to sign in to a website.")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setObjectName("muted")
        lay.addWidget(self.empty)

        foot = QHBoxLayout()
        self.btn_view = QPushButton("View / delete cookies...")
        self.btn_resign = QPushButton("Sign in again")
        self.btn_remove = QPushButton("Remove site")
        self.btn_remove.setObjectName("danger")
        self.btn_clear_all = QPushButton("Remove everything")
        self.btn_clear_all.setObjectName("danger")
        btn_close = QPushButton("Close")
        for b in (self.btn_view, self.btn_resign, self.btn_remove, self.btn_clear_all):
            foot.addWidget(b)
        foot.addStretch(1)
        foot.addWidget(btn_close)
        lay.addLayout(foot)
        self.hint = QLabel("Select a site in the list to view or delete its cookies.")
        self.hint.setObjectName("muted")
        lay.addWidget(self.hint)

        self.btn_other.clicked.connect(self._sign_in_typed)
        self.addr.returnPressed.connect(self._sign_in_typed)
        self.btn_import.clicked.connect(self._import_browser)
        self.btn_file.clicked.connect(self._import_file)
        self.btn_view.clicked.connect(self._view)
        self.btn_resign.clicked.connect(self._resign)
        self.btn_remove.clicked.connect(self._remove)
        self.btn_clear_all.clicked.connect(self._remove_all)
        self.table.cellDoubleClicked.connect(lambda *_: self._view())
        btn_close.clicked.connect(self.accept)
        self.table.itemSelectionChanged.connect(self._buttons)
        self.refresh()

    # --- table
    def refresh(self) -> None:
        entries = self.vault.entries()
        self.table.setRowCount(len(entries))
        for r, e in enumerate(entries):
            if e.expired:
                status = "Expired - sign in again"
            elif e.signed_in is False:
                status = "No login found"
            else:
                status = "Active" if e.signed_in is None else "Signed in"
            vals = [e.label + (f"  ({e.site})" if e.label != e.site else ""), status, str(e.count),
                    _when(e.expires) if e.expires else "Session", _when(e.updated), self._src(e.source)]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if c == 0:
                    it.setData(Qt.UserRole, e.id)
                    it.setData(Qt.UserRole + 1, e.site)
                    it.setData(Qt.UserRole + 2, e.label)
                self.table.setItem(r, c, it)
        self.empty.setVisible(not entries)
        self.btn_clear_all.setEnabled(bool(entries))
        self._buttons()

    @staticmethod
    def _src(s: str) -> str:
        return {"app-browser": "Browser window", "file": "cookies.txt"}.get(s, s.replace("browser:", "Imported from "))

    def _selected(self) -> Tuple[Optional[str], Optional[str], str]:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None, None, ""
        it = self.table.item(rows[0].row(), 0)
        return it.data(Qt.UserRole), it.data(Qt.UserRole + 1), it.data(Qt.UserRole + 2) or ""

    def _buttons(self) -> None:
        sel, _site, _l = self._selected()
        for b in (self.btn_view, self.btn_resign, self.btn_remove):
            b.setEnabled(sel is not None)
        self.hint.setVisible(sel is None and self.table.rowCount() > 0)

    # --- actions
    def _sign_in(self, site_or_url: str) -> None:
        browser_login.sign_in(site_or_url, self)
        self.refresh()

    def _sign_in_typed(self) -> None:
        text = self.addr.text().strip()
        self._sign_in(text)          # empty = a blank browser with quick links; anything else opens that site

    def _resign(self) -> None:
        _id, site, _l = self._selected()
        if site:
            self._sign_in(site)

    def _view(self) -> None:
        eid, _site, label = self._selected()
        if eid:
            CookieViewDialog(eid, label, self).exec()
            self.refresh()

    def _remove(self) -> None:
        eid, _site, _l = self._selected()
        if eid and QMessageBox.question(self, "Remove sign-in", "Delete all saved cookies for this site?") == QMessageBox.Yes:
            self.vault.remove(eid)
            self.refresh()

    def _remove_all(self) -> None:
        if QMessageBox.question(self, "Remove everything", "Delete the saved cookies of ALL sites?") == QMessageBox.Yes:
            for e in self.vault.entries():
                self.vault.remove(e.id)
            self.refresh()

    def _import_browser(self) -> None:
        site = ask_site(self, "Import the login of which website?")
        if not site:
            return
        found = ss.detect_browsers()
        if not found:
            QMessageBox.information(self, "No browser found", "No supported browser profile was found on this PC.")
            return
        names = [ss.BROWSER_LABELS[b] + (" (recommended)" if b in ss.RELIABLE else "") for b in found]
        pick, ok = QInputDialog.getItem(self, "Import from browser", f"Read the {cv.site_label(site)} login from which "
                                        "browser?\n(Close that browser first; Chrome/Edge may fail.)", names, 0, False)
        if not ok:
            return
        browser = found[names.index(pick)]
        try:
            e = self.vault.import_from_browser(browser, site)
        except ValueError as exc:
            QMessageBox.warning(self, "Nothing imported", str(exc))
        except Exception as exc:  # noqa: BLE001 - yt-dlp raises many types
            QMessageBox.warning(self, "Could not read the browser", ss.explain_cookie_error(str(exc), browser))
        else:
            QMessageBox.information(self, "Imported", f"Saved {e.count} cookies for {e.label}.")
        self.refresh()

    def _import_file(self) -> None:
        site = ask_site(self, "This cookies.txt belongs to which website?")
        if not site:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Choose cookies.txt", str(Path.home()), "Cookies (*.txt);;All files (*)")
        if not path:
            return
        try:
            e = self.vault.import_netscape(Path(path), site)
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "Nothing imported", str(exc))
        else:
            QMessageBox.information(self, "Imported", f"Saved {e.count} cookies for {e.label}.")
        self.refresh()

"""Plasma tray icon for the Qt window.

Closing the window hides it. Quit, from the tray menu, stops the status
process and exits. The icon is idle, syncing, or error from the sync
database. It does not call the Proton CLI.
"""

import os

import cloudstatus
import syncdb
from ui.status_service import StatusService, close_action

try:
    from i18n import _
except ImportError:
    def _(text):
        return text

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _icon_file(kind):
    names = {
        "idle": "tray_connected.png",
        "syncing": "tray_scripts.png",
        "error": "tray_expired.png",
        "stopped": "tray_stopped.png",
    }
    return os.path.join(APP_DIR, names.get(kind, "tray_stopped.png"))


def tray_kind(rows):
    """idle, syncing, or error. An empty database is idle."""
    code, _details = cloudstatus.account_status(rows)
    if code == cloudstatus.STATUS_ERROR:
        return "error"
    if code == cloudstatus.STATUS_SYNCING:
        return "syncing"
    return "idle"


def tray_rows(mappings_path):
    if not mappings_path or not os.path.isfile(mappings_path):
        return None
    try:
        database = syncdb.database_path(mappings_path)
        if not os.path.isfile(database):
            return []
        with syncdb.SyncDB(database) as db:
            return db.rows()
    except (OSError, ValueError):
        return None


class Tray:
    def __init__(self, window):
        from PySide6.QtCore import QTimer
        from PySide6.QtGui import QIcon
        from PySide6.QtWidgets import QMenu, QSystemTrayIcon
        self.window = window
        self.service = StatusService()
        from PySide6.QtWidgets import QApplication
        # Parent is the application, not the window. Hiding the window would
        # otherwise drop the icon with it, and the process would keep running.
        self._icon = QSystemTrayIcon(QApplication.instance())
        self._icon.setIcon(QIcon(_icon_file("stopped")))
        menu = QMenu()
        menu.addAction(_("Open"), self._open)
        menu.addAction(_("Quit"), self._quit)
        self._icon.setContextMenu(menu)
        self._icon.activated.connect(self._activated)
        self._ready = False
        QTimer.singleShot(0, self._show_when_ready)
        self._timer = QTimer(QApplication.instance())
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.refresh()

    def _show_when_ready(self):
        from PySide6.QtWidgets import QSystemTrayIcon
        self._ready = QSystemTrayIcon.isSystemTrayAvailable()
        if self._ready:
            self._icon.show()

    def refresh(self):
        from PySide6.QtGui import QIcon
        path = self.window.doc.path
        self.service.ensure(path)
        rows = tray_rows(path)
        if rows is None:
            kind = "stopped"
            tip = _("Proton Drive Sync")
        else:
            kind = tray_kind(rows)
            tips = {
                "idle": _("Proton Drive Sync — synced"),
                "syncing": _("Proton Drive Sync — syncing"),
                "error": _("Proton Drive Sync — needs attention"),
            }
            tip = tips[kind]
        self._icon.setIcon(QIcon(_icon_file(kind)))
        self._icon.setToolTip(tip)

    def _open(self):
        self.window._qt.show()
        self.window._qt.raise_()
        self.window._qt.activateWindow()

    def _activated(self, reason):
        from PySide6.QtWidgets import QSystemTrayIcon
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._open()

    def _quit(self):
        self.window._quitting = True
        watchers = getattr(self.window, "watchers", None)
        if watchers is not None:
            watchers.stop()
        self.service.stop()
        self._timer.stop()
        self._icon.hide()
        from PySide6.QtWidgets import QApplication
        QApplication.quit()

    def handle_close(self, event):
        dirty = bool(self.window.doc.dirty)
        confirmed = True if not dirty else self.window._confirm_close()
        action = close_action(self.window._quitting, dirty, confirmed)
        if action == "stay":
            event.ignore()
            return
        self._show_when_ready()
        if action == "hide" and self._icon.isVisible():
            event.ignore()
            self.window._qt.hide()
            return
        self._exit(event)

    def _exit(self, event):
        self.service.stop()
        watchers = getattr(self.window, "watchers", None)
        if watchers is not None:
            watchers.stop()
        self._timer.stop()
        self._icon.hide()
        event.accept()
        from PySide6.QtWidgets import QApplication
        QApplication.quit()

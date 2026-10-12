"""Fenêtre principale : barre latérale, bandeau de compte, pages."""

import os
import socket
import threading

try:
    from i18n import _
except ImportError:
    def _(s):
        return s

from ui.document import Document, backup_blurb
from ui.live_sync import LiveSync
from ui.pages.mappings import MappingsPage
from ui.pages.schedule import SchedulePage
from ui.pages.settings import SettingsPage
from ui import widgets

_SINGLETON = "\0proton_mapping_editor_%d" % os.getuid() if os.name != "nt" else None


def startup_mappings_path(config_path=None):
    """Fichier ouvert au lancement : argument, puis le dernier enregistré, puis systemd."""
    if config_path and os.path.isfile(config_path):
        return config_path
    try:
        import config as appconfig
        remembered = appconfig.last_mappings_path()
    except Exception:
        remembered = ""
    if remembered and os.path.isfile(remembered):
        return remembered
    return _mappings_from_units()


def _mappings_from_units():
    """The mappings file the watcher was already started with, if it is still there."""
    try:
        import realtime_manager
        path = realtime_manager.read_units_mappings_path()
    except Exception:
        return None
    if path and os.path.isfile(path):
        return path
    return None


class MainWindow:
    """Enveloppe autour de QMainWindow, construite seulement si Qt est là."""

    def __init__(self, config_path=None):
        from PySide6.QtCore import QTimer, Signal
        from PySide6.QtWidgets import (
            QButtonGroup, QFrame, QHBoxLayout, QLabel, QMainWindow,
            QPushButton, QStackedWidget, QVBoxLayout, QWidget,
        )

        class _Window(QMainWindow):
            status_sig = Signal(str)
            auth_sig = Signal(bool)
            account_sig = Signal(str)
            authed_sig = Signal(bool, str)
            disk_sig = Signal()

            def closeEvent(self_inner, event):
                tray = getattr(self, "_tray", None)
                if tray is not None:
                    tray.handle_close(event)
                    return
                if not self._confirm_close():
                    event.ignore()
                    return
                event.accept()

        self._qt = _Window()
        self._quitting = False
        self.doc = Document()
        self.watchers = LiveSync()
        self.cli_flags = {"revisions": None, "shared": None}
        self._qt.setWindowTitle(_("Mappings editor — Proton Drive sync"))
        self._commit = None
        self._qt.resize(1060, 700)
        self._qt.setMinimumSize(880, 580)
        self._fit_screen()

        root = QWidget()
        root.setObjectName("AppRoot")
        self._qt.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        side = QWidget()
        side.setObjectName("Sidebar")
        side.setFixedWidth(220)
        side_l = QVBoxLayout(side)
        side_l.setContentsMargins(12, 16, 12, 16)
        brand = QLabel("Drive sync")
        brand.setObjectName("Title")
        side_l.addWidget(brand)
        sub = QLabel(backup_blurb(self.doc.mappings))
        sub.setObjectName("Footer")
        sub.setWordWrap(True)
        side_l.addWidget(sub)
        self._mode_label = sub
        side_l.addSpacing(12)

        self.stack = QStackedWidget()
        self.stack.setObjectName("PageHost")
        pages = (
            ("mappings", "📂", _("Mappings")),
            ("schedule", "⏰", _("Sync schedule")),
            ("settings", "⚙", _("Configuration")),
        )
        group = QButtonGroup(self._qt)
        group.setExclusive(True)
        self._pages = {}
        holders = {}
        for index, (key, icon, label) in enumerate(pages):
            button = QPushButton(icon + "  " + label)
            button.setObjectName("Nav")
            button.setCheckable(True)
            group.addButton(button, index)
            side_l.addWidget(button)
            holder = QWidget()
            self.stack.addWidget(holder)
            holders[key] = holder
        side_l.addStretch(1)
        group.idClicked.connect(self.stack.setCurrentIndex)
        group.button(0).setChecked(True)
        outer.addWidget(side)

        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)
        top = QFrame()
        top.setObjectName("TopBar")
        top_l = QHBoxLayout(top)
        self.file_chip = QLabel(_("(new file)"))
        self.account_chip = QLabel(_("Checking…"))
        self.account_chip.setObjectName("AccountWait")
        top_l.addWidget(self.file_chip, 1)
        top_l.addWidget(self.account_chip)
        right.addWidget(top)
        right.addWidget(self.stack, 1)
        self.status = QLabel(_("Ready."))
        self.status.setObjectName("Muted")
        right.addWidget(self.status)
        wrap = QWidget()
        wrap.setLayout(right)
        outer.addWidget(wrap, 1)

        self.mappings = MappingsPage(holders["mappings"], self)
        self.schedule = SchedulePage(holders["schedule"], self)
        self.settings = SettingsPage(holders["settings"], self)
        self._pages = {0: self.mappings, 1: self.schedule, 2: self.settings}
        self._signed = False
        self.stack.currentChanged.connect(self._shown)

        self._qt.status_sig.connect(self.status.setText)
        self._qt.auth_sig.connect(self._paint_auth)
        self._qt.account_sig.connect(self._paint_account)
        self._qt.authed_sig.connect(self._on_auth)
        self._qt.disk_sig.connect(self.mappings._on_disk)

        config_path = startup_mappings_path(config_path)
        if config_path and os.path.exists(config_path):
            try:
                self.doc.load(config_path)
                self.mappings._refresh()
            except Exception as exc:
                widgets.error(self._qt, str(exc), _("Load error"))
        self.refresh_file_chip()
        QTimer.singleShot(300, self._probe_cli)
        QTimer.singleShot(500, self._startup_notices)
        from ui.tray import Tray
        self._tray = Tray(self)
        # Opening the window changes nothing on disk. Live sync resumes only
        # for a folder the person chose with 🔄 Live sync….
        QTimer.singleShot(400, self.mappings.resume_live)

    def _startup_notices(self):
        """Avis de déplacement des réglages, puis réécriture d'une vieille unité.
        Ni l'un ni l'autre ne tournent dans le constructeur."""
        try:
            import paths
            text = (paths.settings_move_notice_text()
                    if paths.settings_move_notice_pending() else None)
        except Exception:
            text = None
        if text:
            widgets.info(self._qt, text, _("Settings moved"))
            try:
                paths.clear_settings_move_notice()
            except Exception:
                pass
        self._refresh_legacy_unit()

    def _refresh_legacy_unit(self):
        """Réécrit le service s'il relancerait encore un passage en échec.
        systemctl reste dans un fil ; la ligne de statut revient par le signal."""
        def work():
            try:
                import schedule_manager
                if not schedule_manager.service_missing_restart_prevent_5():
                    return
                ok, message = schedule_manager.refresh_units()
            except Exception as exc:
                ok, message = False, str(exc)
            if ok:
                self._qt.status_sig.emit(_(
                    "Scheduled service updated so a failed pass is not restarted."))
            else:
                self._qt.status_sig.emit(_(
                    "Could not update the scheduled service: {e}").format(e=message))

        threading.Thread(target=work, daemon=True).start()

    def _fit_screen(self):
        from PySide6.QtWidgets import QApplication
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        width = min(1060, int(area.width() * 0.95))
        height = min(700, int(area.height() * 0.90))
        self._qt.resize(max(width, 880 if area.width() >= 880 else area.width()),
                        max(min(height, area.height()), 400))

    def _shown(self, index):
        page = self._pages.get(index)
        if page is not None:
            page.on_show()

    def show(self):
        self._qt.show()

    def close(self):
        self._qt.close()

    def set_status(self, text):
        self._qt.status_sig.emit(text or "")

    def set_auth(self, ok):
        self._qt.auth_sig.emit(bool(ok))

    def set_account_line(self, text):
        self._qt.account_sig.emit(text or "")

    def _paint_auth(self, ok):
        self._signed = bool(ok)
        if ok:
            if self.account_chip.text() in ("", _("Checking…"), _("Session unavailable")):
                self.account_chip.setText(_("Signed in."))
            self.account_chip.setObjectName("AccountOk")
        else:
            if self.account_chip.text() in ("", _("Checking…")):
                self.account_chip.setText(_("Session unavailable"))
            self.account_chip.setObjectName("AccountBad")
        self.account_chip.style().unpolish(self.account_chip)
        self.account_chip.style().polish(self.account_chip)
        self.settings.apply_account(self.account_chip.text())

    def _on_auth(self, ok, detail):
        source = getattr(self.mappings, "_pending_source", "") or ""
        announce = bool(getattr(self.mappings, "_announce_cli", False))
        self.mappings._pending_source = ""
        self.mappings._announce_cli = False
        self._signed = bool(ok)
        if ok:
            self.set_account_line(detail or _("Signed in."))
            if source:
                self.mappings._queue_pass(source, announce)
            return
        text = detail or _("Session unavailable")
        self.account_chip.setText(text)
        self.set_auth(False)
        self.set_account_line(text)
        if announce:
            widgets.error(self, text, _("Proton account"))

    def _paint_account(self, text):
        self.account_chip.setText(text)
        bad = not self._signed
        self.account_chip.setObjectName("AccountBad" if bad else "AccountOk")
        self.account_chip.style().unpolish(self.account_chip)
        self.account_chip.style().polish(self.account_chip)
        self.settings.apply_account(text)

    def refresh_file_chip(self):
        if not self.doc.path:
            name = _("(new file)")
        else:
            name = os.path.basename(self.doc.path)
        if self.doc.dirty:
            name += " *"
        self.file_chip.setText(name)
        title = _("Mappings editor — Proton Drive sync")
        commit = self._short_commit()
        if commit:
            self._qt.setWindowTitle(f"{title} — {name} — {commit}")
        else:
            self._qt.setWindowTitle(f"{title} — {name}")

    def _short_commit(self):
        cached = getattr(self, "_commit", None)
        if cached is not None:
            return cached
        from ui.live_sync import short_commit
        self._commit = short_commit(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        return self._commit

    def refresh_direction_line(self):
        label = getattr(self, "_mode_label", None)
        if label is not None:
            label.setText(backup_blurb(self.doc.mappings))

    def _confirm_close(self):
        if not self.doc.dirty:
            return True
        return widgets.confirm(
            self._qt,
            _("Some changes have not been saved. Quit without saving?"),
            _("Unsaved changes"), _("Quit without saving"), _("Cancel"))

    def report_auth(self, ok, detail):
        self._qt.authed_sig.emit(bool(ok), detail or "")

    def _probe_cli(self):
        def work():
            try:
                import proton_sync
                self.cli_flags["revisions"] = bool(proton_sync.cli_supports_revisions())
                shared = proton_sync.cli_supports_shared_delete()
                self.cli_flags["shared"] = None if shared is None else bool(shared)
            except Exception:
                self.cli_flags["revisions"] = None
                self.cli_flags["shared"] = None
        threading.Thread(target=work, daemon=True).start()


def try_become_primary():
    """Socket abstrait Linux, le même nom que l'éditeur Tk. None = pas de garde."""
    if not _SINGLETON or not hasattr(socket, "AF_UNIX"):
        return "skip"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(_SINGLETON)
    except OSError:
        server.close()
        return None
    server.listen(5)
    return server


def signal_existing():
    if not _SINGLETON or not hasattr(socket, "AF_UNIX"):
        return False
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(2.0)
        client.connect(_SINGLETON)
        client.sendall(b"raise")
        client.close()
        return True
    except OSError:
        return False


def listen_raises(window, server):
    import queue
    from PySide6.QtCore import QTimer
    pending = queue.Queue()

    def loop():
        while True:
            try:
                conn, _addr = server.accept()
            except OSError:
                return
            try:
                conn.recv(64)
            except OSError:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass
            pending.put(1)

    def poll():
        raised = False
        try:
            while True:
                pending.get_nowait()
                raised = True
        except queue.Empty:
            pass
        if raised:
            window._qt.show()
            window._qt.raise_()
            window._qt.activateWindow()

    threading.Thread(target=loop, daemon=True).start()
    timer = QTimer(window._qt)
    timer.timeout.connect(poll)
    timer.start(200)
    window._raise_timer = timer

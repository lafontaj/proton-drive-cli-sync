"""Page Planification. Les unités viennent de schedule_manager, pas d'un second générateur.

Afficher la page lit l'état. Seuls les boutons écrivent ou démarrent une unité.
"""

import os
import threading

try:
    from i18n import _
except ImportError:
    def _(s):
        return s

from ui import calendar, widgets

_DAYS = (
    ("Mon", _("Monday")), ("Tue", _("Tuesday")), ("Wed", _("Wednesday")),
    ("Thu", _("Thursday")), ("Fri", _("Friday")), ("Sat", _("Saturday")),
    ("Sun", _("Sunday")),
)


def describe_calendar(cal):
    """« every day at 03:00 », ou le texte systemd brut s'il n'est pas reconnu."""
    parsed = calendar.parse_on_calendar(cal or "")
    if not parsed:
        return cal or ""
    freq, hour, dow = parsed
    if freq == "hourly":
        return _("every hour")
    day = dict(_DAYS).get(dow, dow)
    if freq == "weekly":
        return _("every {d} at {h:02d}:00").format(d=day, h=hour)
    return _("every day at {h:02d}:00").format(h=hour)


def next_run_text(line):
    """Date de la ligne list-timers (« Thu 2026-10-08 03:00:00 CEST … »)."""
    parts = (line or "").split()
    if len(parts) >= 4 and parts[0][:1].isalpha() and parts[1][:1].isdigit():
        return " ".join(parts[:4])
    return ""


def state_lines(st):
    """État en phrases simples, sans noms d'unités systemd."""
    if not st.get("service_exists") or not st.get("timer_exists"):
        return [_("Scheduled sync is not set up. Pick when to sync below and "
                  "press Save schedule.")]
    lines = []
    when = describe_calendar(st.get("calendar"))
    if st.get("timer_active"):
        lines.append(_("Scheduled sync is on: {w}.").format(w=when) if when
                     else _("Scheduled sync is on."))
        nxt = next_run_text(st.get("next_run"))
        if nxt:
            lines.append(_("Next sync: {t}").format(t=nxt))
    else:
        lines.append(_("Scheduled sync is off. Turn on starts it again ({w}).")
                     .format(w=when) if when else _("Scheduled sync is off."))
    if st.get("mappings_path"):
        lines.append(_("Mappings file: {p}").format(p=st["mappings_path"]))
    if st.get("delete"):
        lines.append(_("Deletions are sent to Proton Drive, for folders that "
                       "allow it."))
    if not st.get("linger"):
        lines.append(_("It runs while you are logged in, even with this window "
                       "closed."))
    return lines


class SchedulePage:
    def __init__(self, host, window):
        from PySide6.QtCore import QObject, Signal
        from PySide6.QtWidgets import (
            QCheckBox, QComboBox, QHBoxLayout, QLabel, QPlainTextEdit,
            QPushButton, QVBoxLayout,
        )
        self.window = window
        root = QVBoxLayout(host)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        status, status_l = widgets.section(host, _("Sync schedule"))
        intro = QLabel(_(
            "Syncs every mapping in the open file at set times, both ways for "
            "two-way folders. It does not need this window to be open."))
        intro.setWordWrap(True)
        intro.setObjectName("Muted")
        status_l.addWidget(intro)
        self.state = QLabel(_("Reading…"))
        self.state.setWordWrap(True)
        status_l.addWidget(self.state)
        root.addWidget(status)

        actions, layout = widgets.section(host, _("When to sync"))
        row = QHBoxLayout()
        self.freq = QComboBox()
        self.freq.addItem(_("Every hour"), "hourly")
        self.freq.addItem(_("Every day"), "daily")
        self.freq.addItem(_("Every week"), "weekly")
        self.freq.setCurrentIndex(self.freq.findData("daily"))
        self.day = QComboBox()
        for token, label in _DAYS:
            self.day.addItem(label, token)
        self.hour = QComboBox()
        for h in range(24):
            self.hour.addItem(f"{h:02d}:00", h)
        self.hour.setCurrentIndex(3)
        self.freq.currentIndexChanged.connect(self._freq_changed)
        row.addWidget(self.freq)
        self.day_label = QLabel(_("on"))
        row.addWidget(self.day_label)
        row.addWidget(self.day)
        self.hour_label = QLabel(_("at"))
        row.addWidget(self.hour_label)
        row.addWidget(self.hour)
        row.addStretch(1)
        layout.addLayout(row)
        self.delete = QCheckBox(_(
            "Also remove from Proton Drive what I delete here"))
        layout.addWidget(self.delete)
        delete_note = QLabel(_(
            "Only folders whose Edit allows deletion. Removed files go to the "
            "Proton trash, where you can restore them."))
        delete_note.setWordWrap(True)
        delete_note.setObjectName("Muted")
        layout.addWidget(delete_note)

        buttons = QHBoxLayout()
        self.apply_btn = QPushButton(_("💾 Save schedule"))
        self.apply_btn.setObjectName("Primary")
        self.on_btn = QPushButton(_("Turn on"))
        self.off_btn = QPushButton(_("Turn off"))
        self.run_btn = QPushButton(_("▶ Sync now"))
        self.journal_btn = QPushButton(_("Show last scheduled sync"))
        self.apply_btn.clicked.connect(self.on_apply)
        self.on_btn.clicked.connect(lambda: self._call("enable_timer"))
        self.off_btn.clicked.connect(lambda: self._call("disable_timer"))
        self.run_btn.clicked.connect(lambda: self._call("run_now"))
        self.journal_btn.clicked.connect(self.on_journal)
        for button in (self.apply_btn, self.on_btn, self.off_btn, self.run_btn,
                       self.journal_btn):
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        root.addWidget(actions)

        log_card, log_l = widgets.section(host, _("Last scheduled sync"))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(160)
        log_l.addWidget(self.log)
        root.addWidget(log_card, 1)
        self._freq_changed()

        class _Bus(QObject):
            text = Signal(str)
            status = Signal(object)

        self._bus = _Bus()
        self._bus.text.connect(self.log.setPlainText)
        self._bus.status.connect(self._paint)

    def on_show(self):
        self.refresh()

    def _freq_changed(self):
        hourly = self.freq.currentData() == "hourly"
        weekly = self.freq.currentData() == "weekly"
        self.hour.setVisible(not hourly)
        self.hour_label.setVisible(not hourly)
        self.day.setVisible(weekly)
        self.day_label.setVisible(weekly)

    def _path(self):
        return self.window.doc.path

    def refresh(self):
        """systemctl tourne dans un fil ; la page se repeint par le signal."""
        self.state.setText(_("Reading…"))

        def work():
            try:
                import schedule_manager
                st = schedule_manager.status()
            except Exception as exc:
                st = {"error": str(exc)}
            self._bus.status.emit(st)

        threading.Thread(target=work, daemon=True).start()

    def _paint(self, st):
        if st.get("error"):
            self.state.setText(st["error"])
            return
        self.state.setText("\n".join(state_lines(st)))
        installed = bool(st.get("service_exists") and st.get("timer_exists"))
        self.on_btn.setEnabled(installed and not st.get("timer_active"))
        self.off_btn.setEnabled(installed and bool(st.get("timer_active")))
        self.run_btn.setEnabled(bool(st.get("service_exists")))
        parsed = calendar.parse_on_calendar(st.get("calendar") or "")
        if parsed:
            freq, hour, dow = parsed
            idx = self.freq.findData(freq)
            if idx >= 0:
                self.freq.setCurrentIndex(idx)
            hidx = self.hour.findData(hour)
            if hidx >= 0:
                self.hour.setCurrentIndex(hidx)
            didx = self.day.findData(dow)
            if didx >= 0:
                self.day.setCurrentIndex(didx)
        self.delete.setChecked(bool(st.get("delete")))

    def on_apply(self):
        path = self._path()
        if not path:
            widgets.info(self.window, _("Save the mappings file first, then "
                                        "set the schedule."), _("No file"))
            return
        if self.window.doc.dirty and not widgets.confirm(
                self.window,
                _("The mappings file has unsaved changes. The schedule reads "
                  "the saved file, so those changes are not synced until you "
                  "save.\n\nSave the schedule anyway?"),
                _("Sync schedule"), _("Save schedule"), _("Cancel")):
            return
        delete = self.delete.isChecked()
        if delete and not widgets.confirm(
                self.window,
                _("Scheduled syncs will also remove from Proton Drive what you "
                  "delete on this computer, with nobody watching.\n\n"
                  "Only folders whose Edit allows deletion are affected, and "
                  "removed files go to the Proton trash, where you can restore "
                  "them until you empty it. With the mass-deletion guard on "
                  "(Configuration), a large deletion is held back and "
                  "reported instead.\n\n"
                  "Tip: run ▶ Run sync with Test (dry-run) and Propagate "
                  "deletions first to see what would go.\n\nContinue?"),
                _("Remove deleted files on schedule?"), _("Yes, remove them"),
                _("Cancel")):
            return
        try:
            import schedule_manager
            installed = schedule_manager.read_service_mappings_path()
            if installed and os.path.realpath(installed) != os.path.realpath(path):
                if not widgets.confirm(
                        self.window,
                        _("The schedule currently syncs:\n  {old}\n"
                          "You are switching it to:\n  {new}\n\n"
                          "Only one mappings file can be scheduled. Continue?").format(
                              old=installed, new=path),
                        _("Sync schedule"),
                        _("Switch to this file"),
                        _("Cancel (keep the current file)")):
                    return
            cal = calendar.build_on_calendar(
                self.freq.currentData(), self.hour.currentData(), self.day.currentData())
            ok, msg = schedule_manager.install_or_update(
                path, on_calendar=cal, delete=delete, enable=True)
        except Exception as exc:
            widgets.error(self.window, str(exc), _("Sync schedule"))
            return
        if ok:
            msg = _("Scheduled sync is on: {w}.").format(w=describe_calendar(cal))
        (widgets.info if ok else widgets.error)(self.window, msg, _("Sync schedule"))
        self.refresh()

    def _call(self, name):
        try:
            import schedule_manager
            ok, msg = getattr(schedule_manager, name)()
        except Exception as exc:
            widgets.error(self.window, str(exc), _("Sync schedule"))
            return
        if ok and name == "run_now":
            msg = _("A sync started in the background. Show last scheduled "
                    "sync tells how it went.")
        (widgets.info if ok else widgets.error)(self.window, msg, _("Sync schedule"))
        self.refresh()

    def on_journal(self):
        self.log.setPlainText(_("Reading the journal…"))

        def work():
            try:
                import schedule_manager
                _ok, text = schedule_manager.journal_last_run()
            except Exception as exc:
                text = str(exc)
            self._bus.text.emit(text)

        threading.Thread(target=work, daemon=True).start()

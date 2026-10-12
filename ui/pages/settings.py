"""Page Configuration : compte, CLI, langue, NAS, extensions, barre, lanceur."""

try:
    from i18n import _
except ImportError:
    def _(s):
        return s

from ui import launcher, widgets
from ui.pages.mapping_dialogs import LoginDialog


class SettingsPage:
    def __init__(self, host, window):
        from PySide6.QtWidgets import (
            QCheckBox, QComboBox, QHBoxLayout, QLabel, QLineEdit, QPushButton,
            QVBoxLayout,
        )
        self.window = window
        self._rows = []
        self.scroll, inner, root = widgets.scroll_page(host)

        account, account_l = widgets.section(inner, _("Proton account"))
        email_row = QHBoxLayout()
        self.account = QLabel(_("Checking…"))
        self.signed_badge = QLabel(_("SIGNED IN"))
        self.signed_badge.setObjectName("SignedIn")
        self.signed_badge.hide()
        email_row.addWidget(self.account)
        email_row.addWidget(self.signed_badge)
        email_row.addStretch(1)
        account_l.addLayout(email_row)
        acct = QHBoxLayout()
        self.sign_in = QPushButton(_("Sign in to Proton"))
        sign_out = QPushButton(_("Sign out"))
        self.sign_in.clicked.connect(self.on_sign_in)
        sign_out.clicked.connect(self.on_sign_out)
        acct.addWidget(self.sign_in)
        acct.addWidget(sign_out)
        acct.addStretch(1)
        account_l.addLayout(acct)
        root.addWidget(account)

        cli_card, cli_l = widgets.section(inner, _("Proton Drive CLI"))
        self.cli = QLineEdit()
        cli_l.addWidget(self.cli)
        root.addWidget(cli_card)

        file_card, file_l = widgets.section(inner, _("Settings file"))
        self.settings_path = QLabel("")
        self.settings_path.setWordWrap(True)
        self.settings_path.setObjectName("Muted")
        file_l.addWidget(self.settings_path)
        root.addWidget(file_card)

        guard_card, guard_l = widgets.section(inner, _("Mass deletion"))
        self.guard = QCheckBox(_(
            "Stop a pass that would trash most of a remote folder"))
        guard_l.addWidget(self.guard)
        guard_l.addWidget(QLabel(_(
            "Off by default: a file you delete locally is taken as meant, and "
            "the Proton trash is the safety net. When on, a folder is left "
            "untouched when the pass would trash at least the minimum and "
            "more than the fraction of its remote children. The window can "
            "run that pass again if you meant it.")))
        grow = QHBoxLayout()
        grow.addWidget(QLabel(_("Minimum")))
        self.guard_min = QLineEdit()
        grow.addWidget(self.guard_min)
        grow.addWidget(QLabel(_("Fraction")))
        self.guard_ratio = QLineEdit()
        grow.addWidget(self.guard_ratio)
        guard_l.addLayout(grow)
        root.addWidget(guard_card)

        lang_card, lang_l = widgets.section(inner, _("Interface language"))
        self.language = QComboBox()
        self.language.addItem(_("Automatic"), "auto")
        try:
            import i18n
            codes = i18n.SUPPORTED
        except Exception:
            codes = ("en", "fr")
        for code in codes:
            self.language.addItem(code, code)
        lang_l.addWidget(self.language)
        root.addWidget(lang_card)

        nas_card, nas_l = widgets.section(inner, _("NAS"))
        self.nas = QCheckBox(_("Use a NAS"))
        nas_l.addWidget(self.nas)
        mount_row = QHBoxLayout()
        mount_row.addWidget(QLabel(_("NAS mount point: ")))
        self.mount = QLineEdit()
        mount_row.addWidget(self.mount)
        nas_l.addLayout(mount_row)
        ident_row = QHBoxLayout()
        ident_row.addWidget(QLabel(_("NAS identity (account name): ")))
        self.identity = QLineEdit()
        ident_row.addWidget(self.identity)
        nas_l.addLayout(ident_row)
        nas_l.addWidget(QLabel(_("NAS data-path correspondence:")))
        self._pairs = QVBoxLayout()
        nas_l.addLayout(self._pairs)
        add_pair = QPushButton(_("Add a correspondence"))
        add_pair.clicked.connect(lambda: self._add_pair())
        nas_l.addWidget(add_pair)
        root.addWidget(nas_card)

        ext_card, ext_l = widgets.section(inner, _("File extensions"))
        self.rename = QCheckBox(_("Rename extensions to lowercase"))
        ext_l.addWidget(self.rename)
        ext_row = QHBoxLayout()
        ext_row.addWidget(QLabel(_("Collision suffix")))
        self.suffix = QLineEdit()
        ext_row.addWidget(self.suffix)
        ext_l.addLayout(ext_row)
        self.whitelist = QLineEdit()
        self.whitelist.setPlaceholderText(_("Extensions, separated by spaces"))
        ext_l.addWidget(self.whitelist)
        root.addWidget(ext_card)

        tray_card, tray_l = widgets.section(inner, _("System tray"))
        self.tray = QCheckBox(_("Status icon in the tray"))
        tray_l.addWidget(self.tray)
        root.addWidget(tray_card)

        launch_card, launch_l = widgets.section(inner, _("Application launcher"))
        self.menu = QCheckBox(_("Applications menu launcher"))
        self.desktop = QCheckBox(_("Desktop shortcut"))
        self.open_current = QCheckBox(_("Open the current mappings file"))
        self.login = QCheckBox(_("Open this window when I log in"))
        launch_l.addWidget(self.menu)
        launch_l.addWidget(self.desktop)
        launch_l.addWidget(self.open_current)
        launch_l.addWidget(self.login)
        root.addWidget(launch_card)

        save = QPushButton(_("Save"))
        save.setObjectName("Primary")
        save.clicked.connect(self.on_save)
        root.addWidget(save)
        root.addStretch(1)

    def on_show(self):
        self._load()
        self._refresh_account()

    def _add_pair(self, local="", nas=""):
        from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QPushButton, QWidget
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        loc = QLineEdit(local)
        loc.setPlaceholderText(_("Seen on this machine (desktop)"))
        remote = QLineEdit(nas)
        remote.setPlaceholderText(_("Seen on the NAS"))
        remove = QPushButton(_("Delete"))
        layout.addWidget(loc)
        layout.addWidget(remote)
        layout.addWidget(remove)
        self._pairs.addWidget(row)
        entry = {"row": row, "local": loc, "nas": remote}

        def drop():
            self._pairs.removeWidget(row)
            row.deleteLater()
            self._rows.remove(entry)

        remove.clicked.connect(drop)
        self._rows.append(entry)

    def _clear_pairs(self):
        for entry in list(self._rows):
            self._pairs.removeWidget(entry["row"])
            entry["row"].deleteLater()
        self._rows.clear()

    def _load(self):
        try:
            import config as appconfig
            import i18n
        except Exception:
            return
        self.cli.setText(appconfig.proton_cli_path() or "")
        try:
            import paths
            self.settings_path.setText(paths.settings_path())
        except Exception:
            self.settings_path.setText("")
        self.guard.setChecked(appconfig.mass_delete_guard())
        self.guard_min.setText(str(appconfig.max_delete_min()))
        self.guard_ratio.setText(str(appconfig.max_delete_ratio()))
        lang = i18n.read_language_setting()
        idx = self.language.findData(lang)
        if idx >= 0:
            self.language.setCurrentIndex(idx)
        self.nas.setChecked(bool(appconfig.nas_enabled()))
        self.mount.setText(appconfig.nas_mount_path() or "")
        self.identity.setText(appconfig.account_name() or "")
        self._clear_pairs()
        for pair in appconfig.nas_path_map():
            self._add_pair(pair.get("local", ""), pair.get("nas", ""))
        self.rename.setChecked(bool(appconfig.rename_ext_enabled()))
        self.suffix.setText(appconfig.rename_ext_collision_suffix() or "")
        self.whitelist.setText(appconfig.format_ext_list())
        self.tray.setChecked(bool(appconfig.tray_enabled()))
        self.menu.setChecked(os_path_exists(launcher.menu_path()))
        self.desktop.setChecked(os_path_exists(launcher.desktop_path()))
        self.open_current.setChecked(launcher.existing_opens_current())
        self.login.setChecked(os_path_exists(launcher.ui_autostart_path()))

    def _refresh_account(self):
        import threading

        def work():
            try:
                import realtime_manager
                ok, detail = realtime_manager.auth_status()
            except Exception as exc:
                ok, detail = False, str(exc)
            email = ""
            if ok:
                try:
                    import proton_sync
                    email = proton_sync.get_account_email() or ""
                except Exception:
                    email = ""
            self.window.report_auth(ok, email if ok else detail)

        threading.Thread(target=work, daemon=True).start()

    def apply_account(self, text):
        """Met à jour l'e-mail, le badge et le bouton selon l'état de session."""
        signed = bool(getattr(self.window, "_signed", False))
        if signed and text != _("Signed in."):
            self.account.setText(text)
        elif signed:
            self.account.setText("")
        else:
            self.account.setText(text or _("Session unavailable"))
        self.sign_in.setVisible(not signed)
        self.signed_badge.setVisible(signed)

    def on_sign_in(self):
        LoginDialog(self.window)
        live = self.window.mappings._live_mapping()
        if live is not None and self.window.doc.path:
            self.window.mappings._pending_source = live["source"]
            self.window.mappings._announce_cli = False
        self._refresh_account()

    def on_sign_out(self):
        try:
            import realtime_manager
            ok, msg = realtime_manager.auth_logout()
        except Exception as exc:
            widgets.error(self.window, str(exc), _("Proton account"))
            return
        (widgets.info if ok else widgets.error)(self.window, msg, _("Proton account"))
        self.window.set_auth(False)
        self._refresh_account()

    def on_save(self):
        try:
            import config as appconfig
            import i18n
        except Exception as exc:
            widgets.error(self.window, str(exc), _("Configuration"))
            return
        i18n.write_language_setting(self.language.currentData())
        if not appconfig.set_max_delete_min(self.guard_min.text().strip()):
            widgets.warn(
                self.window,
                _("The mass-delete minimum has to be a whole number, 0 or more."),
                _("Mass deletion"))
            return
        if not appconfig.set_max_delete_ratio(self.guard_ratio.text().strip()):
            widgets.warn(
                self.window,
                _("The mass-delete fraction has to be between 0 and 1."),
                _("Mass deletion"))
            return
        appconfig.set_proton_cli_path(self.cli.text().strip())
        appconfig.set_mass_delete_guard(self.guard.isChecked())
        appconfig.set_nas_enabled(self.nas.isChecked())
        mount = self.mount.text().strip()
        if mount:
            appconfig.set_nas_mount_path(mount)
        appconfig.set_account_name(self.identity.text().strip())
        pairs = []
        for entry in self._rows:
            loc = entry["local"].text().strip()
            nas = entry["nas"].text().strip()
            if loc and nas:
                pairs.append({"local": loc, "nas": nas})
        appconfig.set_nas_path_map(pairs)
        if self.rename.isChecked():
            appconfig.set_rename_ext_enabled(True)
            appconfig.set_rename_ext_auto_disabled(True)
        else:
            appconfig.set_rename_ext_enabled(False)
        suffix = self.suffix.text().strip()
        if suffix:
            ok_suffix, reason = appconfig.validate_collision_suffix(suffix)
            if ok_suffix:
                appconfig.set_rename_ext_collision_suffix(suffix)
            else:
                widgets.warn(self.window, reason, _("File extensions"))
        try:
            appconfig.set_rename_ext_whitelist(appconfig.parse_ext_list(self.whitelist.text()))
        except Exception:
            pass
        appconfig.set_tray_enabled(self.tray.isChecked())
        launcher.apply_tray(self.tray.isChecked())
        target = self.window.doc.path if self.open_current.isChecked() else None
        launcher.apply_launcher(self.menu.isChecked(), self.desktop.isChecked(), target)
        if self.login.isChecked():
            launcher.install_ui_autostart()
        else:
            launcher.remove_ui_autostart()
        widgets.info(
            self.window,
            _("Saved. The interface language applies the next time you open the window."),
            _("Configuration"))


def os_path_exists(path):
    import os
    return os.path.exists(path)

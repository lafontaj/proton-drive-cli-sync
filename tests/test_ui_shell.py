"""La fenêtre Qt se construit sans écran (plateforme offscreen)."""

import os

import pytest


def _need_qt():
    """Saute le test si Qt ne se charge pas : PySide6 absent, ou présent sans
    ses bibliothèques système (libEGL sur un runner CI minimal)."""
    try:
        import PySide6.QtWidgets  # noqa: F401
    except ImportError as exc:
        pytest.skip("PySide6.QtWidgets cannot be imported: %s" % exc)

# Icônes de la barre précédente, remises sur les boutons Mappings.


def test_window_builds(monkeypatch):
    _need_qt()
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QCheckBox, QPushButton, QScrollArea

    from ui import theme
    from ui.shell import MainWindow

    app = QApplication.instance() or QApplication([])
    theme.apply(app)
    window = MainWindow()
    window.show()
    assert window.stack.count() == 3
    assert window.mappings.run_btn.objectName() == "Primary"
    assert window.mappings.stop_btn.objectName() == "Danger"
    mappings = window.stack.widget(0)
    labels = [w.text() for w in mappings.findChildren(QPushButton)]
    labels += [w.text() for w in mappings.findChildren(QCheckBox)]
    assert any(text.startswith("📂") for text in labels)
    assert any(text.startswith("▶") for text in labels)
    assert window.mappings.excl_summary.text().startswith("🌐")
    nav = [
        button.text() for button in window._qt.findChildren(QPushButton)
        if button.objectName() == "Nav"
    ]
    assert [text[0] for text in nav] == ["📂", "⏰", "⚙"]
    assert any("Live sync" in text for text in labels)
    assert "Automatic sync is off" in window.mappings.live_banner.text()
    assert isinstance(window.settings.scroll, QScrollArea)
    assert window.stack.widget(2).findChild(QScrollArea) is window.settings.scroll
    assert os.environ.get("QT_QPA_PLATFORM") == "offscreen"
    window.close()


def test_window_reloads_the_last_mappings_file(tmp_path, monkeypatch):
    _need_qt()
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    import json
    import config
    from PySide6.QtWidgets import QApplication
    from ui.shell import MainWindow

    folder = tmp_path / "Docs"
    folder.mkdir()
    cfg = tmp_path / "mappings.json"
    cfg.write_text(json.dumps([{
        "type": "folder",
        "source": str(folder),
        "dest_parent": "/my-files/Docs",
    }]), encoding="utf-8")
    assert config.set_last_mappings_path(str(cfg))
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    assert os.path.normpath(window.doc.path) == os.path.normpath(str(cfg))
    from ui.live_sync import short_commit
    commit = short_commit(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if commit:
        assert commit in window._qt.windowTitle()
    window.close()
    app.processEvents()


def _open_window_and_wait(monkeypatch, cfg):
    import time
    import config
    import realtime_manager
    import volume
    from PySide6.QtWidgets import QApplication
    from ui.shell import MainWindow

    started = []
    monkeypatch.setattr(volume, "start_watcher", lambda *a, **k: started.append(a))
    monkeypatch.setattr(
        realtime_manager, "install_or_update_units",
        lambda *a, **k: started.append(a) or (True, "", None))
    monkeypatch.setattr(volume, "ensure_dolphin_place", lambda *a, **k: started.append(a))
    monkeypatch.setattr(volume, "ensure_drive_root", lambda *a, **k: started.append(a))
    assert config.set_last_mappings_path(str(cfg))
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    deadline = time.time() + 1.5
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)
    return app, window, started


def test_opening_the_window_changes_nothing(tmp_path, monkeypatch):
    _need_qt()
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    import json
    from ui import launcher

    folder = tmp_path / "Docs"
    folder.mkdir()
    cfg = tmp_path / "mappings.json"
    text = json.dumps([
        {"type": "folder", "source": str(folder), "dest_parent": "/my-files/Docs"},
    ])
    cfg.write_text(text, encoding="utf-8")
    launcher.remove_ui_autostart()
    app, window, started = _open_window_and_wait(monkeypatch, cfg)
    assert cfg.read_text(encoding="utf-8") == text
    assert not os.path.exists(launcher.ui_autostart_path())
    assert started == []
    assert not getattr(window.mappings, "_live_source", "")
    window.close()
    app.processEvents()


def test_a_trial_pick_is_asked_about_and_not_started(tmp_path, monkeypatch):
    _need_qt()
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    import json
    from ui import launcher, widgets

    folder = tmp_path / "Docs"
    folder.mkdir()
    cfg = tmp_path / "mappings.json"
    text = json.dumps([
        {"type": "folder", "source": str(folder), "dest_parent": "/my-files/Docs",
         "direction": "twoway", "live": True, "allow_delete": True,
         "delete_mode": "trash"},
    ])
    cfg.write_text(text, encoding="utf-8")
    launcher.remove_ui_autostart()
    asked = []
    monkeypatch.setattr(widgets, "confirm", lambda *a, **k: asked.append(a) or True)
    monkeypatch.setattr(widgets, "confirm_check", lambda *a, **k: (False, False))
    app, window, started = _open_window_and_wait(monkeypatch, cfg)
    assert len(asked) == 1
    assert cfg.read_text(encoding="utf-8") == text
    assert not os.path.exists(launcher.ui_autostart_path())
    assert started == []
    window.close()
    app.processEvents()


def test_showing_the_schedule_page_changes_nothing(tmp_path, monkeypatch):
    # Showing it only reads the timer state.
    _need_qt()
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    import time
    import schedule_manager
    from PySide6.QtWidgets import QApplication
    from ui.shell import MainWindow

    written = []
    for name in ("install_or_update", "enable_timer", "disable_timer",
                 "run_now", "refresh_units"):
        monkeypatch.setattr(
            schedule_manager, name,
            lambda *a, _n=name, **k: written.append(_n) or (True, ""))
    monkeypatch.setattr(schedule_manager, "service_missing_restart_prevent_5",
                        lambda: False)
    monkeypatch.setattr(schedule_manager, "status", lambda: {
        "service_exists": True, "timer_exists": True, "timer_active": True,
        "calendar": "*-*-* 05:00:00", "next_run": None, "linger": True,
        "mappings_path": "/m.json", "delete": False})
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.stack.setCurrentIndex(1)
    deadline = time.time() + 1.0
    while time.time() < deadline and "05:00" not in window.schedule.state.text():
        app.processEvents()
        time.sleep(0.02)
    assert "every day at 05:00" in window.schedule.state.text()
    assert window.schedule.hour.currentData() == 5
    assert not window.schedule.on_btn.isEnabled()
    assert window.schedule.off_btn.isEnabled()
    assert written == []
    window.close()
    app.processEvents()


def test_mapping_dialog_names_the_folder_on_proton(monkeypatch):
    # The field is the parent; the line under it is the folder itself.
    _need_qt()
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QDialog
    from ui.pages.mapping_dialogs import MappingDialog

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(QDialog, "exec", lambda self: QDialog.Rejected)
    dlg = MappingDialog(None, "folder", {
        "type": "folder", "source": "/home/u/Docs",
        "dest_parent": "/my-files/Backups", "direction": "twoway"})
    assert "On Proton Drive: /my-files/Backups/Docs" in dlg.where.text()
    assert "come down" in dlg.where.text()
    dlg.upload.setChecked(True)
    assert dlg.where.text() == "On Proton Drive: /my-files/Backups/Docs"
    dlg.dest.setText("/my-files/Other")
    assert "/my-files/Other/Docs" in dlg.where.text()
    app.processEvents()

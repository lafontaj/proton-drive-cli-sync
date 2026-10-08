"""Headless defaults and settings locations.

A missing rename_ext_enabled key used to follow the stored default True, so a
headless run renamed IMG.JPG even when the CLI already accepts uppercase
extensions.
"""

import importlib
import json
import os
import stat
from pathlib import Path

import pytest

import config
import paths

REPO = Path(__file__).resolve().parents[1]


def _mapping(source, **extra):
    mapping = {
        "type": "folder",
        "source": str(source),
        "dest_parent": "/my-files/Backups",
    }
    mapping.update(extra)
    return mapping


def test_headless_run_does_not_rename_extensions_on_modern_cli(
        fake_drive, local_tree, write_mappings, engine):
    src = local_tree({"Docs/IMG.JPG": (b"img", 1_000_000_000)})
    cfg = write_mappings([_mapping(src / "Docs")])
    result = engine(cfg)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (src / "Docs" / "IMG.JPG").is_file()
    assert not (src / "Docs" / "IMG.jpg").exists()


def test_explicit_rename_ext_true_after_migration_is_honored_headless(
        fake_drive, local_tree, write_mappings, engine):
    src = local_tree({"Docs/IMG.JPG": (b"img", 1_000_000_000)})
    cfg = write_mappings([_mapping(src / "Docs")])
    result = engine(cfg, settings_doc={
        "language": "en",
        "rename_ext_enabled": True,
        "rename_ext_auto_disabled": True,
    })
    assert result.returncode == 0, result.stdout + result.stderr
    assert (src / "Docs" / "IMG.jpg").is_file()
    assert not (src / "Docs" / "IMG.JPG").exists()


def test_rename_ext_true_without_migration_flag_uses_version_default(
        fake_drive, local_tree, write_mappings, engine):
    src = local_tree({"Docs/IMG.JPG": (b"img", 1_000_000_000)})
    cfg = write_mappings([_mapping(src / "Docs")])
    result = engine(cfg, settings_doc={
        "language": "en",
        "rename_ext_enabled": True,
    })
    assert result.returncode == 0, result.stdout + result.stderr
    assert (src / "Docs" / "IMG.JPG").is_file()
    assert not (src / "Docs" / "IMG.jpg").exists()


def test_explicit_rename_ext_false_is_honored_on_old_cli(
        fake_drive, local_tree, write_mappings, engine):
    fake_drive.set_version("0.4.0")
    src = local_tree({"Docs/IMG.JPG": (b"img", 1_000_000_000)})
    cfg = write_mappings([_mapping(src / "Docs")])
    result = engine(cfg, settings_doc={
        "language": "en",
        "rename_ext_enabled": False,
    })
    assert result.returncode == 0, result.stdout + result.stderr
    assert (src / "Docs" / "IMG.JPG").is_file()
    assert not (src / "Docs" / "IMG.jpg").exists()


def test_old_cli_keeps_rename_ext_default_on(
        fake_drive, local_tree, write_mappings, engine):
    fake_drive.set_version("0.4.0")
    src = local_tree({"Docs/IMG.JPG": (b"img", 1_000_000_000)})
    cfg = write_mappings([_mapping(src / "Docs")])
    result = engine(cfg, settings_doc={"language": "en"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert (src / "Docs" / "IMG.jpg").is_file()
    assert not (src / "Docs" / "IMG.JPG").exists()


def test_no_rename_ext_forces_off_for_one_run(
        fake_drive, local_tree, write_mappings, engine):
    fake_drive.set_version("0.4.0")
    src = local_tree({"Docs/IMG.JPG": (b"img", 1_000_000_000)})
    cfg = write_mappings([_mapping(src / "Docs")])
    result = engine(cfg, "--no-rename-ext", settings_doc={"language": "en"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert (src / "Docs" / "IMG.JPG").is_file()
    assert not (src / "Docs" / "IMG.jpg").exists()


def test_settings_env_override_wins(tmp_path, monkeypatch):
    chosen = tmp_path / "custom-settings.json"
    chosen.write_text("{}\n", encoding="utf-8")
    xdg = tmp_path / "xdg"
    existing = xdg / "proton-drive-sync" / "settings.json"
    existing.parent.mkdir(parents=True)
    existing.write_text('{"language": "fr"}\n', encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.setenv("PROTON_SYNC_SETTINGS", str(chosen))
    assert paths.settings_path() == str(chosen)


def test_settings_migrated_from_legacy_once(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("PROTON_SYNC_SETTINGS", raising=False)
    app = tmp_path / "app"
    app.mkdir()
    legacy = app / "settings.json"
    payload = '{"language": "fr", "rename_ext_enabled": false}\n'
    legacy.write_text(payload, encoding="utf-8")
    legacy_mtime = legacy.stat().st_mtime_ns
    monkeypatch.setattr(paths, "APP_DIR", str(app))
    monkeypatch.setattr(paths, "_unwritable_said", False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    dest = Path(paths.settings_path())
    err = capsys.readouterr().err
    assert dest.is_file()
    copied = json.loads(dest.read_text(encoding="utf-8"))
    assert copied["language"] == "fr"
    assert copied["rename_ext_enabled"] is False
    assert copied["settings_move_notice_pending"] is True
    assert legacy.read_text(encoding="utf-8") == payload
    assert legacy.stat().st_mtime_ns == legacy_mtime
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    assert "Settings copied to" in err
    copied_mtime = dest.stat().st_mtime_ns
    assert paths.settings_path() == str(dest)
    assert capsys.readouterr().err == ""
    assert dest.stat().st_mtime_ns == copied_mtime
    importlib.reload(paths)
    monkeypatch.setattr(paths, "APP_DIR", str(app))
    assert paths.settings_path() == str(dest)
    assert capsys.readouterr().err == ""
    again = json.loads(dest.read_text(encoding="utf-8"))
    assert again["settings_move_notice_pending"] is True
    assert again["language"] == "fr"
    assert legacy.stat().st_mtime_ns == legacy_mtime


def test_settings_xdg_preferred_when_both_exist(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("PROTON_SYNC_SETTINGS", raising=False)
    app = tmp_path / "app"
    app.mkdir()
    legacy = app / "settings.json"
    legacy.write_text('{"from": "legacy"}\n', encoding="utf-8")
    xdg = tmp_path / "xdg"
    dest = xdg / "proton-drive-sync" / "settings.json"
    dest.parent.mkdir(parents=True)
    dest.write_text('{"from": "xdg"}\n', encoding="utf-8")
    before = dest.stat().st_mtime_ns
    monkeypatch.setattr(paths, "APP_DIR", str(app))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    assert paths.settings_path() == str(dest)
    assert capsys.readouterr().err == ""
    assert dest.read_text(encoding="utf-8") == '{"from": "xdg"}\n'
    assert dest.stat().st_mtime_ns == before
    assert legacy.read_text(encoding="utf-8") == '{"from": "legacy"}\n'


def test_unwritable_xdg_falls_back_to_legacy_read(tmp_path, monkeypatch, capsys):
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    monkeypatch.delenv("PROTON_SYNC_SETTINGS", raising=False)
    app = tmp_path / "app"
    app.mkdir()
    legacy = app / "settings.json"
    legacy.write_text('{"language": "en"}\n', encoding="utf-8")
    xdg = tmp_path / "xdg"
    xdg.mkdir()
    xdg.chmod(0o500)
    monkeypatch.setattr(paths, "APP_DIR", str(app))
    monkeypatch.setattr(paths, "_unwritable_said", False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    try:
        got = paths.settings_path()
        err = capsys.readouterr().err
    finally:
        xdg.chmod(0o700)
    assert got == str(legacy)
    assert "Could not write settings" in err
    assert not (xdg / "proton-drive-sync" / "settings.json").exists()


def test_i18n_and_config_agree_on_settings_path(tmp_path, monkeypatch):
    monkeypatch.delenv("PROTON_SYNC_SETTINGS", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    import i18n
    assert config._write_raw("language", "fr") is True
    assert i18n.read_setting("language") == "fr"
    assert i18n.write_setting("tray_enabled", True) is True
    assert config.get("tray_enabled") is True


def test_fresh_settings_write_creates_xdg_file_mode_0600(tmp_path, monkeypatch):
    monkeypatch.delenv("PROTON_SYNC_SETTINGS", raising=False)
    app = tmp_path / "app"
    app.mkdir()
    monkeypatch.setattr(paths, "APP_DIR", str(app))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    import i18n
    assert i18n.write_setting("language", "en") is True
    dest = tmp_path / "xdg" / "proton-drive-sync" / "settings.json"
    assert dest.is_file()
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    assert not (app / "settings.json").exists()
    assert not (REPO / "settings.json").exists()
    assert i18n.write_setting("nas_mount_path", "/mnt/nas") is True
    data = json.loads(dest.read_text(encoding="utf-8"))
    assert data["language"] == "en"
    assert data["nas_mount_path"] == "/mnt/nas"


def test_nas_script_list_includes_paths():
    import realtime_manager
    assert "paths.py" in realtime_manager._NAS_SCRIPT_FILES


def test_cli_version_cache_under_data_dir(
        fake_drive, local_tree, write_mappings, engine, isolated_home):
    src = local_tree({"Docs/a.txt": (b"hello", 1_000_000_000)})
    cfg = write_mappings([_mapping(src / "Docs")])
    result = engine(cfg)
    assert result.returncode == 0, result.stdout + result.stderr
    cache = isolated_home / ".proton-drive-sync" / "cli-version.json"
    assert cache.is_file()
    assert not (isolated_home / ".proton_sync" / "cli-version.json").exists()
    stored = json.loads(cache.read_text(encoding="utf-8"))
    assert stored["version"] == "0.8.0"


def test_non_object_legacy_copy_skips_notice_key(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("PROTON_SYNC_SETTINGS", raising=False)
    app = tmp_path / "app"
    app.mkdir()
    legacy = app / "settings.json"
    legacy.write_text("[1, 2]\n", encoding="utf-8")
    monkeypatch.setattr(paths, "APP_DIR", str(app))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    dest = Path(paths.settings_path())
    assert dest.read_text(encoding="utf-8") == "[1, 2]\n"
    assert legacy.read_text(encoding="utf-8") == "[1, 2]\n"
    assert paths.settings_move_notice_pending() is False
    assert "Settings copied to" in capsys.readouterr().err


def test_invalid_legacy_json_copy_does_not_crash(tmp_path, monkeypatch):
    monkeypatch.delenv("PROTON_SYNC_SETTINGS", raising=False)
    app = tmp_path / "app"
    app.mkdir()
    legacy = app / "settings.json"
    legacy.write_text("not json\n", encoding="utf-8")
    monkeypatch.setattr(paths, "APP_DIR", str(app))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    dest = Path(paths.settings_path())
    assert dest.read_text(encoding="utf-8") == "not json\n"
    assert paths.settings_move_notice_pending() is False


def test_env_override_does_not_copy_legacy(tmp_path, monkeypatch):
    chosen = tmp_path / "custom-settings.json"
    chosen.write_text('{"language": "en"}\n', encoding="utf-8")
    app = tmp_path / "app"
    app.mkdir()
    legacy = app / "settings.json"
    legacy.write_text('{"language": "fr"}\n', encoding="utf-8")
    monkeypatch.setattr(paths, "APP_DIR", str(app))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("PROTON_SYNC_SETTINGS", str(chosen))
    assert paths.settings_path() == str(chosen)
    assert not (tmp_path / "xdg" / "proton-drive-sync" / "settings.json").exists()
    assert legacy.read_text(encoding="utf-8") == '{"language": "fr"}\n'
    assert paths.settings_move_notice_pending() is False


def test_no_legacy_means_no_settings_move_notice(tmp_path, monkeypatch):
    monkeypatch.delenv("PROTON_SYNC_SETTINGS", raising=False)
    app = tmp_path / "app"
    app.mkdir()
    monkeypatch.setattr(paths, "APP_DIR", str(app))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    dest = tmp_path / "xdg" / "proton-drive-sync" / "settings.json"
    assert paths.settings_path() == str(dest)
    assert not dest.exists()
    assert paths.settings_move_notice_pending() is False


def test_editor_clears_settings_move_notice(tmp_path, monkeypatch):
    chosen = tmp_path / "editor-settings.json"
    chosen.write_text(json.dumps({
        "language": "fr",
        "rename_ext_enabled": False,
        "custom": {"kept": 1},
        "settings_move_notice_pending": True,
    }), encoding="utf-8")
    monkeypatch.setenv("PROTON_SYNC_SETTINGS", str(chosen))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    # Même fonctions que MappingEditor._announce_settings_move, sans Tk.
    # La source anglaise, pas le catalogue : i18n peut déjà être chargé.
    assert paths.settings_move_notice_pending() is True
    monkeypatch.setattr(paths, "_", lambda message: message)
    text = paths.settings_move_notice_text()
    folder = os.path.join(str(tmp_path / "xdg"), "proton-drive-sync")
    assert str(chosen) in text
    assert "no longer read" in text
    assert "scripts folder" in text
    assert folder in text
    assert paths.clear_settings_move_notice() is True
    data = json.loads(chosen.read_text(encoding="utf-8"))
    assert "settings_move_notice_pending" not in data
    assert data["language"] == "fr"
    assert data["rename_ext_enabled"] is False
    assert data["custom"] == {"kept": 1}
    assert paths.settings_move_notice_pending() is False
    assert paths.clear_settings_move_notice() is False
    assert not (REPO / "settings.json").exists()


def test_cli_found_on_path_when_no_setting(tmp_path, monkeypatch):
    app = tmp_path / "app"
    app.mkdir()
    monkeypatch.setattr(config, "APP_DIR", str(app))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    binary = bindir / "proton-drive"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.delenv("PROTON_DRIVE_CLI", raising=False)
    found = config.resolve_proton_cli()
    assert os.path.samefile(found, binary)

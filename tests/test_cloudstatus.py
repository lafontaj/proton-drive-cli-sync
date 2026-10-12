"""File-manager status comes from the sync database, not the Proton CLI."""

import json
import os
import shutil
import subprocess
import time

import pytest

import cloudstatus
import syncdb


def _row(path, state):
    return {
        "local_path": path,
        "remote_path": "/my-files/" + os.path.basename(path),
        "state": state,
    }


def test_file_status_distinguishes_conflict_from_synced(tmp_path):
    mappings = tmp_path / "mappings.json"
    mappings.write_text("{}", encoding="utf-8")
    with syncdb.SyncDB(syncdb.database_path(str(mappings), str(tmp_path))) as db:
        db.upsert(_row("/data/Bad/b.txt", "conflict"))
        db.upsert(_row("/data/Ok/a.txt", "synced"))
        assert cloudstatus.file_status(db, "/data/Bad/b.txt") == "conflict"
        assert cloudstatus.file_status(db, "/data/Ok/a.txt") == "synced"
        assert cloudstatus.file_status(db, "/data/missing.txt") == "unknown"
        assert cloudstatus.file_status(db, "") == "unknown"


def test_account_status_is_error_when_any_row_conflicts():
    conflict, details = cloudstatus.account_status([
        _row("/data/Bad/b.txt", "conflict"),
        _row("/data/Bad/c.txt", "synced"),
    ])
    idle, idle_details = cloudstatus.account_status([
        _row("/data/Ok/a.txt", "synced"),
    ])
    syncing, _details = cloudstatus.account_status([
        _row("/data/Wait/d.txt", "pending-down"),
    ])
    assert conflict == cloudstatus.STATUS_ERROR
    assert idle == cloudstatus.STATUS_IDLE
    assert syncing == cloudstatus.STATUS_SYNCING
    assert "conflict=1" in details
    assert "synced=1" in details
    assert "conflict=0" in idle_details
    assert cloudstatus.account_status([]) == (
        cloudstatus.STATUS_IDLE,
        "synced=0 pending-up=0 pending-down=0 conflict=0 error=0 unsynced=0",
    )
    unsynced, _unsynced_details = cloudstatus.account_status([
        _row("/data/New/a.txt", "unsynced"),
    ])
    assert unsynced == cloudstatus.STATUS_SYNCING


def test_emblem_map_marks_sync_transfer_and_problem():
    assert cloudstatus.emblem_for("synced") == cloudstatus.EMBLEM_SYNCED
    assert cloudstatus.emblem_for("pending-up") == cloudstatus.EMBLEM_TRANSFERRING
    assert cloudstatus.emblem_for("pending-down") == cloudstatus.EMBLEM_TRANSFERRING
    assert cloudstatus.emblem_for("conflict") == cloudstatus.EMBLEM_PROBLEM
    assert cloudstatus.emblem_for("error") == cloudstatus.EMBLEM_PROBLEM
    assert cloudstatus.emblem_for("unsynced") == cloudstatus.EMBLEM_UNSYNCED
    assert cloudstatus.emblem_for("unknown") == ""
    assert cloudstatus.emblem_for(None) == ""
    assert set(cloudstatus.EMBLEMS) == {
        "synced", "pending-up", "pending-down", "conflict", "error", "unsynced",
    }


def test_path_emblem_marks_a_folder_from_the_files_inside(tmp_path):
    mappings = tmp_path / "mappings.json"
    mappings.write_text("{}", encoding="utf-8")
    with syncdb.SyncDB(syncdb.database_path(str(mappings), str(tmp_path))) as db:
        db.upsert(_row("/data/Ok/a.txt", "synced"))
        db.upsert(_row("/data/Wait/b.txt", "pending-up"))
        db.upsert(_row("/data/Bad/c.txt", "synced"))
        db.upsert(_row("/data/Bad/d.txt", "conflict"))
        assert cloudstatus.path_emblem(db, "/data/Ok/a.txt") == cloudstatus.EMBLEM_SYNCED
        assert cloudstatus.path_emblem(db, "/data/Ok") == cloudstatus.EMBLEM_SYNCED
        assert cloudstatus.path_emblem(db, "/data/Wait") == cloudstatus.EMBLEM_TRANSFERRING
        assert cloudstatus.path_emblem(db, "/data/Bad") == cloudstatus.EMBLEM_PROBLEM
        assert cloudstatus.path_emblem(db, "/data/Other") == ""


def test_directory_status_is_one_folder_of_emblems(tmp_path):
    mappings = tmp_path / "mappings.json"
    mappings.write_text("{}", encoding="utf-8")
    with syncdb.SyncDB(syncdb.database_path(str(mappings), str(tmp_path))) as db:
        db.upsert(_row("/data/Ok/a.txt", "synced"))
        db.upsert(_row("/data/Ok/b.txt", "pending-up"))
        db.upsert(_row("/data/Ok/c.txt", "pending-down"))
        db.upsert(_row("/data/Ok/d.txt", "conflict"))
        db.upsert(_row("/data/Ok/e.txt", "error"))
        db.upsert(_row("/data/Ok/sub/f.txt", "conflict"))
        db.upsert(_row("/data/Okay/nope.txt", "synced"))
        found = cloudstatus.directory_status(db, "/data/Ok")
    assert found == {
        "/data/Ok/a.txt": cloudstatus.EMBLEM_SYNCED,
        "/data/Ok/b.txt": cloudstatus.EMBLEM_TRANSFERRING,
        "/data/Ok/c.txt": cloudstatus.EMBLEM_TRANSFERRING,
        "/data/Ok/d.txt": cloudstatus.EMBLEM_PROBLEM,
        "/data/Ok/e.txt": cloudstatus.EMBLEM_PROBLEM,
    }
    assert cloudstatus.directory_status(db, "") == {}


def test_dolphin_plugin_uses_the_emblem_names():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cpp = open(os.path.join(
        root, "packaging", "dolphin", "proton_drive_sync_plugin.cpp"),
        encoding="utf-8").read()
    header = open(os.path.join(
        root, "packaging", "dolphin", "proton_drive_sync_plugin.h"),
        encoding="utf-8").read()
    cmake = open(os.path.join(
        root, "packaging", "dolphin", "CMakeLists.txt"),
        encoding="utf-8").read()
    for name in set(cloudstatus.EMBLEMS.values()):
        assert 'QLatin1String("%s")' % name in cpp
    assert "KOverlayIconPlugin" in cpp
    assert "K_PLUGIN_CLASS" not in cpp
    assert 'QLatin1String("emblem-new")' in cpp
    assert 'QStringLiteral("vcs-locally-modified")' in cpp
    assert "StatusChanged" in cpp
    assert "Q_SLOTS" in header
    assert "onStatusChanged" in header
    assert 'Q_PLUGIN_METADATA(IID "org.kde.overlayicon.protondrivesync")' in header
    assert 'kf6/overlayicon' in cmake


def test_rows_under_keeps_one_mapping_folder():
    rows = [
        _row("/data/Ok/a.txt", "synced"),
        _row("/data/Bad/b.txt", "conflict"),
        _row("/data/Okay/nope.txt", "synced"),
    ]
    under = cloudstatus.rows_under(rows, "/data/Ok")
    assert [row["local_path"] for row in under] == ["/data/Ok/a.txt"]


def test_status_modules_do_not_call_the_cli():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("cloudstatus.py", "cloudproviders.py", "cloudlaunch.py"):
        text = open(os.path.join(root, name), encoding="utf-8").read()
        assert "run_cli" not in text
        assert "proton_sync" not in text
        assert "subprocess" not in text


def _query(address, bad, good, account_bad, account_ok):
    script = """
import sys
import dbus
bus = dbus.bus.BusConnection(sys.argv[1])
root = bus.get_object(
    "org.protondrivesync.CloudProviders",
    "/org/protondrivesync/CloudProviders")
files = dbus.Interface(root, "org.protondrivesync.FileStatus")
print(files.GetFileStatus(sys.argv[2]))
print(files.GetFileStatus(sys.argv[3]))
directory = files.GetDirectoryStatus("/data/Bad")
items = sorted("%s=%s" % (key, directory[key]) for key in directory)
print(",".join(items))
print(files.GetPathEmblem("/data/Bad"))
props = dbus.Interface(
    bus.get_object("org.protondrivesync.CloudProviders", sys.argv[4]),
    "org.freedesktop.DBus.Properties")
print(int(props.Get("org.freedesktop.CloudProviders.Account", "Status")))
props_ok = dbus.Interface(
    bus.get_object("org.protondrivesync.CloudProviders", sys.argv[5]),
    "org.freedesktop.DBus.Properties")
print(int(props_ok.Get("org.freedesktop.CloudProviders.Account", "Status")))
managed = dbus.Interface(root, "org.freedesktop.DBus.ObjectManager")
objects = managed.GetManagedObjects()
print(sys.argv[4] in objects)
"""
    return subprocess.run(
        ["/usr/bin/python3", "-c", script,
         address, bad, good, account_bad, account_ok],
        capture_output=True, text=True, timeout=10)


def test_running_service_distinguishes_conflict_from_synced(tmp_path):
    if shutil.which("dbus-daemon") is None or not os.path.isfile("/usr/bin/python3"):
        pytest.skip("dbus-daemon or system python3 is not available")
    mappings = tmp_path / "mappings.json"
    mappings.write_text(json.dumps({
        "mappings": [
            {"type": "folder", "source": "/data/Ok",
             "dest_parent": "/my-files/Ok", "direction": "twoway"},
            {"type": "folder", "source": "/data/Bad",
             "dest_parent": "/my-files/Bad", "direction": "twoway"},
        ],
    }), encoding="utf-8")
    with syncdb.SyncDB(syncdb.database_path(str(mappings), str(tmp_path))) as db:
        db.upsert(_row("/data/Ok/a.txt", "synced"))
        db.upsert(_row("/data/Bad/b.txt", "conflict"))
        db.upsert(_row("/data/Bad/c.txt", "synced"))
    daemon = subprocess.Popen(
        ["dbus-daemon", "--session", "--nofork", "--print-address=1"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    service = None
    try:
        address = daemon.stdout.readline().strip()
        assert address.startswith("unix:"), address or daemon.stderr.read()
        env = os.environ.copy()
        env["DBUS_SESSION_BUS_ADDRESS"] = address
        env.pop("DBUS_SESSION_BUS_PID", None)
        env["PATH"] = "/usr/bin:/bin"
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        service = subprocess.Popen(
            ["/usr/bin/python3", os.path.join(root, "cloudproviders.py"),
             "--mappings", str(mappings), "--data-dir", str(tmp_path)],
            cwd=root, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        bad = "/data/Bad/b.txt"
        good = "/data/Bad/c.txt"
        account_ok = cloudstatus.OBJECT_PATH + "/Account/0"
        account_bad = cloudstatus.OBJECT_PATH + "/Account/1"
        last = None
        for _attempt in range(50):
            if service.poll() is not None:
                raise AssertionError(service.stderr.read())
            last = _query(address, bad, good, account_bad, account_ok)
            if last.returncode == 0:
                break
            time.sleep(0.1)
        else:
            raise AssertionError(last.stderr if last else "no query")
        lines = last.stdout.splitlines()
        assert lines[0] == "conflict"
        assert lines[1] == "synced"
        assert lines[0] != lines[1]
        assert lines[2] == ",".join(sorted([
            "/data/Bad/b.txt=" + cloudstatus.EMBLEM_PROBLEM,
            "/data/Bad/c.txt=" + cloudstatus.EMBLEM_SYNCED,
        ]))
        assert lines[3] == cloudstatus.EMBLEM_PROBLEM
        assert lines[4] == str(cloudstatus.STATUS_ERROR)
        assert lines[5] == str(cloudstatus.STATUS_IDLE)
        assert lines[6] == "True"
    finally:
        if service is not None and service.poll() is None:
            service.terminate()
            service.wait(timeout=5)
        if daemon.poll() is None:
            daemon.terminate()
            daemon.wait(timeout=5)


def test_marks_follow_the_folder_links_in_proton_drive(tmp_path):
    real = tmp_path / "data" / "Docs"
    real.mkdir(parents=True)
    real = type(real)(os.path.realpath(str(real)))
    (real / "a.txt").write_text("a", encoding="utf-8")
    root = tmp_path / "Proton Drive"
    root.mkdir()
    os.symlink(str(real), str(root / "Docs"))
    shown = os.path.join(str(root), "Docs")
    mappings = tmp_path / "mappings.json"
    mappings.write_text("{}", encoding="utf-8")
    with syncdb.SyncDB(syncdb.database_path(str(mappings), str(tmp_path))) as db:
        db.upsert(_row(str(real / "a.txt"), "conflict"))
        assert cloudstatus.file_status(db, os.path.join(shown, "a.txt")) == "conflict"
        assert cloudstatus.path_emblem(db, shown) == cloudstatus.EMBLEM_PROBLEM
        assert cloudstatus.directory_status(db, shown) == {
            os.path.join(shown, "a.txt"): cloudstatus.EMBLEM_PROBLEM}
        # The real paths still answer as before.
        assert cloudstatus.directory_status(db, str(real)) == {
            str(real / "a.txt"): cloudstatus.EMBLEM_PROBLEM}

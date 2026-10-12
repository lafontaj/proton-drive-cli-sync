"""Volume folder, Dolphin place, and the status process the window owns."""

import os
import sqlite3

import pytest

import syncdb
import volume
from ui.status_service import StatusService, close_action


def test_empty_directory_is_refused_when_it_has_files(tmp_path):
    folder = tmp_path / "Proton Drive"
    folder.mkdir()
    (folder / "already.txt").write_text("x", encoding="utf-8")
    try:
        volume.ensure_empty_directory(str(folder))
    except ValueError as exc:
        assert str(exc) == "not empty"
    else:
        raise AssertionError("a folder with files was accepted")
    empty = tmp_path / "Empty"
    volume.ensure_empty_directory(str(empty))
    assert empty.is_dir()
    assert list(empty.iterdir()) == []


def test_dolphin_place_is_added_without_removing_other_bookmarks(tmp_path):
    places = tmp_path / "user-places.xbel"
    places.write_text(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<xbel version=\"1.0\">\n"
        " <bookmark href=\"file:///home/keep\">\n"
        "  <title>Keep</title>\n"
        " </bookmark>\n"
        "</xbel>\n",
        encoding="utf-8")
    local = tmp_path / "Proton Drive"
    local.mkdir()
    assert volume.ensure_dolphin_place(str(local), str(places))
    text = places.read_text(encoding="utf-8")
    assert "file:///home/keep" in text
    assert volume.PLACE_ID in text
    assert "Proton%20Drive" in text
    assert volume.ensure_dolphin_place(str(local), str(places))
    again = places.read_text(encoding="utf-8")
    assert again.count(volume.PLACE_ID) == 1
    assert "file:///home/keep" in again


def test_local_change_is_unsynced_then_pending_then_synced(tmp_path, monkeypatch):
    import twoway
    root = tmp_path / "Proton Drive"
    root.mkdir()
    local = root / "a.txt"
    local.write_bytes(b"AAAA")
    os.utime(local, (1_000_000_000, 1_000_000_000))
    cfg = tmp_path / "mappings.json"
    cfg.write_text("[]", encoding="utf-8")
    mapping = volume.mapping_for(str(root))
    assert volume.note_local_change(str(cfg), [mapping], str(local))
    with syncdb.SyncDB(syncdb.database_path(str(cfg))) as db:
        assert db.get(os.path.normpath(str(local)))["state"] == "unsynced"

    seen = []

    def upload_batch(paths, folder, **kwargs):
        with syncdb.SyncDB(syncdb.database_path(str(cfg))) as db:
            seen.append(db.get(os.path.normpath(str(local)))["state"])
        return True

    monkeypatch.setattr(twoway._ps, "upload_batch", upload_batch)
    ctx = twoway._Ctx(
        mapping, syncdb.SyncDB(syncdb.database_path(str(cfg))),
        None, None, False, False, {}, str(root), str(cfg))
    assert twoway._upload(str(local), "/my-files", None, ctx)
    assert seen == ["pending-up"]
    with syncdb.SyncDB(syncdb.database_path(str(cfg))) as db:
        assert db.get(os.path.normpath(str(local)))["state"] == "synced"
    ctx.db.close()


def test_schema_v1_accepts_unsynced_after_open(tmp_path):
    path = tmp_path / "old.sync.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta (key, value) VALUES ('schema_version', '1');
        CREATE TABLE files (
            local_path TEXT PRIMARY KEY,
            remote_path TEXT NOT NULL,
            remote_node_id TEXT,
            sha1 TEXT,
            claimed_size INTEGER,
            claimed_mtime REAL,
            local_inode INTEGER,
            local_mtime REAL,
            state TEXT NOT NULL CHECK (
                state IN ('synced', 'pending-up', 'pending-down', 'conflict', 'error')
            )
        );
        """
    )
    conn.execute(
        "INSERT INTO files (local_path, remote_path, state) VALUES (?, ?, ?)",
        ("/data/a.txt", "/my-files/a.txt", "synced"),
    )
    conn.commit()
    conn.close()
    with syncdb.SyncDB(str(path)) as db:
        db.upsert({
            "local_path": "/data/b.txt",
            "remote_path": "/my-files/b.txt",
            "state": "unsynced",
        })
        assert db.get("/data/a.txt")["state"] == "synced"
        assert db.get("/data/b.txt")["state"] == "unsynced"


def test_choose_mapping_points_proton_drive_at_that_folder(tmp_path, monkeypatch):
    import local_watcher
    docs = tmp_path / "Docs"
    other = tmp_path / "Other"
    docs.mkdir()
    other.mkdir()
    (docs / "a.txt").write_bytes(b"AAAA")
    rows = [
        {"type": "folder", "source": str(docs), "dest_parent": "/my-files/Backups"},
        {"type": "folder", "source": str(other), "dest_parent": "/my-files/Other"},
    ]
    chosen = volume.mark_live(rows, str(docs), allow_delete=True)
    assert chosen["live"] is True
    assert chosen["live_confirmed"] is True
    assert chosen["direction"] == "twoway"
    assert chosen["allow_delete"] is True
    assert chosen["delete_mode"] == "trash"
    assert "volume" not in chosen
    assert "live" not in rows[1]
    cfg = tmp_path / "mappings.json"
    cfg.write_text("[]", encoding="utf-8")
    assert volume.note_local_change(str(cfg), rows, str(docs / "a.txt"))
    with syncdb.SyncDB(syncdb.database_path(str(cfg))) as db:
        row = db.get(os.path.normpath(str(docs / "a.txt")))
    assert row["state"] == "unsynced"
    assert row["remote_path"] == "/my-files/Backups/Docs/a.txt"
    monkeypatch.setattr(local_watcher, "source_kind_of", lambda _path: "local")
    watched = local_watcher.select_targets(rows)
    assert [item["watch_dir"] for item in watched] == [os.path.normpath(str(docs))]
    places = tmp_path / "user-places.xbel"
    volume.ensure_dolphin_place(str(docs), str(places))
    text = places.read_text(encoding="utf-8")
    assert volume.PLACE_TITLE in text
    assert text.count("<bookmark ") == 1


def test_nothing_is_chosen_for_the_person():
    rows = [
        {"type": "folder", "source": "/data/Docs", "dest_parent": "/my-files/Docs"},
        {"type": "folder", "source": "/data/Other", "dest_parent": "/my-files/Other",
         "direction": "twoway"},
    ]
    before = [dict(row) for row in rows]
    assert volume.confirmed_live(rows) is None
    assert volume.unconfirmed_live(rows) is None
    assert rows == before
    assert not hasattr(volume, "ensure_chosen")


def test_choosing_a_folder_keeps_its_deletion_answer():
    rows = [
        {"type": "folder", "source": "/data/Docs", "dest_parent": "/my-files/Docs",
         "allow_delete": True, "delete_mode": "permanent"},
        {"type": "folder", "source": "/data/Other", "dest_parent": "/my-files/Other",
         "live": True, "live_confirmed": True},
    ]
    chosen = volume.mark_live(rows, "/data/Docs", allow_delete=False)
    assert chosen["allow_delete"] is False
    assert chosen["direction"] == "twoway"
    assert volume.confirmed_live(rows) is chosen
    assert "live" not in rows[1] and "live_confirmed" not in rows[1]
    untouched = [{"type": "folder", "source": "/data/X", "dest_parent": "/my-files"}]
    volume.mark_live(untouched, "/data/X")
    assert "allow_delete" not in untouched[0]


def test_a_trial_pick_is_not_resumed_and_can_be_cleared():
    rows = [
        {"type": "folder", "source": "/data/Docs", "dest_parent": "/my-files/Docs",
         "direction": "twoway", "live": True, "allow_delete": True,
         "delete_mode": "trash"},
    ]
    assert volume.confirmed_live(rows) is None
    assert volume.unconfirmed_live(rows) is rows[0]
    was = volume.clear_live(rows, allow_delete=False)
    assert was is rows[0]
    assert "live" not in rows[0]
    assert rows[0]["allow_delete"] is False
    assert rows[0]["direction"] == "twoway"
    assert volume.unconfirmed_live(rows) is None


def test_a_change_is_due_after_the_quiet_period():
    import time
    from ui.live_sync import LiveSync, PassFollow, due, ignored_edit_name, status_line
    assert due(None, 10) is False
    assert due(8, 9) is False
    assert due(8, 10) is True
    assert ignored_edit_name(".swp") is True
    assert ignored_edit_name("notes.txt~") is True
    assert ignored_edit_name("notes.txt") is False
    fired = []
    sync = LiveSync()
    sync._on_change = lambda: fired.append("go")
    sync._dirty_at = time.monotonic()
    sync._pump_once()
    assert fired == []
    sync._dirty_at = time.monotonic() - 3
    sync._pump_once()
    assert fired == ["go"]
    follow = PassFollow()
    assert follow.request(True) is False
    assert follow.request(True) is False
    assert follow.finished() is True
    assert follow.finished() is False
    line = status_line("/data/Docs", 1_700_000_000, 1_700_000_030)
    assert line.startswith("Docs — last pass ")
    assert "next remote check" in line


def _two_way(source, **extra):
    mapping = {"type": "folder", "source": str(source),
               "dest_parent": "/my-files", "direction": "twoway"}
    mapping.update(extra)
    return mapping


def test_drive_root_shows_each_two_way_folder(tmp_path):
    docs, work, up = (tmp_path / "a" / "Docs", tmp_path / "b" / "Docs",
                      tmp_path / "Uploads")
    for folder in (docs, work, up):
        folder.mkdir(parents=True)
    (docs / "d.txt").write_text("d", encoding="utf-8")
    root, record = tmp_path / "Proton Drive", tmp_path / "state" / "links.json"
    mappings = [_two_way(docs), _two_way(work),
                {"type": "folder", "source": str(up), "dest_parent": "/my-files"}]
    assert volume.ensure_drive_root(mappings, str(root), str(record)) == str(root)
    assert root.is_dir() and not root.is_symlink()
    # Same folder name twice: the second gets a number. Upload-only is left out.
    assert sorted(os.listdir(root)) == ["Docs", "Docs (2)"]
    assert (root / "Docs" / "d.txt").read_text(encoding="utf-8") == "d"
    # Running again changes nothing.
    volume.ensure_drive_root(mappings, str(root), str(record))
    assert sorted(os.listdir(root)) == ["Docs", "Docs (2)"]


def test_drive_root_drops_only_its_own_stale_links(tmp_path):
    docs, other = tmp_path / "Docs", tmp_path / "Other"
    docs.mkdir()
    other.mkdir()
    root, record = tmp_path / "Proton Drive", tmp_path / "links.json"
    volume.ensure_drive_root([_two_way(docs)], str(root), str(record))
    mine = root / "Mine"
    os.symlink(str(other), str(mine))
    (root / "notes.txt").write_text("keep", encoding="utf-8")
    # The mapping became upload-only: its link goes, the folder stays.
    volume.ensure_drive_root(
        [{"type": "folder", "source": str(docs), "dest_parent": "/my-files"}],
        str(root), str(record))
    assert sorted(os.listdir(root)) == ["Mine", "notes.txt"]
    assert docs.is_dir()


def test_drive_root_replaces_the_old_single_folder_link(tmp_path):
    docs = tmp_path / "Docs"
    docs.mkdir()
    (docs / "a.txt").write_text("x", encoding="utf-8")
    root = tmp_path / "Proton Drive"
    os.symlink(str(docs), str(root))
    volume.ensure_drive_root([_two_way(docs)], str(root), str(tmp_path / "l.json"))
    assert root.is_dir() and not root.is_symlink()
    assert os.listdir(root) == ["Docs"]
    assert (docs / "a.txt").read_text(encoding="utf-8") == "x"


def test_drive_root_adds_no_links_where_they_would_be_uploaded(tmp_path):
    root, docs = tmp_path / "Proton Drive", tmp_path / "Docs"
    root.mkdir()
    docs.mkdir()
    record = tmp_path / "l.json"
    # A volume syncs the root itself.
    volume.ensure_drive_root(
        [volume.mapping_for(str(root)), _two_way(docs)], str(root), str(record))
    assert os.listdir(root) == []
    # The root inside a mapped folder.
    volume.ensure_drive_root(
        [_two_way(tmp_path), _two_way(docs)], str(root), str(record))
    assert os.listdir(root) == []
    # A file in the way is refused, not replaced.
    blocked = tmp_path / "file"
    blocked.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        volume.ensure_drive_root([_two_way(docs)], str(blocked), str(record))
    assert blocked.read_text(encoding="utf-8") == "x"


def test_new_places_file_is_the_shape_dolphin_reads(tmp_path):
    places = tmp_path / "user-places.xbel"
    local = tmp_path / "Docs"
    local.mkdir()
    assert volume.ensure_dolphin_place(str(local), str(places))
    text = places.read_text(encoding="utf-8")
    assert "<!DOCTYPE xbel>" in text
    assert "bookmark:icon" in text
    assert "<ID>" + volume.PLACE_ID + "</ID>" in text
    assert "<IsHidden>false</IsHidden>" in text
    assert volume.PLACE_TITLE in text
    assert "file:///home/keep" not in text


def test_close_action_hides_until_quit():
    assert close_action(False, False, True) == "hide"
    assert close_action(False, True, False) == "stay"
    assert close_action(True, True, False) == "quit"


class _Proc:
    def __init__(self):
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def kill(self):
        self.returncode = 0


def test_status_service_starts_one_child_and_stops_it(tmp_path):
    mappings = tmp_path / "mappings.json"
    mappings.write_text("[]", encoding="utf-8")
    started = []

    def popen(argv, **kwargs):
        started.append(argv)
        return _Proc()

    service = StatusService(probe=lambda: False, popen=popen)
    assert service.ensure(str(mappings)) is True
    assert service.ensure(str(mappings)) is True
    assert len(started) == 1
    assert started[0][1].endswith("cloudproviders.py")
    assert started[0][2:] == ["--mappings", str(mappings)]
    service.stop()
    assert service.running() is False
    busy = StatusService(probe=lambda: True, popen=popen)
    assert busy.ensure(str(mappings)) is False
    assert len(started) == 1


def test_moving_the_place_never_adds_one_back(tmp_path):
    places = tmp_path / "user-places.xbel"
    old, root = tmp_path / "Docs", tmp_path / "Proton Drive"
    old.mkdir()
    root.mkdir()
    assert volume.ensure_dolphin_place(str(root), str(places), add=False) is False
    assert not places.exists()
    assert volume.ensure_dolphin_place(str(old), str(places))
    assert volume.ensure_dolphin_place(str(root), str(places), add=False)
    text = places.read_text(encoding="utf-8")
    assert volume._file_uri(str(root)) in text
    assert volume._file_uri(str(old)) + '"' not in text
    # Removed by the person: saving does not bring it back.
    places.write_text(text.replace(volume.PLACE_ID, "gone"), encoding="utf-8")
    assert volume.ensure_dolphin_place(str(root), str(places), add=False) is False
    assert volume.PLACE_ID not in places.read_text(encoding="utf-8")

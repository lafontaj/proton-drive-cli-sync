"""One twoway mapping round-trips both directions and a conflict.

A one-way mapping in the same file never downloads.
"""

import os

import remote_state
import syncdb


def _mapping(source, dest, **extra):
    mapping = {
        "type": "folder",
        "source": str(source),
        "dest_parent": dest,
    }
    mapping.update(extra)
    return mapping


def _downloads(calls):
    return [
        call for call in calls
        if len(call) >= 2 and call[0] == "filesystem" and call[1] == "download"
    ]


def _json_lists(calls):
    found = []
    for call in calls:
        if len(call) >= 3 and call[0] == "filesystem" and call[1] == "list" and "-j" in call:
            found.append(call[2])
    return found


def _set_remote(fake_drive, path, data, mtime):
    def mutate(state):
        remote_state.store_file(state, path, data, mtime)
        normalized = remote_state.normalize(path)
        if state["uploads"] and state["uploads"][-1] == normalized:
            state["uploads"].pop()
    fake_drive._update(mutate)


def _db(isolated_home, cfg):
    return syncdb.database_path(
        os.path.basename(str(cfg)),
        data_dir=str(isolated_home / ".proton-drive-sync"))


def test_volume_downloads_into_the_local_root(
        fake_drive, local_tree, write_mappings, engine):
    src = local_tree({})
    fake_drive.seed_file("/my-files/hello.txt", b"HELLO", 1_000_000_000)
    cfg = write_mappings([
        _mapping(src, "/my-files", direction="twoway", volume=True,
                 allow_delete=False),
    ])
    result = engine(cfg)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (src / "hello.txt").read_bytes() == b"HELLO"
    assert not (src / src.name).exists()
    listed = _json_lists(fake_drive.calls())
    assert "/my-files" in listed
    assert "/my-files/" + src.name not in listed


def test_volume_subpath_does_not_append_the_local_folder_name():
    import proton_sync
    volume = {
        "source": "/data/Proton Drive",
        "dest_parent": "/my-files",
        "direction": "twoway",
        "volume": True,
    }
    parent, err = proton_sync._remote_parent_for_subpath(
        volume, "/data/Proton Drive/Docs")
    assert err is None
    assert parent == "/my-files"
    root, err = proton_sync._remote_parent_for_subpath(
        volume, "/data/Proton Drive")
    assert err is None
    assert root == "/my-files"
    normal = {
        "source": "/data/Docs",
        "dest_parent": "/my-files/Backups",
        "direction": "twoway",
    }
    parent, err = proton_sync._remote_parent_for_subpath(normal, "/data/Docs/Sub")
    assert err is None
    assert parent == "/my-files/Backups/Docs"


def test_failed_upload_is_recorded_as_error(
        fake_drive, local_tree, write_mappings, engine, isolated_home):
    src = local_tree({"Docs/a.txt": (b"AAAA", 1_000_000_000)})
    cfg = write_mappings([
        _mapping(src / "Docs", "/my-files/Backups", direction="twoway"),
    ])
    fake_drive.add_fault(cmd="upload", match="a.txt", mode="fail", times=5)
    result = engine(cfg)
    assert result.returncode == 5, result.stdout + result.stderr
    with syncdb.SyncDB(_db(isolated_home, cfg)) as database:
        row = database.get(str(src / "Docs" / "a.txt"))
    assert row["state"] == "error"


def test_twoway_round_trip_and_one_way_does_not_download(
        fake_drive, local_tree, write_mappings, engine):
    src = local_tree({
        "Two/a.txt": (b"AAAA", 1_000_000_000),
        "One/b.txt": (b"BBBB", 1_000_000_000),
    })
    cfg = write_mappings([
        _mapping(src / "Two", "/my-files/TwoWay", direction="twoway"),
        _mapping(src / "One", "/my-files/OneWay"),
    ])
    remote = "/my-files/TwoWay/Two/a.txt"
    one_remote = "/my-files/OneWay/One/b.txt"

    first = engine(cfg)
    assert first.returncode == 0, first.stdout + first.stderr
    assert fake_drive.content(remote) == b"AAAA"
    assert fake_drive.content(one_remote) == b"BBBB"
    assert _downloads(fake_drive.calls()) == []

    before = len(fake_drive.calls())
    again = engine(cfg)
    assert again.returncode == 0, again.stdout + again.stderr
    second = fake_drive.calls()[before:]
    assert "/my-files/TwoWay/Two" in _json_lists(second)
    assert _downloads(second) == []

    local_tree.write("Two/a.txt", b"BBBB", 1_000_000_100)
    uploaded = engine(cfg)
    assert uploaded.returncode == 0, uploaded.stdout + uploaded.stderr
    assert fake_drive.content(remote) == b"BBBB"
    assert (src / "Two" / "a.txt").read_bytes() == b"BBBB"

    _set_remote(fake_drive, remote, b"CCCC", 1_000_000_200)
    downloaded = engine(cfg)
    assert downloaded.returncode == 0, downloaded.stdout + downloaded.stderr
    assert "[download] " in downloaded.stdout
    assert (src / "Two" / "a.txt").read_bytes() == b"CCCC"
    assert fake_drive.content(remote) == b"CCCC"

    local_tree.write("Two/a.txt", b"DDDD", 1_000_000_300)
    _set_remote(fake_drive, remote, b"EEEE", 1_000_000_400)
    conflict = engine(cfg)
    assert conflict.returncode == 0, conflict.stdout + conflict.stderr
    assert "[conflict] " in conflict.stdout
    assert (src / "Two" / "a.txt").read_bytes() == b"DDDD"
    assert fake_drive.content(remote) == b"EEEE"
    assert (src / "Two" / "a (proton conflict).txt").read_bytes() == b"EEEE"

    one_way_downloads = [
        call for call in _downloads(fake_drive.calls())
        if any("/my-files/OneWay" in part for part in call)
    ]
    assert one_way_downloads == []


def test_local_deletion_restores_on_the_next_pass_unless_deletion_is_on(
        fake_drive, local_tree, write_mappings, engine):
    src = local_tree({"Docs/a.txt": (b"AAAA", 1_000_000_000)})
    remote = "/my-files/Backups/Docs/a.txt"
    keep = write_mappings([
        _mapping(src / "Docs", "/my-files/Backups", direction="twoway"),
    ])
    assert engine(keep).returncode == 0
    (src / "Docs" / "a.txt").unlink()
    kept = engine(keep)
    assert kept.returncode == 0, kept.stdout + kept.stderr
    assert fake_drive.content(remote) == b"AAAA"
    assert not (src / "Docs" / "a.txt").exists()
    restored = engine(keep)
    assert restored.returncode == 0, restored.stdout + restored.stderr
    assert (src / "Docs" / "a.txt").read_bytes() == b"AAAA"

    src2 = local_tree({"Gone/a.txt": (b"AAAA", 1_000_000_000)})
    remote2 = "/my-files/TrashMe/Gone/a.txt"
    deleting = write_mappings([
        _mapping(src2 / "Gone", "/my-files/TrashMe", direction="twoway",
                 allow_delete=True, source_kind="local"),
    ])
    assert engine(deleting).returncode == 0
    (src2 / "Gone" / "a.txt").unlink()
    trashed = engine(deleting, "--delete")
    assert trashed.returncode == 0, trashed.stdout + trashed.stderr
    assert fake_drive.trashed(remote2)
    assert not (src2 / "Gone" / "a.txt").exists()


def test_shared_folder_is_not_trashed_without_confirmation(
        fake_drive, local_tree, write_mappings, engine):
    fake_drive.seed_folder("/shared-with-me/Box")
    src = local_tree({"Docs/a.txt": (b"AAAA", 1_000_000_000)})
    remote = "/shared-with-me/Box/Docs/a.txt"
    cfg = write_mappings([
        _mapping(src / "Docs", "/shared-with-me/Box", direction="twoway",
                 allow_delete=True, source_kind="local"),
    ])
    assert engine(cfg).returncode == 0
    (src / "Docs" / "a.txt").unlink()
    kept = engine(cfg, "--delete")
    assert kept.returncode == 0, kept.stdout + kept.stderr
    assert not fake_drive.trashed(remote)
    assert fake_drive.content(remote) == b"AAAA"


def test_confirmed_shared_folder_may_trash(
        fake_drive, local_tree, write_mappings, engine):
    fake_drive.seed_folder("/shared-with-me/Box")
    src = local_tree({"Docs/a.txt": (b"AAAA", 1_000_000_000)})
    remote = "/shared-with-me/Box/Docs/a.txt"
    cfg = write_mappings([
        _mapping(src / "Docs", "/shared-with-me/Box", direction="twoway",
                 allow_delete=True, source_kind="local",
                 shared_delete_confirmed=True),
    ])
    assert engine(cfg).returncode == 0
    (src / "Docs" / "a.txt").unlink()
    trashed = engine(cfg, "--delete")
    assert trashed.returncode == 0, trashed.stdout + trashed.stderr
    assert fake_drive.trashed(remote)


def test_missing_remote_moves_the_local_file_aside(
        fake_drive, local_tree, write_mappings, engine):
    src = local_tree({"Docs/a.txt": (b"AAAA", 1_000_000_000)})
    remote = "/my-files/Backups/Docs/a.txt"
    cfg = write_mappings([
        _mapping(src / "Docs", "/my-files/Backups", direction="twoway"),
    ])
    assert engine(cfg).returncode == 0
    def mutate(state):
        state["nodes"].pop(remote_state.normalize(remote), None)
    fake_drive._update(mutate)
    result = engine(cfg, "--delete")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[held] " in result.stdout
    assert not (src / "Docs" / "a.txt").exists()
    dest = result.stdout.split("[held] ", 1)[1].split(" -> ", 1)[1].splitlines()[0].strip()
    assert os.path.isfile(dest)
    assert open(dest, "rb").read() == b"AAAA"


def test_holding_the_same_name_again_keeps_every_copy(
        fake_drive, local_tree, write_mappings, engine):
    src = local_tree({"Docs/a.txt": (b"v1", 1_000_000_000)})
    remote = "/my-files/Backups/Docs/a.txt"
    cfg = write_mappings([
        _mapping(src / "Docs", "/my-files/Backups", direction="twoway"),
    ])
    held = []
    for i, data in enumerate((b"v1", b"v2", b"v3")):
        if i:
            local_tree.write("Docs/a.txt", data, 1_000_000_000 + i * 100)
        assert engine(cfg).returncode == 0
        assert fake_drive.content(remote) == data
        fake_drive._update(
            lambda state: state["nodes"].pop(remote_state.normalize(remote), None))
        result = engine(cfg, "--delete")
        assert result.returncode == 0, result.stdout + result.stderr
        held.append(result.stdout.split("[held] ", 1)[1].split(" -> ", 1)[1]
                    .splitlines()[0].strip())
    assert len(set(held)) == 3
    assert [open(p, "rb").read() for p in held] == [b"v1", b"v2", b"v3"]


def test_failed_listing_does_not_update_the_sync_row(
        fake_drive, local_tree, write_mappings, engine, isolated_home):
    src = local_tree({"Docs/a.txt": (b"AAAA", 1_000_000_000)})
    cfg = write_mappings([
        _mapping(src / "Docs", "/my-files/Backups", direction="twoway"),
    ])
    assert engine(cfg).returncode == 0
    with syncdb.SyncDB(_db(isolated_home, cfg)) as database:
        before = database.get(str(src / "Docs" / "a.txt"))
    assert before["state"] == "synced"
    assert before["sha1"]
    local_tree.write("Docs/a.txt", b"BBBB", 1_000_000_100)
    fake_drive.add_fault(
        cmd="list", match="/my-files/Backups/Docs", mode="fail", times=1)
    failed = engine(cfg)
    assert failed.returncode == 5, failed.stdout + failed.stderr
    assert "[list-skipped] " in failed.stdout
    assert (src / "Docs" / "a.txt").read_bytes() == b"BBBB"
    assert fake_drive.content("/my-files/Backups/Docs/a.txt") == b"AAAA"
    with syncdb.SyncDB(_db(isolated_home, cfg)) as database:
        after = database.get(str(src / "Docs" / "a.txt"))
    assert after["sha1"] == before["sha1"]
    assert after["state"] == "synced"


def test_proton_document_is_logged_and_not_deleted(
        fake_drive, local_tree, write_mappings, engine, isolated_home):
    src = local_tree({"Docs/a.txt": (b"AAAA", 1_000_000_000)})
    cfg = write_mappings([
        _mapping(src / "Docs", "/my-files/Backups", direction="twoway"),
    ])
    def mutate(state):
        path = "/my-files/Backups/Docs/notes.doc"
        remote_state.ensure_folders(state, path)
        state["nodes"][path] = {"type": "document"}
    fake_drive._update(mutate)
    result = engine(cfg)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[download-skipped] " in result.stdout
    marker = src / "Docs" / "notes.doc"
    assert marker.read_text(encoding="utf-8").startswith("Proton web document")
    assert fake_drive.content("/my-files/Backups/Docs/a.txt") == b"AAAA"
    with syncdb.SyncDB(_db(isolated_home, cfg)) as database:
        row = database.get(str(marker))
    assert row["state"] == "synced"
    again = engine(cfg)
    assert again.returncode == 0, again.stdout + again.stderr
    assert "/my-files/Backups/Docs/notes.doc" not in fake_drive.uploads()
    assert marker.read_text(encoding="utf-8").startswith("Proton web document")


def test_remote_file_downloads_and_a_document_is_not_uploaded(
        fake_drive, local_tree, write_mappings, engine):
    src = local_tree({"Docs/keep.txt": (b"KEEP", 1_000_000_000)})
    fake_drive.seed_file("/my-files/Backups/Docs/new.txt", b"NEW", 1_000_000_000)
    def mutate(state):
        path = "/my-files/Backups/Docs/notes"
        remote_state.ensure_folders(state, path)
        state["nodes"][path] = {"type": "document"}
    fake_drive._update(mutate)
    cfg = write_mappings([
        _mapping(src / "Docs", "/my-files/Backups", direction="twoway"),
    ])
    result = engine(cfg)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (src / "Docs" / "new.txt").read_bytes() == b"NEW"
    assert (src / "Docs" / "keep.txt").read_bytes() == b"KEEP"
    assert (src / "Docs" / "notes").read_text(encoding="utf-8").startswith(
        "Proton web document")
    assert "[download-skipped] " in result.stdout
    again = engine(cfg)
    assert again.returncode == 0, again.stdout + again.stderr
    assert (src / "Docs" / "keep.txt").read_bytes() == b"KEEP"
    assert "/my-files/Backups/Docs/notes" not in fake_drive.uploads()


def test_one_mapping_trashes_and_the_other_does_not(
        fake_drive, local_tree, write_mappings, engine):
    src = local_tree({
        "Docs/a.txt": (b"AAAA", 1_000_000_000),
        "Other/b.txt": (b"BBBB", 1_000_000_000),
    })
    remote_a = "/my-files/Backups/Docs/a.txt"
    remote_b = "/my-files/Other/Other/b.txt"
    cfg = write_mappings([
        _mapping(src / "Docs", "/my-files/Backups", direction="twoway",
                 allow_delete=True, delete_mode="trash", source_kind="local",
                 live=True, live_confirmed=True),
        _mapping(src / "Other", "/my-files/Other", direction="twoway",
                 allow_delete=False),
    ])
    assert engine(cfg, "--delete").returncode == 0
    (src / "Docs" / "a.txt").unlink()
    (src / "Other" / "b.txt").unlink()
    result = engine(cfg, "--delete")
    assert result.returncode == 0, result.stdout + result.stderr
    assert fake_drive.trashed(remote_a)
    assert not fake_drive.trashed(remote_b)
    assert fake_drive.content(remote_b) == b"BBBB"


def _state(isolated_home, cfg, path):
    with syncdb.SyncDB(_db(isolated_home, cfg)) as database:
        row = database.get(str(path))
    return row["state"] if row else None


def _trashing(src, dest):
    return _mapping(src, dest, direction="twoway", allow_delete=True,
                    delete_mode="trash", source_kind="local")


def test_allow_delete_alone_does_not_trash_without_the_delete_switch(
        fake_drive, local_tree, write_mappings, engine):
    src = local_tree({"Docs/a.txt": (b"AAAA", 1_000_000_000)})
    remote = "/my-files/Backups/Docs/a.txt"
    cfg = write_mappings([_trashing(src / "Docs", "/my-files/Backups")])
    assert engine(cfg).returncode == 0
    (src / "Docs" / "a.txt").unlink()
    first = engine(cfg)
    assert first.returncode == 0, first.stdout + first.stderr
    assert not fake_drive.trashed(remote)
    assert fake_drive.content(remote) == b"AAAA"
    second = engine(cfg)
    assert second.returncode == 0, second.stdout + second.stderr
    assert not fake_drive.trashed(remote)
    assert (src / "Docs" / "a.txt").read_bytes() == b"AAAA"


def test_local_deletion_does_not_trash_a_newer_remote_edit(
        fake_drive, local_tree, write_mappings, engine):
    src = local_tree({"Docs/a.txt": (b"AAAA", 1_000_000_000)})
    remote = "/my-files/Backups/Docs/a.txt"
    cfg = write_mappings([_trashing(src / "Docs", "/my-files/Backups")])
    assert engine(cfg, "--delete").returncode == 0
    (src / "Docs" / "a.txt").unlink()
    _set_remote(fake_drive, remote, b"NEWER", 1_000_000_500)
    result = engine(cfg, "--delete")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not fake_drive.trashed(remote)
    assert fake_drive.content(remote) == b"NEWER"
    assert (src / "Docs" / "a.txt").read_bytes() == b"NEWER"


def test_remote_deletion_does_not_drop_a_newer_local_edit(
        fake_drive, local_tree, write_mappings, engine):
    src = local_tree({"Docs/a.txt": (b"AAAA", 1_000_000_000)})
    remote = "/my-files/Backups/Docs/a.txt"
    cfg = write_mappings([_trashing(src / "Docs", "/my-files/Backups")])
    assert engine(cfg, "--delete").returncode == 0
    def mutate(state):
        state["nodes"].pop(remote_state.normalize(remote), None)
    fake_drive._update(mutate)
    local_tree.write("Docs/a.txt", b"EDITED", 1_000_000_500)
    result = engine(cfg, "--delete")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[held] " not in result.stdout
    assert (src / "Docs" / "a.txt").read_bytes() == b"EDITED"
    assert fake_drive.content(remote) == b"EDITED"


def test_remote_deletion_is_not_applied_without_the_delete_switch(
        fake_drive, local_tree, write_mappings, engine):
    src = local_tree({"Docs/a.txt": (b"AAAA", 1_000_000_000)})
    remote = "/my-files/Backups/Docs/a.txt"
    cfg = write_mappings([
        _mapping(src / "Docs", "/my-files/Backups", direction="twoway"),
    ])
    assert engine(cfg).returncode == 0
    def mutate(state):
        state["nodes"].pop(remote_state.normalize(remote), None)
    fake_drive._update(mutate)
    result = engine(cfg)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[held] " not in result.stdout
    assert "[kept] " in result.stdout
    assert (src / "Docs" / "a.txt").read_bytes() == b"AAAA"


def _conflict(fake_drive, local_tree, write_mappings, engine):
    src = local_tree({"Docs/a.txt": (b"AAAA", 1_000_000_000)})
    remote = "/my-files/Backups/Docs/a.txt"
    cfg = write_mappings([
        _mapping(src / "Docs", "/my-files/Backups", direction="twoway"),
    ])
    assert engine(cfg).returncode == 0
    local_tree.write("Docs/a.txt", b"MINE", 1_000_000_300)
    _set_remote(fake_drive, remote, b"THEIRS", 1_000_000_400)
    result = engine(cfg)
    assert result.returncode == 0, result.stdout + result.stderr
    copy = src / "Docs" / "a (proton conflict).txt"
    assert copy.read_bytes() == b"THEIRS"
    return src, remote, cfg, copy


def test_conflict_clears_once_both_copies_match(
        fake_drive, local_tree, write_mappings, engine, isolated_home):
    src, remote, cfg, copy = _conflict(fake_drive, local_tree, write_mappings, engine)
    local_tree.write("Docs/a.txt", b"THEIRS", 1_000_000_600)
    copy.unlink()
    result = engine(cfg)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[conflict-resolved] " in result.stdout
    assert _state(isolated_home, cfg, src / "Docs" / "a.txt") == "synced"
    assert fake_drive.content(remote) == b"THEIRS"
    again = engine(cfg)
    assert "[conflict" not in again.stdout


def test_conflict_waits_while_the_copy_is_there(
        fake_drive, local_tree, write_mappings, engine, isolated_home):
    src, remote, cfg, copy = _conflict(fake_drive, local_tree, write_mappings, engine)
    result = engine(cfg)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "waiting for you to choose" in result.stdout
    assert fake_drive.content(remote) == b"THEIRS"
    assert copy.read_bytes() == b"THEIRS"
    assert _state(isolated_home, cfg, src / "Docs" / "a.txt") == "conflict"


def test_conflict_keeps_mine_after_the_copy_is_removed(
        fake_drive, local_tree, write_mappings, engine, isolated_home):
    src, remote, cfg, copy = _conflict(fake_drive, local_tree, write_mappings, engine)
    copy.unlink()
    result = engine(cfg)
    assert result.returncode == 0, result.stdout + result.stderr
    assert fake_drive.content(remote) == b"MINE"
    assert _state(isolated_home, cfg, src / "Docs" / "a.txt") == "synced"


def test_conflict_keeps_theirs_after_the_original_is_removed(
        fake_drive, local_tree, write_mappings, engine, isolated_home):
    src, remote, cfg, copy = _conflict(fake_drive, local_tree, write_mappings, engine)
    (src / "Docs" / "a.txt").unlink()
    result = engine(cfg, "--delete")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not fake_drive.trashed(remote)
    assert (src / "Docs" / "a.txt").read_bytes() == b"THEIRS"
    assert _state(isolated_home, cfg, src / "Docs" / "a.txt") == "synced"


def test_conflict_saves_a_newer_remote_edit_before_sending_mine(
        fake_drive, local_tree, write_mappings, engine, isolated_home):
    src, remote, cfg, copy = _conflict(fake_drive, local_tree, write_mappings, engine)
    copy.unlink()
    _set_remote(fake_drive, remote, b"THEIRS-AGAIN", 1_000_000_900)
    result = engine(cfg)
    assert result.returncode == 0, result.stdout + result.stderr
    assert fake_drive.content(remote) == b"THEIRS-AGAIN"
    assert (src / "Docs" / "a.txt").read_bytes() == b"MINE"
    assert copy.read_bytes() == b"THEIRS-AGAIN"
    assert _state(isolated_home, cfg, src / "Docs" / "a.txt") == "conflict"


def test_a_second_conflict_does_not_overwrite_an_older_copy(
        fake_drive, local_tree, write_mappings, engine):
    src, remote, cfg, copy = _conflict(fake_drive, local_tree, write_mappings, engine)
    copy.write_bytes(b"MY NOTES ON THEIRS")
    local_tree.write("Docs/a.txt", b"THEIRS", 1_000_000_600)
    assert engine(cfg).returncode == 0
    local_tree.write("Docs/a.txt", b"MINE-2", 1_000_000_700)
    _set_remote(fake_drive, remote, b"THEIRS-2", 1_000_000_800)
    result = engine(cfg)
    assert result.returncode == 0, result.stdout + result.stderr
    assert copy.read_bytes() == b"MY NOTES ON THEIRS"
    assert (src / "Docs" / "a (proton conflict 2).txt").read_bytes() == b"THEIRS-2"
    assert "/my-files/Backups/Docs/a (proton conflict 2).txt" not in fake_drive.uploads()


def test_a_trial_picked_folder_does_not_trash_until_confirmed(
        fake_drive, local_tree, write_mappings, engine):
    src = local_tree({"Docs/a.txt": (b"AAAA", 1_000_000_000)})
    remote = "/my-files/Backups/Docs/a.txt"
    mapping = _trashing(src / "Docs", "/my-files/Backups")
    mapping["live"] = True
    cfg = write_mappings([mapping])
    assert engine(cfg, "--delete").returncode == 0
    (src / "Docs" / "a.txt").unlink()
    result = engine(cfg, "--delete")
    assert "Live sync…" in result.stdout
    assert not fake_drive.trashed(remote)
    assert fake_drive.content(remote) == b"AAAA"


def test_a_folder_made_on_the_website_comes_down_with_a_new_local_file(
        fake_drive, local_tree, write_mappings, engine):
    # A new local file and a folder made in the web app, one pass.
    src = local_tree({"Docs/a.txt": (b"AAAA", 1_000_000_000)})
    cfg = write_mappings([_mapping(src / "Docs", "/my-files/Backups", direction="twoway")])
    assert engine(cfg).returncode == 0
    local_tree.write("Docs/new.txt", b"NEW", 1_000_000_100)
    fake_drive.seed_folder("/my-files/Backups/Docs/WebFolder")
    fake_drive.seed_file("/my-files/Backups/Docs/WebFolder/w.txt", b"WEB")
    result = engine(cfg)
    assert result.returncode == 0, result.stdout
    assert fake_drive.content("/my-files/Backups/Docs/new.txt") == b"NEW"
    assert (src / "Docs" / "WebFolder" / "w.txt").read_bytes() == b"WEB"


def test_a_failed_download_says_why_in_plain_words(
        fake_drive, local_tree, write_mappings, engine):
    from ui import run

    src = local_tree({"Docs/a.txt": (b"AAAA", 1_000_000_000)})
    cfg = write_mappings([_mapping(src / "Docs", "/my-files/Backups", direction="twoway")])
    assert engine(cfg).returncode == 0
    fake_drive.seed_file("/my-files/Backups/Docs/web.txt", b"WEB")
    fake_drive.add_fault(cmd="download", times=5)
    result = engine(cfg)
    assert result.returncode == 5, result.stdout
    lines = result.stdout.splitlines()
    assert "could not be sent or downloaded" in result.stdout
    failed = [line for line in lines if line.startswith("[download-failed]")]
    reasons = [line for line in lines if "could not be sent or downloaded" in line]
    assert failed and reasons
    # Shown without Verbose, and under "Errors only".
    for line in failed + reasons:
        assert run.visible_text(line, verbose=False, errors_only=False)
        assert run.visible_text(line, verbose=False, errors_only=True)
    counters = run.parse_run_result(lines[-1])
    assert counters["files_failed"] >= 1
    status = run.sync_status(5, counters)
    assert "could not be sent or downloaded" in status
    assert not (src / "Docs" / "web.txt").exists()


def test_a_failed_download_names_the_cli_error_and_the_new_file(
        fake_drive, local_tree, write_mappings, engine):
    src = local_tree({"Docs/a.txt": (b"AAAA", 1_000_000_000)})
    cfg = write_mappings([_mapping(src / "Docs", "/my-files/Backups", direction="twoway")])
    assert engine(cfg).returncode == 0
    fake_drive.seed_file("/my-files/Backups/Docs/web.txt", b"WEB")
    fake_drive.add_fault(cmd="download", times=5, stderr="Error: node is not ready")
    result = engine(cfg)
    assert result.returncode == 5, result.stdout
    failed = [line for line in result.stdout.splitlines()
              if line.startswith("[download-failed]")]
    assert failed, result.stdout
    # The file was only on Proton: nothing local was "kept".
    assert "could not download" in failed[0]
    assert "local file kept" not in failed[0]
    assert "Proton CLI said: Error: node is not ready" in failed[0]


def test_a_download_under_another_name_is_still_used(tmp_path, monkeypatch):
    import types

    import twoway

    def wrote(name):
        def run_cli(args, **_kw):
            directory = args[-1]
            if name:
                (tmp_path / directory / name).write_bytes(b"WEB")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        return run_cli

    first = tmp_path / "one"
    first.mkdir()
    monkeypatch.setattr(twoway._ps, "run_cli", wrote("web (1).txt"))
    path, skipped, reason = twoway._fetch("/my-files/web.txt", str(first))
    assert (path, skipped, reason) == (str(first / "web (1).txt"), False, "")

    empty = tmp_path / "two"
    empty.mkdir()
    monkeypatch.setattr(twoway._ps, "run_cli", wrote(None))
    path, skipped, reason = twoway._fetch("/my-files/web.txt", str(empty))
    assert path is None and not skipped
    assert "wrote 0 files" in reason


def test_direction_is_a_dialog_key():
    """Switching a mapping back to upload-only drops the key for good."""
    from mapping_keys import carry_unknown_keys
    old = {"type": "folder", "source": "/s", "dest_parent": "/d",
           "direction": "twoway", "poll_minutes": 5}
    new = carry_unknown_keys(old, {"type": "folder", "source": "/s", "dest_parent": "/d"})
    assert "direction" not in new
    assert new["poll_minutes"] == 5

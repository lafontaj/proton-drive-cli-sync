"""Download decision and the fake `filesystem download` command.

One-way passes must not call download. The decision compares claimed size,
SHA-1 and claimed modification time, never the encrypted size.
"""

import hashlib
import os

import proton_sync
import syncdb


def _sha1(data):
    return hashlib.sha1(data).hexdigest()


def _file(directory, name, data):
    path = directory / name
    path.write_bytes(data)
    return path


def _row(path, data, **extra):
    row = {
        "local_path": str(path),
        "remote_path": "/my-files/" + path.name,
        "remote_node_id": None,
        "sha1": _sha1(data),
        "claimed_size": len(data),
        "claimed_mtime": 1_000_000_000.0,
        "local_inode": path.stat().st_ino,
        "local_mtime": path.stat().st_mtime,
        "state": "synced",
    }
    row.update(extra)
    return row


def _remote(data, **extra):
    info = {
        "type": "file",
        "claimed_size": len(data),
        "sha1": _sha1(data),
        "mtime": 1_000_000_000.0,
        "size": len(data) + 64,
    }
    info.update(extra)
    return info


def test_unchanged_file_is_not_downloaded(tmp_path):
    data = b"AAAA"
    path = _file(tmp_path, "a.txt", data)
    action, reason = proton_sync.download_decision(
        str(path), _remote(data), _row(path, data))
    assert (action, reason) == ("skip", "unchanged")


def test_local_only_edit_is_not_downloaded(tmp_path):
    original = b"AAAA"
    path = _file(tmp_path, "a.txt", b"BBBB")
    action, reason = proton_sync.download_decision(
        str(path), _remote(original), _row(path, original))
    assert (action, reason) == ("skip", "local-only")


def test_remote_only_edit_is_downloaded(tmp_path):
    original = b"AAAA"
    path = _file(tmp_path, "a.txt", original)
    action, reason = proton_sync.download_decision(
        str(path), _remote(b"BBBB"), _row(path, original))
    assert (action, reason) == ("download", "remote-only")


def test_both_sides_edit_is_a_conflict(tmp_path):
    path = _file(tmp_path, "a.txt", b"BBBB")
    action, reason = proton_sync.download_decision(
        str(path), _remote(b"CCCC"), _row(path, b"AAAA"))
    assert (action, reason) == ("conflict", "both-changed")


def test_encrypted_size_does_not_count_as_a_remote_change(tmp_path):
    data = b"AAAA"
    path = _file(tmp_path, "a.txt", data)
    remote = _remote(data)
    remote["size"] = len(data) + 64
    action, reason = proton_sync.download_decision(
        str(path), remote, _row(path, data))
    assert (action, reason) == ("skip", "unchanged")


def test_encrypted_size_alone_does_not_download_over_a_local_file(tmp_path):
    data = b"AAAA"
    path = _file(tmp_path, "a.txt", data)
    remote = {"type": "file", "size": len(data) + 64, "mtime": 1_000_000_000.0}
    action, _reason = proton_sync.download_decision(
        str(path), remote, _row(path, data))
    assert action != "download"


def test_sync_database_round_trip(tmp_path):
    data = b"AAAA"
    path = _file(tmp_path, "a.txt", data)
    db_path = syncdb.database_path(
        "mappings-user1.json", data_dir=str(tmp_path / "state"))
    row = _row(path, data, remote_node_id="uid-1")
    with syncdb.SyncDB(db_path) as database:
        database.upsert(row)
        stored = database.get(str(path))
        row["state"] = "pending-down"
        database.upsert(row)
        updated = database.get(str(path))
    assert stored["remote_node_id"] == "uid-1"
    assert stored["sha1"] == _sha1(data)
    assert stored["state"] == "synced"
    assert updated["state"] == "pending-down"
    assert os.path.basename(db_path) == "mappings-user1.sync.sqlite"


def test_database_path_uses_the_config_data_dir():
    import config
    path = syncdb.database_path("mappings-user1.json")
    assert os.path.dirname(path) == config.DATA_DIR
    assert os.path.basename(path) == "mappings-user1.sync.sqlite"


def test_listing_exposes_claimed_size_and_node_id(fake_drive):
    fake_drive.seed_file("/my-files/a.txt", b"AAAA", mtime=1_000_000_000)

    def mutate(state):
        state["nodes"]["/my-files/a.txt"]["uid"] = "uid-1"

    fake_drive._update(mutate)
    listing = proton_sync.get_remote_listing("/my-files")
    assert listing.ok
    info = listing["a.txt"]
    assert info["node_id"] == "uid-1"
    assert info["claimed_size"] == 4
    assert info["sha1"] == _sha1(b"AAAA")


def test_one_way_pass_does_not_download(fake_drive, local_tree, write_mappings, engine):
    src = local_tree({"Docs/a.txt": (b"AAAA", 1_000_000_000)})
    cfg = write_mappings([{
        "type": "folder",
        "source": str(src / "Docs"),
        "dest_parent": "/my-files/Backups",
    }])
    result = engine(cfg)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = fake_drive.calls()
    assert any(
        len(call) >= 2 and call[0] == "filesystem" and call[1] == "upload"
        for call in calls
    )
    assert not any(
        len(call) >= 2 and call[0] == "filesystem" and call[1] == "download"
        for call in calls
    )


def test_download_writes_the_remote_file_into_the_directory(fake_drive, tmp_path):
    fake_drive.seed_file("/my-files/a.txt", b"AAAA", mtime=1_000_000_000)
    dest = tmp_path / "down"
    dest.mkdir()
    result = fake_drive.run(
        "filesystem", "download", "-f", "replace",
        "/my-files/a.txt", str(dest),
    )
    assert result.returncode == 0, result.stderr
    assert (dest / "a.txt").read_bytes() == b"AAAA"


def test_download_skip_leaves_the_local_file(fake_drive, tmp_path):
    fake_drive.seed_file("/my-files/a.txt", b"REMOTE", mtime=1_000_000_000)
    dest = tmp_path / "down"
    dest.mkdir()
    (dest / "a.txt").write_bytes(b"LOCAL")
    result = fake_drive.run(
        "filesystem", "download", "-f", "skip",
        "/my-files/a.txt", str(dest),
    )
    assert result.returncode == 0, result.stderr
    assert (dest / "a.txt").read_bytes() == b"LOCAL"


def test_download_replace_and_remove_overwrite(fake_drive, tmp_path):
    fake_drive.seed_file("/my-files/a.txt", b"REMOTE", mtime=1_000_000_000)
    for strategy in ("replace", "remove"):
        dest = tmp_path / strategy
        dest.mkdir()
        (dest / "a.txt").write_bytes(b"LOCAL")
        result = fake_drive.run(
            "filesystem", "download", "-f", strategy,
            "/my-files/a.txt", str(dest),
        )
        assert result.returncode == 0, result.stderr
        assert (dest / "a.txt").read_bytes() == b"REMOTE"


def test_download_keep_both_and_rename_write_a_second_name(fake_drive, tmp_path):
    fake_drive.seed_file("/my-files/a.txt", b"REMOTE", mtime=1_000_000_000)
    for strategy in ("keep-both", "rename"):
        dest = tmp_path / strategy
        dest.mkdir()
        (dest / "a.txt").write_bytes(b"LOCAL")
        result = fake_drive.run(
            "filesystem", "download", "--conflict-strategy", strategy,
            "/my-files/a.txt", str(dest),
        )
        assert result.returncode == 0, result.stderr
        assert (dest / "a.txt").read_bytes() == b"LOCAL"
        assert (dest / "a (1).txt").read_bytes() == b"REMOTE"


def test_download_without_a_strategy_does_not_overwrite(fake_drive, tmp_path):
    fake_drive.seed_file("/my-files/a.txt", b"REMOTE", mtime=1_000_000_000)
    dest = tmp_path / "down"
    dest.mkdir()
    (dest / "a.txt").write_bytes(b"LOCAL")
    result = fake_drive.run(
        "filesystem", "download", "/my-files/a.txt", str(dest),
    )
    assert result.returncode == 1
    assert (dest / "a.txt").read_bytes() == b"LOCAL"


def test_failed_download_writes_nothing(fake_drive, tmp_path):
    fake_drive.seed_file("/my-files/a.txt", b"REMOTE", mtime=1_000_000_000)
    fake_drive.add_fault(cmd="download", match="/my-files/a.txt", mode="fail", times=1)
    dest = tmp_path / "down"
    dest.mkdir()
    result = fake_drive.run(
        "filesystem", "download", "-f", "replace",
        "/my-files/a.txt", str(dest),
    )
    assert result.returncode == 1
    assert not (dest / "a.txt").exists()
    assert list(dest.iterdir()) == []

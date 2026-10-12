"""Characterization of the engine as it behaves today. These tests must pass."""

import fcntl
import json
import os

import pytest


def _mapping(source, **extra):
    mapping = {
        "type": "folder",
        "source": str(source),
        "dest_parent": "/my-files/Backups",
    }
    mapping.update(extra)
    return mapping


def _list_paths(calls):
    paths = []
    for call in calls:
        if len(call) >= 3 and call[0] == "filesystem" and call[1] == "list":
            paths.append(call[2])
    return paths


def test_first_pass_uploads_tree_under_basename(fake_drive, local_tree, write_mappings, engine):
    src = local_tree({
        "Docs/a.txt": (b"hello", 1_000_000_000),
        "Docs/sub/b.txt": (b"world", 1_000_000_000),
    })
    cfg = write_mappings([_mapping(src / "Docs")])
    result = engine(cfg)
    assert result.returncode == 0, result.stdout + result.stderr
    assert fake_drive.content("/my-files/Backups/Docs/a.txt") == b"hello"
    assert fake_drive.content("/my-files/Backups/Docs/sub/b.txt") == b"world"


def test_second_pass_unchanged_makes_no_uploads_or_listings(
        fake_drive, local_tree, write_mappings, engine):
    src = local_tree({
        "Docs/a.txt": (b"hello", 1_000_000_000),
        "Docs/sub/b.txt": (b"world", 1_000_000_000),
    })
    cfg = write_mappings([_mapping(src / "Docs")])
    first = engine(cfg)
    assert first.returncode == 0, first.stdout + first.stderr
    seen = len(fake_drive.calls())
    uploaded = len(fake_drive.uploads())
    second = engine(cfg)
    assert second.returncode == 0, second.stdout + second.stderr
    fresh = fake_drive.calls()[seen:]
    assert fake_drive.uploads()[uploaded:] == []
    listed = _list_paths(fresh)
    assert listed, "the auth probe should still list /"
    for path in listed:
        assert path in ("/", "/my-files"), path


def test_size_change_is_uploaded(fake_drive, local_tree, write_mappings, engine):
    src = local_tree({"Docs/a.txt": (b"AAAA", 1_000_000_000)})
    cfg = write_mappings([_mapping(src / "Docs")])
    assert engine(cfg).returncode == 0
    local_tree.write("Docs/a.txt", b"AAAA-more", 1_000_000_100)
    result = engine(cfg)
    assert result.returncode == 0, result.stdout + result.stderr
    assert fake_drive.content("/my-files/Backups/Docs/a.txt") == b"AAAA-more"


def test_excluded_names_and_patterns_not_uploaded(fake_drive, local_tree, write_mappings, engine):
    src = local_tree({
        "Docs/keep.txt": (b"keep", 1_000_000_000),
        "Docs/notes.tmp": (b"temp", 1_000_000_000),
        "Docs/secret.txt": (b"hide", 1_000_000_000),
    })
    cfg = write_mappings(
        [_mapping(src / "Docs", exclusions={"names": ["secret.txt"], "patterns": []})],
        exclusions={"names": [], "patterns": ["*.tmp"]},
    )
    result = engine(cfg)
    assert result.returncode == 0, result.stdout + result.stderr
    assert fake_drive.content("/my-files/Backups/Docs/keep.txt") == b"keep"
    assert fake_drive.content("/my-files/Backups/Docs/notes.tmp") is None
    assert fake_drive.content("/my-files/Backups/Docs/secret.txt") is None


def test_listing_failure_skips_folder_without_upload_or_trash(
        fake_drive, local_tree, write_mappings, engine):
    src = local_tree({"Docs/a.txt": (b"hello", 1_000_000_000)})
    fake_drive.seed_file("/my-files/Backups/Docs/orphan.txt", b"old")
    fake_drive.add_fault(cmd="list", match="/my-files/Backups/Docs")
    cfg = write_mappings([_mapping(
        src / "Docs", allow_delete=True, source_kind="local")])
    result = engine(cfg, "--delete")
    assert fake_drive.content("/my-files/Backups/Docs/a.txt") is None, (
        result.stdout + result.stderr
    )
    assert not fake_drive.trashed("/my-files/Backups/Docs/orphan.txt")
    assert not any(path.endswith("/a.txt") for path in fake_drive.uploads())


def test_delete_requires_flag_and_allow_delete(fake_drive, local_tree, write_mappings, engine):
    src = local_tree({"Docs/keep.txt": (b"keep", 1_000_000_000)})
    fake_drive.seed_file("/my-files/Backups/Docs/orphan.txt", b"old")
    orphan = "/my-files/Backups/Docs/orphan.txt"

    additive = write_mappings([_mapping(src / "Docs", allow_delete=True, source_kind="local")])
    assert engine(additive).returncode == 0
    assert not fake_drive.trashed(orphan)

    denied = write_mappings([_mapping(src / "Docs", allow_delete=False, source_kind="local")])
    assert engine(denied, "--delete", "--ignore-cache").returncode == 0
    assert not fake_drive.trashed(orphan)

    allowed = write_mappings([_mapping(src / "Docs", allow_delete=True, source_kind="local")])
    result = engine(allowed, "--delete", "--ignore-cache")
    assert result.returncode == 0, result.stdout + result.stderr
    assert fake_drive.trashed(orphan)


def test_exit_2_when_auth_probe_fails(fake_drive, local_tree, write_mappings, engine):
    src = local_tree({"Docs/a.txt": (b"hello", 1_000_000_000)})
    fake_drive.add_fault(cmd="auth")
    cfg = write_mappings([_mapping(src / "Docs")])
    result = engine(cfg)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "[auth-failed]" in result.stdout
    assert fake_drive.content("/my-files/Backups/Docs/a.txt") is None


def test_exit_1_when_lock_held(fake_drive, local_tree, write_mappings, engine, isolated_home):
    src = local_tree({"Docs/a.txt": (b"hello", 1_000_000_000)})
    cfg = write_mappings([_mapping(src / "Docs")])
    lock_path = isolated_home / ".proton-drive-sync" / "proton_sync.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = engine(cfg)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert result.returncode == 1, result.stdout + result.stderr
    assert str(lock_path) in result.stdout
    assert not any(call and call[0] == "filesystem" for call in fake_drive.calls())


def test_exit_4_on_account_change(fake_drive, local_tree, write_mappings, engine, isolated_home):
    src = local_tree({"Docs/a.txt": (b"hello", 1_000_000_000)})
    cfg = write_mappings([_mapping(src / "Docs")])
    cache = isolated_home / ".proton-drive-sync" / "cache" / "mappings.cache"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"__meta__": {"account": "other@example.com"}}), encoding="utf-8")
    result = engine(cfg)
    assert result.returncode == 4, result.stdout + result.stderr
    assert "[account-changed]" in result.stdout
    assert fake_drive.content("/my-files/Backups/Docs/a.txt") is None


def test_live_subpath_syncs_without_a_full_pass(
        fake_drive, local_tree, write_mappings, engine):
    src = local_tree({"Docs/sub/a.txt": (b"hello", 1_000_000_000)})
    cfg = write_mappings([_mapping(
        src / "Docs", live=True, direction="twoway")])
    result = engine(
        cfg, "--subpath", str(src / "Docs" / "sub"), "--mapping-source", str(src / "Docs"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[subpath-cold]" not in result.stdout
    assert fake_drive.content("/my-files/Backups/Docs/sub/a.txt") == b"hello"


def test_subpath_cold_exits_3(fake_drive, local_tree, write_mappings, engine):
    src = local_tree({"Docs/sub/a.txt": (b"hello", 1_000_000_000)})
    cfg = write_mappings([_mapping(src / "Docs")])
    result = engine(
        cfg, "--subpath", str(src / "Docs" / "sub"), "--mapping-source", str(src / "Docs"))
    assert result.returncode == 3, result.stdout + result.stderr
    assert "[subpath-cold]" in result.stdout or "[subpath-cold-root]" in result.stdout
    assert fake_drive.content("/my-files/Backups/Docs/sub/a.txt") is None


def test_symlink_to_file_uploaded_symlinked_dir_not_followed(
        fake_drive, local_tree, write_mappings, engine, tmp_path):
    src = local_tree({"Docs/real.txt": (b"data", 1_000_000_000)})
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "inside.txt").write_bytes(b"secret")
    docs = src / "Docs"
    (docs / "link.txt").symlink_to(docs / "real.txt")
    (docs / "dirlink").symlink_to(outside, target_is_directory=True)
    cfg = write_mappings([_mapping(docs)])
    result = engine(cfg)
    assert result.returncode == 0, result.stdout + result.stderr
    assert fake_drive.content("/my-files/Backups/Docs/real.txt") == b"data"
    assert fake_drive.content("/my-files/Backups/Docs/link.txt") == b"data"
    assert fake_drive.content("/my-files/Backups/Docs/dirlink/inside.txt") is None
    assert "inside.txt" not in fake_drive.listing("/my-files/Backups/Docs")


def test_partial_batch_failure_retries_individually_and_logs_failure(
        fake_drive, local_tree, write_mappings, engine):
    src = local_tree({
        "Docs/good.txt": (b"good", 1_000_000_000),
        "Docs/bad.txt": (b"bad!", 1_000_000_000),
    })
    fake_drive.add_fault(cmd="upload", match="bad.txt", times=5, stderr="nope")
    cfg = write_mappings([_mapping(src / "Docs")])
    result = engine(cfg)
    assert fake_drive.content("/my-files/Backups/Docs/good.txt") == b"good", (
        result.stdout + result.stderr
    )
    assert fake_drive.content("/my-files/Backups/Docs/bad.txt") is None
    assert "[upload-failed]" in result.stdout, result.stdout + result.stderr


def test_engine_refuses_unset_proton_drive_cli(
        fake_drive, local_tree, write_mappings, engine, monkeypatch):
    src = local_tree({"Docs/a.txt": (b"a", 1_000_000_000)})
    cfg = write_mappings([_mapping(src / "Docs")])
    monkeypatch.delenv("PROTON_DRIVE_CLI", raising=False)
    with pytest.raises(RuntimeError, match="PROTON_DRIVE_CLI"):
        engine(cfg)

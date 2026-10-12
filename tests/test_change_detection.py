"""Change detection checks from the two-way work: the comparator, files
without a remote digest, file mappings and identical copies already on Drive."""

import ast
import datetime
import hashlib
import os
from pathlib import Path

import pytest

import proton_sync

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _forget_remote_folders():
    """These tests call the engine in-process. sync_folder remembers remote
    folders on the module; clear them so the next test starts clean."""
    proton_sync._REMOTE_KNOWN.clear()
    yield
    proton_sync._REMOTE_KNOWN.clear()


def _mapping(source, **extra):
    mapping = {
        "type": "folder",
        "source": str(source),
        "dest_parent": "/my-files/Backups",
    }
    mapping.update(extra)
    return mapping


def _load_comparator():
    """The comparison helpers, exec'd out of proton_sync.py.

    Loading only these functions means importing the engine cannot create
    directories. needs_upload now calls upload_decision, which calls
    _remote_mtime_seconds and _report_decision, so those travel with it.
    """
    tree = ast.parse((REPO / "proton_sync.py").read_text(encoding="utf-8"))
    names = {
        "_local_signature",
        "_local_sha1",
        "_remote_mtime_seconds",
        "_report_decision",
        "upload_decision",
        "needs_upload",
    }
    functions = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    scope = {
        "os": os,
        "datetime": datetime,
        "_": lambda text: text,
        "_MTIME_MATCH_SECONDS": 2.0,
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]),
                 "isolated_comparison", "exec"), scope)
    return scope


def test_equal_size_edit_detected_by_comparator(tmp_path):
    scope = _load_comparator()
    folder = tmp_path / "folder"
    folder.mkdir()
    path = folder / "example.txt"
    path.write_bytes(b"AAAA")
    os.utime(path, (1_000_000_000, 1_000_000_000))
    before = scope["_local_signature"](str(folder), "/my-files/Test")
    remote = {"size": 4, "sha1": hashlib.sha1(b"AAAA").hexdigest()}
    path.write_bytes(b"BBBB")
    os.utime(path, (1_000_000_100, 1_000_000_100))
    after = scope["_local_signature"](str(folder), "/my-files/Test")
    check = scope["needs_upload"]
    assert before != after
    assert check(str(path), remote) is True


def test_batch_recovery_not_fooled_by_old_equal_size_remote(
        fake_drive, local_tree, write_mappings, engine):
    fake_drive.seed_file("/my-files/Backups/Docs/a.txt", b"AAAA", mtime=1_000_000_000)
    src = local_tree({"Docs/a.txt": (b"BBBB", 1_000_000_100)})
    fake_drive.add_fault(cmd="upload", match="a.txt", times=5, stderr="nope")
    cfg = write_mappings([_mapping(src / "Docs")])
    # The fault fails the batch. Recovery must not treat the old same-size
    # remote file as already replaced. times=5 keeps the individual retry failing
    # too, so the engine reports [upload-failed] and the bytes stay AAAA.
    result = engine(cfg)
    assert any(
        len(call) >= 2
        and call[0] == "filesystem"
        and call[1] == "upload"
        and "a.txt" in call
        for call in fake_drive.calls()
    ), fake_drive.calls()
    content = fake_drive.content("/my-files/Backups/Docs/a.txt")
    reported = "[upload-failed]" in result.stdout
    assert content == b"BBBB" or reported, result.stdout + result.stderr


def test_unchanged_files_are_not_hashed(fake_drive, local_tree, monkeypatch):
    src = local_tree({
        "Docs/keep.txt": (b"AAAA", 1_000_000_000),
        "Docs/move.txt": (b"CCCC", 1_000_000_000),
    })
    cache_path = src / "cache.json"
    cache = proton_sync.Cache(str(cache_path))
    folder = str(src / "Docs")
    assert proton_sync.sync_folder(folder, "/my-files/Backups", cache=cache, rename_ext=False)
    local_tree.write("Docs/move.txt", b"DDDD", 1_000_000_100)
    hashed = []
    real = proton_sync._local_sha1

    def counted(path, *args, **kwargs):
        hashed.append(path)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(proton_sync, "_local_sha1", counted)
    assert proton_sync.sync_folder(folder, "/my-files/Backups", cache=cache, rename_ext=False)
    assert not any(path.endswith("keep.txt") for path in hashed), hashed
    assert any(path.endswith("move.txt") for path in hashed), hashed


def test_equal_size_edit_without_remote_digest_is_uploaded(
        fake_drive, local_tree, write_mappings, engine):
    src = local_tree({"Docs/a.txt": (b"AAAA", 1_000_000_000)})
    cfg = write_mappings([_mapping(src / "Docs")])
    assert engine(cfg).returncode == 0
    fake_drive.omit_digest("/my-files/Backups/Docs/a.txt")
    local_tree.write("Docs/a.txt", b"BBBB", 1_000_000_100)
    assert engine(cfg).returncode == 0
    assert fake_drive.content("/my-files/Backups/Docs/a.txt") == b"BBBB"


def test_equal_size_unchanged_without_remote_digest_is_skipped(
        fake_drive, local_tree, write_mappings, engine):
    src = local_tree({
        "Docs/keep.txt": (b"AAAA", 1_000_000_000),
        "Docs/move.txt": (b"CCCC", 1_000_000_000),
    })
    # Remote claimed mtime is not the local mtime, so row 8 cannot skip
    # keep.txt. The first pass matches the SHA-1, does not upload, and caches
    # the local size and mtime. After the digest is removed, only that
    # baseline (row 6) keeps the second pass from uploading.
    fake_drive.seed_file(
        "/my-files/Backups/Docs/keep.txt", b"AAAA", mtime=900_000_000)
    cfg = write_mappings([_mapping(src / "Docs")])
    assert engine(cfg).returncode == 0
    assert fake_drive.revisions("/my-files/Backups/Docs/keep.txt") == 1
    fake_drive.omit_digest("/my-files/Backups/Docs/keep.txt")
    local_tree.write("Docs/move.txt", b"DDDD", 1_000_000_100)
    assert engine(cfg).returncode == 0
    assert fake_drive.content("/my-files/Backups/Docs/keep.txt") == b"AAAA"
    assert fake_drive.revisions("/my-files/Backups/Docs/keep.txt") == 1
    assert fake_drive.content("/my-files/Backups/Docs/move.txt") == b"DDDD"


def test_existing_identical_remote_not_reuploaded_without_cache(
        fake_drive, local_tree, write_mappings, engine):
    fake_drive.seed_file("/my-files/Backups/Docs/a.txt", b"AAAA", mtime=1_000_000_000)
    src = local_tree({"Docs/a.txt": (b"AAAA", 1_000_000_000)})
    cfg = write_mappings([_mapping(src / "Docs")])
    result = engine(cfg)
    assert result.returncode == 0, result.stdout + result.stderr
    assert fake_drive.content("/my-files/Backups/Docs/a.txt") == b"AAAA"
    assert fake_drive.revisions("/my-files/Backups/Docs/a.txt") == 1
    assert "/my-files/Backups/Docs/a.txt" not in fake_drive.uploads()


def test_ignore_cache_rechecks_preserved_mtime_edit(
        fake_drive, local_tree, write_mappings, engine):
    src = local_tree({"Docs/a.txt": (b"AAAA", 1_000_000_000)})
    cfg = write_mappings([_mapping(src / "Docs")])
    assert engine(cfg).returncode == 0
    # Same size and the same mtime on a.txt. Overwriting it in place does not
    # change the directory signature, so a sibling is added to force a listing.
    # The baseline still matches a.txt, and the normal pass leaves AAAA in place.
    calls_before = len(fake_drive.calls())
    local_tree.write("Docs/a.txt", b"BBBB", 1_000_000_000)
    local_tree.write("Docs/other.txt", b"x", 1_000_000_050)
    assert engine(cfg).returncode == 0
    listed = [
        call for call in fake_drive.calls()[calls_before:]
        if len(call) >= 3
        and call[0] == "filesystem"
        and call[1] == "list"
        and "/my-files/Backups/Docs" in call
    ]
    assert listed, fake_drive.calls()[calls_before:]
    assert fake_drive.content("/my-files/Backups/Docs/a.txt") == b"AAAA"
    assert engine(cfg, "--ignore-cache").returncode == 0
    assert fake_drive.content("/my-files/Backups/Docs/a.txt") == b"BBBB"


def test_sync_file_mapping_equal_size_edit(fake_drive, local_tree, write_mappings, engine):
    src = local_tree({"notes.txt": (b"AAAA", 1_000_000_000)})
    cfg = write_mappings([{
        "type": "file",
        "source": str(src / "notes.txt"),
        "dest_parent": "/my-files/Backups",
    }])
    assert engine(cfg).returncode == 0
    local_tree.write("notes.txt", b"BBBB", 1_000_000_100)
    assert engine(cfg).returncode == 0
    assert fake_drive.content("/my-files/Backups/notes.txt") == b"BBBB"


def test_signature_format_unchanged(tmp_path):
    folder = tmp_path / "tree"
    folder.mkdir()
    (folder / "b.txt").write_bytes(b"bb")
    (folder / "a.txt").write_bytes(b"a")
    os.utime(folder / "a.txt", ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
    os.utime(folder / "b.txt", ns=(1_700_000_100_000_000_000, 1_700_000_100_000_000_000))
    os.utime(folder, ns=(1_700_000_200_000_000_000, 1_700_000_200_000_000_000))
    got = proton_sync._local_signature(str(folder), "/my-files/Test")
    assert got == {
        "dir_mtime": 1_700_000_200.0,
        "files": [
            ["a.txt", 1, 1_700_000_000.0],
            ["b.txt", 2, 1_700_000_100.0],
        ],
        "remote_folder": "/my-files/Test",
        "excl": None,
    }


def test_remote_mtime_normalization():
    """The one format `filesystem list -j` returns: ISO-8601 UTC with milliseconds."""
    iso = "2016-02-29T21:42:04.000Z"
    expected = datetime.datetime(2016, 2, 29, 21, 42, 4, tzinfo=datetime.timezone.utc).timestamp()
    size, mtime, sha1 = proton_sync._extract_remote_meta({
        "modificationTime": "2026-05-29T16:44:25.000Z",
        "totalStorageSize": 99,
        "activeRevision": {
            "ok": True,
            "value": {
                "claimedSize": 4,
                "claimedModificationTime": iso,
                "claimedDigests": {"sha1": "abc"},
            },
        },
    })
    assert size == 4
    assert mtime == expected
    assert sha1 == "abc"
    # Server modificationTime is not a stand-in when the claimed field is absent.
    _size, bare, _sha = proton_sync._extract_remote_meta({
        "modificationTime": iso,
        "activeRevision": {"ok": True, "value": {"claimedSize": 4}},
    })
    assert bare is None
    assert proton_sync._remote_mtime_seconds("not-a-date") is None
    assert proton_sync._remote_mtime_seconds(None) is None
    # Not emitted by the pinned CLI. Covered so a bare number cannot be
    # misread, and so bool/NaN cannot fall through the numeric branch.
    assert proton_sync._remote_mtime_seconds(1_456_782_124) == 1_456_782_124.0
    assert proton_sync._remote_mtime_seconds(1_456_782_124_000) == 1_456_782_124.0
    assert proton_sync._remote_mtime_seconds(float("nan")) is None
    assert proton_sync._remote_mtime_seconds(float("inf")) is None
    assert proton_sync._remote_mtime_seconds(True) is None
    # Same instant, offset form, in case a Date is printed with +00:00.
    assert proton_sync._remote_mtime_seconds("2016-02-29T21:42:04.000+00:00") == expected

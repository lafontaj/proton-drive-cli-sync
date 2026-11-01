"""The fake CLI matches the JSON and exit codes the engine parses."""

import hashlib
import json
import os
import re
import subprocess
import time

import proton_sync


def test_list_json_shape(fake_drive):
    fake_drive.seed_file("/my-files/Backups/a.txt", b"hello", mtime=1_000_000_000)
    result = fake_drive.run("filesystem", "list", "/my-files/Backups", "-j")
    assert result.returncode == 0, result.stderr
    items = json.loads(result.stdout)
    assert len(items) == 1
    item = items[0]
    assert item["name"] == "a.txt"
    assert proton_sync._unwrap(item["type"]) == "file"
    assert proton_sync._unwrap(item["keyAuthor"]) == "tester@example.com"
    revision = proton_sync._unwrap(item["activeRevision"])
    assert revision["claimedSize"] == 5
    assert revision["claimedModificationTime"] == "2001-09-09T01:46:40.000Z"
    assert revision["claimedDigests"]["sha1"] == hashlib.sha1(b"hello").hexdigest()
    assert "activeRevision" not in json.loads(
        fake_drive.run("filesystem", "list", "/my-files", "-j").stdout
    )[0]


def test_upload_from_cwd_uses_names(fake_drive, tmp_path):
    fake_drive.seed_folder("/my-files/Backups")
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "a.txt").write_bytes(b"hello")
    result = fake_drive.run(
        "filesystem", "upload", "-f", "replace", "-d", "merge",
        "a.txt", "/my-files/Backups",
        cwd=str(folder),
    )
    assert result.returncode == 0, result.stderr
    assert fake_drive.content("/my-files/Backups/a.txt") == b"hello"
    assert fake_drive.upload_cwds()[-1]["cwd"] == str(folder)
    upload_calls = [c for c in fake_drive.calls() if len(c) > 1 and c[1] == "upload"]
    assert upload_calls
    assert "a.txt" in upload_calls[-1]
    assert str(folder) not in " ".join(upload_calls[-1])


def test_glob_escape_is_unescaped(fake_drive, tmp_path):
    fake_drive.seed_folder("/my-files/Backups")
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "a[b.txt").write_bytes(b"bracket")
    result = fake_drive.run(
        "filesystem", "upload", "-f", "replace", "-d", "merge",
        "a[[]b.txt", "/my-files/Backups",
        cwd=str(folder),
    )
    assert result.returncode == 0, result.stderr
    assert fake_drive.content("/my-files/Backups/a[b.txt") == b"bracket"


def test_replace_bumps_revisions(fake_drive, tmp_path):
    fake_drive.seed_folder("/my-files/Backups")
    folder = tmp_path / "docs"
    folder.mkdir()
    path = folder / "a.txt"
    path.write_bytes(b"one")
    args = ("filesystem", "upload", "-f", "replace", "-d", "merge", "a.txt", "/my-files/Backups")
    assert fake_drive.run(*args, cwd=str(folder)).returncode == 0
    path.write_bytes(b"two")
    assert fake_drive.run(*args, cwd=str(folder)).returncode == 0
    assert fake_drive.content("/my-files/Backups/a.txt") == b"two"
    assert fake_drive.revisions("/my-files/Backups/a.txt") == 2


def test_trash_hides_from_list(fake_drive):
    fake_drive.seed_file("/my-files/Backups/a.txt", b"hello")
    fake_drive.seed_file("/my-files/Backups/keep.txt", b"stay")
    result = fake_drive.run("filesystem", "trash", "/my-files/Backups/a.txt")
    assert result.returncode == 0, result.stderr
    assert fake_drive.trashed("/my-files/Backups/a.txt")
    assert "a.txt" not in fake_drive.listing("/my-files/Backups")
    assert "keep.txt" in fake_drive.listing("/my-files/Backups")


def test_per_file_fault_is_a_partial_batch(fake_drive, tmp_path):
    fake_drive.seed_folder("/my-files/Backups")
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "good.txt").write_bytes(b"good")
    (folder / "bad.txt").write_bytes(b"bad!")
    fake_drive.add_fault(cmd="upload", match="bad.txt", mode="fail", times=1, stderr="nope")
    result = fake_drive.run(
        "filesystem", "upload", "-f", "replace", "-d", "merge",
        "good.txt", "bad.txt", "/my-files/Backups",
        cwd=str(folder),
    )
    assert result.returncode == 1
    assert "- bad.txt: nope" in result.stdout
    assert "1 item(s) failed to upload" in result.stderr
    assert fake_drive.content("/my-files/Backups/good.txt") == b"good"
    assert fake_drive.content("/my-files/Backups/bad.txt") is None


def test_version_text_matches_engine_regex(fake_drive):
    result = fake_drive.run("--version")
    assert result.returncode == 0
    match = re.search(r"cli-drive@(\d+\.\d+\.\d+)", result.stdout)
    assert match is not None
    assert match.group(1) == "0.9.0"


def test_unknown_command_exits_2(fake_drive):
    result = fake_drive.run("filesystem", "dance")
    assert result.returncode == 2
    assert "fake: unsupported command" in result.stderr


def test_empty_argv_exits_2(fake_drive):
    result = fake_drive.run()
    assert result.returncode == 2
    assert "fake: unsupported command" in result.stderr


def test_upload_missing_parent_exits_1(fake_drive, tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "a.txt").write_bytes(b"x")
    result = fake_drive.run(
        "filesystem", "upload", "-f", "replace", "-d", "merge",
        "a.txt", "/my-files/Nope",
        cwd=str(folder),
    )
    assert result.returncode == 1, result.stderr
    assert "not found" in result.stderr
    assert fake_drive.content("/my-files/Nope/a.txt") is None


def test_list_trashed_folder_exits_1(fake_drive):
    fake_drive.seed_folder("/my-files/Backups/Gone")
    assert fake_drive.run("filesystem", "trash", "/my-files/Backups/Gone").returncode == 0
    result = fake_drive.run("filesystem", "list", "/my-files/Backups/Gone", "-j")
    assert result.returncode == 1, result.stderr
    assert "not found" in result.stderr


def test_hang_releases_the_state_lock(fake_drive, tmp_path):
    fake_drive.seed_folder("/my-files/Backups")
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "a.txt").write_bytes(b"x")
    fake_drive.add_fault(
        cmd="upload", match="a.txt", mode="hang", times=1, seconds=30,
    )
    proc = subprocess.Popen(
        [
            str(fake_drive.cli),
            "filesystem", "upload", "-f", "replace", "-d", "merge",
            "a.txt", "/my-files/Backups",
        ],
        cwd=str(folder),
        env=os.environ.copy(),
    )
    try:
        deadline = time.monotonic() + 5
        started = False
        while time.monotonic() < deadline:
            if any(len(call) > 1 and call[1] == "upload" for call in fake_drive.calls()):
                started = True
                break
            time.sleep(0.05)
        assert started, "hang upload did not start"
        started_at = time.monotonic()
        fake_drive.add_fault(cmd="list", match="/my-files/nope")
        assert time.monotonic() - started_at < 1
    finally:
        proc.kill()
        proc.wait(timeout=5)


def _uid(fake_drive, path):
    holder = {}

    def look(state):
        holder["uid"] = state["nodes"][path].get("uid")
    fake_drive._update(look)
    return holder["uid"]


def test_rename_keeps_the_uid_and_refuses_an_existing_name(fake_drive):
    fake_drive.seed_file("/my-files/a.txt", b"A", mtime=1_000_000_000)
    fake_drive.seed_file("/my-files/b.txt", b"B", mtime=1_000_000_000)
    fake_drive.run("filesystem", "list", "/my-files", "-j")
    before = _uid(fake_drive, "/my-files/a.txt")
    assert before
    result = fake_drive.run("filesystem", "rename", "--", "/my-files/a.txt", "b.txt")
    assert result.returncode == 1
    assert "already exists" in result.stderr
    assert fake_drive.content("/my-files/b.txt") == b"B"
    result = fake_drive.run("filesystem", "rename", "--", "/my-files/a.txt", "-c.txt")
    assert result.returncode == 0, result.stderr
    assert fake_drive.content("/my-files/-c.txt") == b"A"
    assert _uid(fake_drive, "/my-files/-c.txt") == before


def test_rename_of_a_missing_node_fails(fake_drive):
    result = fake_drive.run("filesystem", "rename", "--", "/my-files/x.txt", "y.txt")
    assert result.returncode == 1
    assert "Node not found" in result.stderr


def test_rename_and_move_take_no_option(fake_drive):
    fake_drive.seed_file("/my-files/a.txt", b"A", mtime=1_000_000_000)
    fake_drive.seed_folder("/my-files/dir")
    renamed = fake_drive.run("filesystem", "rename", "-f", "skip",
                             "/my-files/a.txt", "b.txt")
    moved = fake_drive.run("filesystem", "move", "-f", "skip",
                           "/my-files/a.txt", "/my-files/dir")
    for result in (renamed, moved):
        assert result.returncode != 0 and "Unknown option" in result.stderr
    assert fake_drive.content("/my-files/a.txt") == b"A"


def test_move_takes_several_sources_and_keeps_uids(fake_drive):
    fake_drive.seed_file("/my-files/a.txt", b"A", mtime=1_000_000_000)
    fake_drive.seed_file("/my-files/b.txt", b"B", mtime=1_000_000_000)
    fake_drive.seed_folder("/my-files/dir")
    fake_drive.run("filesystem", "list", "/my-files", "-j")
    uid = _uid(fake_drive, "/my-files/b.txt")
    result = fake_drive.run("filesystem", "move", "--", "/my-files/a.txt",
                            "/my-files/b.txt", "/my-files/dir")
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("✅") == 2
    assert fake_drive.content("/my-files/dir/b.txt") == b"B"
    assert _uid(fake_drive, "/my-files/dir/b.txt") == uid
    assert fake_drive.listing("/my-files") == ["dir"]


def test_move_onto_an_existing_name_reports_it_and_moves_nothing(fake_drive):
    fake_drive.seed_file("/my-files/a.txt", b"A", mtime=1_000_000_000)
    fake_drive.seed_file("/my-files/dir/a.txt", b"OLD", mtime=1_000_000_000)
    result = fake_drive.run("filesystem", "move", "--", "/my-files/a.txt",
                            "/my-files/dir")
    assert result.returncode == 0
    assert "❌ a.txt" in result.stdout or "❌" in result.stdout
    assert fake_drive.content("/my-files/dir/a.txt") == b"OLD"
    assert fake_drive.content("/my-files/a.txt") == b"A"


def test_move_into_a_missing_folder_fails(fake_drive):
    fake_drive.seed_file("/my-files/a.txt", b"A", mtime=1_000_000_000)
    result = fake_drive.run("filesystem", "move", "--", "/my-files/a.txt",
                            "/my-files/nope")
    assert result.returncode == 1
    assert "Node not found" in result.stderr

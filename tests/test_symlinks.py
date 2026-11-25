"""Symbolic links are not sent.

The real CLI checks each local path with lstat and refuses anything that is
neither a regular file nor a directory ("Not a regular file or directory"); it
never follows a link, and Proton Drive has no link type. Sending a link failed
on every pass, so the mapping was never complete. The engine now leaves links
aside, says how many in one line per mapping, and never trashes what carries a
link's name on Proton. The fake CLI refuses links the way the real one does.
"""

import os

import proton_sync


def _mapping(source, **extra):
    m = {"type": "folder", "source": str(source), "dest_parent": "/my-files/Backups"}
    m.update(extra)
    return m


def _tree_with_links(local_tree, tmp_path):
    src = local_tree({
        "Docs/real.txt": (b"real", 1_000_000_000),
        "Docs/sub/main.py": (b"code", 1_000_000_000),
    })
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    docs = src / "Docs"
    os.symlink(docs / "real.txt", docs / "sub" / "link-to-file.txt")
    os.symlink(docs / "sub", docs / "link-to-dir")
    os.symlink(tmp_path / "gone.txt", docs / "broken-link.txt")
    os.symlink(outside, docs / "link-outside.txt")
    return src


def test_the_fake_cli_refuses_a_link_like_the_real_one(fake_drive, tmp_path):
    fake_drive.seed_folder("/my-files/Backups")
    (tmp_path / "a.txt").write_bytes(b"a")
    os.symlink(tmp_path / "a.txt", tmp_path / "l.txt")
    res = fake_drive.run("filesystem", "upload", "-f", "replace", "-d", "merge", "--",
                         "l.txt", "/my-files/Backups", cwd=str(tmp_path))
    assert res.returncode == 1
    assert "Not a regular file or directory" in res.stderr


def test_links_are_left_aside_and_the_mapping_is_complete(
        fake_drive, local_tree, write_mappings, engine, tmp_path):
    src = _tree_with_links(local_tree, tmp_path)
    result = engine(write_mappings([_mapping(src / "Docs")]))
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "Not a regular file or directory" not in out
    assert "upload failed" not in out
    assert "ℹ  4 symbolic link(s) not sent" in out
    assert '"files_failed": 0' in out
    assert '"mappings_complete": 1' in out
    assert fake_drive.content("/my-files/Backups/Docs/real.txt") == b"real"
    assert fake_drive.content("/my-files/Backups/Docs/sub/main.py") == b"code"
    for name in ("sub/link-to-file.txt", "link-outside.txt", "broken-link.txt"):
        assert fake_drive.content("/my-files/Backups/Docs/" + name) is None


def test_verbose_names_each_link_and_its_target(
        fake_drive, local_tree, write_mappings, engine, tmp_path):
    src = _tree_with_links(local_tree, tmp_path)
    result = engine(write_mappings([_mapping(src / "Docs")]), "--verbose")
    assert "⏭  symbolic link not sent: " in result.stdout
    assert str(src / "Docs" / "link-outside.txt") + " → " + str(tmp_path / "outside.txt") \
        in result.stdout


def test_no_line_when_there_is_no_link(fake_drive, local_tree, write_mappings, engine):
    src = local_tree({"Docs/real.txt": (b"real", 1_000_000_000)})
    result = engine(write_mappings([_mapping(src / "Docs")]))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "symbolic link" not in result.stdout


def test_a_copy_on_proton_under_a_links_name_is_never_trashed(
        fake_drive, local_tree, write_mappings, engine, tmp_path):
    """A copy sent long ago under the link's name stays, as an excluded name does."""
    src = _tree_with_links(local_tree, tmp_path)
    fake_drive.seed_file("/my-files/Backups/Docs/link-outside.txt", b"old copy")
    fake_drive.seed_file("/my-files/Backups/Docs/sub/link-to-file.txt", b"old copy")
    fake_drive.seed_file("/my-files/Backups/Docs/orphan.txt", b"orphan")
    result = engine(write_mappings([_mapping(src / "Docs", allow_delete=True,
                                             source_kind="local")]),
                    "--delete")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not fake_drive.trashed("/my-files/Backups/Docs/link-outside.txt")
    assert not fake_drive.trashed("/my-files/Backups/Docs/sub/link-to-file.txt")
    assert fake_drive.trashed("/my-files/Backups/Docs/orphan.txt")


def test_a_file_mapping_on_a_link_is_left_aside(
        fake_drive, local_tree, write_mappings, engine, tmp_path):
    src = local_tree({"Docs/real.txt": (b"real", 1_000_000_000)})
    os.symlink(src / "Docs" / "real.txt", tmp_path / "single-link.txt")
    mapping = {"type": "file", "source": str(tmp_path / "single-link.txt"),
               "dest_parent": "/my-files/Backups"}
    result = engine(write_mappings([mapping]))
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "ℹ  1 symbolic link(s) not sent" in out
    assert fake_drive.content("/my-files/Backups/single-link.txt") is None


def test_the_cache_signature_ignores_links(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"a")
    before = proton_sync._local_signature(str(tmp_path), "/my-files/X", None)
    os.symlink(tmp_path / "a.txt", tmp_path / "l.txt")
    after = proton_sync._local_signature(str(tmp_path), "/my-files/X", None)
    assert {k: v for k, v in before.items() if k != "dir_mtime"} == \
        {k: v for k, v in after.items() if k != "dir_mtime"}


def test_the_shown_reason_is_the_cause_not_the_summary():
    reason = proton_sync._failure_reason
    assert reason("Transfer summary:\n  Uploaded: 0 items (0 B) | "
                  "Not a regular file or directory: /x/s.json") == \
        "Not a regular file or directory: /x/s.json"
    assert reason("Transfer summary:\n  Uploaded: 0 items (0 B)\n  Failed: 1 items\n"
                  "  - a.db: IntegrityError: does not match | 1 item(s) failed to upload") == \
        "a.db: IntegrityError: does not match"
    assert reason("upload frozen, stopped by the engine") == \
        "upload frozen, stopped by the engine"
    assert reason("") == ""


def test_the_summary_comes_back_when_the_cache_skips_the_folder(
        fake_drive, local_tree, write_mappings, engine, tmp_path):
    """The summary is for every pass: a folder skipped by the cache still has
    its links counted (found in production: the second priming showed none)."""
    src = _tree_with_links(local_tree, tmp_path)
    cfg = write_mappings([_mapping(src / "Docs")])
    engine(cfg)
    result = engine(cfg, "--verbose")
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "⚡" in out                     # the folders were skipped by the cache
    assert "ℹ  4 symbolic link(s) not sent" in out
    assert "⏭  symbolic link not sent: " + str(src / "Docs" / "link-outside.txt") in out

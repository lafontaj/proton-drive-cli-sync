"""Renames and moves in the two-way reconcile.

A file or folder renamed or moved on one side is renamed or moved on the other
by its node id (Proton) or its inode and SHA-1 (here). Nothing is uploaded,
downloaded or trashed, the database row keeps its id, and the next pass is
quiet. Every doubtful case falls back to what the pass did before.
"""

__version__ = "1.0.0"

import os
import shutil

import pytest

import remote_state
import syncdb

REMOTE = "/my-files/Backups"


@pytest.fixture
def write_mappings(write_mappings):
    def _write(twoway, exclusions=None):
        return write_mappings([], exclusions=exclusions,
                              sync_mappings=[dict(m, direction="twoway")
                                             for m in twoway])
    return _write


@pytest.fixture
def engine(engine):
    class _Reconciling:
        update_settings = staticmethod(engine.update_settings)

        def __call__(self, cfg, *args):
            return engine.reconcile(cfg, *args)
    return _Reconciling()


def _mapping(source, dest, **extra):
    mapping = {"type": "folder", "source": str(source), "dest_parent": dest,
               "allow_delete": True, "source_kind": "local"}
    mapping.update(extra)
    return mapping


def _db(isolated_home, cfg):
    return syncdb.database_path(
        os.path.basename(str(cfg)),
        data_dir=str(isolated_home / ".proton-drive-sync"))


def _rows(isolated_home, cfg):
    with syncdb.SyncDB(_db(isolated_home, cfg)) as database:
        return ({r["local_path"]: r for r in database.rows()},
                {r["local_path"]: r for r in database.folders()})


def _nodes(fake_drive):
    holder = {}

    def look(state):
        holder["nodes"] = {k: bool(v.get("trashed"))
                           for k, v in state["nodes"].items()}
    fake_drive._update(look)
    return holder["nodes"]


def _live(fake_drive, remote):
    return _nodes(fake_drive).get(remote_state.normalize(remote)) is False


def _verbs(fake_drive, start=0, *verbs):
    return [c for c in fake_drive.calls()[start:]
            if len(c) > 1 and c[0] == "filesystem" and c[1] in verbs]


def _transfers(fake_drive, start):
    return _verbs(fake_drive, start, "upload", "download", "trash")


def _proton_move(fake_drive, old, new):
    def mutate(state):
        remote_state.ensure_folders(state, new)
        remote_state.relocate(state, old, new)
    fake_drive._update(mutate)


def _start(local_tree, write_mappings, engine, files, **extra):
    """Sync the tree twice: the first pass uploads, the second records ids."""
    src = local_tree(files)
    cfg = write_mappings([_mapping(src / "Docs", REMOTE, **extra)])
    for _ in range(2):
        assert engine(cfg).returncode == 0
    return src, cfg


def _quiet(engine, fake_drive, cfg):
    start = len(fake_drive.calls())
    result = engine(cfg, "--delete")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[moved]" not in result.stdout
    assert _transfers(fake_drive, start) == []
    assert _verbs(fake_drive, start, "rename", "move") == []


FILES = {"Docs/a.txt": (b"AAAA", 1_000_000_000),
         "Docs/keep.txt": (b"KEEP", 1_000_000_000)}


def test_file_renamed_here_is_renamed_on_proton(
        fake_drive, local_tree, write_mappings, engine, isolated_home):
    src, cfg = _start(local_tree, write_mappings, engine, FILES)
    before, _ = _rows(isolated_home, cfg)
    row = before[str(src / "Docs" / "a.txt")]
    assert row["remote_node_id"]
    start = len(fake_drive.calls())
    os.rename(src / "Docs" / "a.txt", src / "Docs" / "b.txt")
    result = engine(cfg, "--delete")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _transfers(fake_drive, start) == []
    assert _verbs(fake_drive, start, "rename") == [
        ["filesystem", "rename", "--", REMOTE + "/Docs/a.txt", "b.txt"]]
    assert not _live(fake_drive, REMOTE + "/Docs/a.txt")
    assert fake_drive.content(REMOTE + "/Docs/b.txt") == b"AAAA"
    assert "[moved] {} -> {}: renamed here".format(
        src / "Docs" / "a.txt", src / "Docs" / "b.txt") in result.stdout
    after, _ = _rows(isolated_home, cfg)
    assert str(src / "Docs" / "a.txt") not in after
    moved = after[str(src / "Docs" / "b.txt")]
    assert moved["remote_node_id"] == row["remote_node_id"]
    assert moved["sha1"] == row["sha1"] and moved["state"] == "synced"
    assert moved["remote_path"] == REMOTE + "/Docs/b.txt"
    _quiet(engine, fake_drive, cfg)


def test_file_moved_here_is_moved_on_proton(
        fake_drive, local_tree, write_mappings, engine, isolated_home):
    src, cfg = _start(local_tree, write_mappings, engine, FILES)
    start = len(fake_drive.calls())
    (src / "Docs" / "sub").mkdir()
    os.rename(src / "Docs" / "a.txt", src / "Docs" / "sub" / "a.txt")
    result = engine(cfg, "--delete")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _transfers(fake_drive, start) == []
    assert _verbs(fake_drive, start, "move") == [
        ["filesystem", "move", "--", REMOTE + "/Docs/a.txt",
         REMOTE + "/Docs/sub"]]
    assert _verbs(fake_drive, start, "rename") == []
    assert fake_drive.content(REMOTE + "/Docs/sub/a.txt") == b"AAAA"
    assert not _live(fake_drive, REMOTE + "/Docs/a.txt")
    _quiet(engine, fake_drive, cfg)


def test_file_moved_and_renamed_here_is_moved_then_renamed(
        fake_drive, local_tree, write_mappings, engine, isolated_home):
    src, cfg = _start(local_tree, write_mappings, engine, FILES)
    before, _ = _rows(isolated_home, cfg)
    start = len(fake_drive.calls())
    (src / "Docs" / "sub").mkdir()
    os.rename(src / "Docs" / "a.txt", src / "Docs" / "sub" / "z.txt")
    assert engine(cfg, "--delete").returncode == 0
    assert _transfers(fake_drive, start) == []
    assert [c[1] for c in _verbs(fake_drive, start, "move", "rename")] == [
        "move", "rename"]
    assert fake_drive.content(REMOTE + "/Docs/sub/z.txt") == b"AAAA"
    after, _ = _rows(isolated_home, cfg)
    assert after[str(src / "Docs" / "sub" / "z.txt")]["remote_node_id"] == \
        before[str(src / "Docs" / "a.txt")]["remote_node_id"]
    _quiet(engine, fake_drive, cfg)


def test_folder_renamed_here_is_renamed_once_on_proton(
        fake_drive, local_tree, write_mappings, engine, isolated_home):
    src, cfg = _start(local_tree, write_mappings, engine, {
        "Docs/dir/a.txt": (b"A", 1_000_000_000),
        "Docs/dir/deep/b.txt": (b"B", 1_000_000_000)})
    before, folders = _rows(isolated_home, cfg)
    start = len(fake_drive.calls())
    os.rename(src / "Docs" / "dir", src / "Docs" / "dir2")
    result = engine(cfg, "--delete")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _transfers(fake_drive, start) == []
    assert _verbs(fake_drive, start, "rename", "move") == [
        ["filesystem", "rename", "--", REMOTE + "/Docs/dir", "dir2"]]
    assert fake_drive.content(REMOTE + "/Docs/dir2/deep/b.txt") == b"B"
    after, folders_after = _rows(isolated_home, cfg)
    old_row = before[str(src / "Docs" / "dir" / "deep" / "b.txt")]
    new_row = after[str(src / "Docs" / "dir2" / "deep" / "b.txt")]
    assert new_row["remote_node_id"] == old_row["remote_node_id"]
    assert new_row["remote_path"] == REMOTE + "/Docs/dir2/deep/b.txt"
    assert str(src / "Docs" / "dir2" / "deep") in folders_after
    assert not [p for p in list(after) + list(folders_after)
                if p.startswith(str(src / "Docs" / "dir") + os.sep)]
    _quiet(engine, fake_drive, cfg)


def test_file_renamed_on_proton_is_renamed_here(
        fake_drive, local_tree, write_mappings, engine, isolated_home):
    src, cfg = _start(local_tree, write_mappings, engine, FILES)
    before, _ = _rows(isolated_home, cfg)
    start = len(fake_drive.calls())
    fake_drive._update(lambda s: remote_state.relocate(
        s, REMOTE + "/Docs/a.txt", REMOTE + "/Docs/b.txt"))
    result = engine(cfg, "--delete")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _transfers(fake_drive, start) == []
    assert sorted(os.listdir(src / "Docs")) == ["b.txt", "keep.txt"]
    assert "[moved] {} -> {}: renamed on Proton, renamed here too, nothing " \
        "downloaded.".format(src / "Docs" / "a.txt",
                             src / "Docs" / "b.txt") in result.stdout
    after, _ = _rows(isolated_home, cfg)
    moved = after[str(src / "Docs" / "b.txt")]
    assert moved["remote_node_id"] == \
        before[str(src / "Docs" / "a.txt")]["remote_node_id"]
    assert moved["state"] == "synced"
    _quiet(engine, fake_drive, cfg)


def test_file_moved_on_proton_is_moved_here(
        fake_drive, local_tree, write_mappings, engine, isolated_home):
    src, cfg = _start(local_tree, write_mappings, engine, FILES)
    start = len(fake_drive.calls())
    _proton_move(fake_drive, REMOTE + "/Docs/a.txt",
                 REMOTE + "/Docs/sub/a.txt")
    result = engine(cfg, "--delete")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _transfers(fake_drive, start) == []
    assert not (src / "Docs" / "a.txt").exists()
    assert (src / "Docs" / "sub" / "a.txt").read_bytes() == b"AAAA"
    _quiet(engine, fake_drive, cfg)


def test_file_moved_and_renamed_on_proton_is_moved_here(
        fake_drive, local_tree, write_mappings, engine, isolated_home):
    src, cfg = _start(local_tree, write_mappings, engine, FILES)
    start = len(fake_drive.calls())
    _proton_move(fake_drive, REMOTE + "/Docs/a.txt",
                 REMOTE + "/Docs/sub/z.txt")
    assert engine(cfg, "--delete").returncode == 0
    assert _transfers(fake_drive, start) == []
    assert (src / "Docs" / "sub" / "z.txt").read_bytes() == b"AAAA"
    assert not (src / "Docs" / "a.txt").exists()
    _quiet(engine, fake_drive, cfg)


def test_folder_renamed_on_proton_is_renamed_here(
        fake_drive, local_tree, write_mappings, engine, isolated_home):
    src, cfg = _start(local_tree, write_mappings, engine, {
        "Docs/dir/a.txt": (b"A", 1_000_000_000),
        "Docs/dir/deep/b.txt": (b"B", 1_000_000_000)})
    before, _ = _rows(isolated_home, cfg)
    start = len(fake_drive.calls())
    fake_drive._update(lambda s: remote_state.relocate(
        s, REMOTE + "/Docs/dir", REMOTE + "/Docs/dir2"))
    result = engine(cfg, "--delete")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _transfers(fake_drive, start) == []
    assert not (src / "Docs" / "dir").exists()
    assert (src / "Docs" / "dir2" / "deep" / "b.txt").read_bytes() == b"B"
    after, _ = _rows(isolated_home, cfg)
    assert after[str(src / "Docs" / "dir2" / "deep" / "b.txt")][
        "remote_node_id"] == before[str(
            src / "Docs" / "dir" / "deep" / "b.txt")]["remote_node_id"]
    _quiet(engine, fake_drive, cfg)


def test_a_move_is_not_a_deletion(
        fake_drive, local_tree, write_mappings, engine):
    """No --delete and no allow_delete are needed, and the guard stays out."""
    src, cfg = _start(local_tree, write_mappings, engine, FILES,
                      allow_delete=False)
    start = len(fake_drive.calls())
    os.rename(src / "Docs" / "a.txt", src / "Docs" / "b.txt")
    result = engine(cfg)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _transfers(fake_drive, start) == []
    assert fake_drive.content(REMOTE + "/Docs/b.txt") == b"AAAA"
    assert "mass" not in result.stdout.lower()


def test_name_taken_here_means_no_move(
        fake_drive, local_tree, write_mappings, engine):
    src, cfg = _start(local_tree, write_mappings, engine, FILES)
    start = len(fake_drive.calls())
    local_tree({"Docs/b.txt": (b"OTHER", 1_000_000_000)})
    fake_drive._update(lambda s: remote_state.relocate(
        s, REMOTE + "/Docs/a.txt", REMOTE + "/Docs/b.txt"))
    result = engine(cfg, "--delete")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[moved]" not in result.stdout
    assert (src / "Docs" / "b.txt").exists()
    assert _verbs(fake_drive, start, "rename", "move") == []


def test_name_taken_on_proton_means_no_move(
        fake_drive, local_tree, write_mappings, engine):
    src, cfg = _start(local_tree, write_mappings, engine, FILES)
    start = len(fake_drive.calls())
    fake_drive.seed_file(REMOTE + "/Docs/b.txt", b"THEIRS", 1_000_000_500)
    os.rename(src / "Docs" / "a.txt", src / "Docs" / "b.txt")
    result = engine(cfg, "--delete")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[moved]" not in result.stdout
    assert _verbs(fake_drive, start, "rename", "move") == []
    assert not _live(fake_drive, REMOTE + "/Docs/a.txt") or \
        fake_drive.content(REMOTE + "/Docs/a.txt") == b"AAAA"


def test_moved_out_of_the_mapping_on_proton_counts_as_removed(
        fake_drive, local_tree, write_mappings, engine):
    src, cfg = _start(local_tree, write_mappings, engine, FILES)
    start = len(fake_drive.calls())
    _proton_move(fake_drive, REMOTE + "/Docs/a.txt", "/my-files/Elsewhere/a.txt")
    result = engine(cfg, "--delete")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[moved]" not in result.stdout
    assert not (src / "Docs" / "a.txt").exists()
    assert (src / "Docs" / "keep.txt").exists()
    assert fake_drive.content("/my-files/Elsewhere/a.txt") == b"AAAA"
    assert _verbs(fake_drive, start, "upload", "download") == []


def test_moved_to_an_excluded_name_on_proton_counts_as_removed(
        fake_drive, local_tree, write_mappings, engine):
    src = local_tree(FILES)
    cfg = write_mappings(
        [_mapping(src / "Docs", REMOTE)],
        exclusions={"names": ["skipme.txt"], "patterns": []})
    for _ in range(2):
        assert engine(cfg).returncode == 0
    fake_drive._update(lambda s: remote_state.relocate(
        s, REMOTE + "/Docs/a.txt", REMOTE + "/Docs/skipme.txt"))
    result = engine(cfg, "--delete")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[moved]" not in result.stdout
    assert not (src / "Docs" / "skipme.txt").exists()
    assert fake_drive.content(REMOTE + "/Docs/skipme.txt") == b"AAAA"


def test_rows_that_are_not_synced_are_not_moved(
        fake_drive, local_tree, write_mappings, engine, isolated_home):
    src, cfg = _start(local_tree, write_mappings, engine, FILES)
    path = str(src / "Docs" / "a.txt")
    with syncdb.SyncDB(_db(isolated_home, cfg)) as database:
        row = database.get(path)
        row["state"] = "conflict"
        database.upsert(row)
    start = len(fake_drive.calls())
    os.rename(path, str(src / "Docs" / "b.txt"))
    result = engine(cfg, "--delete")
    assert "[moved]" not in result.stdout, result.stdout
    assert _verbs(fake_drive, start, "rename", "move") == []


def test_both_sides_moved_to_the_same_name_only_updates_the_row(
        fake_drive, local_tree, write_mappings, engine, isolated_home):
    src, cfg = _start(local_tree, write_mappings, engine, FILES)
    before, _ = _rows(isolated_home, cfg)
    start = len(fake_drive.calls())
    os.rename(src / "Docs" / "a.txt", src / "Docs" / "b.txt")
    fake_drive._update(lambda s: remote_state.relocate(
        s, REMOTE + "/Docs/a.txt", REMOTE + "/Docs/b.txt"))
    result = engine(cfg, "--delete")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _transfers(fake_drive, start) == []
    assert _verbs(fake_drive, start, "rename", "move") == []
    after, _ = _rows(isolated_home, cfg)
    assert after[str(src / "Docs" / "b.txt")]["remote_node_id"] == \
        before[str(src / "Docs" / "a.txt")]["remote_node_id"]
    assert str(src / "Docs" / "a.txt") not in after
    _quiet(engine, fake_drive, cfg)


def test_both_sides_moved_to_different_names_keeps_both(
        fake_drive, local_tree, write_mappings, engine, isolated_home):
    src, cfg = _start(local_tree, write_mappings, engine, FILES)
    start = len(fake_drive.calls())
    os.rename(src / "Docs" / "a.txt", src / "Docs" / "mine.txt")
    fake_drive._update(lambda s: remote_state.relocate(
        s, REMOTE + "/Docs/a.txt", REMOTE + "/Docs/theirs.txt"))
    result = engine(cfg, "--delete")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ℹ" in result.stdout and "both" in result.stdout.lower()
    assert _verbs(fake_drive, start, "rename", "move", "trash") == []
    assert (src / "Docs" / "mine.txt").read_bytes() == b"AAAA"
    assert (src / "Docs" / "theirs.txt").read_bytes() == b"AAAA"
    assert fake_drive.content(REMOTE + "/Docs/mine.txt") == b"AAAA"
    assert fake_drive.content(REMOTE + "/Docs/theirs.txt") == b"AAAA"
    after, _ = _rows(isolated_home, cfg)
    assert str(src / "Docs" / "a.txt") not in after


def test_a_failed_rename_is_reported_and_the_normal_pass_runs(
        fake_drive, local_tree, write_mappings, engine, isolated_home):
    src, cfg = _start(local_tree, write_mappings, engine, FILES)
    fake_drive.add_fault(cmd="rename", stderr="Rename is down")
    os.rename(src / "Docs" / "a.txt", src / "Docs" / "b.txt")
    result = engine(cfg, "--delete")
    assert result.stdout.count("❌") >= 1, result.stdout
    assert "Rename is down" in result.stdout + result.stderr
    assert "[moved]" not in result.stdout
    # The normal pass uploads the new name and trashes the old one.
    assert fake_drive.content(REMOTE + "/Docs/b.txt") == b"AAAA"
    assert not _live(fake_drive, REMOTE + "/Docs/a.txt")
    assert '"rename_failed": 1' in result.stdout


def test_a_failed_move_is_reported(
        fake_drive, local_tree, write_mappings, engine):
    src, cfg = _start(local_tree, write_mappings, engine, FILES)
    fake_drive.add_fault(cmd="move", stderr="Move is down")
    (src / "Docs" / "sub").mkdir()
    os.rename(src / "Docs" / "a.txt", src / "Docs" / "sub" / "a.txt")
    result = engine(cfg, "--delete")
    assert "❌" in result.stdout and "Move is down" in result.stdout, \
        result.stdout
    assert fake_drive.content(REMOTE + "/Docs/sub/a.txt") == b"AAAA"


def test_dry_run_changes_nothing(
        fake_drive, local_tree, write_mappings, engine, isolated_home):
    src, cfg = _start(local_tree, write_mappings, engine, FILES)
    before = _rows(isolated_home, cfg)
    start = len(fake_drive.calls())
    os.rename(src / "Docs" / "a.txt", src / "Docs" / "b.txt")
    result = engine(cfg, "--delete", "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _verbs(fake_drive, start, "rename", "move", "upload",
                  "download", "trash") == []
    assert fake_drive.content(REMOTE + "/Docs/a.txt") == b"AAAA"
    assert "[DRY-RUN]" in result.stdout
    assert _rows(isolated_home, cfg) == before
    # Same on the other side.
    os.rename(src / "Docs" / "b.txt", src / "Docs" / "a.txt")
    fake_drive._update(lambda s: remote_state.relocate(
        s, REMOTE + "/Docs/a.txt", REMOTE + "/Docs/c.txt"))
    result = engine(cfg, "--delete", "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (src / "Docs" / "a.txt").exists()
    assert not (src / "Docs" / "c.txt").exists()
    assert _rows(isolated_home, cfg) == before


def test_a_lost_mount_skips_detection(monkeypatch):
    import twoway

    class Ctx:
        opts = {"mount_lost": True}

    def boom(*args):
        raise AssertionError("nothing may be read when the mount is lost")
    monkeypatch.setattr(twoway, "_walk_remote", boom)
    monkeypatch.setattr(twoway, "_mapping_rows", boom)
    assert twoway._apply_moves(Ctx(), "/my-files/x") is None


def test_excluded_files_in_a_moved_folder_stay_where_they_are(
        fake_drive, local_tree, write_mappings, engine):
    src = local_tree({"Docs/dir/a.txt": (b"A", 1_000_000_000)})
    cfg = write_mappings(
        [_mapping(src / "Docs", REMOTE)],
        exclusions={"names": [".ds_store"], "patterns": []})
    for _ in range(2):
        assert engine(cfg).returncode == 0
    # A file excluded here sits in the folder on Proton.
    fake_drive.seed_file(REMOTE + "/Docs/dir/.DS_Store", b"x")
    start = len(fake_drive.calls())
    os.rename(src / "Docs" / "dir", src / "Docs" / "dir2")
    result = engine(cfg, "--delete")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _verbs(fake_drive, start, "upload", "download", "trash") == []
    assert fake_drive.content(REMOTE + "/Docs/dir2/a.txt") == b"A"
    assert not (src / "Docs" / "dir2" / ".DS_Store").exists()


def test_a_name_starting_with_a_dash_is_passed_after_the_separator(
        fake_drive, local_tree, write_mappings, engine):
    src, cfg = _start(local_tree, write_mappings, engine, FILES)
    start = len(fake_drive.calls())
    os.rename(src / "Docs" / "a.txt", src / "Docs" / "-b.txt")
    result = engine(cfg, "--delete")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _verbs(fake_drive, start, "rename") == [
        ["filesystem", "rename", "--", REMOTE + "/Docs/a.txt", "-b.txt"]]
    assert fake_drive.content(REMOTE + "/Docs/-b.txt") == b"AAAA"
    assert _transfers(fake_drive, start) == []


def test_a_pass_with_a_remote_rename_lists_each_folder_once(
        fake_drive, local_tree, write_mappings, engine):
    src, cfg = _start(local_tree, write_mappings, engine, {
        "Docs/a.txt": (b"A", 1_000_000_000),
        "Docs/dir/b.txt": (b"B", 1_000_000_000),
        "Docs/dir/deep/c.txt": (b"C", 1_000_000_000)})
    quiet_start = len(fake_drive.calls())
    assert engine(cfg, "--delete").returncode == 0
    quiet = [c[2] for c in _verbs(fake_drive, quiet_start, "list")]
    start = len(fake_drive.calls())
    fake_drive._update(lambda s: remote_state.relocate(
        s, REMOTE + "/Docs/a.txt", REMOTE + "/Docs/z.txt"))
    assert engine(cfg, "--delete").returncode == 0
    listed = [c[2] for c in _verbs(fake_drive, start, "list")]
    assert (src / "Docs" / "z.txt").exists()
    for folder in set(listed):
        assert listed.count(folder) == 1, listed
    assert sorted(set(listed)) == sorted(set(quiet)), (listed, quiet)


def test_the_mapping_root_is_never_moved(
        fake_drive, local_tree, write_mappings, engine):
    src, cfg = _start(local_tree, write_mappings, engine, FILES)
    start = len(fake_drive.calls())
    fake_drive._update(lambda s: remote_state.relocate(
        s, REMOTE + "/Docs", REMOTE + "/Docs2"))
    engine(cfg, "--delete")
    assert (src / "Docs").is_dir()
    assert _verbs(fake_drive, start, "rename", "move") == []


def test_a_symlink_to_a_moved_file_is_not_a_move(
        fake_drive, local_tree, write_mappings, engine):
    src, cfg = _start(local_tree, write_mappings, engine, FILES)
    start = len(fake_drive.calls())
    os.rename(src / "Docs" / "a.txt", src / "outside.txt")
    os.symlink(src / "outside.txt", src / "Docs" / "link.txt")
    result = engine(cfg, "--delete")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _verbs(fake_drive, start, "rename", "move") == []
    assert "[moved]" not in result.stdout


def test_a_file_moved_here_into_a_folder_renamed_on_proton(
        fake_drive, local_tree, write_mappings, engine):
    src, cfg = _start(local_tree, write_mappings, engine, {
        "Docs/a.txt": (b"AAAA", 1_000_000_000),
        "Docs/sub/other.txt": (b"OTHER", 1_000_000_000)})
    start = len(fake_drive.calls())
    os.rename(src / "Docs" / "a.txt", src / "Docs" / "sub" / "a.txt")
    fake_drive._update(lambda s: remote_state.relocate(
        s, REMOTE + "/Docs/sub", REMOTE + "/Docs/sub2"))
    result = engine(cfg, "--delete")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not _live(fake_drive, REMOTE + "/Docs/sub")
    assert fake_drive.content(REMOTE + "/Docs/sub2/other.txt") == b"OTHER"
    for _ in range(3):
        result = engine(cfg, "--delete")
        assert result.returncode == 0, result.stdout + result.stderr
    assert (src / "Docs" / "sub2" / "a.txt").read_bytes() == b"AAAA"
    assert (src / "Docs" / "sub2" / "other.txt").read_bytes() == b"OTHER"
    assert not (src / "Docs" / "sub").exists()
    assert fake_drive.content(REMOTE + "/Docs/sub2/a.txt") == b"AAAA"
    assert not _live(fake_drive, REMOTE + "/Docs/a.txt")
    _quiet(engine, fake_drive, cfg)

"""Folder baseline of the two-way reconcile: a folder deleted on one side.

A folder row seen on both sides is the baseline. Missing on one side with
such a row means deleted there; with no row it is new on the other side.
"""

__version__ = "1.0.0"

import os
import shutil
import sqlite3

import pytest

import remote_state
import syncdb


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


def _nodes(fake_drive):
    holder = {}

    def look(state):
        holder["nodes"] = {k: bool(v.get("trashed"))
                           for k, v in state["nodes"].items()}
    fake_drive._update(look)
    return holder["nodes"]


def _live(fake_drive, remote):
    return _nodes(fake_drive).get(remote_state.normalize(remote)) is False


def _trashes(fake_drive):
    return [c for c in fake_drive.calls() if c[:2] == ["filesystem", "trash"]]


def _drop_remote(fake_drive, *remotes):
    def mutate(state):
        for remote in remotes:
            prefix = remote_state.normalize(remote)
            for key in list(state["nodes"]):
                if key == prefix or key.startswith(prefix + "/"):
                    state["nodes"].pop(key)
    fake_drive._update(mutate)


def _rows(isolated_home, cfg, under):
    with syncdb.SyncDB(_db(isolated_home, cfg)) as database:
        prefix = str(under)
        files = [r for r in database.rows()
                 if r["local_path"].startswith(prefix)]
        folders = [r for r in database.folders()
                   if r["local_path"].startswith(prefix)]
    return files, folders


def _tree(local_tree, write_mappings, engine, files, **extra):
    src = local_tree(files)
    cfg = write_mappings([_mapping(src / "Docs", "/my-files/Backups", **extra)])
    assert engine(cfg).returncode == 0
    return src, cfg


def test_folder_deleted_here_is_trashed_once_and_stays_gone(
        fake_drive, local_tree, write_mappings, engine, isolated_home):
    src, cfg = _tree(local_tree, write_mappings, engine, {
        "Docs/a.txt": (b"A", 1_000_000_000),
        "Docs/sub/b.txt": (b"B", 1_000_000_000),
        "Docs/sub/deep/c.txt": (b"C", 1_000_000_000)})
    shutil.rmtree(src / "Docs" / "sub")
    result = engine(cfg, "--delete")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _trashes(fake_drive) == [
        ["filesystem", "trash", "/my-files/Backups/Docs/sub"]]
    assert not _live(fake_drive, "/my-files/Backups/Docs/sub")
    assert not (src / "Docs" / "sub").exists()
    files, folders = _rows(isolated_home, cfg, src / "Docs" / "sub")
    assert files == [] and folders == []
    again = engine(cfg, "--delete")
    assert again.returncode == 0, again.stdout + again.stderr
    assert "[download]" not in again.stdout
    assert len(_trashes(fake_drive)) == 1
    assert not (src / "Docs" / "sub").exists()


def test_folder_deleted_here_with_a_file_changed_on_proton_is_restored(
        fake_drive, local_tree, write_mappings, engine):
    src, cfg = _tree(local_tree, write_mappings, engine, {
        "Docs/sub/b.txt": (b"B", 1_000_000_000),
        "Docs/sub/c.txt": (b"C", 1_000_000_000)})
    shutil.rmtree(src / "Docs" / "sub")
    fake_drive.seed_file("/my-files/Backups/Docs/sub/b.txt", b"B2",
                         1_000_000_500)
    result = engine(cfg, "--delete")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (src / "Docs" / "sub" / "b.txt").read_bytes() == b"B2"
    # The unchanged sibling follows the file rules: deleted here, trashed.
    assert not (src / "Docs" / "sub" / "c.txt").exists()
    assert not _live(fake_drive, "/my-files/Backups/Docs/sub/c.txt")
    assert _live(fake_drive, "/my-files/Backups/Docs/sub")
    assert ["filesystem", "trash", "/my-files/Backups/Docs/sub"] not in _trashes(fake_drive)
    # One line says why the folder is back, before the file lines.
    why = result.stdout.index(
        "  ℹ  [restore] {}: deleted here, but changed on Proton since".format(
            src / "Docs" / "sub"))
    assert why < result.stdout.index("[download]")


def test_folder_deleted_here_with_a_new_file_on_proton_is_restored(
        fake_drive, local_tree, write_mappings, engine):
    src, cfg = _tree(local_tree, write_mappings, engine, {
        "Docs/sub/b.txt": (b"B", 1_000_000_000)})
    shutil.rmtree(src / "Docs" / "sub")
    fake_drive.seed_file("/my-files/Backups/Docs/sub/new.txt", b"N")
    result = engine(cfg, "--delete")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (src / "Docs" / "sub" / "new.txt").read_bytes() == b"N"
    assert not _live(fake_drive, "/my-files/Backups/Docs/sub/b.txt")
    assert _live(fake_drive, "/my-files/Backups/Docs/sub/new.txt")
    assert _live(fake_drive, "/my-files/Backups/Docs/sub")
    assert ["filesystem", "trash", "/my-files/Backups/Docs/sub"] not in _trashes(fake_drive)


def test_excluded_name_on_proton_does_not_keep_a_folder_deleted_here(
        fake_drive, local_tree, write_mappings, engine):
    src = local_tree({"Docs/a.txt": (b"A", 1_000_000_000),
                      "Docs/sub/b.txt": (b"B", 1_000_000_000)})
    cfg = write_mappings(
        [_mapping(src / "Docs", "/my-files/Backups", allow_delete=True,
                  source_kind="local")],
        exclusions={"names": [".ds_store"], "patterns": []})
    assert engine(cfg).returncode == 0
    # A file another device left on Proton, excluded here.
    fake_drive.seed_file("/my-files/Backups/Docs/sub/.DS_Store", b"x")
    shutil.rmtree(src / "Docs" / "sub")
    result = engine(cfg, "--delete")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _trashes(fake_drive) == [
        ["filesystem", "trash", "/my-files/Backups/Docs/sub"]], result.stdout
    assert not (src / "Docs" / "sub").exists()
    assert "[restore]" not in result.stdout


def test_folder_deleted_on_proton_is_held_and_removed_here(
        fake_drive, local_tree, write_mappings, engine, isolated_home):
    src, cfg = _tree(local_tree, write_mappings, engine, {
        "Docs/a.txt": (b"A", 1_000_000_000),
        "Docs/sub/b.txt": (b"B", 1_000_000_000),
        "Docs/sub/deep/c.txt": (b"C", 1_000_000_000)})
    _drop_remote(fake_drive, "/my-files/Backups/Docs/sub")
    result = engine(cfg, "--delete")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (src / "Docs" / "sub").exists()
    assert (src / "Docs" / "a.txt").exists()
    held = [os.path.join(p, f) for p, _d, fs in os.walk(src)
            for f in fs if ".proton-sync-holding-" in p]
    assert sorted(open(h, "rb").read() for h in held) == [b"B", b"C"]
    assert _nodes(fake_drive).get(
        remote_state.normalize("/my-files/Backups/Docs/sub")) is None
    files, folders = _rows(isolated_home, cfg, src / "Docs" / "sub")
    assert files == [] and folders == []
    again = engine(cfg, "--delete")
    assert again.returncode == 0, again.stdout + again.stderr
    assert "[held]" not in again.stdout and "[folder-missing]" not in again.stdout
    assert _nodes(fake_drive).get(
        remote_state.normalize("/my-files/Backups/Docs/sub")) is None


def test_folder_deleted_on_proton_with_a_local_edit_comes_back(
        fake_drive, local_tree, write_mappings, engine):
    src, cfg = _tree(local_tree, write_mappings, engine, {
        "Docs/sub/b.txt": (b"B", 1_000_000_000),
        "Docs/sub/c.txt": (b"C", 1_000_000_000)})
    _drop_remote(fake_drive, "/my-files/Backups/Docs/sub")
    local_tree.write("Docs/sub/b.txt", b"B-edited", 1_000_000_900)
    result = engine(cfg, "--delete")
    assert result.returncode == 0, result.stdout + result.stderr
    assert fake_drive.content("/my-files/Backups/Docs/sub/b.txt") == b"B-edited"
    assert (src / "Docs" / "sub" / "b.txt").read_bytes() == b"B-edited"
    assert not (src / "Docs" / "sub" / "c.txt").exists()
    assert _live(fake_drive, "/my-files/Backups/Docs/sub")
    again = engine(cfg, "--delete")
    assert again.returncode == 0, again.stdout + again.stderr
    assert "[held]" not in again.stdout and "[download]" not in again.stdout


def test_empty_folder_deleted_here_is_trashed(
        fake_drive, local_tree, write_mappings, engine):
    src, cfg = _tree(local_tree, write_mappings, engine, {
        "Docs/a.txt": (b"A", 1_000_000_000)})
    os.mkdir(src / "Docs" / "empty")
    assert engine(cfg).returncode == 0
    assert _live(fake_drive, "/my-files/Backups/Docs/empty")
    os.rmdir(src / "Docs" / "empty")
    assert engine(cfg, "--delete").returncode == 0
    assert not _live(fake_drive, "/my-files/Backups/Docs/empty")
    assert not (src / "Docs" / "empty").exists()


def test_empty_folder_deleted_on_proton_is_removed_here(
        fake_drive, local_tree, write_mappings, engine):
    src, cfg = _tree(local_tree, write_mappings, engine, {
        "Docs/a.txt": (b"A", 1_000_000_000)})
    os.mkdir(src / "Docs" / "empty")
    assert engine(cfg).returncode == 0
    _drop_remote(fake_drive, "/my-files/Backups/Docs/empty")
    assert engine(cfg, "--delete").returncode == 0
    assert not (src / "Docs" / "empty").exists()
    assert _nodes(fake_drive).get(
        remote_state.normalize("/my-files/Backups/Docs/empty")) is None


def test_excluded_file_keeps_the_local_folder_deleted_on_proton(
        fake_drive, local_tree, write_mappings, engine):
    src = local_tree({"Docs/a.txt": (b"A", 1_000_000_000),
                      "Docs/sub/b.txt": (b"B", 1_000_000_000)})
    cfg = write_mappings([_mapping(src / "Docs", "/my-files/Backups")],
                         exclusions={"names": ["keep.me"], "patterns": []})
    assert engine(cfg).returncode == 0
    local_tree.write("Docs/sub/keep.me", b"K", 1_000_000_000)
    _drop_remote(fake_drive, "/my-files/Backups/Docs/sub")
    result = engine(cfg, "--delete")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (src / "Docs" / "sub" / "keep.me").read_bytes() == b"K"
    assert not (src / "Docs" / "sub" / "b.txt").exists()


@pytest.mark.parametrize("extra,args", [
    ({"allow_delete": False}, ("--delete",)),
    ({}, ()),
])
def test_without_both_switches_no_folder_is_deleted_on_either_side(
        fake_drive, local_tree, write_mappings, engine, extra, args):
    src, cfg = _tree(local_tree, write_mappings, engine, {
        "Docs/gone-there/a.txt": (b"A", 1_000_000_000),
        "Docs/gone-here/b.txt": (b"B", 1_000_000_000)}, **extra)
    _drop_remote(fake_drive, "/my-files/Backups/Docs/gone-there")
    shutil.rmtree(src / "Docs" / "gone-here")
    result = engine(cfg, *args)
    assert "[folder-missing] " in result.stdout, result.stdout
    assert (src / "Docs" / "gone-there" / "a.txt").exists()
    assert _trashes(fake_drive) == []
    assert _nodes(fake_drive).get(
        remote_state.normalize("/my-files/Backups/Docs/gone-there")) is None
    # Deletions are off: the folder deleted here comes back, as before, and
    # its file on the next pass.
    assert (src / "Docs" / "gone-here").is_dir()
    assert engine(cfg, *args).returncode in (0, 5)
    assert (src / "Docs" / "gone-here" / "b.txt").read_bytes() == b"B"


def test_mass_guard_counts_the_files_of_a_folder_deleted_here(
        fake_drive, local_tree, write_mappings, engine):
    engine.update_settings(mass_delete_guard=True)
    files = {"Docs/keep.txt": (b"K", 1_000_000_000)}
    files.update({"Docs/sub/f%02d.txt" % i: (b"x%d" % i, 1_000_000_000)
                  for i in range(25)})
    src, cfg = _tree(local_tree, write_mappings, engine, files)
    shutil.rmtree(src / "Docs" / "sub")
    refused = engine(cfg, "--delete")
    assert "[delete-guard]" in refused.stdout, refused.stdout
    assert _trashes(fake_drive) == []
    assert not (src / "Docs" / "sub").exists()
    allowed = engine(cfg, "--delete", "--allow-mass-delete")
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr
    assert ["filesystem", "trash", "/my-files/Backups/Docs/sub"] in _trashes(fake_drive)


def test_mass_guard_counts_the_files_of_a_folder_deleted_on_proton(
        fake_drive, local_tree, write_mappings, engine):
    files = {"Docs/keep.txt": (b"K", 1_000_000_000)}
    files.update({"Docs/sub/d%d/f%d.txt" % (d, i): (b"x%d%d" % (d, i), 1_000_000_000)
                  for d in range(5) for i in range(5)})
    src, cfg = _tree(local_tree, write_mappings, engine, files)
    _drop_remote(fake_drive, "/my-files/Backups/Docs/sub")
    refused = engine(cfg, "--delete")
    assert "[delete-guard]" in refused.stdout, refused.stdout
    assert len([p for p, _d, fs in os.walk(src / "Docs" / "sub") for f in fs]) == 25
    allowed = engine(cfg, "--delete", "--allow-mass-delete")
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr
    assert not (src / "Docs" / "sub").exists()


def test_no_folder_row_keeps_todays_behaviour_both_ways(
        fake_drive, local_tree, write_mappings, engine, isolated_home):
    src, cfg = _tree(local_tree, write_mappings, engine, {
        "Docs/gone-there/a.txt": (b"A", 1_000_000_000),
        "Docs/gone-here/b.txt": (b"B", 1_000_000_000)})
    # A database from before the folder rows: files are known, folders are not.
    connection = sqlite3.connect(_db(isolated_home, cfg))
    connection.execute("DELETE FROM folders")
    connection.commit()
    connection.close()
    _drop_remote(fake_drive, "/my-files/Backups/Docs/gone-there")
    shutil.rmtree(src / "Docs" / "gone-here")
    result = engine(cfg, "--delete")
    # Deleted on Proton, no baseline: refused as before, nothing held.
    assert "[folder-missing] " in result.stdout, result.stdout
    assert (src / "Docs" / "gone-there" / "a.txt").exists()
    assert not [n for n in os.listdir(src) if n.startswith(".proton-sync-holding")]
    # Deleted here, no baseline: the folder is not trashed as a whole.
    assert ["filesystem", "trash", "/my-files/Backups/Docs/gone-here"] \
        not in _trashes(fake_drive)
    assert (src / "Docs" / "gone-here").is_dir()


def test_trash_failure_keeps_rows_and_the_next_pass_retries(
        fake_drive, local_tree, write_mappings, engine, isolated_home):
    src, cfg = _tree(local_tree, write_mappings, engine, {
        "Docs/sub/b.txt": (b"B", 1_000_000_000)})
    shutil.rmtree(src / "Docs" / "sub")
    fake_drive.add_fault(cmd="trash", match="/my-files/Backups/Docs/sub",
                         mode="fail", times=1)
    failed = engine(cfg, "--delete")
    assert failed.returncode != 0, failed.stdout
    assert _live(fake_drive, "/my-files/Backups/Docs/sub")
    assert not (src / "Docs" / "sub").exists()
    files, folders = _rows(isolated_home, cfg, src / "Docs" / "sub")
    assert files and folders
    retry = engine(cfg, "--delete")
    assert retry.returncode == 0, retry.stdout + retry.stderr
    assert not _live(fake_drive, "/my-files/Backups/Docs/sub")
    files, folders = _rows(isolated_home, cfg, src / "Docs" / "sub")
    assert files == [] and folders == []


def test_folder_gone_on_both_sides_is_forgotten(
        fake_drive, local_tree, write_mappings, engine, isolated_home):
    src, cfg = _tree(local_tree, write_mappings, engine, {
        "Docs/a.txt": (b"A", 1_000_000_000),
        "Docs/sub/b.txt": (b"B", 1_000_000_000)})
    shutil.rmtree(src / "Docs" / "sub")
    _drop_remote(fake_drive, "/my-files/Backups/Docs/sub")
    assert engine(cfg, "--delete").returncode == 0
    files, folders = _rows(isolated_home, cfg, src / "Docs" / "sub")
    assert folders == []
    assert _trashes(fake_drive) == []


def test_forget_tree_matches_whole_path_parts(tmp_path):
    with syncdb.SyncDB(tmp_path / "s.db") as database:
        for path in ("/a/b", "/a/b/c", "/a/bc", "/a/bc/d"):
            database.upsert_folder({"local_path": path, "remote_path": "/r",
                                    "local_seen": 1, "remote_seen": 1})
            database.upsert({"local_path": path + "/f", "remote_path": "/r/f",
                             "state": "synced"})
        database.forget_tree("/a/b")
        assert sorted(r["local_path"] for r in database.folders()) == [
            "/a/bc", "/a/bc/d"]
        assert sorted(r["local_path"] for r in database.rows()) == [
            "/a/bc/d/f", "/a/bc/f"]


def test_folder_missing_refusal_starts_with_a_warning_glyph(
        fake_drive, local_tree, write_mappings, engine):
    src, cfg = _tree(local_tree, write_mappings, engine, {
        "Docs/gone-there/a.txt": (b"A", 1_000_000_000)}, allow_delete=False)
    _drop_remote(fake_drive, "/my-files/Backups/Docs/gone-there")
    result = engine(cfg)
    assert any(line.startswith("  ⚠  [folder-missing] ")
               for line in result.stdout.splitlines()), result.stdout


def test_delete_guard_refusal_starts_with_a_warning_glyph(
        fake_drive, local_tree, write_mappings, engine):
    engine.update_settings(mass_delete_guard=True)
    files = {"Docs/keep.txt": (b"K", 1_000_000_000)}
    files.update({"Docs/sub/f%02d.txt" % i: (b"x%d" % i, 1_000_000_000)
                  for i in range(25)})
    src, cfg = _tree(local_tree, write_mappings, engine, files)
    shutil.rmtree(src / "Docs" / "sub")
    refused = engine(cfg, "--delete")
    assert any(line.startswith("  ⚠  [delete-guard] ")
               for line in refused.stdout.splitlines()), refused.stdout

"""The events trigger of the two-way sync.

The events probe only decides which folders a pass looks at; the listing stays
the truth. These tests drive the engine with the fake probe (tests/fakes/
fake_probe.py), which reads the fake Drive, and check the pass against the
full listing: what is looked at, what is never lost, and every way the probe
can fail.
"""

__version__ = "1.1.0"

import json
import os
import shutil
import subprocess
import sys

import pytest

import remote_state
import syncdb
import twoway_events

REMOTE = "/my-files/Backups"
FAKES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakes")


def _without_probe(monkeypatch, probe):
    """Take the fake probe's folder off the PATH."""
    rest = [d for d in os.environ.get("PATH", "").split(os.pathsep)
            if d != probe.bindir]
    monkeypatch.setenv("PATH", os.pathsep.join(rest))


@pytest.fixture
def probe(tmp_path, monkeypatch):
    """Install the fake probe as ``proton-sync-events`` in a folder put first on
    the PATH, and return the log of its runs."""
    target = tmp_path / "probebin"
    target.mkdir()
    for name in ("fake_probe.py", "remote_state.py"):
        shutil.copy(os.path.join(FAKES, name), target / name)
    binary = target / "proton-sync-events"
    shutil.copy(target / "fake_probe.py", binary)
    binary.chmod(0o755)
    log = tmp_path / "probe-log.jsonl"
    monkeypatch.setenv("PATH", str(target) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("FAKE_PROBE_LOG", str(log))
    for name in ("FAKE_PROBE_FAULT", "FAKE_PROBE_SCRIPT", "FAKE_PROBE_NOTE"):
        monkeypatch.delenv(name, raising=False)

    class _Probe:
        bindir = str(target)

        def runs(self, start=0):
            if not log.exists():
                return []
            lines = log.read_text(encoding="utf-8").splitlines()
            return [json.loads(line) for line in lines][start:]

        def count(self):
            return len(self.runs())

    return _Probe()


@pytest.fixture
def write_mappings(write_mappings):
    def _write(twoway, exclusions=None):
        return write_mappings([], exclusions=exclusions,
                              sync_mappings=[dict(m, direction="twoway")
                                             for m in twoway])
    return _write


def _mapping(source, dest=REMOTE, **extra):
    mapping = {"type": "folder", "source": str(source), "dest_parent": dest,
               "allow_delete": True, "source_kind": "local"}
    mapping.update(extra)
    return mapping


def _rows(isolated_home, cfg):
    path = syncdb.database_path(
        os.path.basename(str(cfg)),
        data_dir=str(isolated_home / ".proton-drive-sync"))
    with syncdb.SyncDB(path) as database:
        return ({r["local_path"]: r for r in database.rows()},
                {r["local_path"]: r for r in database.folders()})


def _flags(isolated_home, cfg):
    path = syncdb.database_path(
        os.path.basename(str(cfg)),
        data_dir=str(isolated_home / ".proton-drive-sync"))
    with syncdb.SyncDB(path) as database:
        return [k for (k,) in database._conn.execute(
            "SELECT key FROM meta WHERE key LIKE 'events_rescan:%'")]


def _meta(isolated_home, cfg, like, drop=False):
    """The meta rows whose key starts with ``like``: {key: value}."""
    path = syncdb.database_path(
        os.path.basename(str(cfg)),
        data_dir=str(isolated_home / ".proton-drive-sync"))
    with syncdb.SyncDB(path) as database:
        found = dict(database._conn.execute(
            "SELECT key, value FROM meta WHERE key LIKE ?", (like + "%",)))
        if drop:
            for key in found:
                database.delete_meta(key)
        return found


def _verbs(fake_drive, start, *verbs):
    return [c for c in fake_drive.calls()[start:]
            if len(c) > 1 and c[0] == "filesystem" and c[1] in verbs]


def _transfers(fake_drive, start):
    return _verbs(fake_drive, start, "upload", "download", "trash")


def _listed(fake_drive, start):
    """Proton folders listed since ``start``; the engine's own start-up check
    of "/" and "/my-files" is not a listing of the mapping."""
    found = set()
    for call in _verbs(fake_drive, start, "list"):
        paths = [a for a in call[2:] if a.startswith("/")]
        found.update(p for p in paths if p.startswith(REMOTE))
    return sorted(found)


def _events(engine, cfg, *args):
    """One pass with the events trigger (no --twoway-full-check)."""
    return engine(cfg, "--delete", *args, full_check=False)


FILES = {"Docs/a.txt": (b"AAAA", 1_000_000_000),
         "Docs/sub/b.txt": (b"BBBB", 1_000_000_000),
         "Docs/other/c.txt": (b"CCCC", 1_000_000_000)}


def _started(fake_drive, local_tree, write_mappings, engine, probe, files=None,
             **extra):
    """Passes until the tree is synced and the probe has nothing left to say."""
    src = local_tree(FILES if files is None else files)
    cfg = write_mappings([_mapping(src / "Docs", **extra)])
    fake_drive.seed_folder(REMOTE + "/Docs")    # the probe needs the folder
    for _ in range(3):
        result = _events(engine, cfg)
        assert result.returncode == 0, result.stdout + result.stderr
    return src, cfg


def _quiet(engine, fake_drive, probe, cfg):
    start, runs = len(fake_drive.calls()), probe.count()
    result = _events(engine, cfg)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _listed(fake_drive, start) == []
    assert _transfers(fake_drive, start) == []
    assert probe.count() == runs + 1          # read once, with --keep, no ack
    assert "-k" in probe.runs(runs)[0]["args"]
    return result


# ── No trigger setting ───────────────────────────────────────────────────────


def test_an_old_twoway_trigger_key_is_ignored(
        fake_drive, local_tree, write_mappings, engine, probe):
    src = local_tree(FILES)
    cfg = write_mappings([_mapping(src / "Docs")])
    fake_drive.seed_folder(REMOTE + "/Docs")
    engine.update_settings(twoway_trigger="none")
    result = engine(cfg, full_check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Two-way sync paused" not in result.stdout
    assert probe.count() >= 1
    assert fake_drive.content(REMOTE + "/Docs/a.txt") == b"AAAA"


# ── What a pass looks at ─────────────────────────────────────────────────────


def test_first_run_lists_everything_then_a_quiet_pass_lists_nothing(
        fake_drive, local_tree, write_mappings, engine, probe):
    src = local_tree(FILES)
    cfg = write_mappings([_mapping(src / "Docs")])
    fake_drive.seed_folder(REMOTE + "/Docs")
    result = _events(engine, cfg)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ℹ  [events] Listing the whole Proton side: the probe has no " \
        "starting point yet for this folder" in result.stdout
    assert fake_drive.content(REMOTE + "/Docs/sub/b.txt") == b"BBBB"
    assert probe.count() == 1                 # initializing needs no second read
    for _ in range(2):                        # the uploads show up as events
        assert _events(engine, cfg).returncode == 0
    result = _quiet(engine, fake_drive, probe, cfg)
    assert "0 event(s) read" in result.stdout
    assert _flags(engine.isolated_home, cfg) == []


def test_a_new_mapping_whose_proton_folder_is_missing_creates_it_then_uses_the_probe(
        fake_drive, local_tree, write_mappings, engine, probe):
    src = local_tree(FILES)
    cfg = write_mappings([_mapping(src / "Docs")])
    result = _events(engine, cfg)             # no Proton folder yet
    assert result.returncode == 0, result.stdout + result.stderr
    assert "its folder does not exist yet" in result.stdout
    assert "⏸" not in result.stdout
    assert fake_drive.content(REMOTE + "/Docs/sub/b.txt") == b"BBBB"
    for _ in range(3):                        # now the probe starts and settles
        assert _events(engine, cfg).returncode == 0
    _quiet(engine, fake_drive, probe, cfg)


def test_a_missing_proton_folder_with_history_pauses(
        fake_drive, local_tree, write_mappings, engine, probe):
    src, cfg = _started(fake_drive, local_tree, write_mappings, engine, probe)
    root = REMOTE + "/Docs"

    def gone(state):                          # the folder vanished on Proton
        for path in [p for p in state["nodes"]
                     if p == root or p.startswith(root + "/")]:
            del state["nodes"][path]
    fake_drive._update(gone)
    start = len(fake_drive.calls())
    result = _events(engine, cfg)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "⏸ Two-way sync paused for" in result.stdout
    assert _listed(fake_drive, start) == []


def test_a_change_on_proton_looks_only_at_its_folder(
        fake_drive, local_tree, write_mappings, engine, probe):
    src, cfg = _started(fake_drive, local_tree, write_mappings, engine, probe)
    fake_drive.seed_file(REMOTE + "/Docs/sub/new.txt", b"NEW")
    start, runs = len(fake_drive.calls()), probe.count()
    result = _events(engine, cfg)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (src / "Docs" / "sub" / "new.txt").read_bytes() == b"NEW"
    assert _listed(fake_drive, start) == [REMOTE + "/Docs", REMOTE + "/Docs/sub"]
    ran = probe.runs(runs)
    assert len(ran) == 2
    assert "-k" in ran[0]["args"] and "-k" not in ran[1]["args"]
    assert "1 event(s) read" in result.stdout
    assert _flags(engine.isolated_home, cfg) == []
    _quiet(engine, fake_drive, probe, cfg)


def test_a_change_here_looks_only_at_its_folder(
        fake_drive, local_tree, write_mappings, engine, probe):
    src, cfg = _started(fake_drive, local_tree, write_mappings, engine, probe)
    # Quiet first: events caused by our own uploads are read and settled.
    (src / "Docs" / "other" / "fresh.txt").write_bytes(b"FRESH")
    start = len(fake_drive.calls())
    result = _events(engine, cfg)
    assert result.returncode == 0, result.stdout + result.stderr
    assert fake_drive.content(REMOTE + "/Docs/other/fresh.txt") == b"FRESH"
    assert _listed(fake_drive, start) == [
        REMOTE + "/Docs", REMOTE + "/Docs/other"]
    assert _events(engine, cfg).returncode == 0   # reads our own upload back
    _quiet(engine, fake_drive, probe, cfg)


def test_a_changed_file_here_is_found_by_the_local_walk(
        fake_drive, local_tree, write_mappings, engine, probe):
    src, cfg = _started(fake_drive, local_tree, write_mappings, engine, probe)
    local_tree.write("Docs/sub/b.txt", b"BBBBBB", 1_000_000_500)
    start = len(fake_drive.calls())
    assert _events(engine, cfg).returncode == 0
    assert fake_drive.content(REMOTE + "/Docs/sub/b.txt") == b"BBBBBB"
    assert REMOTE + "/Docs/other" not in _listed(fake_drive, start)


def test_a_file_gone_here_is_found_by_the_local_walk(
        fake_drive, local_tree, write_mappings, engine, probe):
    src, cfg = _started(fake_drive, local_tree, write_mappings, engine, probe)
    (src / "Docs" / "sub" / "b.txt").unlink()
    start = len(fake_drive.calls())
    assert _events(engine, cfg).returncode == 0
    assert fake_drive.trashed(REMOTE + "/Docs/sub/b.txt")
    assert REMOTE + "/Docs/other" not in _listed(fake_drive, start)


def test_a_removed_event_only_marks_the_parent_and_the_listing_decides(
        fake_drive, local_tree, write_mappings, engine, probe):
    src, cfg = _started(fake_drive, local_tree, write_mappings, engine, probe)
    fake_drive._update(lambda s: remote_state.trash_tree(
        s, REMOTE + "/Docs/sub/b.txt"))
    start = len(fake_drive.calls())
    result = _events(engine, cfg)
    assert result.returncode == 0, result.stdout + result.stderr
    # The same decision as a full pass: Proton deleted it, --delete is on.
    assert not (src / "Docs" / "sub" / "b.txt").exists()
    assert (src / "Docs" / "a.txt").exists()
    assert _listed(fake_drive, start) == [REMOTE + "/Docs", REMOTE + "/Docs/sub"]


def test_a_removed_event_without_delete_does_not_remove_anything_here(
        fake_drive, local_tree, write_mappings, engine, probe):
    src, cfg = _started(fake_drive, local_tree, write_mappings, engine, probe)
    fake_drive._update(lambda s: remote_state.trash_tree(
        s, REMOTE + "/Docs/sub/b.txt"))
    result = engine(cfg, full_check=False)    # no --delete
    assert result.returncode == 0, result.stdout + result.stderr
    assert (src / "Docs" / "sub" / "b.txt").exists()


def test_excluded_names_never_count_as_a_change(
        fake_drive, local_tree, write_mappings, engine, probe):
    src = local_tree(FILES)
    cfg = write_mappings([_mapping(src / "Docs")],
                         exclusions={"names": ["skip.tmp"]})
    fake_drive.seed_folder(REMOTE + "/Docs")
    for _ in range(3):
        assert _events(engine, cfg).returncode == 0
    fake_drive.seed_file(REMOTE + "/Docs/sub/skip.tmp", b"JUNK")
    start = len(fake_drive.calls())
    result = _events(engine, cfg)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (src / "Docs" / "sub" / "skip.tmp").exists()
    assert _listed(fake_drive, start) == []
    assert _transfers(fake_drive, start) == []


def test_a_new_folder_on_proton_is_listed_with_what_it_holds(
        fake_drive, local_tree, write_mappings, engine, probe):
    src, cfg = _started(fake_drive, local_tree, write_mappings, engine, probe)
    fake_drive.seed_file(REMOTE + "/Docs/fresh/deep/x.txt", b"X")
    assert _events(engine, cfg).returncode == 0
    assert (src / "Docs" / "fresh" / "deep" / "x.txt").read_bytes() == b"X"


# ── Moves by event ───────────────────────────────────────────────────────────


def test_a_file_moved_on_proton_is_moved_here_without_a_transfer(
        fake_drive, local_tree, write_mappings, engine, probe, isolated_home):
    src, cfg = _started(fake_drive, local_tree, write_mappings, engine, probe)
    before, _folders = _rows(isolated_home, cfg)
    row = before[str(src / "Docs" / "a.txt")]

    def mutate(state):
        remote_state.ensure_folders(state, REMOTE + "/Docs/sub/a.txt")
        remote_state.relocate(state, REMOTE + "/Docs/a.txt",
                              REMOTE + "/Docs/sub/a.txt")
    fake_drive._update(mutate)
    start = len(fake_drive.calls())
    result = _events(engine, cfg)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _transfers(fake_drive, start) == []
    assert not (src / "Docs" / "a.txt").exists()
    assert (src / "Docs" / "sub" / "a.txt").read_bytes() == b"AAAA"
    after, _folders = _rows(isolated_home, cfg)
    assert after[str(src / "Docs" / "sub" / "a.txt")]["remote_node_id"] == \
        row["remote_node_id"]
    assert "[moved]" in result.stdout


def test_a_file_moved_from_another_folder_into_a_new_folder_is_not_downloaded(
        fake_drive, local_tree, write_mappings, engine, probe, isolated_home):
    src, cfg = _started(fake_drive, local_tree, write_mappings, engine, probe)
    before, _folders = _rows(isolated_home, cfg)
    row = before[str(src / "Docs" / "other" / "c.txt")]

    def mutate(state):
        remote_state.ensure_folders(state, REMOTE + "/Docs/n1/n2/c.txt")
        remote_state.relocate(state, REMOTE + "/Docs/other/c.txt",
                              REMOTE + "/Docs/n1/n2/c.txt")
    fake_drive._update(mutate)
    start = len(fake_drive.calls())
    result = _events(engine, cfg)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _transfers(fake_drive, start) == []
    assert not (src / "Docs" / "other" / "c.txt").exists()
    assert (src / "Docs" / "n1" / "n2" / "c.txt").read_bytes() == b"CCCC"
    after, _folders = _rows(isolated_home, cfg)
    assert after[str(src / "Docs" / "n1" / "n2" / "c.txt")][
        "remote_node_id"] == row["remote_node_id"]
    assert "[moved]" in result.stdout
    assert not [p for p in (src / "Docs").rglob("*") if "holding" in p.name]
    _quiet(engine, fake_drive, probe, cfg)


def test_a_folder_moved_on_proton_is_moved_here_with_what_it_holds(
        fake_drive, local_tree, write_mappings, engine, probe, isolated_home):
    src, cfg = _started(fake_drive, local_tree, write_mappings, engine, probe)

    def mutate(state):
        remote_state.relocate(state, REMOTE + "/Docs/other",
                              REMOTE + "/Docs/renamed")
    fake_drive._update(mutate)
    start = len(fake_drive.calls())
    result = _events(engine, cfg)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _transfers(fake_drive, start) == []
    assert not (src / "Docs" / "other").exists()
    assert (src / "Docs" / "renamed" / "c.txt").read_bytes() == b"CCCC"


def test_a_moved_file_with_other_content_is_left_to_the_normal_pass(
        fake_drive, local_tree, write_mappings, engine, probe):
    src, cfg = _started(fake_drive, local_tree, write_mappings, engine, probe)

    def mutate(state):
        remote_state.relocate(state, REMOTE + "/Docs/a.txt",
                              REMOTE + "/Docs/sub/a.txt")
        remote_state.store_file(state, REMOTE + "/Docs/sub/a.txt", b"CHANGED",
                                1_000_000_900)
    fake_drive._update(mutate)
    result = _events(engine, cfg)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (src / "Docs" / "sub" / "a.txt").read_bytes() == b"CHANGED"


# ── Nothing lost ─────────────────────────────────────────────────────────────


def test_a_lost_mount_scans_nothing_and_does_not_acknowledge(
        fake_drive, local_tree, write_mappings, engine, probe, monkeypatch):
    src, cfg = _started(fake_drive, local_tree, write_mappings, engine, probe)
    fake_drive.seed_file(REMOTE + "/Docs/sub/new.txt", b"NEW")
    monkeypatch.setenv(
        "ENGINE_TEST_PATCH",
        "proton_sync._delete_guard_ok = lambda *a, **k: (False, 'mount gone')")
    start, runs = len(fake_drive.calls()), probe.count()
    result = _events(engine, cfg)
    assert "mount check failed" in result.stdout
    assert _listed(fake_drive, start) == []
    assert _transfers(fake_drive, start) == []
    assert not (src / "Docs" / "sub" / "new.txt").exists()
    ran = probe.runs(runs)
    assert len(ran) == 1 and "-k" in ran[0]["args"]    # bookmark not moved
    monkeypatch.delenv("ENGINE_TEST_PATCH")
    assert _events(engine, cfg).returncode == 0         # read again, applied
    assert (src / "Docs" / "sub" / "new.txt").read_bytes() == b"NEW"


@pytest.mark.parametrize("fault, words", [
    ("fail", "it failed"),
    ("nosession", "auth login"),
    ("badjson", "not valid JSON"),
    ("escape", "outside the watched folder"),
])
def test_a_failing_probe_pauses_the_mapping_and_lists_nothing(
        fake_drive, local_tree, write_mappings, engine, probe, monkeypatch,
        isolated_home, fault, words):
    src, cfg = _started(fake_drive, local_tree, write_mappings, engine, probe)
    fake_drive.seed_file(REMOTE + "/Docs/sub/new.txt", b"NEW")
    monkeypatch.setenv("FAKE_PROBE_FAULT", fault)
    start, runs = len(fake_drive.calls()), probe.count()
    result = _events(engine, cfg)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "⏸ Two-way sync paused for" in result.stdout
    assert "the events probe could not be used" in result.stdout
    assert words in result.stdout
    assert "\"twoway_paused\": 1" in result.stdout
    assert not (src / "Docs" / "sub" / "new.txt").exists()
    assert _listed(fake_drive, start) == []                      # no listing
    ran = probe.runs(runs)
    assert len(ran) == 1 and "-k" in ran[0]["args"]              # no acknowledge
    monkeypatch.delenv("FAKE_PROBE_FAULT")                       # tried again
    result = _events(engine, cfg)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (src / "Docs" / "sub" / "new.txt").read_bytes() == b"NEW"


def test_a_missing_probe_pauses_the_mapping(
        fake_drive, local_tree, write_mappings, engine, probe, monkeypatch):
    src, cfg = _started(fake_drive, local_tree, write_mappings, engine, probe)
    fake_drive.seed_file(REMOTE + "/Docs/sub/new.txt", b"NEW")
    _without_probe(monkeypatch, probe)
    start = len(fake_drive.calls())
    result = _events(engine, cfg)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "⏸ Two-way sync paused for" in result.stdout
    assert "was not found" in result.stdout
    assert not (src / "Docs" / "sub" / "new.txt").exists()
    assert _listed(fake_drive, start) == []


def test_a_full_check_with_a_failing_probe_still_lists_everything(
        fake_drive, local_tree, write_mappings, engine, probe, monkeypatch):
    src, cfg = _started(fake_drive, local_tree, write_mappings, engine, probe)
    fake_drive.seed_file(REMOTE + "/Docs/sub/new.txt", b"NEW")
    monkeypatch.setenv("FAKE_PROBE_FAULT", "fail")
    start = len(fake_drive.calls())
    result = engine(cfg, "--delete")                 # --twoway-full-check
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Two-way sync paused" not in result.stdout
    assert "⚠  [events] The events probe could not be used" in result.stdout
    assert (src / "Docs" / "sub" / "new.txt").read_bytes() == b"NEW"
    assert REMOTE + "/Docs/other" in _listed(fake_drive, start)


def test_the_watched_folder_trashed_gives_a_full_listing_and_loses_nothing(
        fake_drive, local_tree, write_mappings, engine, probe):
    src, cfg = _started(fake_drive, local_tree, write_mappings, engine, probe)
    fake_drive._update(lambda s: remote_state.trash_tree(s, REMOTE + "/Docs"))
    start = len(fake_drive.calls())
    result = _events(engine, cfg)
    assert "State reset" in result.stdout or "was lost" in result.stdout
    assert (src / "Docs" / "a.txt").read_bytes() == b"AAAA"
    assert (src / "Docs" / "sub" / "b.txt").exists()
    assert _verbs(fake_drive, start, "trash") == []
    # The next run: the probe cannot use a trashed folder, so the mapping is
    # paused and nothing is listed or lost.
    start = len(fake_drive.calls())
    again = _events(engine, cfg)
    assert (src / "Docs" / "a.txt").exists()
    assert _verbs(fake_drive, start, "trash") == []
    assert "⏸ Two-way sync paused" in again.stdout


def test_a_failed_acknowledge_leaves_the_rescan_flag_and_the_next_pass_lists_all(
        fake_drive, local_tree, write_mappings, engine, probe, monkeypatch,
        isolated_home):
    src, cfg = _started(fake_drive, local_tree, write_mappings, engine, probe)
    fake_drive.seed_file(REMOTE + "/Docs/sub/new.txt", b"NEW")
    monkeypatch.setenv("FAKE_PROBE_FAULT", "fail:ack")
    result = _events(engine, cfg)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "could not be marked as read" in result.stdout
    assert (src / "Docs" / "sub" / "new.txt").read_bytes() == b"NEW"
    assert len(_flags(isolated_home, cfg)) == 1
    monkeypatch.delenv("FAKE_PROBE_FAULT")
    start = len(fake_drive.calls())
    result = _events(engine, cfg)
    assert "the previous pass did not finish applying the events" in result.stdout
    assert REMOTE + "/Docs/other" in _listed(fake_drive, start)   # everything
    assert _flags(isolated_home, cfg) == []
    _quiet(engine, fake_drive, probe, cfg)


def test_a_crash_between_the_two_reads_loses_nothing(
        fake_drive, local_tree, write_mappings, engine, probe, monkeypatch,
        isolated_home):
    src, cfg = _started(fake_drive, local_tree, write_mappings, engine, probe)
    fake_drive.seed_file(REMOTE + "/Docs/sub/new.txt", b"NEW")
    monkeypatch.setenv("FAKE_PROBE_FAULT", "killparent:ack")
    result = _events(engine, cfg)
    assert result.returncode != 0                     # the engine was killed
    assert len(_flags(isolated_home, cfg)) == 1       # set before the second read
    monkeypatch.delenv("FAKE_PROBE_FAULT")
    result = _events(engine, cfg)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (src / "Docs" / "sub" / "new.txt").read_bytes() == b"NEW"
    assert _flags(isolated_home, cfg) == []
    _quiet(engine, fake_drive, probe, cfg)


def test_a_crash_before_any_pass_work_is_done_is_read_again(
        fake_drive, local_tree, write_mappings, engine, probe, monkeypatch):
    src, cfg = _started(fake_drive, local_tree, write_mappings, engine, probe)
    fake_drive.seed_file(REMOTE + "/Docs/sub/new.txt", b"NEW")
    monkeypatch.setenv("ENGINE_TEST_PATCH", (
        "import os, signal\n"
        "_real = twoway._sync_dir\n"
        "def _die(*a, **k):\n"
        "    os.kill(os.getpid(), signal.SIGKILL)\n"
        "twoway._sync_dir = _die\n"))
    runs = probe.count()
    result = _events(engine, cfg)
    assert result.returncode != 0
    assert len(probe.runs(runs)) == 1                 # only the --keep read
    monkeypatch.delenv("ENGINE_TEST_PATCH")
    assert _events(engine, cfg).returncode == 0
    assert (src / "Docs" / "sub" / "new.txt").read_bytes() == b"NEW"


def test_a_change_arriving_between_the_two_reads_is_applied_in_the_same_pass(
        fake_drive, local_tree, write_mappings, engine, probe, monkeypatch,
        tmp_path, isolated_home):
    src, cfg = _started(fake_drive, local_tree, write_mappings, engine, probe)
    fake_drive.seed_file(REMOTE + "/Docs/sub/first.txt", b"ONE")
    script = tmp_path / "probe-script.json"
    script.write_text(json.dumps({"ack": [
        {"op": "write", "path": REMOTE + "/Docs/other/late.txt",
         "text": "LATE", "mtime": 1_000_000_000}]}), encoding="utf-8")
    monkeypatch.setenv("FAKE_PROBE_SCRIPT", str(script))
    result = _events(engine, cfg)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (src / "Docs" / "sub" / "first.txt").read_bytes() == b"ONE"
    assert (src / "Docs" / "other" / "late.txt").read_bytes() == b"LATE"
    assert "1 change(s) arrived while the pass ran" in result.stdout
    assert _flags(isolated_home, cfg) == []
    monkeypatch.delenv("FAKE_PROBE_SCRIPT")
    _quiet(engine, fake_drive, probe, cfg)


def test_a_full_check_lists_everything_and_still_acknowledges(
        fake_drive, local_tree, write_mappings, engine, probe):
    src, cfg = _started(fake_drive, local_tree, write_mappings, engine, probe)
    fake_drive.seed_file(REMOTE + "/Docs/sub/new.txt", b"NEW")
    start, runs = len(fake_drive.calls()), probe.count()
    result = engine(cfg, "--delete")                 # --twoway-full-check
    assert result.returncode == 0, result.stdout + result.stderr
    assert "a full check was asked for" in result.stdout
    assert REMOTE + "/Docs/other" in _listed(fake_drive, start)
    ran = probe.runs(runs)
    assert len(ran) == 2 and "-k" not in ran[1]["args"]
    _quiet(engine, fake_drive, probe, cfg)


def test_a_dry_run_does_not_call_the_probe_and_writes_no_data_folder(
        fake_drive, local_tree, write_mappings, engine, probe, isolated_home):
    src = local_tree(FILES)
    cfg = write_mappings([_mapping(src / "Docs")])
    fake_drive.seed_folder(REMOTE + "/Docs")
    result = _events(engine, cfg, "--dry-run")      # folders missing: exit 5, as before
    assert "ℹ  [events] Listing the whole Proton side: a dry run does not " \
        "use the events probe" in result.stdout
    assert probe.count() == 0
    assert not (isolated_home / ".proton-drive-sync" / "events").exists()
    assert _flags(isolated_home, cfg) == []
    assert "a.txt" not in fake_drive.listing(REMOTE + "/Docs")   # nothing applied
    # The first real pass still finds the probe without a starting point.
    result = _events(engine, cfg)
    assert "the probe has no starting point yet" in result.stdout


# ── Two mappings ─────────────────────────────────────────────────────────────


def test_two_mappings_get_two_data_folders_that_never_overlap(
        fake_drive, local_tree, write_mappings, engine, probe, tmp_path):
    one = local_tree({"One/a.txt": (b"A", 1_000_000_000)})
    other = tmp_path / "src2"
    other.mkdir()
    (other / "Two").mkdir()
    (other / "Two" / "b.txt").write_bytes(b"B")
    cfg = write_mappings([_mapping(one / "One"), _mapping(other / "Two")])
    fake_drive.seed_folder(REMOTE + "/One")
    fake_drive.seed_folder(REMOTE + "/Two")
    for _ in range(3):
        result = _events(engine, cfg)
        assert result.returncode == 0, result.stdout + result.stderr
    dirs = {run["data_dir"] for run in probe.runs()}
    assert len(dirs) == 2
    first, second = sorted(dirs)
    assert not first.startswith(second + os.sep)
    assert not second.startswith(first + os.sep)
    assert fake_drive.content(REMOTE + "/One/a.txt") == b"A"
    assert fake_drive.content(REMOTE + "/Two/b.txt") == b"B"
    for run in probe.runs():
        assert run["args"][-1] in (REMOTE + "/One", REMOTE + "/Two")


# ── The probe, read directly ─────────────────────────────────────────────────


@pytest.fixture
def direct(fake_drive, probe, tmp_path, monkeypatch):
    monkeypatch.setattr(twoway_events, "events_dir",
                        lambda mapping: str(tmp_path / "events-data"))
    fake_drive.seed_file(REMOTE + "/Docs/a.txt", b"A")
    return {"source": str(tmp_path / "Docs"), "dest_parent": REMOTE}


def test_read_changes_parses_what_the_probe_reports(direct, fake_drive):
    first = twoway_events.read_changes(direct, keep=True)
    assert first.ok and first.initialized and first.changes == []
    fake_drive.seed_file(REMOTE + "/Docs/b.txt", b"B")
    second = twoway_events.read_changes(direct, keep=True)
    assert second.ok and not second.initialized and second.events_read == 1
    assert [(c["action"], c["path"]) for c in second.changes] == [
        ("created", "b.txt")]
    again = twoway_events.read_changes(direct, keep=False)
    assert [c["path"] for c in again.changes] == ["b.txt"]
    assert twoway_events.read_changes(direct, keep=False).changes == []


def test_read_changes_missing_probe(direct, probe, monkeypatch):
    _without_probe(monkeypatch, probe)
    result = twoway_events.read_changes(direct, keep=True)
    assert not result.ok and "not found" in result.reason


def test_read_changes_failing_probe(direct, monkeypatch):
    monkeypatch.setenv("FAKE_PROBE_FAULT", "fail")
    result = twoway_events.read_changes(direct, keep=True)
    assert not result.ok and "it failed (exit 1)" in result.reason


def test_read_changes_no_session_names_the_login_command(direct, monkeypatch):
    monkeypatch.setenv("FAKE_PROBE_FAULT", "nosession")
    result = twoway_events.read_changes(direct, keep=True)
    assert not result.ok
    assert "proton-sync-events auth login" in result.reason


def test_read_changes_timeout(direct, monkeypatch):
    monkeypatch.setenv("FAKE_PROBE_FAULT", "hang")
    monkeypatch.setattr(twoway_events, "PROBE_TIMEOUT", 1)
    result = twoway_events.read_changes(direct, keep=True)
    assert not result.ok and "did not answer within 1 seconds" in result.reason


def test_read_changes_invalid_json(direct, monkeypatch):
    monkeypatch.setenv("FAKE_PROBE_FAULT", "badjson")
    result = twoway_events.read_changes(direct, keep=True)
    assert not result.ok and "not valid JSON" in result.reason


def test_read_changes_path_escaping_the_folder_is_invalid(direct, monkeypatch):
    monkeypatch.setenv("FAKE_PROBE_FAULT", "escape")
    result = twoway_events.read_changes(direct, keep=True)
    assert not result.ok
    assert "outside the watched folder" in result.reason


@pytest.mark.parametrize("path", ["../x", "/abs", "a//b", "a/../b", "", "./a"])
def test_unsafe_paths_are_refused(path):
    assert twoway_events._safe_path(path) is None


def test_safe_paths_are_kept():
    assert twoway_events._safe_path("a/b c/d.txt") == "a/b c/d.txt"


@pytest.mark.parametrize("text", [
    "[]", '{"initialized": "yes", "changes": []}',
    '{"initialized": false, "eventsRead": -1, "changes": []}',
    '{"initialized": false, "eventsRead": 1, "changes": {}}',
    '{"initialized": false, "eventsRead": 1, "changes": [{"action": "bogus",'
    ' "type": "file", "path": "a"}]}',
    '{"initialized": false, "eventsRead": 1, "changes": [{"action": "moved",'
    ' "type": "file", "path": "a"}]}',
])
def test_malformed_output_is_a_failure(text):
    assert not twoway_events._parse(text).ok


def test_the_fake_probe_rejects_what_the_real_one_rejects(tmp_path):
    fake = os.path.join(FAKES, "fake_probe.py")
    env = dict(os.environ, PROTON_SYNC_EVENTS_DIR=str(tmp_path / "d"),
               FAKE_PROTON_STATE=str(tmp_path / "state.json"))
    for option in ("-x", "--json-lines", "--since", "-q"):
        done = subprocess.run(
            [sys.executable, fake, "events", "changes", option, "-j", "/f"],
            env=env, capture_output=True, text=True)
        assert done.returncode != 0
        assert "Unknown option" in done.stderr


def test_the_fake_probe_help_lists_exactly_the_real_options(tmp_path):
    """The contract test: skipped when the real binary is not installed."""
    real = os.environ.get("PROTON_SYNC_EVENTS_REAL") or shutil.which(
        "proton-sync-events")
    if not real or not os.access(real, os.X_OK):
        pytest.skip("the real proton-sync-events binary is not installed")
    import re
    fake = os.path.join(FAKES, "fake_probe.py")
    env = dict(os.environ, PROTON_SYNC_EVENTS_DIR=str(tmp_path / "d"))

    def options(cmd):
        done = subprocess.run(cmd + ["events", "changes", "--help"], env=env,
                              capture_output=True, text=True, timeout=60,
                              stdin=subprocess.DEVNULL)
        text = done.stdout + done.stderr
        return {o for o in re.findall(r"(?<!\w)(--?[a-z][\w-]*)", text)
                if o in ("-j", "--json", "-k", "--keep", "-e", "--events")}
    assert options([real]) == options([sys.executable, fake]) == {
        "-j", "--json", "-k", "--keep", "-e", "--events"}
    done = subprocess.run(
        [real, "events", "changes", "--bogus-option", "/x"], env=env,
        capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    assert done.returncode != 0


# ── Review fixes ─────────────────────────────────────────────────────────────

_FAIL_ONE_LISTING = (
    "_real = proton_sync.get_remote_listing\n"
    "def _listing(path, verbose=False):\n"
    "    if path.endswith('/Docs/sub'):\n"
    "        return proton_sync.RemoteListing(ok=False, error='boom')\n"
    "    return _real(path, verbose)\n"
    "proton_sync.get_remote_listing = _listing\n")


def test_a_first_listing_that_stops_halfway_is_not_forgotten(
        fake_drive, local_tree, write_mappings, engine, probe, monkeypatch,
        isolated_home):
    src = local_tree(FILES)
    cfg = write_mappings([_mapping(src / "Docs")])
    fake_drive.seed_file(REMOTE + "/Docs/sub/missed.txt", b"MISSED")
    monkeypatch.setenv("ENGINE_TEST_PATCH", _FAIL_ONE_LISTING)
    result = _events(engine, cfg)          # the probe starts, the listing fails
    assert "the probe has no starting point yet" in result.stdout
    assert not (src / "Docs" / "sub" / "missed.txt").exists()
    assert len(_flags(isolated_home, cfg)) == 1
    monkeypatch.delenv("ENGINE_TEST_PATCH")
    start = len(fake_drive.calls())
    result = _events(engine, cfg)
    assert "the previous pass did not finish applying the events" in result.stdout
    assert (src / "Docs" / "sub" / "missed.txt").read_bytes() == b"MISSED"
    assert REMOTE + "/Docs/other" in _listed(fake_drive, start)
    assert _flags(isolated_home, cfg) == []


def test_a_mapping_without_rows_gets_a_full_listing(
        fake_drive, local_tree, write_mappings, engine, probe, isolated_home):
    src, cfg = _started(fake_drive, local_tree, write_mappings, engine, probe)
    fake_drive.seed_file(REMOTE + "/Docs/sub/early.txt", b"EARLY")
    for path in isolated_home.rglob("*.sync.sqlite*"):        # the database is wiped
        path.unlink()
    runs = probe.count()
    start = len(fake_drive.calls())
    result = _events(engine, cfg)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "nothing is recorded yet for this folder" in result.stdout
    assert REMOTE + "/Docs/other" in _listed(fake_drive, start)
    assert (src / "Docs" / "sub" / "early.txt").read_bytes() == b"EARLY"
    assert len(probe.runs(runs)) >= 1


def test_the_probe_note_about_a_full_refresh_means_a_full_listing(
        fake_drive, local_tree, write_mappings, engine, probe, monkeypatch):
    src, cfg = _started(fake_drive, local_tree, write_mappings, engine, probe)
    monkeypatch.setenv("FAKE_PROBE_NOTE", "Proton asked for a full refresh.")
    start = len(fake_drive.calls())
    result = _events(engine, cfg)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "must be read again" in result.stdout
    assert REMOTE + "/Docs/other" in _listed(fake_drive, start)


def test_a_deletion_waiting_without_delete_keeps_only_its_folder_in_view(
        fake_drive, local_tree, write_mappings, engine, probe, isolated_home):
    src, cfg = _started(fake_drive, local_tree, write_mappings, engine, probe)
    fake_drive._update(lambda s: remote_state.trash_tree(
        s, REMOTE + "/Docs/sub/b.txt"))
    runs = probe.count()
    result = engine(cfg, full_check=False)            # no --delete
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[kept]" in result.stdout
    assert "A deletion is waiting" in result.stdout
    assert (src / "Docs" / "sub" / "b.txt").exists()
    ran = probe.runs(runs)
    assert len(ran) == 2 and "-k" not in ran[1]["args"]   # acknowledged
    assert list(_meta(isolated_home, cfg, "events_waiting:").values()) == [
        '["sub"]']
    assert _flags(isolated_home, cfg) == []
    # No new event, still no --delete: only the held folder is listed.
    for _ in range(2):
        start = len(fake_drive.calls())
        result = engine(cfg, full_check=False)
        assert result.returncode == 0, result.stdout + result.stderr
        assert _listed(fake_drive, start) == [REMOTE + "/Docs", REMOTE + "/Docs/sub"]
        assert (src / "Docs" / "sub" / "b.txt").exists()
    # A --delete pass applies it, and the next pass is quiet again.
    result = _events(engine, cfg)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (src / "Docs" / "sub" / "b.txt").exists()
    assert _meta(isolated_home, cfg, "events_waiting:") == {}
    _quiet(engine, fake_drive, probe, cfg)


def test_a_deletion_waiting_in_a_full_listing_clears_the_flag_and_keeps_its_folder(
        fake_drive, local_tree, write_mappings, engine, probe, isolated_home):
    src, cfg = _started(fake_drive, local_tree, write_mappings, engine, probe)
    fake_drive._update(lambda s: remote_state.trash_tree(
        s, REMOTE + "/Docs/sub/b.txt"))
    result = engine(cfg)                    # full check, no --delete
    assert result.returncode == 0, result.stdout + result.stderr
    assert "A deletion is waiting" in result.stdout
    assert (src / "Docs" / "sub" / "b.txt").exists()
    assert _flags(isolated_home, cfg) == []
    assert list(_meta(isolated_home, cfg, "events_waiting:").values()) == [
        '["sub"]']
    start = len(fake_drive.calls())
    result = engine(cfg, full_check=False)  # scoped: the held folder only
    assert result.returncode == 0, result.stdout + result.stderr
    assert REMOTE + "/Docs/other" not in _listed(fake_drive, start)
    assert REMOTE + "/Docs/sub" in _listed(fake_drive, start)
    result = _events(engine, cfg)           # --delete applies it
    assert not (src / "Docs" / "sub" / "b.txt").exists()
    assert _meta(isolated_home, cfg, "events_waiting:") == {}


# ── The probe's first walk ───────────────────────────────────────────────────

_SHORT_WALK = ("import twoway_events; twoway_events.FIRST_WALK_TIMEOUT = 1; "
               "twoway_events.SLOW_FAILURE = 0")


def test_a_first_walk_that_times_out_pauses_the_mapping_for_a_day(
        fake_drive, local_tree, write_mappings, engine, probe, monkeypatch,
        isolated_home):
    src = local_tree(FILES)
    cfg = write_mappings([_mapping(src / "Docs")])
    fake_drive.seed_folder(REMOTE + "/Docs")
    monkeypatch.setenv("ENGINE_TEST_PATCH", _SHORT_WALK)
    monkeypatch.setenv("FAKE_PROBE_FAULT", "hang")
    start = len(fake_drive.calls())
    result = _events(engine, cfg)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "⏸ Two-way sync paused for" in result.stdout
    assert "did not answer within 1 seconds" in result.stdout
    assert _listed(fake_drive, start) == []
    assert len(_meta(isolated_home, cfg, "events_first_failed:")) == 1
    # The next pass does not call the probe again, and says when it will.
    monkeypatch.delenv("FAKE_PROBE_FAULT")
    runs = probe.count()
    result = _events(engine, cfg)
    assert "first walk failed" in result.stdout
    assert "It is tried again after" in result.stdout
    assert probe.count() == runs
    assert _listed(fake_drive, start) == []
    # A day later it is tried again, and the failure is forgotten.
    ((key, value),) = _meta(isolated_home, cfg, "events_first_failed:").items()
    old = json.loads(value)
    old["time"] -= 25 * 3600
    path = syncdb.database_path(
        os.path.basename(str(cfg)),
        data_dir=str(isolated_home / ".proton-drive-sync"))
    with syncdb.SyncDB(path) as database:
        database.set_meta(key, json.dumps(old))
    result = _events(engine, cfg)
    assert result.returncode == 0, result.stdout + result.stderr
    assert probe.count() > runs
    assert fake_drive.content(REMOTE + "/Docs/a.txt") == b"AAAA"
    assert _meta(isolated_home, cfg, "events_first_failed:") == {}


def test_a_full_check_tries_a_failed_first_walk_at_once(
        fake_drive, local_tree, write_mappings, engine, probe, monkeypatch,
        isolated_home):
    src = local_tree(FILES)
    cfg = write_mappings([_mapping(src / "Docs")])
    fake_drive.seed_folder(REMOTE + "/Docs")
    monkeypatch.setenv("ENGINE_TEST_PATCH", _SHORT_WALK)
    monkeypatch.setenv("FAKE_PROBE_FAULT", "hang:1")
    _events(engine, cfg)
    assert len(_meta(isolated_home, cfg, "events_first_failed:")) == 1
    runs = probe.count()
    result = engine(cfg, "--delete")                 # --twoway-full-check
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Two-way sync paused" not in result.stdout
    assert probe.count() > runs
    assert fake_drive.content(REMOTE + "/Docs/a.txt") == b"AAAA"


def test_a_fast_failure_of_the_first_walk_is_tried_on_every_pass(
        fake_drive, local_tree, write_mappings, engine, probe, monkeypatch,
        isolated_home):
    src = local_tree(FILES)
    cfg = write_mappings([_mapping(src / "Docs")])
    fake_drive.seed_folder(REMOTE + "/Docs")
    monkeypatch.setenv("FAKE_PROBE_FAULT", "nosession")
    for n in (1, 2):
        result = _events(engine, cfg)
        assert "⏸ Two-way sync paused for" in result.stdout
        assert probe.count() == n
    assert _meta(isolated_home, cfg, "events_first_failed:") == {}


def test_the_probe_state_file_tells_a_first_walk(direct):
    assert not twoway_events.has_state(direct)
    assert twoway_events.read_changes(direct, keep=True).ok
    assert twoway_events.has_state(direct)
    other = dict(direct, dest_parent=REMOTE + "/Elsewhere")
    assert not twoway_events.has_state(other)       # another folder


def test_a_local_rename_is_renamed_on_proton_without_a_transfer(
        fake_drive, local_tree, write_mappings, engine, probe, isolated_home):
    src, cfg = _started(fake_drive, local_tree, write_mappings, engine, probe)
    os.rename(src / "Docs" / "sub" / "b.txt", src / "Docs" / "sub" / "bb.txt")
    start = len(fake_drive.calls())
    result = _events(engine, cfg)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _transfers(fake_drive, start) == []
    assert len(_verbs(fake_drive, start, "rename")) == 1
    assert fake_drive.content(REMOTE + "/Docs/sub/bb.txt") == b"BBBB"
    assert REMOTE + "/Docs/other" not in _listed(fake_drive, start)


def test_a_local_rename_onto_a_name_taken_on_proton_makes_no_rename_call(
        fake_drive, local_tree, write_mappings, engine, probe):
    src, cfg = _started(fake_drive, local_tree, write_mappings, engine, probe)
    fake_drive.seed_file(REMOTE + "/Docs/sub/taken.txt", b"THEIRS")
    os.rename(src / "Docs" / "sub" / "b.txt", src / "Docs" / "sub" / "taken.txt")
    start = len(fake_drive.calls())
    result = _events(engine, cfg)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _verbs(fake_drive, start, "rename", "move") == []
    assert fake_drive.content(REMOTE + "/Docs/sub/taken.txt") == b"THEIRS"
    names = sorted(os.listdir(src / "Docs" / "sub"))
    assert any("proton conflict" in n for n in names), names


def test_a_symlinked_file_does_not_make_every_pass_dirty(
        fake_drive, local_tree, write_mappings, engine, probe, tmp_path):
    target = tmp_path / "elsewhere.txt"
    target.write_bytes(b"LINKED")
    os.utime(target, (1_000_000_000, 1_000_000_000))
    src = local_tree(FILES)
    os.symlink(target, src / "Docs" / "sub" / "link.txt")
    cfg = write_mappings([_mapping(src / "Docs")])
    for _ in range(3):
        assert _events(engine, cfg).returncode == 0
    _quiet(engine, fake_drive, probe, cfg)


def test_a_local_deletion_without_delete_says_it_will_be_restored(
        fake_drive, local_tree, write_mappings, engine, probe, isolated_home):
    src, cfg = _started(fake_drive, local_tree, write_mappings, engine, probe)
    os.remove(src / "Docs" / "sub" / "b.txt")
    first = engine(cfg, full_check=False)                 # no --delete
    assert first.returncode == 0, first.stdout + first.stderr
    assert "A deletion is waiting" not in first.stdout, first.stdout
    assert _meta(isolated_home, cfg, "events_waiting:") == {}
    second = engine(cfg, full_check=False)
    assert second.returncode == 0, second.stdout + second.stderr
    line = "deleted here, but deletions are off for this pass"
    assert line in second.stdout, second.stdout
    assert "[download]" in second.stdout, second.stdout
    assert second.stdout.index(line) < second.stdout.index("[download]")
    assert (src / "Docs" / "sub" / "b.txt").exists()
    assert not fake_drive.trashed(REMOTE + "/Docs/sub/b.txt")
    _quiet(engine, fake_drive, probe, cfg)


def test_a_local_folder_deletion_without_delete_says_it_will_be_restored(
        fake_drive, local_tree, write_mappings, engine, probe, isolated_home):
    src, cfg = _started(fake_drive, local_tree, write_mappings, engine, probe)
    shutil.rmtree(src / "Docs" / "sub")
    first = engine(cfg, full_check=False)                 # no --delete
    assert first.returncode == 0, first.stdout + first.stderr
    assert "deleted here, but deletions are off for this pass" in first.stdout
    assert "A deletion is waiting" not in first.stdout, first.stdout
    # The folder comes back with what it held, in the same pass, and the
    # line comes once, for the folder, before its downloads.
    assert (src / "Docs" / "sub" / "b.txt").exists()
    assert first.stdout.count("deletions are off for this pass") == 1
    assert (first.stdout.index("deletions are off for this pass")
            < first.stdout.index("[download]"))
    assert not fake_drive.trashed(REMOTE + "/Docs/sub")
    _quiet(engine, fake_drive, probe, cfg)

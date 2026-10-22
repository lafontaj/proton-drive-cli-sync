"""Arrival-folder conflicts between the two mappings files in service.

The scheduled pass and real time may run on two different mappings files.
A mapping of one and a mapping of the other that land in the same Proton
folder, or one inside the other, trash each other's files exactly like two
mappings of one file. The engine holds back the deletions of its own mapping
in such a pair. The same mapping present in both files (same source, same
destination, same effective exclusions) is not a conflict.
"""

import json


def _mapping(source, dest, **extra):
    mapping = {"type": "folder", "source": str(source), "dest_parent": dest,
               "allow_delete": True, "source_kind": "local"}
    mapping.update(extra)
    return mapping


def _write(path, mappings, exclusions=None):
    doc = {"mappings": mappings}
    if exclusions is not None:
        doc["exclusions"] = exclusions
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _services(home, schedule=None, realtime=None):
    units = home / ".config" / "systemd" / "user"
    units.mkdir(parents=True, exist_ok=True)
    if schedule:
        (units / "proton-sync.service").write_text(
            "[Service]\nExecStart=/usr/bin/python3 /app/proton_sync.py %s\n" % schedule,
            encoding="utf-8")
    if realtime:
        (units / "proton-consume.service").write_text(
            "[Service]\nExecStart=/usr/bin/python3 /app/realtime_consumer.py %s\n" % realtime,
            encoding="utf-8")


def _trash_calls(fake_drive):
    return [c for c in fake_drive.calls() if c[:2] == ["filesystem", "trash"]]


def _photos(local_tree):
    return local_tree({"here/Photos/a.jpg": (b"AAAA", 1_000_000_000),
                       "here/Photos/old.jpg": (b"OLD", 1_000_000_000),
                       "nas/Photos/b.jpg": (b"BBBB", 1_000_000_000)})


def test_conflict_with_the_other_file_holds_deletions(
        fake_drive, local_tree, isolated_home, tmp_path, engine):
    root = _photos(local_tree)
    sched = _write(tmp_path / "mappings-sched.json",
                   [_mapping(root / "here" / "Photos", "/my-files/Backups")])
    rt = _write(tmp_path / "mappings-rt.json",
                [_mapping(root / "nas" / "Photos", "/my-files/Backups")])
    _services(isolated_home, schedule=sched, realtime=rt)
    assert engine(rt).returncode == 0
    assert engine(sched).returncode == 0
    result = engine(sched, "--delete")
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert _trash_calls(fake_drive) == [], out
    assert "[arrival-conflict]" in out and "mappings-rt.json" in out, out
    assert sorted(fake_drive.listing("/my-files/Backups/Photos")) == [
        "a.jpg", "b.jpg", "old.jpg"]


def test_same_mapping_in_both_files_is_not_a_conflict(
        fake_drive, local_tree, isolated_home, tmp_path, engine):
    root = _photos(local_tree)
    m = _mapping(root / "here" / "Photos", "/my-files/Backups")
    sched = _write(tmp_path / "mappings-sched.json", [m])
    rt = _write(tmp_path / "mappings-rt.json", [dict(m)])
    _services(isolated_home, schedule=sched, realtime=rt)
    assert engine(sched).returncode == 0
    (root / "here" / "Photos" / "old.jpg").unlink()
    result = engine(sched, "--delete")
    out = result.stdout + result.stderr
    assert "[arrival-conflict]" not in out, out
    assert fake_drive.trashed("/my-files/Backups/Photos/old.jpg"), out


def test_same_mapping_with_other_exclusions_is_a_conflict(
        fake_drive, local_tree, isolated_home, tmp_path, engine):
    root = _photos(local_tree)
    m = _mapping(root / "here" / "Photos", "/my-files/Backups")
    sched = _write(tmp_path / "mappings-sched.json", [m])
    rt = _write(tmp_path / "mappings-rt.json", [dict(m)],
                exclusions={"names": ["old.jpg"], "patterns": []})
    _services(isolated_home, schedule=sched, realtime=rt)
    assert engine(sched).returncode == 0
    result = engine(rt, "--delete")
    out = result.stdout + result.stderr
    # The real-time file excludes old.jpg: without the hold, its pass would
    # trash what the scheduled pass sends.
    assert _trash_calls(fake_drive) == [], out
    assert "mappings-sched.json" in out, out


def test_one_file_for_both_services_reads_nothing_else(
        fake_drive, local_tree, isolated_home, tmp_path, engine):
    root = _photos(local_tree)
    main = _write(tmp_path / "mappings-main.json",
                  [_mapping(root / "here" / "Photos", "/my-files/Backups")])
    # A file that is not in service may conflict: it does not count.
    _write(tmp_path / "mappings-idle.json",
           [_mapping(root / "nas" / "Photos", "/my-files/Backups")])
    _services(isolated_home, schedule=main, realtime=main)
    assert engine(main).returncode == 0
    (root / "here" / "Photos" / "old.jpg").unlink()
    result = engine(main, "--delete")
    out = result.stdout + result.stderr
    assert "[arrival-conflict]" not in out, out
    assert fake_drive.trashed("/my-files/Backups/Photos/old.jpg"), out


def test_manual_pass_on_a_third_file_is_checked_too(
        fake_drive, local_tree, isolated_home, tmp_path, engine):
    root = _photos(local_tree)
    main = _write(tmp_path / "mappings-main.json",
                  [_mapping(root / "nas" / "Photos", "/my-files/Backups")])
    third = _write(tmp_path / "mappings-third.json",
                   [_mapping(root / "here" / "Photos", "/my-files/Backups")])
    _services(isolated_home, schedule=main, realtime=main)
    assert engine(main).returncode == 0
    result = engine(third, "--delete")
    out = result.stdout + result.stderr
    assert _trash_calls(fake_drive) == [], out
    assert "mappings-main.json" in out, out


def test_missing_other_file_is_ignored(
        fake_drive, local_tree, isolated_home, tmp_path, engine):
    root = _photos(local_tree)
    sched = _write(tmp_path / "mappings-sched.json",
                   [_mapping(root / "here" / "Photos", "/my-files/Backups")])
    _services(isolated_home, schedule=sched, realtime=tmp_path / "gone.json")
    assert engine(sched).returncode == 0
    (root / "here" / "Photos" / "old.jpg").unlink()
    result = engine(sched, "--delete")
    out = result.stdout + result.stderr
    assert "[arrival-conflict]" not in out, out
    assert fake_drive.trashed("/my-files/Backups/Photos/old.jpg"), out


def test_nested_with_the_other_file_holds_deletions(
        fake_drive, local_tree, isolated_home, tmp_path, engine):
    root = local_tree({"here/Documents/a.txt": (b"A", 1_000_000_000),
                       "nas/Invoices/b.txt": (b"B", 1_000_000_000)})
    sched = _write(tmp_path / "mappings-sched.json",
                   [_mapping(root / "here" / "Documents", "/my-files/Backups")])
    rt = _write(tmp_path / "mappings-rt.json",
                [_mapping(root / "nas" / "Invoices", "/my-files/Backups/Documents")])
    _services(isolated_home, schedule=sched, realtime=rt)
    assert engine(rt).returncode == 0
    result = engine(sched, "--delete")
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert _trash_calls(fake_drive) == [], out
    assert "are nested" in out and "mappings-rt.json" in out, out


def test_unknown_service_files_refuse_all_deletions(monkeypatch, tmp_path):
    import proton_sync

    def broken():
        raise ImportError("realtime_manager")
    monkeypatch.setattr(proton_sync, "_running_mappings_files", broken)
    maps = [_mapping("/h/A", "/my-files/x"), _mapping("/h/B", "/my-files/y")]
    held = {}
    assert proton_sync._hold_cross_file_conflicts(
        str(tmp_path / "m.json"), maps, held) is False
    assert [m["allow_delete"] for m in maps] == [False, False]


def test_disabled_service_does_not_count(
        fake_drive, local_tree, isolated_home, tmp_path, engine, monkeypatch):
    root = _photos(local_tree)
    sched = _write(tmp_path / "mappings-sched.json",
                   [_mapping(root / "here" / "Photos", "/my-files/Backups")])
    rt = _write(tmp_path / "mappings-rt.json",
                [_mapping(root / "nas" / "Photos", "/my-files/Backups")])
    _services(isolated_home, schedule=sched, realtime=rt)
    # Real time was tried once, then disabled: it no longer counts.
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    tool = fake_bin / "systemctl"
    tool.write_text("#!/bin/sh\ncase \"$*\" in *proton-consume*) echo disabled; exit 1;; "
                    "*) echo enabled;; esac\n", encoding="utf-8")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin) + ":" + __import__("os").environ["PATH"])
    assert engine(sched).returncode == 0
    (root / "here" / "Photos" / "old.jpg").unlink()
    result = engine(sched, "--delete")
    out = result.stdout + result.stderr
    assert "[arrival-conflict]" not in out, out
    assert fake_drive.trashed("/my-files/Backups/Photos/old.jpg"), out


def _write_sync(path, mappings, sync_mappings):
    path.write_text(json.dumps({"mappings": mappings,
                                "sync_mappings": sync_mappings}), encoding="utf-8")
    return path


def _two_way(source, dest, **extra):
    return _mapping(source, dest, direction="twoway", **extra)


def test_upload_mapping_overlapping_a_two_way_of_the_other_file_holds_deletions(
        fake_drive, local_tree, isolated_home, tmp_path, engine):
    root = _photos(local_tree)
    sched = _write(tmp_path / "mappings-sched.json",
                   [_mapping(root / "here" / "Photos", "/my-files/Backups")])
    rt = _write_sync(tmp_path / "mappings-rt.json", [],
                     [_two_way(root / "nas" / "Photos", "/my-files/Backups")])
    _services(isolated_home, schedule=sched, realtime=rt)
    assert engine(sched).returncode == 0
    result = engine(sched, "--delete")
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert _trash_calls(fake_drive) == [], out
    assert "[twoway-conflict]" in out and "mappings-rt.json" in out, out
    assert "old.jpg" in fake_drive.listing("/my-files/Backups/Photos")


def test_two_way_mapping_overlapping_a_mapping_of_the_other_file_does_not_run(
        fake_drive, local_tree, isolated_home, tmp_path, engine):
    root = _photos(local_tree)
    sched = _write(tmp_path / "mappings-sched.json",
                   [_mapping(root / "here" / "Photos", "/my-files/Backups")])
    rt = _write_sync(tmp_path / "mappings-rt.json", [],
                     [_two_way(root / "nas" / "Photos", "/my-files/Backups")])
    _services(isolated_home, schedule=sched, realtime=rt)
    fake_drive.seed_file("/my-files/Backups/Photos/web.jpg", b"W")
    result = engine(rt)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "Two-way mapping not run" in out and "mappings-sched.json" in out, out
    assert not (root / "nas" / "Photos" / "web.jpg").exists()
    assert "b.jpg" not in fake_drive.listing("/my-files/Backups/Photos")


def test_two_way_mapping_overlapping_a_two_way_of_the_other_file_does_not_run(
        fake_drive, local_tree, isolated_home, tmp_path, engine):
    root = _photos(local_tree)
    sched = _write_sync(tmp_path / "mappings-sched.json", [],
                        [_two_way(root / "here" / "Photos", "/my-files/Backups")])
    rt = _write_sync(tmp_path / "mappings-rt.json", [],
                     [_two_way(root / "nas" / "Photos", "/my-files/Backups")])
    _services(isolated_home, schedule=sched, realtime=rt)
    result = engine(rt)
    out = result.stdout + result.stderr
    assert "Two-way mapping not run" in out and "mappings-sched.json" in out, out
    assert "b.jpg" not in fake_drive.listing("/my-files/Backups/Photos")

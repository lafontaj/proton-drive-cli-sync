"""A finished pass reports failures instead of exiting 0.

The phase-03 xfails in test_known_bugs.py failed before this phase because the
engine exited 0. These tests cover the rest of the run-result contract.
"""

import fcntl
import json
import os

import pytest

import proton_sync
import realtime_consumer
import schedule_manager


def _mapping(source, **extra):
    mapping = {
        "type": "folder",
        "source": str(source),
        "dest_parent": "/my-files/Backups",
    }
    mapping.update(extra)
    return mapping


def _run_result(stdout):
    lines = [line for line in stdout.splitlines() if line.startswith("[run-result] ")]
    assert len(lines) == 1, stdout
    return json.loads(lines[0][len("[run-result] "):])


def _last_run_path(isolated_home):
    return isolated_home / ".proton-drive-sync" / "last-run.json"


def test_clean_pass_exits_0_with_run_result_line(
        fake_drive, local_tree, write_mappings, engine):
    src = local_tree({
        "Docs/a.txt": (b"hello", 1_000_000_000),
        "Docs/sub/b.txt": (b"world", 1_000_000_000),
    })
    cfg = write_mappings([_mapping(src / "Docs")])
    result = engine(cfg)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = _run_result(result.stdout)
    assert payload["exit"] == 0
    assert payload["mode"] == "full"
    assert payload["files_uploaded"] == 2
    assert payload["files_failed"] == 0
    assert payload["files_would_upload"] == 0
    assert payload["mappings_total"] == 1
    assert payload["mappings_complete"] == 1
    lines = result.stdout.splitlines()
    assert lines[-1].startswith("[run-result] ")
    index = next(i for i, line in enumerate(lines) if line.startswith("[run-result] "))
    assert lines[index - 1].startswith("Summary:")
    assert "Done." in lines[:index]


def test_run_result_stays_last_when_last_run_unwritable(
        fake_drive, local_tree, write_mappings, engine, isolated_home):
    src = local_tree({"Docs/a.txt": (b"hello", 1_000_000_000)})
    blocked = isolated_home / ".proton-drive-sync" / "last-run.json"
    blocked.parent.mkdir(parents=True, exist_ok=True)
    blocked.mkdir()
    cfg = write_mappings([_mapping(src / "Docs")])
    result = engine(cfg)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines()[-1].startswith("[run-result] ")
    assert "Could not write the last-run file" in result.stdout


def test_permission_denied_exits_5(fake_drive, local_tree, write_mappings, engine):
    src = local_tree({"Docs/a.txt": (b"hello", 1_000_000_000)})
    fake_drive.add_fault(
        cmd="create-folder",
        match="/my-files/Backups",
        stderr="Vous n'avez pas l'autorisation d'effectuer cette action.",
    )
    cfg = write_mappings([_mapping(src / "Docs")])
    result = engine(cfg)
    assert result.returncode == 5, result.stdout + result.stderr
    assert _run_result(result.stdout)["folders_permission_denied"] >= 1
    assert fake_drive.content("/my-files/Backups/Docs/a.txt") is None


def test_file_mapping_upload_failure_exits_5(fake_drive, local_tree, write_mappings, engine):
    src = local_tree({"notes.txt": (b"hello", 1_000_000_000)})
    fake_drive.add_fault(cmd="upload", match="notes.txt", times=5, stderr="nope")
    cfg = write_mappings([{
        "type": "file",
        "source": str(src / "notes.txt"),
        "dest_parent": "/my-files/Backups",
    }])
    result = engine(cfg)
    assert result.returncode == 5, result.stdout + result.stderr
    payload = _run_result(result.stdout)
    assert payload["files_failed"] >= 1
    assert payload["mappings_complete"] == 0


def test_stall_skip_exits_5(
        fake_drive, local_tree, write_mappings, engine, isolated_home, tmp_path):
    src = local_tree({"Docs/a.txt": (b"hello", 1_000_000_000)})
    settings = tmp_path / "engine-settings.json"
    settings.write_text(
        json.dumps({"language": "en", "cli_stall_max_kills": 2}) + "\n",
        encoding="utf-8",
    )
    stall = isolated_home / ".proton-drive-sync" / "upload-stalls.json"
    stall.parent.mkdir(parents=True, exist_ok=True)
    stall.write_text(json.dumps({"/my-files/Backups/Docs": 2}) + "\n", encoding="utf-8")
    cfg = write_mappings([_mapping(src / "Docs")])
    result = engine(cfg)
    assert result.returncode == 5, result.stdout + result.stderr
    assert _run_result(result.stdout)["folders_stall_skipped"] == 1


def test_listing_failure_exits_5(fake_drive, local_tree, write_mappings, engine):
    src = local_tree({"Docs/a.txt": (b"hello", 1_000_000_000)})
    fake_drive.add_fault(cmd="list", match="/my-files/Backups/Docs", times=5)
    cfg = write_mappings([_mapping(src / "Docs")])
    result = engine(cfg)
    assert result.returncode == 5, result.stdout + result.stderr
    payload = _run_result(result.stdout)
    assert payload["folders_listing_failed"] >= 1
    assert payload["files_uploaded"] == 0
    assert fake_drive.content("/my-files/Backups/Docs/a.txt") is None


def test_dry_run_new_subfolder_is_not_a_failure(
        fake_drive, local_tree, write_mappings, engine):
    src = local_tree({"Docs/a.txt": (b"hello", 1_000_000_000)})
    cfg = write_mappings([_mapping(src / "Docs")])
    warmed = engine(cfg)
    assert warmed.returncode == 0, warmed.stdout + warmed.stderr
    local_tree.write("Docs/extra/c.txt", b"new", 1_000_000_100)
    dry = engine(cfg, "--dry-run")
    assert dry.returncode == 0, dry.stdout + dry.stderr
    payload = _run_result(dry.stdout)
    assert payload["folders_listing_failed"] == 0
    assert payload["files_would_upload"] >= 1
    assert "[DRY-RUN] would upload:" in dry.stdout


def test_dry_run_new_mapping_is_not_a_failure(
        fake_drive, local_tree, write_mappings, engine):
    src = local_tree({"Docs/a.txt": (b"hello", 1_000_000_000)})
    cfg = write_mappings([_mapping(src / "Docs")])
    dry = engine(cfg, "--dry-run")
    assert dry.returncode == 0, dry.stdout + dry.stderr
    payload = _run_result(dry.stdout)
    assert payload["folders_listing_failed"] == 0
    assert payload["files_would_upload"] == 1
    assert "would upload:" in dry.stdout


def test_dry_run_listing_fault_on_existing_folder_exits_5(
        fake_drive, local_tree, write_mappings, engine):
    src = local_tree({"Docs/a.txt": (b"hello", 1_000_000_000)})
    cfg = write_mappings([_mapping(src / "Docs")])
    warmed = engine(cfg)
    assert warmed.returncode == 0, warmed.stdout + warmed.stderr
    local_tree.write("Docs/a.txt", b"hello!", 1_000_000_100)
    fake_drive.add_fault(cmd="list", match="/my-files/Backups/Docs", times=5)
    dry = engine(cfg, "--dry-run")
    assert dry.returncode == 5, dry.stdout + dry.stderr
    payload = _run_result(dry.stdout)
    assert payload["folders_listing_failed"] >= 1
    assert "Could not list" in dry.stdout


def test_unreadable_folder_exits_5(fake_drive, local_tree, write_mappings, engine):
    if not hasattr(os, "geteuid") or os.geteuid() == 0:
        pytest.skip("mode 000 is ignored when running as root")
    src = local_tree({
        "Docs/keep.txt": (b"keep", 1_000_000_000),
        "Docs/hidden/a.txt": (b"secret", 1_000_000_000),
    })
    hidden = src / "Docs" / "hidden"
    hidden.chmod(0)
    try:
        cfg = write_mappings([_mapping(src / "Docs")])
        result = engine(cfg)
    finally:
        hidden.chmod(0o755)
    assert result.returncode == 5, result.stdout + result.stderr
    payload = _run_result(result.stdout)
    assert payload["folders_unreadable"] >= 1


def test_missing_source_exits_5(fake_drive, write_mappings, engine, tmp_path):
    missing = tmp_path / "no-such-source"
    cfg = write_mappings([_mapping(missing)])
    result = engine(cfg)
    assert result.returncode == 5, result.stdout + result.stderr
    payload = _run_result(result.stdout)
    assert payload["sources_missing"] == 1
    assert payload["mappings_complete"] == 0


def test_trash_failure_exits_5(fake_drive, local_tree, write_mappings, engine):
    src = local_tree({"Docs/a.txt": (b"hello", 1_000_000_000)})
    cfg = write_mappings([_mapping(src / "Docs", allow_delete=True, source_kind="local")])
    assert engine(cfg).returncode == 0
    (src / "Docs" / "a.txt").unlink()
    fake_drive.add_fault(cmd="trash", match="/my-files/Backups/Docs/a.txt", times=3)
    result = engine(cfg, "--delete")
    assert result.returncode == 5, result.stdout + result.stderr
    assert not fake_drive.trashed("/my-files/Backups/Docs/a.txt")
    assert _run_result(result.stdout)["trash_failed"] >= 1


def test_subpath_cold_still_exits_3(
        fake_drive, local_tree, write_mappings, engine, isolated_home):
    src = local_tree({"Docs/sub/a.txt": (b"hello", 1_000_000_000)})
    cfg = write_mappings([_mapping(src / "Docs")])
    result = engine(cfg, "--subpath", str(src / "Docs" / "sub"),
                    "--mapping-source", str(src / "Docs"))
    assert result.returncode == 3, result.stdout + result.stderr
    assert "[subpath-cold]" in result.stdout or "[subpath-cold-root]" in result.stdout
    assert _run_result(result.stdout)["exit"] == 3
    assert not _last_run_path(isolated_home).exists()


def test_last_run_json_written_atomically_and_keeps_last_full_on_subpath(
        fake_drive, local_tree, write_mappings, engine, isolated_home):
    src = local_tree({"Docs/a.txt": (b"hello", 1_000_000_000)})
    cfg = write_mappings([_mapping(src / "Docs")])
    full = engine(cfg)
    assert full.returncode == 0, full.stdout + full.stderr
    path = _last_run_path(isolated_home)
    assert path.is_file()
    assert not path.with_name("last-run.json.tmp").exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    record = data["last_full"]
    assert record["exit"] == 0
    assert record["mode"] == "full"
    assert record["engine_version"] == proton_sync.__version__
    assert record["cli_version"] == "0.8.0"
    assert "T" in record["started_at"] and "T" in record["finished_at"]
    assert record["counters"]["files_uploaded"] == 1
    assert "last_subpath" not in data

    sub = engine(cfg, "--subpath", str(src / "Docs"), "--mapping-source", str(src / "Docs"))
    assert sub.returncode == 0, sub.stdout + sub.stderr
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["last_full"] == record
    assert after["last_subpath"]["exit"] == 0
    assert after["last_subpath"]["mode"] == "subpath"
    assert not path.with_name("last-run.json.tmp").exists()


def test_last_run_not_written_on_dry_run_or_lock_contention(
        fake_drive, local_tree, write_mappings, engine, isolated_home):
    src = local_tree({"Docs/a.txt": (b"hello", 1_000_000_000)})
    cfg = write_mappings([_mapping(src / "Docs")])
    # The remote folder already exists. This dry-run is a changed file, not a
    # folder that is not on Drive yet.
    warmed = engine(cfg)
    assert warmed.returncode == 0, warmed.stdout + warmed.stderr
    _last_run_path(isolated_home).unlink()
    local_tree.write("Docs/a.txt", b"hello!", 1_000_000_100)
    dry = engine(cfg, "--dry-run")
    assert dry.returncode == 0, dry.stdout + dry.stderr
    payload = _run_result(dry.stdout)
    assert payload["files_uploaded"] == 0
    assert payload["files_would_upload"] == 1
    assert not _last_run_path(isolated_home).exists()

    lock_path = isolated_home / ".proton-drive-sync" / "proton_sync.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        held = engine(cfg)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert held.returncode == 1, held.stdout + held.stderr
    assert "[run-result]" not in held.stdout
    assert not _last_run_path(isolated_home).exists()


def test_last_run_written_on_auth_and_account(
        fake_drive, local_tree, write_mappings, engine, isolated_home):
    src = local_tree({"Docs/a.txt": (b"hello", 1_000_000_000)})
    cfg = write_mappings([_mapping(src / "Docs")])
    fake_drive.add_fault(cmd="auth")
    auth = engine(cfg)
    assert auth.returncode == 2, auth.stdout + auth.stderr
    data = json.loads(_last_run_path(isolated_home).read_text(encoding="utf-8"))
    assert data["last_full"]["exit"] == 2
    # The auth fault has no `times`, so it would keep firing. Drop it before
    # the account-change pass, which has to get past the auth probe.
    def _clear_faults(state):
        state["faults"] = []

    fake_drive._update(_clear_faults)

    cache = isolated_home / ".proton-drive-sync" / "cache" / "mappings.cache"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"__meta__": {"account": "other@example.com"}}),
                     encoding="utf-8")
    changed = engine(cfg)
    assert changed.returncode == 4, changed.stdout + changed.stderr
    data = json.loads(_last_run_path(isolated_home).read_text(encoding="utf-8"))
    assert data["last_full"]["exit"] == 4


def _consumer_case(tmp_path):
    target = tmp_path / "Docs"
    target.mkdir()
    queue = tmp_path / "queue"
    queue.mkdir()
    marker = queue / "m1"
    marker.write_text(json.dumps({"path": str(target), "delete": False}), encoding="utf-8")
    state = realtime_consumer.DebounceState()
    state.observe(str(target), str(marker), 0)
    mappings = [{"type": "folder", "source": str(target), "dest_parent": "/my-files/Backups"}]
    return target, queue, marker, state, mappings


def test_consumer_keeps_markers_on_exit_5(tmp_path):
    target, queue, marker, state, mappings = _consumer_case(tmp_path)
    state.mark_cold(str(target), 0)
    logs = []

    def runner(cmd):
        return 5, '[run-result] {"exit": 5, "mode": "subpath", "files_failed": 2}\n'

    launched = realtime_consumer.process_ready(
        state, str(target), mappings, "unused.json", logs.append, runner=runner, now=0)
    assert launched is False
    assert marker.is_file()
    assert str(target) in state.cold
    text = "\n".join(logs)
    assert "partial failure, markers kept" in text
    assert "files_failed=2" in text
    inflight = queue / "inflight"
    if inflight.exists():
        assert list(inflight.iterdir()) == []


def test_consumer_records_unreadable_on_exit_5(tmp_path):
    target, _queue, marker, state, mappings = _consumer_case(tmp_path)
    hidden = str(target) + "/hidden"
    logs = []

    def runner(cmd):
        return 5, (
            "  ❌ [unreadable] " + hidden + "\n"
            '[run-result] {"exit": 5, "mode": "subpath", "folders_unreadable": 1}\n'
        )

    launched = realtime_consumer.process_ready(
        state, str(target), mappings, "unused.json", logs.append, runner=runner, now=0)
    assert launched is False
    assert hidden in state.known_unreadable()
    assert marker.is_file()


def test_consumer_acks_markers_on_exit_0(tmp_path):
    target, _queue, marker, state, mappings = _consumer_case(tmp_path)
    state.mark_cold(str(target), 0)
    state.note_failure(str(target), 0)

    def runner(cmd):
        return 0, "Done.\n[run-result] {\"exit\": 0, \"mode\": \"subpath\"}\n"

    launched = realtime_consumer.process_ready(
        state, str(target), mappings, "unused.json", lambda _m: None,
        runner=runner, now=100)
    assert launched is True
    assert not marker.exists()
    assert str(target) not in state.cold
    assert state.fail_wait == {}


def test_consumer_backoff_grows_and_resets(tmp_path):
    target = tmp_path / "Docs"
    target.mkdir()
    queue = tmp_path / "queue"
    queue.mkdir()
    marker = queue / "m1"
    body = json.dumps({"path": str(target), "delete": False})
    marker.write_text(body, encoding="utf-8")
    mappings = [{"type": "folder", "source": str(target), "dest_parent": "/my-files/Backups"}]
    state = realtime_consumer.DebounceState()
    launches = []

    def runner(cmd):
        launches.append(clock["t"])
        if len(launches) < 3:
            return 5, '[run-result] {"exit": 5, "mode": "subpath", "files_failed": 1}\n'
        return 0, "Done.\n"

    clock = {"t": 0}

    def cycle(when):
        clock["t"] = when
        realtime_consumer.run_once(
            state, [str(queue)], mappings, "unused.json", 0, when, lambda _m: None,
            runner=runner)

    cycle(1000)
    cycle(1030)
    cycle(1060)
    cycle(1100)
    cycle(1180)
    assert launches == [1000, 1060, 1180]
    marker.write_text(body, encoding="utf-8")
    cycle(1181)
    assert launches == [1000, 1060, 1180, 1181]

    capped = realtime_consumer.DebounceState()
    now = 0
    delays = []
    for _ in range(7):
        delays.append(capped.fail_step.get("x", realtime_consumer.FAILURE_BACKOFF_BASE))
        capped.note_failure("x", now)
        now = capped.fail_wait["x"]
    assert delays[:6] == [60, 120, 240, 480, 960, 1800]
    assert delays[6] == 1800


def test_consumer_auth_code_does_not_arm_backoff(tmp_path):
    target, _queue, marker, state, mappings = _consumer_case(tmp_path)

    def runner(cmd):
        return 2, "⚠ [auth-failed] locked\n"

    realtime_consumer.process_ready(
        state, str(target), mappings, "unused.json", lambda _m: None,
        runner=runner, now=0)
    assert marker.is_file()
    assert state.fail_wait == {}


def test_service_unit_prevents_restart_on_5(tmp_path):
    text = schedule_manager.build_service_text(str(tmp_path / "mappings.json"))
    assert "RestartPreventExitStatus=5" in text
    assert "SuccessExitStatus=0 2 4" in text


def test_parse_result_labels_code_5():
    text = 'Done.\n[run-result] {"exit": 5, "mode": "full", "files_failed": 1}\n'
    assert schedule_manager._parse_result(text) == (False, 5)
    label = schedule_manager._result_label(False, 5)
    assert label == "⚠ completed with failures (code 5)"
    systemd = "Main process exited, code=exited, status=5/FAILURE\nDone.\n"
    assert schedule_manager._parse_result(systemd) == (False, 5)


def test_refresh_units_preserves_settings(tmp_path, monkeypatch):
    unit_dir = tmp_path / "user"
    unit_dir.mkdir()
    service = unit_dir / schedule_manager.SERVICE_NAME
    timer = unit_dir / schedule_manager.TIMER_NAME
    monkeypatch.setattr(schedule_manager, "SYSTEMD_USER_DIR", str(unit_dir))
    monkeypatch.setattr(schedule_manager, "SERVICE_PATH", str(service))
    monkeypatch.setattr(schedule_manager, "TIMER_PATH", str(timer))
    service.write_text(
        "[Service]\n"
        "Environment=PROTON_DRIVE_CLI=/opt/prod/proton-drive\n"
        "ExecStart=/usr/bin/python3 /opt/prod/proton_sync.py /data/mappings.json --delete\n"
        "SuccessExitStatus=0 2\n",
        encoding="utf-8",
    )
    timer.write_text(
        "[Timer]\nOnCalendar=*-*-* 04:15:00\n",
        encoding="utf-8",
    )

    def fake_run(args):
        if "is-active" in args:
            return 0, "inactive\n", ""
        return 0, "", ""

    monkeypatch.setattr(schedule_manager, "_run", fake_run)
    ok, _message = schedule_manager.refresh_units()
    assert ok
    rewritten = service.read_text(encoding="utf-8")
    assert "/data/mappings.json" in rewritten
    assert "--delete" in rewritten
    assert "RestartPreventExitStatus=5" in rewritten
    assert "SuccessExitStatus=0 2 4" in rewritten
    assert ("ExecStart=/usr/bin/python3 /opt/prod/proton_sync.py "
            "/data/mappings.json --delete") in rewritten
    assert "Environment=PROTON_DRIVE_CLI=/opt/prod/proton-drive" in rewritten
    assert schedule_manager.DEFAULT_ENGINE not in rewritten
    assert schedule_manager.DEFAULT_CLI not in rewritten
    assert "OnCalendar=*-*-* 04:15:00" in timer.read_text(encoding="utf-8")


def test_refresh_units_does_not_add_cli_path(tmp_path, monkeypatch):
    unit_dir = tmp_path / "user"
    unit_dir.mkdir()
    service = unit_dir / schedule_manager.SERVICE_NAME
    timer = unit_dir / schedule_manager.TIMER_NAME
    monkeypatch.setattr(schedule_manager, "SYSTEMD_USER_DIR", str(unit_dir))
    monkeypatch.setattr(schedule_manager, "SERVICE_PATH", str(service))
    monkeypatch.setattr(schedule_manager, "TIMER_PATH", str(timer))
    service.write_text(
        "[Service]\n"
        "ExecStart=/usr/bin/python3 /opt/prod/proton_sync.py /data/mappings.json\n",
        encoding="utf-8",
    )
    timer.write_text("[Timer]\nOnCalendar=*-*-* 03:00:00\n", encoding="utf-8")
    monkeypatch.setattr(schedule_manager, "_run", lambda args: (0, "inactive\n", ""))
    ok, _message = schedule_manager.refresh_units()
    assert ok
    rewritten = service.read_text(encoding="utf-8")
    assert "PROTON_DRIVE_CLI=" not in rewritten
    assert "ExecStart=/usr/bin/python3 /opt/prod/proton_sync.py /data/mappings.json\n" in rewritten
    assert "--delete" not in rewritten
    assert schedule_manager.DEFAULT_ENGINE not in rewritten


def test_service_missing_restart_prevent_5(tmp_path, monkeypatch):
    service = tmp_path / schedule_manager.SERVICE_NAME
    monkeypatch.setattr(schedule_manager, "SERVICE_PATH", str(service))

    assert schedule_manager.service_missing_restart_prevent_5() is False

    service.write_text(
        "[Service]\n"
        "ExecStart=/usr/bin/python3 /opt/proton_sync.py /data/mappings.json --delete\n",
        encoding="utf-8",
    )
    assert schedule_manager.service_missing_restart_prevent_5() is True

    service.write_text(
        service.read_text(encoding="utf-8") + "RestartPreventExitStatus=5\n",
        encoding="utf-8",
    )
    assert schedule_manager.service_missing_restart_prevent_5() is False


def _editor_lines():
    import pathlib
    source = (pathlib.Path(__file__).resolve().parents[1]
              / "proton_mapping_editor.py").read_text(encoding="utf-8")
    start = source.index("# --- editor output filter (no Tk) ---")
    end = source.index("# --- end editor output filter ---")
    namespace = {"json": json}
    exec(source[start:end], namespace)
    return namespace["visible_editor_lines"]


def test_editor_hides_machine_line_and_clean_summary():
    show = _editor_lines()
    summary = (
        "Summary: uploaded 1, would upload 0, failed 0, vanished 0, "
        "listing failed 0, unreadable 0, permission denied 0, "
        "stall-skipped 0, trashed 0, trash failed 0, deletions refused 0, "
        "sources missing 0, mappings 1/1.\n"
    )
    machine = (
        '[run-result] {"exit": 0, "mode": "full", "files_failed": 0, '
        '"folders_listing_failed": 0, "folders_unreadable": 0, '
        '"folders_permission_denied": 0, "folders_stall_skipped": 0, '
        '"trash_failed": 0, "deletions_refused": 0, "sources_missing": 0}\n'
    )
    french = (
        "Résumé : envoyés 1, enverrait 0, échecs 0, disparus 0, "
        "listage échoué 0, illisibles 0, permission refusée 0, "
        "sautés après blocage 0, à la corbeille 0, corbeille échouée 0, "
        "suppressions refusées 0, sources absentes 0, mappings 1/1.\n"
    )
    lines = ["📂 /data/Docs\n", "Done.\n", summary, machine]
    assert show(lines) == ["📂 /data/Docs\n", "Done.\n"]
    assert show(lines, errors_only=True) == []
    assert show([french, machine], errors_only=True) == []
    assert show([french, machine]) == []
    detailed = show(lines, verbose=True)
    assert summary in detailed
    assert machine not in detailed


def test_editor_shows_failure_summary_in_the_default_view():
    show = _editor_lines()
    summary = (
        "Summary: uploaded 0, would upload 0, failed 2, vanished 0, "
        "listing failed 0, unreadable 0, permission denied 0, "
        "stall-skipped 0, trashed 0, trash failed 0, deletions refused 0, "
        "sources missing 0, mappings 0/1.\n"
    )
    machine = (
        '[run-result] {"exit": 5, "mode": "full", "files_failed": 2, '
        '"folders_listing_failed": 0, "folders_unreadable": 0, '
        '"folders_permission_denied": 0, "folders_stall_skipped": 0, '
        '"trash_failed": 0, "deletions_refused": 0, "sources_missing": 0}\n'
    )
    lines = ["📂 /data/Docs\n", summary, machine]
    assert show(lines) == ["📂 /data/Docs\n", summary]
    assert show(lines, errors_only=True) == [summary]
    detailed = show(lines, verbose=True)
    assert summary in detailed
    assert machine not in detailed
    warning = ("    ⚠  Could not list /my-files/Backups/Docs — "
               "folder skipped this pass\n")
    assert show([warning], errors_only=True) == [warning]


def test_errors_only_wins_over_detailed():
    show = _editor_lines()
    detail = "    [DRY-RUN] would upload: /data/Docs/a.txt\n"
    blank = "\n"
    warning = "    ⚠  Could not list /my-files/Backups — folder skipped\n"
    summary = (
        "Summary: uploaded 1, would upload 0, failed 0, vanished 0, "
        "listing failed 0, unreadable 0, permission denied 0, "
        "stall-skipped 0, trashed 0, trash failed 0, deletions refused 0, "
        "sources missing 0, mappings 1/1.\n"
    )
    machine = (
        '[run-result] {"exit": 0, "files_failed": 0, '
        '"folders_listing_failed": 0, "folders_unreadable": 0, '
        '"folders_permission_denied": 0, "folders_stall_skipped": 0, '
        '"trash_failed": 0, "deletions_refused": 0, "sources_missing": 0}\n'
    )
    lines = ["📂 /data/Docs\n", detail, blank, warning, summary, machine]
    both = show(lines, verbose=True, errors_only=True)
    only = show(lines, verbose=False, errors_only=True)
    detailed = show(lines, verbose=True, errors_only=False)
    assert both == only
    assert warning in both
    assert detail not in both
    assert blank not in both
    assert machine not in both
    assert summary not in both
    assert detail in detailed


def test_detailed_keeps_blank_lines():
    show = _editor_lines()
    lines = [
        "=== Launch ===\n",
        "\n",
        "▶ Mapping 1/1 : /data/Docs  =>  /my-files/Backups\n",
        "📂 /data/Docs\n",
        "\n",
        "Done.\n",
        '[run-result] {"exit": 0, "files_failed": 0, '
        '"folders_listing_failed": 0, "folders_unreadable": 0, '
        '"folders_permission_denied": 0, "folders_stall_skipped": 0, '
        '"trash_failed": 0, "deletions_refused": 0, "sources_missing": 0}\n',
    ]
    shown = show(lines, verbose=True, errors_only=False)
    assert shown == lines[:-1]
    assert shown[1] == "\n"
    assert shown[-2] == "\n"


def test_dry_run_new_file_mapping_is_not_a_failure(
        fake_drive, local_tree, write_mappings, engine):
    src = local_tree({"notes.txt": (b"hello", 1_000_000_000)})
    cfg = write_mappings([{
        "type": "file",
        "source": str(src / "notes.txt"),
        "dest_parent": "/my-files/Backups/New",
    }])
    dry = engine(cfg, "--dry-run")
    assert dry.returncode == 0, dry.stdout + dry.stderr
    payload = _run_result(dry.stdout)
    assert payload["folders_listing_failed"] == 0
    assert payload["files_would_upload"] == 1


def test_refresh_units_keeps_quoted_paths_and_the_launcher(tmp_path, monkeypatch):
    service = _unit_files(tmp_path, monkeypatch, (
        "[Service]\n"
        "Environment=\"PROTON_DRIVE_CLI=/opt/my tools/proton-drive\"\n"
        "ExecStart=/usr/bin/python3 \"/opt/my prod/proton_sync.py\" "
        "\"/data/my mappings.json\" --delete\n"))
    assert schedule_manager.refresh_units()[0]
    rewritten = service.read_text(encoding="utf-8")
    assert ("ExecStart=/usr/bin/python3 \"/opt/my prod/proton_sync.py\" "
            "\"/data/my mappings.json\" --delete\n") in rewritten
    assert "Environment=\"PROTON_DRIVE_CLI=/opt/my tools/proton-drive\"\n" in rewritten

    service.write_text(
        "[Service]\nExecStart=/usr/bin/proton-drive-sync /data/mappings.json\n",
        encoding="utf-8")
    assert schedule_manager.refresh_units()[0]
    rewritten = service.read_text(encoding="utf-8")
    assert "ExecStart=/usr/bin/proton-drive-sync /data/mappings.json\n" in rewritten
    assert "PROTON_DRIVE_CLI=" not in rewritten


def test_install_still_uses_the_running_folder(tmp_path, monkeypatch):
    service = _unit_files(tmp_path, monkeypatch, (
        "[Service]\n"
        "Environment=PROTON_DRIVE_CLI=/opt/prod/proton-drive\n"
        "ExecStart=/usr/bin/python3 /opt/prod/proton_sync.py /data/mappings.json\n"))
    ok, _message = schedule_manager.install_or_update("/data/mappings.json", enable=False)
    assert ok
    rewritten = service.read_text(encoding="utf-8")
    assert "/opt/prod/" not in rewritten
    assert schedule_manager.DEFAULT_ENGINE in rewritten


def test_editor_errors_only_skips_the_healthy_mount_verdict():
    show = _editor_lines()
    ok = "    [delete-guard] OK local : /home/u/Docs\n"
    refused = "    [delete-guard] refusing to trash 9 of 10 remote item(s)\n"
    assert show([ok, refused], verbose=True, errors_only=True) == [refused]


def _unit_files(tmp_path, monkeypatch, service_text):
    unit_dir = tmp_path / "user"
    unit_dir.mkdir()
    service = unit_dir / schedule_manager.SERVICE_NAME
    timer = unit_dir / schedule_manager.TIMER_NAME
    monkeypatch.setattr(schedule_manager, "SYSTEMD_USER_DIR", str(unit_dir))
    monkeypatch.setattr(schedule_manager, "SERVICE_PATH", str(service))
    monkeypatch.setattr(schedule_manager, "TIMER_PATH", str(timer))
    service.write_text(service_text, encoding="utf-8")
    timer.write_text("[Timer]\nOnCalendar=*-*-* 03:00:00\n", encoding="utf-8")
    monkeypatch.setattr(schedule_manager, "_run", lambda args: (0, "inactive\n", ""))
    return service

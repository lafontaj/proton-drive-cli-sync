"""Document de mappings, filtres de sortie et calendrier — sans écran."""

import io
import json
import os

from ui import calendar, document, run
from mapping_keys import carry_unknown_keys


def test_load_list_and_object(tmp_path):
    listed = tmp_path / "list.json"
    listed.write_text(json.dumps([
        {"type": "folder", "source": "/data", "dest_parent": "/my-files/a",
         "future_key": 1},
    ]), encoding="utf-8")
    doc = document.Document()
    doc.load(str(listed))
    assert doc.mappings[0]["future_key"] == 1
    assert doc.global_exclusions == {"names": [], "patterns": []}
    doc.save()
    assert json.loads(listed.read_text(encoding="utf-8"))[0]["future_key"] == 1
    assert not os.path.exists(str(listed) + ".tmp")

    obj = tmp_path / "obj.json"
    obj.write_text(json.dumps({
        "exclusions": {"names": [".tmp"], "patterns": ["*.bak"]},
        "mappings": [{"type": "file", "source": "/f", "dest_parent": "/my-files"}],
        "ignored_top": True,
    }), encoding="utf-8")
    doc.load(str(obj))
    assert doc.global_exclusions["names"] == [".tmp"]
    saved = doc.payload()
    assert saved["exclusions"]["patterns"] == ["*.bak"]
    assert "ignored_top" not in saved


def test_edit_keeps_unknown_keys_and_drops_deletion_when_off():
    old = {
        "type": "folder", "source": "/s", "dest_parent": "/my-files/d",
        "allow_delete": True, "delete_mode": "trash", "source_kind": "local",
        "excluded_remote": "keep", "future_key": {"a": 1},
    }
    new = document.build_mapping(
        old, "folder", "/s", "/my-files/d", "replace", False, "trash", "")
    assert new["future_key"] == {"a": 1}
    # excluded_remote belongs to the dialog: only the box writes it back.
    assert "excluded_remote" not in new
    kept = document.build_mapping(
        old, "folder", "/s", "/my-files/d", "replace", False, "trash", "",
        excluded_remote="keep")
    assert kept["excluded_remote"] == "keep"
    pruned = document.build_mapping(
        kept, "folder", "/s", "/my-files/d", "replace", False, "trash", "",
        excluded_remote="prune")
    assert "excluded_remote" not in pruned
    for key in ("allow_delete", "delete_mode", "source_kind"):
        assert key not in new
    assert carry_unknown_keys(None, {"x": 1}) == {"x": 1}


def test_twoway_direction_is_opt_in_and_unknown_keys_stay():
    old = {
        "type": "folder", "source": "/s", "dest_parent": "/my-files/d",
        "direction": "twoway", "poll_minutes": 5, "future_key": 1,
        "shared_delete_confirmed": True,
    }
    kept = document.build_mapping(
        old, "folder", "/s", "/my-files/d", "replace", False, "trash", "",
        direction="twoway")
    assert kept["direction"] == "twoway"
    assert kept["poll_minutes"] == 5
    assert kept["future_key"] == 1
    assert "shared_delete_confirmed" not in kept
    upload = document.build_mapping(
        old, "folder", "/s", "/my-files/d", "replace", False, "trash", "",
        direction="upload")
    assert "direction" not in upload
    assert upload["poll_minutes"] == 5
    confirmed = document.build_mapping(
        old, "folder", "/s", "/shared-with-me/box", "replace", True, "trash",
        "local", direction="twoway", shared_delete_confirmed=True)
    assert confirmed["shared_delete_confirmed"] is True
    assert document.backup_blurb([]) == "Folder sync to Proton Drive"
    assert document.backup_blurb([upload]) == (
        "Upload only. Two-way can be turned on per mapping.")
    assert document.backup_blurb([kept]) == "Two-way is on for those mappings only"
    # Both dialogs write direction themselves, so it is not carried over.
    carried = {"type": "folder", "source": "/s", "dest_parent": "/my-files"}
    carry_unknown_keys(old, carried)
    assert "direction" not in carried


def test_destination_and_source_rules():
    assert document.destination_ok("/my-files")
    assert document.destination_ok("/my-files/photos")
    assert document.destination_ok("/shared-with-me/team")
    assert not document.destination_ok("/my-filesX")
    assert not document.destination_ok("/photos")
    assert document.edit_error("", "/my-files", False, "") 
    assert document.edit_error("relative", "/my-files", False, "")
    assert document.edit_error("/abs", "/devices", False, "")
    assert document.edit_error("/abs", "/my-files", True, "")
    assert document.edit_error("/abs", "/my-files", True, "local") is None
    assert document.confirm_kind("/shared-with-me/a", True, "trash") == "shared"
    assert document.confirm_kind("/my-files", True, "permanent") == "permanent"
    assert document.confirm_kind("/my-files", False, "trash") is None
    assert document.mapping_remote_path("/my-files", "/data/photos") == "/my-files/photos"


def test_move_refuses_other_account(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    src = tmp_path / "a.json"
    dest = tmp_path / "b.json"
    src.write_text("[]", encoding="utf-8")
    dest.write_text("[]", encoding="utf-8")
    document.atomic_write_json(str(cache / "a.cache"), {"__meta__": {"account": "one"}})
    document.atomic_write_json(str(cache / "b.cache"), {"__meta__": {"account": "two"}})
    mapping = {"type": "folder", "source": "/data", "dest_parent": "/my-files"}
    plan = document.plan_move(
        str(src), str(dest), mapping, {"names": [], "patterns": []}, str(cache))
    assert plan["ok"] is False
    assert plan["reason"] == "other_account"


def test_move_copies_cache_subtree(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    src = tmp_path / "a.json"
    dest = tmp_path / "b.json"
    src.write_text(json.dumps([
        {"type": "folder", "source": "/data", "dest_parent": "/my-files"},
    ]), encoding="utf-8")
    document.atomic_write_json(str(cache / "a.cache"), {
        "__meta__": {"account": "one"},
        "/data": {"subtree_complete": True},
        "/data/child": {"subtree_complete": True},
        "/other": {"subtree_complete": True},
    })
    mapping = {"type": "folder", "source": "/data", "dest_parent": "/my-files"}
    plan = document.plan_move(
        str(src), str(dest), mapping, {"names": [], "patterns": []}, str(cache))
    assert plan["ok"] is True
    document.apply_move(
        str(src), str(dest), mapping, plan["src_account"],
        plan["copy_excl"], plan["src_excl"], str(cache))
    moved = json.loads((cache / "b.cache").read_text(encoding="utf-8"))
    left = json.loads((cache / "a.cache").read_text(encoding="utf-8"))
    assert moved["/data"]["subtree_complete"] is True
    assert "/data" not in left
    assert left["/other"]["subtree_complete"] is True
    body = json.loads(dest.read_text(encoding="utf-8"))
    assert body["mappings"][0]["source"] == "/data"


def test_ready_and_clear_primed(tmp_path):
    path = tmp_path / "m.json"
    cache = tmp_path / "m.cache"
    mapping = {"type": "folder", "source": "/data", "dest_parent": "/my-files"}
    document.atomic_write_json(str(cache), {"/data": {"subtree_complete": True, "sig": {}}})
    assert document.ready_state(mapping, document.read_cache(str(cache)), {}) == "ready"
    document.clear_primed(str(cache), "/data")
    assert document.ready_state(mapping, document.read_cache(str(cache)), {}) == "pending"
    assert document.ready_state({"type": "file", "source": "/f"}, {}, {}) == "na"
    assert not os.path.exists(str(path))


def test_engine_args_progress_and_exit_code():
    args = run.sync_args("/m.json", dry_run=True, delete=True, only_sources=["/a"])
    assert args == ["/m.json", "--dry-run", "--delete", "--only-source", "/a"]
    live = run.live_pass_args("/m.json", "/data/Docs")
    assert live == ["/m.json", "--delete", "--only-source", "/data/Docs"]
    prime = run.prime_args("/m.json", ["/a"])
    assert "--delete" in prime and "--accept-account-change" in prime
    assert prime.count("--only-source") == 1
    reset = run.reset_args("/m.json", ["/a"], wipe=True)
    assert "--reset-source" in reset and "--wipe-remote" in reset
    assert "--delete" not in reset
    assert run.parse_progress("@@PROGRESS state=start files=2 bytes=1048576")
    assert run.parse_progress("@@PROGRESS state=done") == ""
    assert run.parse_progress("hello") is None
    assert run.is_error_line("❌ boom")
    assert not run.is_error_line("📂 /home/Rapport_erreur")
    assert run.visible_text("detail line\n", verbose=False, errors_only=False) is None
    assert run.sync_status(5).startswith("Sync finished with failures")
    assert "code 0" in run.sync_status(0)
    assert "code 5" in run.finished_banner("prime", 5, True, "/log")


class _FakeProc:
    def __init__(self, lines):
        self.stdout = iter(lines)
        self.returncode = None

    def wait(self):
        self.returncode = 0


def test_live_pass_holds_the_summary_until_run_result():
    clean = ('[run-result] {"exit": 0, "files_failed": 0}\n')
    failed = ('[run-result] {"exit": 5, "files_failed": 2}\n')
    summary = "Summary: uploaded 1, failed 0, mappings 1/1.\n"
    for machine, expected in ((clean, ["📂 /data/Docs\n"]),
                              (failed, ["📂 /data/Docs\n", summary])):
        control = run.PassControl()
        shown = []
        control.on_text = shown.append
        log = io.StringIO()
        run._pump(_FakeProc(["📂 /data/Docs\n", summary, machine]), control, log)
        assert shown == expected
        assert machine in log.getvalue()
    control = run.PassControl()
    shown = []
    control.on_text = shown.append
    run._pump(_FakeProc(["📂 /data/Docs\n", "Summary: failed 1\n"]), control, None)
    assert shown == ["📂 /data/Docs\n"]
    assert control.pending is None


def test_calendar_roundtrip():
    assert calendar.build_on_calendar("hourly", 3) == "*-*-* *:00:00"
    assert calendar.build_on_calendar("daily", 3) == "*-*-* 03:00:00"
    assert calendar.build_on_calendar("weekly", 3, "Sun") == "Sun *-*-* 03:00:00"
    assert calendar.parse_on_calendar("Mon *-*-* 15:00:00") == ("weekly", 15, "Mon")
    assert calendar.parse_on_calendar("not a calendar") is None


def test_table_names_the_direction_and_the_real_proton_folder():
    # The folder to open on the website, not its parent.
    up = {"type": "folder", "source": "/home/u/Docs", "dest_parent": "/my-files/Backups"}
    two = dict(up, direction="twoway")
    volume = {"type": "folder", "source": "/home/u/Proton Drive",
              "dest_parent": "/my-files", "direction": "twoway", "volume": True}
    single = {"type": "file", "source": "/home/u/a.txt", "dest_parent": "/my-files/X/"}
    assert document.kind_label(up) == "Upload only"
    assert document.kind_label(two) == "Two-way"
    assert document.kind_label(single) == "File, upload only"
    assert document.proton_location(up) == "/my-files/Backups/Docs"
    assert document.proton_location(two) == "/my-files/Backups/Docs"
    assert document.proton_location(volume) == "/my-files"
    assert document.proton_location(single) == "/my-files/X/a.txt"


def test_two_way_problem_lines_show_without_verbose():
    for line in (
            "[download-failed] could not download /x",
            "[list-skipped] Could not list /my-files/A — folder skipped",
            "    [delete-guard] refusing to trash 9 of 10 remote item(s)",
            "[download] downloaded /home/u/Docs/a.txt",
            "[conflict] both sides changed",
            "[held] /a -> /b",
            "⚠    • 1 file(s) could not be sent or downloaded.",
    ):
        assert run.visible_text(line, verbose=False, errors_only=False), line
    assert run.is_error_line("[list-skipped] Could not list /my-files/A")
    assert run.is_error_line("[delete-guard] refusing to trash")
    # mount_check's healthy verdict (verbose) is not a problem line.
    assert not run.is_error_line("[delete-guard] OK local : /home/u/Docs")
    assert not run.visible_text("    [delete-guard] OK local : /home/u/Docs",
                                verbose=True, errors_only=True)
    assert not run.is_error_line("[download] downloaded /home/u/failed.txt")


def test_code_5_status_names_the_first_reason():
    assert run.parse_run_result('[run-result] {"exit": 5, "files_failed": 2}') == {
        "exit": 5, "files_failed": 2}
    assert run.parse_run_result("[run-result] nope") is None
    assert run.parse_run_result("Summary: …") is None
    text = run.sync_status(5, {"folders_listing_failed": 1})
    assert "could not be read" in text
    assert "code 5" in run.sync_status(5, {})
    assert "code 0" in run.sync_status(0, {"files_failed": 0})


def test_schedule_page_words():
    from ui.pages import schedule

    assert schedule.describe_calendar("*-*-* 03:00:00") == "every day at 03:00"
    assert schedule.describe_calendar("*-*-* *:00:00") == "every hour"
    assert schedule.describe_calendar("Sun *-*-* 15:00:00") == "every Sunday at 15:00"
    assert schedule.describe_calendar("weird") == "weird"
    line = "Thu 2026-10-08 03:00:00 CEST 9h left Wed 2026-10-07 03:00:01 CEST"
    assert schedule.next_run_text(line) == "Thu 2026-10-08 03:00:00 CEST"
    assert schedule.next_run_text("") == ""
    off = schedule.state_lines({})
    assert "not set up" in off[0]
    on = schedule.state_lines({
        "service_exists": True, "timer_exists": True, "timer_active": True,
        "calendar": "*-*-* 03:00:00", "next_run": line, "linger": True,
        "mappings_path": "/m.json"})
    assert on[0] == "Scheduled sync is on: every day at 03:00."
    assert "Thu 2026-10-08" in on[1]

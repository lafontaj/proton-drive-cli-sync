"""Remote changes are polled by the existing consumer, not a new daemon."""

import realtime_consumer
import schedule_manager


def _twoway(source, minutes=None):
    mapping = {
        "type": "folder",
        "source": source,
        "dest_parent": "/my-files/Box",
        "direction": "twoway",
    }
    if minutes is not None:
        mapping["poll_minutes"] = minutes
    return mapping


def test_default_poll_is_five_minutes_and_one_way_is_not_polled():
    assert realtime_consumer.remote_poll_minutes(_twoway("/data/Two")) == 5
    assert realtime_consumer.remote_poll_minutes(
        {"type": "folder", "source": "/data/One", "dest_parent": "/my-files"}) is None
    assert realtime_consumer.remote_poll_minutes(
        _twoway("/data/Two", minutes="soon")) == 5


def test_poll_waits_for_the_interval_then_runs_the_engine():
    mapping = _twoway("/data/Two", minutes=5)
    clocks = {}
    realtime_consumer.note_remote_poll_clocks([mapping], 1000, clocks)
    assert realtime_consumer.due_remote_polls([mapping], 1000 + 4 * 60, clocks) == []
    launched = []

    def runner(cmd):
        launched.append(cmd)
        return 0, "[run-result] {}"

    assert realtime_consumer.poll_remote_mappings(
        [mapping], "/tmp/mappings.json", 1000 + 5 * 60, clocks, lambda _m: None,
        runner=runner) == "done"
    assert launched
    assert "--subpath" in launched[0]
    assert "/data/Two" in launched[0]
    assert realtime_consumer.due_remote_polls(
        [mapping], 1000 + 5 * 60 + 10, clocks) == []


def test_one_way_mapping_in_the_same_list_is_not_launched():
    one = {"type": "folder", "source": "/data/One", "dest_parent": "/my-files"}
    two = _twoway("/data/Two")
    clocks = {}
    realtime_consumer.note_remote_poll_clocks([one, two], 0, clocks)
    launched = []
    realtime_consumer.poll_remote_mappings(
        [one, two], "/tmp/mappings.json", 5 * 60, clocks, lambda _m: None,
        runner=lambda cmd: launched.append(cmd) or (0, ""))
    assert len(launched) == 1
    assert "/data/One" not in launched[0]


def test_nightly_unit_is_still_the_same_engine_pass():
    text = schedule_manager.build_service_text("/tmp/mappings.json")
    assert "proton_sync.py" in text or "proton-drive-sync" in text
    assert "remote-poll" not in text

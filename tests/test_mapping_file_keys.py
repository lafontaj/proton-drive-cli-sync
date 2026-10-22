"""Arrival-folder helpers, and what the editor keeps when it saves.

The editor only manages "exclusions" and "mappings". Keys written by a later
version (for instance "sync_mappings") must survive a save made by this one,
and must not change anything else it does.
"""

import json
import types

import pytest

import mapping_keys as mk


def _m(source, dest):
    return {"type": "folder", "source": source, "dest_parent": dest}


def test_arrival_folder_matches_the_engine():
    assert mk.arrival_folder(_m("/h/Photos/", "/my-files/S/")) == "/my-files/S/Photos"
    assert mk.arrival_folder(_m("/h/notes.txt", "/my-files")) == "/my-files/notes.txt"
    assert mk.arrival_folder(_m("", "/my-files")) is None
    assert mk.arrival_folder({"source": "/h/x"}) is None


def test_conflicts_same_nested_and_segment_boundary():
    maps = [_m("/h/Photos", "/my-files/S"), _m("/n/Photos", "/my-files/S"),
            _m("/h/Documents", "/my-files/S"), _m("/n/Invoices", "/my-files/S/Documents"),
            _m("/h/Doc", "/my-files/S")]
    assert mk.arrival_conflicts(maps) == [
        (0, 1, "same", "/my-files/S/Photos", "/my-files/S/Photos"),
        (2, 3, "nested", "/my-files/S/Documents", "/my-files/S/Documents/Invoices"),
    ]
    # Both mappings of every pair, nested or not.
    assert mk.deletion_holds(maps) == {0: 1, 1: 0, 2: 3, 3: 2}


def test_nested_pair_is_ordered_whatever_the_file_order():
    maps = [_m("/n/Invoices", "/my-files/S/Documents"), _m("/h/Documents", "/my-files/S")]
    assert mk.arrival_conflicts(maps)[0][:3] == (1, 0, "nested")
    assert mk.deletion_holds(maps) == {1: 0, 0: 1}


def test_first_arrival_conflict_kinds():
    others = [_m("/h/Documents", "/my-files/S")]
    assert mk.first_arrival_conflict(_m("/n/Documents", "/my-files/S"), others)[1] == "same"
    assert mk.first_arrival_conflict(_m("/n/Inv", "/my-files/S/Documents"), others)[1] == "inside"
    assert mk.first_arrival_conflict(_m("/n/S", "/my-files"), others)[1] == "contains"
    assert mk.first_arrival_conflict(_m("/n/Other", "/my-files/S"), others) is None


def test_unknown_top_level_keys():
    doc = {"exclusions": {}, "mappings": [], "sync_mappings": [1], "zz": {"a": 1}}
    assert mk.unknown_top_level(doc) == {"sync_mappings": [1], "zz": {"a": 1}}
    assert mk.unknown_top_level([]) == {}


# ── The editor itself (needs tkinter, no display) ────────────────────────────

def _editor_stub():
    ed = pytest.importorskip("proton_mapping_editor")
    warnings = []
    stub = types.SimpleNamespace(
        mappings=[], global_exclusions={"names": [], "patterns": []},
        _extra_top={}, config_path=None,
        status=types.SimpleNamespace(set=lambda *_a: None),
        _set_dirty=lambda *_a: None, _refresh_tree=lambda: None,
        _update_excl_summary=lambda: None, _auto_push_mappings=lambda *_a: None,
        _warn_revisions_unsupported=lambda: None,
        _warn_permanent_delete_unsupported=lambda: None,
        _warn_cross_file_conflicts=lambda: None,
    )
    stub._warn_arrival_conflicts = lambda: ed.MappingEditor._warn_arrival_conflicts(stub)
    return ed, stub, warnings


def test_save_keeps_keys_it_does_not_know(tmp_path, monkeypatch):
    ed, stub, _w = _editor_stub()
    path = tmp_path / "mappings.json"
    later = [{"type": "folder", "source": "/h/Share", "dest_parent": "/my-files",
              "direction": "twoway"}]
    path.write_text(json.dumps({"mappings": [_m("/h/Docs", "/my-files/S")],
                                "sync_mappings": later}), encoding="utf-8")
    ed.MappingEditor._load(stub, str(path))
    stub.mappings.append(_m("/h/More", "/my-files/S"))
    ed.MappingEditor._save(stub, str(path))
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["sync_mappings"] == later
    assert [m["source"] for m in saved["mappings"]] == ["/h/Docs", "/h/More"]
    # The editor itself still shows only its own mappings.
    assert [m["source"] for m in stub.mappings] == ["/h/Docs", "/h/More"]


def test_save_keeps_the_plain_list_format_without_extra_keys(tmp_path):
    ed, stub, _w = _editor_stub()
    path = tmp_path / "mappings.json"
    path.write_text(json.dumps([_m("/h/Docs", "/my-files/S")]), encoding="utf-8")
    ed.MappingEditor._load(stub, str(path))
    ed.MappingEditor._save(stub, str(path))
    assert isinstance(json.loads(path.read_text(encoding="utf-8")), list)


def test_dormant_entries_never_count_as_conflicts(tmp_path, monkeypatch):
    ed, stub, _w = _editor_stub()
    shown = []
    monkeypatch.setattr(ed, "dlg_warning", lambda *a, **k: shown.append(a))
    path = tmp_path / "mappings.json"
    path.write_text(json.dumps({
        "mappings": [_m("/h/Share", "/my-files")],
        "sync_mappings": [{"type": "folder", "source": "/n/Share",
                           "dest_parent": "/my-files", "direction": "twoway"}]}),
        encoding="utf-8")
    ed.MappingEditor._load(stub, str(path))
    assert shown == []


def test_load_warns_once_about_a_shared_arrival_folder(tmp_path, monkeypatch):
    ed, stub, _w = _editor_stub()
    shown = []
    monkeypatch.setattr(ed, "dlg_warning", lambda *a, **k: shown.append(a[1]))
    path = tmp_path / "mappings.json"
    path.write_text(json.dumps([_m("/h/Photos", "/my-files/S"),
                                _m("/n/Photos", "/my-files/S")]), encoding="utf-8")
    ed.MappingEditor._load(stub, str(path))
    assert len(shown) == 1
    assert "/my-files/S/Photos" in shown[0] and "/n/Photos" in shown[0]


def test_upload_sync_holds_and_read_sync_mappings(tmp_path):
    up = [{"type": "folder", "source": "/a/Docs", "dest_parent": "/my-files/B"},
          {"type": "folder", "source": "/a/Other", "dest_parent": "/my-files/C"}]
    tw = [{"type": "folder", "source": "/a/Docs", "dest_parent": "/my-files/B",
           "direction": "twoway"}]
    holds = mk.upload_sync_holds(up, tw)
    assert list(holds) == [0]
    assert holds[0][0] is tw[0]
    assert mk.upload_sync_holds(up, []) == {}
    f = tmp_path / "m.json"
    f.write_text(json.dumps({"mappings": up, "sync_mappings": tw + ["junk"]}))
    assert mk.read_sync_mappings(str(f)) == tw
    assert mk.read_sync_mappings(str(tmp_path / "missing.json")) == []

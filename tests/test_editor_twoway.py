"""Sync mode in the Tk editor: round trip, mode switch, warnings, refusals.

The load/save/warning tests use a stub (no display). The dialog tests need Tk
and a display (Xvfb is enough) and are skipped without one.
"""

import json
import tkinter as tk
import types

import pytest

import mapping_keys as mk


def _m(source, dest, **extra):
    m = {"type": "folder", "source": source, "dest_parent": dest}
    m.update(extra)
    return m


def _tw(source, dest, **extra):
    return _m(source, dest, direction="twoway", **extra)


def _stub(ed):
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
    stub._warn_twoway_conflicts = lambda: ed.MappingEditor._warn_twoway_conflicts(stub)
    return stub


@pytest.fixture
def ed():
    return pytest.importorskip("proton_mapping_editor")


@pytest.fixture
def shown(ed, monkeypatch):
    seen = []
    monkeypatch.setattr(ed, "dlg_warning", lambda *a, **k: seen.append(a[1]))
    return seen


def _load(ed, stub, path, doc):
    path.write_text(json.dumps(doc), encoding="utf-8")
    ed.MappingEditor._load(stub, str(path))


def _save(ed, stub, path):
    ed.MappingEditor._save(stub, str(path))
    return json.loads(path.read_text(encoding="utf-8"))


# ── Round trip ───────────────────────────────────────────────────────────────

_GLOBAL = {"names": ["tmp"], "patterns": []}
_DOCS = {
    "plain list": [_m("/h/Docs", "/my-files/S")],
    "two-way entry with unknown keys": {
        "exclusions": _GLOBAL, "mappings": [_m("/h/Docs", "/my-files/S")],
        "sync_mappings": [_tw("/h/Share", "/my-files", conflict_mode="keep_both",
                              future_key={"a": [1, 2]})]},
    "unknown direction, malformed entries": {
        "exclusions": _GLOBAL, "mappings": [_m("/h/Docs", "/my-files/S")],
        "sync_mappings": [_m("/h/Dl", "/my-files", direction="download"), 7,
                          _tw("/h/Share", "/my-files"), {"direction": "twoway"},
                          _tw("/h/Other", "/my-files/O", type="album")]},
    "sync_mappings is a string": {
        "exclusions": _GLOBAL, "mappings": [], "sync_mappings": "later"},
    "sync_mappings is an object": {
        "exclusions": _GLOBAL, "mappings": [], "sync_mappings": {"a": 1}},
    "sync_mappings is null": {
        "exclusions": _GLOBAL, "mappings": [], "sync_mappings": None},
    "sync_mappings is empty": {
        "exclusions": _GLOBAL, "mappings": [], "sync_mappings": []},
    "only two-way entries": {
        "exclusions": _GLOBAL, "mappings": [], "sync_mappings": [_tw("/h/S", "/my-files")]},
}


@pytest.mark.parametrize("name", list(_DOCS))
def test_save_without_an_edit_leaves_the_file_as_it_was(ed, shown, tmp_path, name):
    stub = _stub(ed)
    path = tmp_path / "mappings.json"
    _load(ed, stub, path, _DOCS[name])
    assert _save(ed, stub, path) == _DOCS[name]


def test_plain_list_stays_a_plain_list(ed, shown, tmp_path):
    stub = _stub(ed)
    path = tmp_path / "mappings.json"
    _load(ed, stub, path, _DOCS["plain list"])
    assert isinstance(_save(ed, stub, path), list)


def test_two_way_entries_are_shown_after_the_upload_ones(ed, shown, tmp_path):
    stub = _stub(ed)
    _load(ed, stub, tmp_path / "m.json", _DOCS["unknown direction, malformed entries"])
    assert [(m["source"], mk.sync_mode(m)) for m in stub.mappings] == [
        ("/h/Docs", "upload"), ("/h/Share", "twoway")]


# ── Mode switch ──────────────────────────────────────────────────────────────

def test_switching_the_mode_moves_the_entry_once_each_way(ed, shown, tmp_path):
    stub = _stub(ed)
    path = tmp_path / "m.json"
    _load(ed, stub, path, {"exclusions": _GLOBAL, "mappings": [
        _m("/h/Docs", "/my-files/S", allow_delete=True, future="x"),
        _m("/h/Pics", "/my-files/P")]})
    mk.set_sync_mode(stub.mappings[0], "twoway")
    saved = _save(ed, stub, path)
    assert [m["source"] for m in saved["mappings"]] == ["/h/Pics"]
    assert [m["source"] for m in saved["sync_mappings"]] == ["/h/Docs"]
    entry = saved["sync_mappings"][0]
    assert entry["direction"] == "twoway" and entry["future"] == "x"
    # A two-way entry never appears under "mappings".
    assert all("direction" not in m for m in saved["mappings"])
    # And back: the stale direction goes, the entry returns to "mappings".
    ed.MappingEditor._load(stub, str(path))
    mk.set_sync_mode(stub.mappings[-1], "upload")
    saved = _save(ed, stub, path)
    assert sorted(m["source"] for m in saved["mappings"]) == ["/h/Docs", "/h/Pics"]
    assert "sync_mappings" not in saved or saved["sync_mappings"] == []
    assert all("direction" not in m for m in saved["mappings"])


def test_a_new_two_way_mapping_is_written_under_sync_mappings(ed, shown, tmp_path):
    stub = _stub(ed)
    path = tmp_path / "m.json"
    _load(ed, stub, path, [_m("/h/Docs", "/my-files/S")])
    stub.mappings.append(_tw("/h/New", "/my-files"))
    saved = _save(ed, stub, path)
    assert isinstance(saved, dict)
    assert [m["source"] for m in saved["mappings"]] == ["/h/Docs"]
    assert [m["source"] for m in saved["sync_mappings"]] == ["/h/New"]


# ── Warnings for the six relations ───────────────────────────────────────────

_RELATIONS = [
    ("local-same", _tw("/h/Docs", "/d1"), _m("/h/Docs", "/d2")),
    ("local-parent", _tw("/h", "/d1"), _m("/h/Docs", "/d2")),
    ("local-child", _tw("/h/Docs/sub", "/d1"), _m("/h/Docs", "/d2")),
    ("arrival-same", _tw("/a/Docs", "/Proton"), _m("/b/Docs", "/Proton")),
    ("arrival-inside", _tw("/a/sub", "/Proton/Docs"), _m("/b/Docs", "/Proton")),
    ("arrival-contains", _tw("/a/Docs", "/Proton"), _m("/b/sub", "/Proton/Docs")),
]


@pytest.mark.parametrize("reason,two,other", _RELATIONS, ids=[r[0] for r in _RELATIONS])
def test_each_relation_warns_on_load_and_on_save(ed, shown, tmp_path, reason, two, other):
    assert mk.sync_mapping_conflicts([two], [other])[0][0] == reason
    stub = _stub(ed)
    path = tmp_path / "m.json"
    _load(ed, stub, path, {"mappings": [other], "sync_mappings": [two]})
    assert len(shown) == 1
    for text in (shown[0],):
        assert text.startswith("⚠")
        assert two["source"] in text and other["source"] in text
        assert "will not sync until this is fixed" in text
    shown.clear()
    saved = _save(ed, stub, path)
    assert len(shown) == 1 and shown[0].startswith("⚠")
    # The mapping stays in the file.
    assert [m["source"] for m in saved["sync_mappings"]] == [two["source"]]


def test_two_way_pair_is_named_once(ed, shown, tmp_path):
    stub = _stub(ed)
    _load(ed, stub, tmp_path / "m.json", {
        "mappings": [], "sync_mappings": [_tw("/h/Docs", "/d1"), _tw("/h/Docs", "/d2")]})
    assert len(shown) == 1
    assert shown[0].count("⚠") == 1


def test_no_warning_without_a_conflict(ed, shown, tmp_path):
    stub = _stub(ed)
    _load(ed, stub, tmp_path / "m.json", {
        "mappings": [_m("/h/Docs", "/my-files/S")],
        "sync_mappings": [_tw("/h/Share", "/my-files")]})
    assert shown == []


def test_upload_checks_ignore_two_way_mappings(ed, shown, tmp_path):
    # Two upload mappings on one arrival folder still warn (existing check);
    # a two-way one on the same folder is the two-way warning's business.
    stub = _stub(ed)
    stub._warn_twoway_conflicts = lambda: None
    _load(ed, stub, tmp_path / "m.json", {
        "mappings": [_m("/h/Photos", "/my-files/S")],
        "sync_mappings": [_tw("/n/Photos", "/my-files/S")]})
    assert shown == []


# ── Cross-file warning, a two-way mapping on either side ─────────────────────

def _write(path, doc):
    path.write_text(json.dumps(doc), encoding="utf-8")
    return str(path)


@pytest.mark.parametrize("here_two,there_two", [(True, False), (False, True), (True, True)],
                         ids=["here", "there", "both"])
def test_cross_file_warning_sees_two_way_on_either_side(
        ed, shown, tmp_path, monkeypatch, here_two, there_two):
    mine = (_tw if here_two else _m)("/h/Docs", "/my-files/S")
    theirs = (_tw if there_two else _m)("/h/Docs", "/my-files/O")
    other = _write(tmp_path / "other.json", {
        "mappings": [] if there_two else [theirs],
        "sync_mappings": [theirs] if there_two else []})
    here = tmp_path / "here.json"
    stub = _stub(ed)
    stub.config_path = str(here)
    stub.mappings = [mine]
    monkeypatch.setattr(ed, "running_mappings_files", lambda: [str(here), other])
    monkeypatch.setattr(ed, "file_in_service", lambda *_a: True)
    ed.MappingEditor._warn_cross_file_conflicts(stub)
    assert len(shown) >= 1
    assert shown[0].startswith("⚠") and "/h/Docs" in shown[0]
    if here_two:
        assert "will not sync until this is fixed" in shown[0]
    else:
        assert "Deletions" in shown[0]


# ── Dialog and refusals (needs a display) ────────────────────────────────────

@pytest.fixture
def host(ed, monkeypatch, shown):
    try:
        obj = ed.MappingEditor.__new__(ed.MappingEditor)
        tk.Tk.__init__(obj)
    except tk.TclError:
        pytest.skip("no display")
    obj.withdraw()
    obj.mappings = []
    obj.config_path = None
    obj.global_exclusions = {"names": [], "patterns": []}
    monkeypatch.setattr(ed, "running_mappings_files", lambda: [])
    monkeypatch.setattr(ed, "file_in_service", lambda *_a: False)
    yield obj
    try:
        obj.destroy()
    except tk.TclError:
        pass


def _walk(widget):
    for child in widget.winfo_children():
        yield child
        yield from _walk(child)


def _text(w):
    try:
        return str(w.cget("text"))
    except tk.TclError:
        return None


def _find(dlg, text):
    for w in _walk(dlg):
        if _text(w) == text:
            return w
    raise AssertionError(f"no widget {text!r}")


def _drive(ed, monkeypatch, steps):
    """Replace wait_window: run steps(dlg) on the open dialog, then close it."""
    def fake(self, dlg):
        steps(dlg)
        if dlg.winfo_exists():
            dlg.destroy()
    monkeypatch.setattr(ed.MappingEditor, "wait_window", fake)


def _entries(dlg):
    return [w for w in _walk(dlg) if w.winfo_class() == "TEntry"]


def test_dialog_offers_the_mode_and_saves_two_way(ed, host, monkeypatch, shown):
    def steps(dlg):
        e = _entries(dlg)
        e[0].delete(0, "end"); e[0].insert(0, "/h/Share")
        _find(dlg, "Two-way").invoke()
        _find(dlg, "OK").invoke()
    _drive(ed, monkeypatch, steps)
    new = host._mapping_dialog("folder")
    assert new is not None and mk.sync_mode(new) == "twoway"
    assert new["direction"] == "twoway" and new["type"] == "folder"


_TRASH = "Proton trash (recoverable)"
_KEEP = ("Keep already-uploaded files and folders on Proton once they are excluded")
_KEPT_ACTIVE = ("NFS (network/NAS)", "Local (internal disk)")


def test_dialog_greys_the_upload_only_controls_for_two_way(ed, host, monkeypatch, shown):
    seen = {}

    def state(dlg, text):
        return str(_find(dlg, text).cget("state"))

    def steps(dlg):
        allow = _find(dlg, "Allow this mapping to delete on Proton what was "
                           "deleted locally")
        if "selected" not in allow.state():
            allow.invoke()
        dlg.update()
        seen["upload"] = {t: state(dlg, t) for t in (_TRASH, _KEEP, *_KEPT_ACTIVE)}
        _find(dlg, "Two-way").invoke()
        dlg.update()
        seen["twoway"] = {t: state(dlg, t) for t in (_TRASH, _KEEP, *_KEPT_ACTIVE)}
        _find(dlg, "Upload").invoke()
        dlg.update()
        seen["back"] = {t: state(dlg, t) for t in (_TRASH, _KEEP, *_KEPT_ACTIVE)}
    _drive(ed, monkeypatch, steps)
    host._mapping_dialog("folder")
    assert seen["upload"][_TRASH] == "normal" and seen["upload"][_KEEP] == "normal"
    assert seen["twoway"][_TRASH] == "disabled" and seen["twoway"][_KEEP] == "disabled"
    # allow_delete and the source kind stay available.
    for t in _KEPT_ACTIVE:
        assert seen["twoway"][t] == "normal"
    assert seen["back"] == seen["upload"]


def test_a_file_mapping_cannot_be_set_to_two_way(ed, host, monkeypatch, shown):
    seen = {}

    def steps(dlg):
        radio = _find(dlg, "Two-way")
        seen["state"] = str(radio.cget("state"))
    _drive(ed, monkeypatch, steps)
    host._mapping_dialog("file")
    assert seen["state"] == "disabled"


def test_on_ok_refuses_two_way_for_a_file_mapping(ed, host, monkeypatch, shown):
    # Defence in depth: a file mapping whose variable says two-way is refused.
    def steps(dlg):
        e = _entries(dlg)
        e[0].delete(0, "end"); e[0].insert(0, "/h/notes.txt")
        for w in _walk(dlg):
            if w.winfo_class() == "TRadiobutton" and str(w.cget("text")) == "Two-way":
                dlg.setvar(str(w.cget("variable")), "twoway")
        _find(dlg, "OK").invoke()
    _drive(ed, monkeypatch, steps)
    assert host._mapping_dialog("file") is None
    assert any(t.startswith("⚠") for t in shown)


def test_editing_keeps_unknown_keys_and_drops_a_stale_direction(ed, host, monkeypatch, shown):
    old = _tw("/h/Share", "/my-files", future={"a": 1})

    def steps(dlg):
        _find(dlg, "Upload").invoke()
        _find(dlg, "OK").invoke()
    _drive(ed, monkeypatch, steps)
    new = host._mapping_dialog("folder", old)
    assert new is not None
    assert mk.sync_mode(new) == "upload" and "direction" not in new
    assert new["future"] == {"a": 1}


# ── Refusals ─────────────────────────────────────────────────────────────────

def _selection(host, *indexes):
    host.tree = types.SimpleNamespace(selection=lambda: tuple(str(i) for i in indexes))


def test_wipe_is_refused_for_two_way(ed, host, monkeypatch, shown, tmp_path):
    host.config_path = str(tmp_path / "m.json")
    host.dirty = False
    host.sync_process = None
    host.mappings = [_m("/h/Docs", "/my-files/S"), _tw("/h/Share", "/my-files")]
    _selection(host, 1)
    monkeypatch.setattr(ed, "DEFAULT_ENGINE", __file__)
    started = []
    monkeypatch.setattr(ed.threading, "Thread", lambda *a, **k: started.append(k))
    asked = []
    monkeypatch.setattr(ed, "dlg_confirm_checkbox", lambda *a, **k: asked.append(a) or (True, True))
    ed.MappingEditor.on_reset_mapping(host)
    assert started == [] and asked == []
    assert len(shown) == 1 and shown[0].startswith("⚠")


def test_move_to_another_file_is_refused_for_two_way(ed, host, monkeypatch, shown, tmp_path):
    host.config_path = str(tmp_path / "m.json")
    host.dirty = False
    host.mappings = [_tw("/h/Share", "/my-files")]
    host._selected_index = lambda: 0
    host._pass_running_warn = lambda: False
    picked = []
    monkeypatch.setattr(ed, "pick_move_target", lambda *a, **k: picked.append(a) or None)
    ed.MappingEditor.on_move_mapping(host)
    assert picked == []
    assert len(shown) == 1 and shown[0].startswith("⚠")


def test_priming_a_selected_two_way_mapping_is_refused(ed, host, monkeypatch, shown, tmp_path):
    host.config_path = str(tmp_path / "m.json")
    host.dirty = False
    host.sync_process = None
    host.mappings = [_tw("/h/Share", "/my-files")]
    _selection(host, 0)
    monkeypatch.setattr(ed, "DEFAULT_ENGINE", __file__)
    ed.MappingEditor.on_prime_cache(host)
    assert len(shown) == 1 and shown[0].startswith("⚠")


# ── Engine end to end, on the fake Drive ─────────────────────────────────────

def test_a_mapping_saved_as_two_way_is_paused_then_syncs_with_the_full_check(
        ed, shown, fake_drive, local_tree, engine, tmp_path):
    src = local_tree({"Share/a.txt": (b"AAAA", 1_000_000_000)})
    stub = _stub(ed)
    cfg = tmp_path / "editor-mappings.json"
    _load(ed, stub, cfg, [])
    new = mk.set_sync_mode(_m(str(src / "Share"), "/my-files"), "twoway")
    stub.mappings.append(new)
    saved = _save(ed, stub, cfg)
    assert saved["mappings"] == [] and saved["sync_mappings"][0]["direction"] == "twoway"
    # No events probe: paused, nothing is sent.
    paused = engine(cfg, full_check=False)
    assert paused.returncode == 0, paused.stdout + paused.stderr
    assert "Two-way sync paused for" in paused.stdout
    assert fake_drive.uploads() == []
    # --twoway-full-check runs it.
    ran = engine(cfg)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert fake_drive.content("/my-files/Share/a.txt") == b"AAAA"

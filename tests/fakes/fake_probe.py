#!/usr/bin/env python3
"""Fake `proton-sync-events`: the same command line and output as the probe in
experimental/events-probe, driven by the fake Drive of FAKE_PROTON_STATE.

    fake_probe.py events changes [-k] [-e] [-j] <folder>

It accepts exactly the options the real probe accepts (-k/--keep, -e/--events,
-j/--json) and rejects every other one. Its state is
$PROTON_SYNC_EVENTS_DIR/changes-state.json: what the watched folder held at the
last bookmark. A change is the difference between that snapshot and the fake
Drive now, found by node uid. The first run on a folder, or on another folder,
only sets the starting point (and saves it even with --keep, like the real one).

Test controls, all in the environment:
  FAKE_PROBE_LOG      file; one JSON line per run with the arguments and data dir
  FAKE_PROBE_FAULT    "<fault>[:<run number>|:ack]" with fault one of fail, nosession,
                      hang, badjson, escape, killparent; ":ack" applies to the
                      runs without --keep only, without a suffix to every run
  FAKE_PROBE_NOTE     text added to the notes of every run (to model a probe note)
  FAKE_PROBE_SCRIPT   JSON file {"<run number>" or "ack": [{"op": "write", "path": "/my-files/x",
                      "text": "..."}, {"op": "trash", "path": ...},
                      {"op": "move", "from": ..., "to": ...}]} applied to the
                      fake Drive just before that run, to model a change that
                      arrives between two runs
"""

__version__ = "1.0.0"

import base64
import hashlib
import json
import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import remote_state  # noqa: E402

KNOWN_OPTIONS = {"-k": "keep", "--keep": "keep", "-e": "events",
                 "--events": "events", "-j": "json", "--json": "json"}

HELP = """Usage: proton-sync-events events changes [options] <path>

Reports what changed inside one Drive folder since the previous run.

Options:
  -j, --json    Print one JSON object.
  -k, --keep    Do not advance the bookmark.
  -e, --events  Also list every event read.
"""


def _unknown_option_text(token):
    return ("Unknown option '%s'. To specify a positional argument starting "
            "with a '-', place it at the end of the command after '--'"
            % token.lstrip("-")[:1])


def _fail(message, code=1):
    print(message, file=sys.stderr)
    sys.exit(code)


def _ack_run(argv):
    """True for a run that moves the bookmark (no --keep)."""
    return not any(t in ("-k", "--keep") for t in argv)


def _count_run(data_dir):
    path = os.path.join(data_dir, "run-count")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            number = int(handle.read().strip() or 0) + 1
    except (OSError, ValueError):
        number = 1
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(str(number))
    return number


def _fault_for(run_number, ack):
    raw = os.environ.get("FAKE_PROBE_FAULT", "")
    if not raw:
        return None
    name, _sep, number = raw.partition(":")
    if number == "ack":
        return name if ack else None
    if number and int(number) != run_number:
        return None
    return name


def _apply_script(run_number, ack):
    path = os.environ.get("FAKE_PROBE_SCRIPT")
    if not path or not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as handle:
        script = json.load(handle)
    steps = (script.get(str(run_number)) or []) + (
        script.get("ack") or [] if ack else [])
    if not steps:
        return
    drive = os.environ["FAKE_PROTON_STATE"]
    state = remote_state.load(drive)
    for step in steps:
        if step["op"] == "write":
            remote_state.store_file(
                state, step["path"], step.get("text", "").encode("utf-8"),
                step.get("mtime", time.time()))
        elif step["op"] == "trash":
            remote_state.trash_tree(state, step["path"])
        elif step["op"] == "move":
            remote_state.relocate(state, step["from"], step["to"])
    remote_state.assign_uids(state)
    remote_state.save(drive, state)


def _snapshot(state, folder):
    """{relative path: entry} of the live nodes below ``folder``."""
    prefix = folder.rstrip("/") + "/"
    found = {}
    for path, node in state["nodes"].items():
        if not path.startswith(prefix) or node.get("trashed"):
            continue
        rel = path[len(prefix):]
        entry = {"uid": node.get("uid"), "type": node.get("type"),
                 "revisions": node.get("revisions") or 1}
        if node.get("type") == "file":
            data = base64.b64decode(node.get("content_b64") or "")
            entry["sha1"] = hashlib.sha1(data).hexdigest()
            entry["size"] = len(data)
            entry["mtime"] = node.get("mtime")
        found[rel] = entry
    return found


def _change(action, rel, entry, **extra):
    out = {"action": action, "type": entry["type"], "path": rel}
    if entry["type"] == "file":
        out["size"] = entry["size"]
        if entry.get("mtime") is not None:
            out["modificationTime"] = remote_state.claimed_modification_time(
                entry["mtime"])
        out["sha1"] = entry["sha1"]
    out.update(extra)
    return out


def _diff(old, cur, state, folder):
    """The changes between two snapshots, one per top item like the real probe:
    a moved or removed folder is reported once, not once per item inside."""
    old_by_uid = {e["uid"]: rel for rel, e in old.items() if e.get("uid")}
    matched = set()
    changes = []
    moved_dirs = []
    for rel in sorted(cur):
        entry = cur[rel]
        before = None
        if entry.get("uid") and entry["uid"] in old_by_uid:
            before = old_by_uid[entry["uid"]]
        elif rel in old and (not old[rel].get("uid") or not entry.get("uid")):
            before = rel
        if before is None:
            changes.append(_change("created", rel, entry))
            continue
        matched.add(before)
        was = old[before]
        if before != rel:
            if entry["type"] == "folder":
                moved_dirs.append((before, rel))
            elif any(before.startswith(o + "/") and
                     rel == n + before[len(o):] for o, n in moved_dirs):
                continue
            extra = {"from": before}
            if entry["type"] == "file" and was.get("sha1") != entry["sha1"]:
                extra["reason"] = "content changed too"
            changes.append(_change("moved", rel, entry, **extra))
        elif entry["type"] == "file" and was.get("sha1") != entry["sha1"]:
            changes.append(_change("modified", rel, entry))
        elif entry["type"] == "file" and was.get("revisions") != entry["revisions"]:
            changes.append(_change("revised", rel, entry,
                                   reason="new version, identical content"))
    # A folder moved after its items were seen inside it: drop those items.
    changes = [c for c in changes if not (
        c["action"] == "moved" and c["type"] == "file" and any(
            c["from"].startswith(o + "/") and c["path"] == n + c["from"][len(o):]
            for o, n in moved_dirs))]
    gone = [rel for rel in sorted(old) if rel not in matched]
    removed_dirs = []
    for rel in gone:
        if any(rel.startswith(d + "/") for d in removed_dirs):
            continue
        entry = old[rel]
        node = state["nodes"].get(folder.rstrip("/") + "/" + rel)
        reason = "trashed" if node and node.get("trashed") else "deleted"
        if entry["type"] == "folder":
            removed_dirs.append(rel)
            inner = sum(1 for r in old if r.startswith(rel + "/"))
            if inner:
                reason += ", with the %d item(s) it contained" % inner
        changes.append(_change("removed", rel, entry, reason=reason))
    return changes


def main(argv):
    if argv[:2] != ["events", "changes"]:
        _fail("Unknown command: %s" % " ".join(argv[:2]))
    rest = argv[2:]
    flags = {}
    paths = []
    after_dashes = False
    for token in rest:
        if after_dashes or not token.startswith("-") or token == "-":
            paths.append(token)
        elif token == "--":
            after_dashes = True
        elif token in ("-h", "--help"):
            print(HELP)
            return 0
        elif token in KNOWN_OPTIONS:
            flags[KNOWN_OPTIONS[token]] = True
        else:
            _fail(_unknown_option_text(token))
    if len(paths) != 1:
        _fail("Usage: proton-sync-events events changes [options] <path>")
    folder = remote_state.normalize(paths[0])
    data_dir = os.environ.get("PROTON_SYNC_EVENTS_DIR")
    if not data_dir:
        _fail("fake: PROTON_SYNC_EVENTS_DIR is unset")
    os.makedirs(data_dir, exist_ok=True)
    run_number = _count_run(data_dir)
    log = os.environ.get("FAKE_PROBE_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"args": argv, "data_dir": data_dir,
                                     "run": run_number}) + "\n")
    ack = _ack_run(argv)
    fault = _fault_for(run_number, ack)
    if fault == "fail":
        _fail("Error: the Proton service did not answer")
    if fault == "nosession":
        _fail("Error: not signed in. Run 'auth login' first.")
    if fault == "hang":
        time.sleep(600)
    if fault == "killparent":
        os.kill(os.getppid(), signal.SIGKILL)
        time.sleep(5)
        return 1
    _apply_script(run_number, ack)

    drive = os.environ.get("FAKE_PROTON_STATE")
    state = remote_state.load(drive)
    remote_state.assign_uids(state)
    node = state["nodes"].get(folder)
    state_path = os.path.join(data_dir, "changes-state.json")
    if not isinstance(node, dict) or node.get("type") != "folder":
        _fail("Error: %s was not found" % folder)
    notes = []
    changes = []
    initialized = False
    saved = None
    try:
        with open(state_path, "r", encoding="utf-8") as handle:
            saved = json.load(handle)
    except (OSError, ValueError):
        saved = None
    if node.get("trashed"):
        if saved is not None and saved.get("folderPath") == folder:
            os.remove(state_path)
            notes.append("The watched folder %s was trashed or deleted. State "
                         "reset; next run starts over." % folder)
            return _print(flags, folder, False, 0, [], notes, False, fault)
        _fail("Error: %s is in the trash" % folder)
    cur = _snapshot(state, folder)
    events_read = 0
    advanced = False
    if saved is None or saved.get("folderPath") != folder:
        if saved is not None:
            notes.append("The watched folder changed (was %s)." % saved["folderPath"])
        initialized = True
        notes.append("Starting point set: %d item(s) known in %s."
                     % (len(cur), folder))
        _save(state_path, folder, cur)
        advanced = True
    else:
        changes = _diff(saved["entries"], cur, state, folder)
        events_read = len(changes)
        if not flags.get("keep"):
            _save(state_path, folder, cur)
            advanced = True
    return _print(flags, folder, initialized, events_read, changes, notes,
                  advanced, fault)


def _save(path, folder, entries):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump({"folderPath": folder, "folderUid": "fake-uid:" + folder,
                   "entries": entries}, handle)
    os.replace(tmp, path)


def _print(flags, folder, initialized, events_read, changes, notes, advanced,
           fault):
    if fault == "badjson":
        print("this is not json {")
        return 0
    if fault == "escape" and changes:
        changes[0]["path"] = "../outside.txt"
    elif fault == "escape":
        changes = [{"action": "created", "type": "file",
                    "path": "../outside.txt", "size": 1, "sha1": "0" * 40}]
    if os.environ.get("FAKE_PROBE_NOTE"):
        notes = list(notes) + [os.environ["FAKE_PROBE_NOTE"]]
    out = {"folder": folder, "initialized": initialized,
           "eventsRead": events_read, "changes": changes, "notes": notes,
           "bookmarkAdvanced": advanced}
    if flags.get("events"):
        out["events"] = []
    if flags.get("json"):
        print(json.dumps(out))
    else:
        for change in changes:
            print("%s %s" % (change["action"], change["path"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

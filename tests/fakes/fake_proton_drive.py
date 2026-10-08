#!/usr/bin/env python3
"""Fake `proton-drive` binary. Reads and writes the JSON state in FAKE_PROTON_STATE.

The engine launches this file as a subprocess (`PROTON_DRIVE_CLI`). It is not
imported by the engine. See `docs/dev/TESTING.md`.
"""

import json
import os
import sys
import time

import remote_state


def _locked_update(path, mutate):
    """Apply `mutate(state)` while holding an exclusive lock, then save."""
    import fcntl
    lock_path = path + ".lock"
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = remote_state.load(path)
            result = mutate(state)
            remote_state.save(path, state)
            return result
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _fail(message, code=1):
    print(message, file=sys.stderr)
    return code


def _split_options(args):
    """Return (options, positionals, unknown) like the real CLI.

    Everything after `--` is positional. Before it, a token that starts with
    `-` is an option; the real CLI rejects one it does not know with
    "Unknown option", which is what a file named `-1_-7.xwmc` used to hit.
    """
    options, positionals = [], []
    i = 0
    while i < len(args):
        token = args[i]
        if token == "--":
            positionals.extend(args[i + 1:])
            return options, positionals, None
        if token in ("-f", "-d") and i + 1 < len(args):
            options.extend(args[i:i + 2])
            i += 2
            continue
        if token in ("--skip-thumbnails", "-t", "-j"):
            options.append(token)
            i += 1
            continue
        if token.startswith("-") and token != "-":
            return options, positionals, token
        positionals.append(token)
        i += 1
    return options, positionals, None


def _unknown_option(token):
    return _fail("Unknown option '%s'. To specify a positional argument starting "
                 "with a '-', place it at the end of the command after '--'"
                 % token.lstrip("-")[:1], 1)


def _parse_upload(args):
    """Return (names, remote_parent, unknown) from `upload` arguments after the verb."""
    _options, names, unknown = _split_options(args)
    if unknown:
        return None, None, unknown
    if len(names) < 2:
        return None, None, None
    return names[:-1], names[-1], None


def _take_wide_upload_faults(state, remote_parent):
    """Faults that apply to the whole upload, not one file name.

    A match that is a filename stays for the per-file pass. No match, or a
    match equal to the remote parent, is command-wide.
    """
    fired = []
    kept = []
    for fault in state.get("faults", []):
        match = fault.get("match")
        wide = fault.get("cmd") == "upload" and (not match or match == remote_parent)
        if not wide:
            kept.append(fault)
            continue
        times = fault.get("times")
        if times is not None and times <= 0:
            kept.append(fault)
            continue
        fired.append(fault)
        if times is None:
            kept.append(fault)
        else:
            updated = dict(fault)
            updated["times"] = times - 1
            if updated["times"] > 0:
                kept.append(updated)
    state["faults"] = kept
    return fired


def _hang(fault):
    """Sleep outside the state lock. See main()."""
    try:
        seconds = int(fault.get("seconds") or 3600)
    except (TypeError, ValueError):
        seconds = 3600
    return ("hang", seconds)


def _upload(state, args):
    names, remote_parent, unknown = _parse_upload(args)
    if unknown:
        return _unknown_option(unknown)
    if not names or not remote_parent:
        return _fail("fake: unsupported command", 2)
    remote_parent = remote_state.normalize(remote_parent)
    state.setdefault("upload_cwds", []).append({
        "argv": ["filesystem", "upload", *list(args)],
        "cwd": os.getcwd(),
    })
    parent = state["nodes"].get(remote_parent)
    if (
        not isinstance(parent, dict)
        or parent.get("type") != "folder"
        or parent.get("trashed")
    ):
        return _fail("not found", 1)
    for fault in _take_wide_upload_faults(state, remote_parent):
        mode = fault.get("mode") or "fail"
        if mode == "hang":
            return _hang(fault)
        if mode == "perm":
            return _fail(fault.get("stderr") or "permission denied", 1)
        if mode == "stderr_text":
            return _fail(fault.get("stderr") or "upload failed", 1)
        # fail / partial with no file name: every name in the batch fails.
        for escaped in names:
            name = os.path.basename(remote_state.unescape_glob(escaped))
            detail = fault.get("stderr") or "upload failed"
            print("- {name}: {detail}".format(name=name, detail=detail))
        print("{n} item(s) failed to upload".format(n=len(names)), file=sys.stderr)
        return 1
    failed = []
    for escaped in names:
        name = remote_state.unescape_glob(escaped)
        stored = os.path.basename(name)
        file_faults = remote_state.consume_faults(state, "upload", name)
        blocking = [f for f in file_faults if (f.get("mode") or "fail") != "hang"]
        hang = [f for f in file_faults if f.get("mode") == "hang"]
        if hang:
            return _hang(hang[0])
        if blocking:
            fault = blocking[0]
            mode = fault.get("mode") or "fail"
            if mode == "perm":
                return _fail(fault.get("stderr") or "permission denied", 1)
            detail = fault.get("stderr") or "upload failed"
            print("- {name}: {detail}".format(name=stored, detail=detail))
            failed.append(stored)
            continue
        try:
            with open(name, "rb") as handle:
                data = handle.read()
            mtime = int(os.stat(name).st_mtime)
        except OSError as exc:
            print("- {name}: {exc}".format(name=stored, exc=exc))
            failed.append(stored)
            continue
        remote_state.store_file(
            state, remote_parent.rstrip("/") + "/" + stored, data, mtime)
    if failed:
        print(
            "{n} item(s) failed to upload".format(n=len(failed)),
            file=sys.stderr,
        )
        return 1
    return 0


def _list(state, path, as_json):
    path = remote_state.normalize(path)
    if path == "/" and not as_json:
        if remote_state.consume_faults(state, "auth", "/"):
            return _fail("auth failed", 1)
        print("ok")
        return 0
    if remote_state.consume_faults(state, "list", path):
        return _fail("not found", 1)
    if path != "/":
        node = state["nodes"].get(path)
        if not isinstance(node, dict) or node.get("trashed"):
            return _fail("not found", 1)
    if not as_json:
        print("ok")
        return 0
    account = state.get("account") or "tester@example.com"
    items = [
        remote_state.list_item(name, node, account)
        for name, node in remote_state.direct_children(state, path)
    ]
    print(json.dumps(items))
    return 0


def _info(state, path):
    path = remote_state.normalize(path)
    if remote_state.consume_faults(state, "info", path):
        return _fail("not found", 1)
    node = state["nodes"].get(path)
    if not isinstance(node, dict) or node.get("trashed"):
        return _fail("not found", 1)
    print(json.dumps(remote_state.list_item(os.path.basename(path), node, state.get("account"))))
    return 0


def _create_folder(state, parent, name):
    parent = remote_state.normalize(parent)
    name = remote_state.unescape_glob(name)
    if parent != "/" and parent not in state["nodes"]:
        return _fail("not found", 1)
    path = (parent.rstrip("/") + "/" + name) if parent != "/" else "/" + name
    fired = remote_state.consume_faults(state, "create-folder", path)
    if fired:
        return _fail(fired[0].get("stderr") or "permission denied", 1)
    if path in state["nodes"] and not state["nodes"][path].get("trashed"):
        return _fail("already exists", 1)
    state["nodes"][path] = {"type": "folder"}
    return 0


def _trash(state, path):
    path = remote_state.normalize(path)
    if remote_state.consume_faults(state, "trash", path):
        return _fail("trash failed", 1)
    if not remote_state.trash_tree(state, path):
        return _fail("not found", 1)
    return 0


def dispatch(state, argv):
    state.setdefault("calls", []).append(list(argv))
    if not argv:
        return _fail("fake: unsupported command", 2)
    if argv[0] == "--version":
        print(state.get("version_text") or "", end="")
        return 0
    if argv[0] != "filesystem" or len(argv) < 2:
        return _fail("fake: unsupported command", 2)
    verb = argv[1]
    rest = [a for a in argv[2:] if a != "-j"]
    as_json = "-j" in argv[2:]
    if verb == "list" and rest:
        return _list(state, rest[0], as_json)
    if verb == "info" and rest:
        return _info(state, rest[0])
    if verb == "create-folder":
        _options, positionals, unknown = _split_options(argv[2:])
        if unknown:
            return _unknown_option(unknown)
        if len(positionals) >= 2:
            return _create_folder(state, positionals[0], positionals[1])
    if verb == "upload":
        return _upload(state, argv[2:])
    if verb == "trash" and rest:
        return _trash(state, rest[0])
    return _fail("fake: unsupported command", 2)


def main(argv):
    path = os.environ.get("FAKE_PROTON_STATE")
    if not path:
        print("fake: FAKE_PROTON_STATE is unset", file=sys.stderr)
        return 2
    result = _locked_update(path, lambda state: dispatch(state, argv))
    if isinstance(result, tuple) and result and result[0] == "hang":
        time.sleep(result[1])
        print("fake: hung", file=sys.stderr)
        return 1
    return result


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

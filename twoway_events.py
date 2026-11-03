"""Events trigger of the two-way sync.

The events probe (``proton-sync-events``, see experimental/events-probe) tells
which paths of one Proton folder changed since its last run. Here those paths
only decide which folders the pass looks at: the listing of a folder stays the
truth, and every guard of the full pass runs unchanged. The probe is asked
first with ``--keep`` (its bookmark stays where it is); only after a complete
pass is it read again without ``--keep`` to move the bookmark, and what that
second run reports beyond the first is applied too.
"""

__version__ = "1.1.0"

import hashlib
import json
import os
import re
import shutil
import subprocess
import time

import syncdb
import twoway

try:
    from i18n import _
except ImportError:
    def _(s):
        return s

try:
    import config as appconfig
except ImportError:
    appconfig = None

PROBE_NAME = "proton-sync-events"
# Seconds a probe run may take before it counts as a failure.
PROBE_TIMEOUT = 300
# The probe's first run lists the whole subtree, one call per folder: it gets
# its own, much longer limit (6 hours).
FIRST_WALK_TIMEOUT = 6 * 3600
# After a first walk that failed slowly, the mapping waits this long (a day)
# before the next try; a failure faster than SLOW_FAILURE seconds costs nothing
# and is tried again on the next pass.
FIRST_WALK_RETRY = 24 * 3600
SLOW_FAILURE = 60
STATE_FILE = "changes-state.json"   # the probe's own, in its data folder

_ACTIONS = ("created", "modified", "revised", "moved", "removed")
_TYPES = ("file", "folder")
_NO_SESSION = re.compile(
    r"log ?in|sign ?in|session|authenticat|credential|unauthori[sz]ed", re.I)
# The probe says the watched folder was lost ("State reset") or that Proton
# asked for a full refresh: either way only a full listing can be trusted.
_STATE_RESET = re.compile(r"State reset|asked for a full refresh", re.I)


class Result:
    """What one probe run returned: ``ok`` with the parsed fields, or not ok
    with a ``reason`` in plain words."""

    def __init__(self, ok, reason="", initialized=False, events_read=0,
                 changes=None, notes=None, elapsed=0.0):
        self.elapsed = elapsed
        self.ok = ok
        self.reason = reason
        self.initialized = initialized
        self.events_read = events_read
        self.changes = changes if changes is not None else []
        self.notes = notes if notes is not None else []


def probe_path():
    """The probe binary: next to the scripts, else on the PATH. None when
    there is none."""
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), PROBE_NAME)
    if os.path.isfile(here) and os.access(here, os.X_OK):
        return here
    return shutil.which(PROBE_NAME)


def remote_folder(mapping):
    """The Proton folder a mapping watches."""
    return twoway._remote_folder(
        mapping.get("source") or "", mapping.get("dest_parent") or "", None)


def mapping_id(mapping):
    """Stable id of a mapping: its source and its Proton folder."""
    text = (mapping.get("source") or "") + "\0" + remote_folder(mapping)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def events_dir(mapping):
    """Data folder of the probe for this mapping; two mappings never share one."""
    if appconfig is not None:
        base = appconfig.DATA_DIR
    else:
        base = os.path.expanduser("~/.proton-drive-sync")
    return os.path.join(base, "events", mapping_id(mapping))


def _safe_path(value):
    """A path relative to the watched folder, or None when it is not one."""
    if not isinstance(value, str) or not value or "\0" in value:
        return None
    if value.startswith("/"):
        return None
    if any(p in ("", ".", "..") for p in value.split("/")):
        return None
    return value


def _parse(text):
    """The Result of a probe's output, or a failure for output that is not the
    documented JSON."""
    try:
        data = json.loads(text)
    except ValueError:
        return Result(False, _("the output is not valid JSON"))
    if not isinstance(data, dict):
        return Result(False, _("the output is not a JSON object"))
    initialized = data.get("initialized")
    events_read = data.get("eventsRead", 0)
    raw = data.get("changes", [])
    if not isinstance(initialized, bool):
        return Result(False, _("the output has no valid \"initialized\" field"))
    if (not isinstance(events_read, int) or isinstance(events_read, bool)
            or events_read < 0):
        return Result(False, _("the output has no valid \"eventsRead\" field"))
    if not isinstance(raw, list):
        return Result(False, _("the output has no valid \"changes\" list"))
    changes = []
    for item in raw:
        if (not isinstance(item, dict) or item.get("action") not in _ACTIONS
                or item.get("type") not in _TYPES):
            return Result(False, _("the output holds a change that is not valid"))
        if _safe_path(item.get("path")) is None:
            return Result(False, _(
                "the output names a path outside the watched folder"))
        if item["action"] == "moved" and _safe_path(item.get("from")) is None:
            return Result(False, _(
                "the output names a move without a valid old path"))
        if "from" in item and item["action"] != "moved":
            if _safe_path(item["from"]) is None:
                return Result(False, _(
                    "the output names a path outside the watched folder"))
        changes.append(item)
    notes = data.get("notes", [])
    notes = [str(n) for n in notes] if isinstance(notes, list) else []
    return Result(True, "", initialized, events_read, changes, notes)


def has_state(mapping):
    """True when the probe's state file in this mapping's data folder is
    readable and names this mapping's Proton folder (its folderUid is there
    too). Without it the probe's next run is its first walk."""
    try:
        with open(os.path.join(events_dir(mapping), STATE_FILE), "r",
                  encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return False
    return (isinstance(data, dict)
            and data.get("folderPath") == remote_folder(mapping)
            and isinstance(data.get("folderUid"), str)
            and bool(data.get("folderUid")))


def read_changes(mapping, keep, timeout=None):
    """Run the probe on the mapping's Proton folder. ``keep`` leaves its
    bookmark where it is. Returns a Result; a missing binary, a failure, a
    timeout or output that is not valid is a failure with its reason.
    ``timeout`` is in seconds."""
    started = time.monotonic()
    result = _read_changes(
        mapping, keep, PROBE_TIMEOUT if timeout is None else timeout)
    result.elapsed = time.monotonic() - started
    return result


def _read_changes(mapping, keep, timeout):
    probe = probe_path()
    if not probe:
        return Result(False, _(
            "the events probe {n} was not found (put it next to the scripts or "
            "on the PATH)").format(n=PROBE_NAME))
    if not os.path.isfile(probe) or not os.access(probe, os.X_OK):
        return Result(False, _(
            "the events probe {p} was not found or cannot be run").format(p=probe))
    folder = remote_folder(mapping)
    cmd = [probe, "events", "changes", "-j"]
    if keep:
        cmd.append("-k")
    cmd.append(folder)
    data_dir = events_dir(mapping)
    try:
        os.makedirs(data_dir, exist_ok=True)
    except OSError as exc:
        return Result(False, _("its data folder cannot be created: {e}").format(
            e=exc))
    env = dict(os.environ, PROTON_SYNC_EVENTS_DIR=data_dir)
    try:
        done = subprocess.run(
            cmd, env=env, stdin=subprocess.DEVNULL, capture_output=True,
            text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return Result(False, _("it did not answer within {s} seconds").format(
            s=timeout))
    except OSError as exc:
        return Result(False, _("it could not be started: {e}").format(e=exc))
    if done.returncode != 0:
        said = ((done.stderr or "") + " " + (done.stdout or "")).strip()
        said = " ".join(said.split())[:300]
        if _NO_SESSION.search(said):
            return Result(False, _(
                "there is no signed-in session; run: {n} auth login").format(
                    n=PROBE_NAME))
        return Result(False, _("it failed (exit {c}): {s}").format(
            c=done.returncode, s=said or _("no message")))
    return _parse(done.stdout)


# ── The rescan flag ──────────────────────────────────────────────────────────
# Set from the moment the probe's bookmark may move until what it reported has
# been applied; while it stands, the next pass lists everything.


def _flag_key(mapping):
    return "events_rescan:" + mapping_id(mapping)


def _flag(config_path, mapping, value=None):
    """Read the flag (value None), set it (True) or clear it (False)."""
    db = syncdb.SyncDB(syncdb.database_path(config_path))
    try:
        if value is None:
            return db.get_meta(_flag_key(mapping)) == "1"
        if value:
            db.set_meta(_flag_key(mapping), "1")
        else:
            db.delete_meta(_flag_key(mapping))
        return value
    finally:
        db.close()


def _meta(config_path, key, value=None, drop=False):
    """Read a meta row (default), write it (value) or delete it (drop)."""
    db = syncdb.SyncDB(syncdb.database_path(config_path))
    try:
        if drop:
            db.delete_meta(key)
            return None
        if value is not None:
            db.set_meta(key, value)
            return value
        return db.get_meta(key)
    finally:
        db.close()


def _waiting_key(mapping):
    return "events_waiting:" + mapping_id(mapping)


def _failed_key(mapping):
    return "events_first_failed:" + mapping_id(mapping)


def _held_folders(config_path, mapping):
    """The Proton folders (relative to the mapping root) where a deletion was
    waiting at the end of the last pass."""
    try:
        found = json.loads(_meta(config_path, _waiting_key(mapping)) or "[]")
    except ValueError:
        return set()
    if not isinstance(found, list):
        return set()
    return {f for f in found if isinstance(f, str)}


def _store_held(config_path, mapping, folders):
    if folders:
        _meta(config_path, _waiting_key(mapping),
              json.dumps(sorted(folders)))
    else:
        _meta(config_path, _waiting_key(mapping), drop=True)


def _first_failure(config_path, mapping):
    """(time, reason) of a first walk that failed, or None."""
    try:
        found = json.loads(_meta(config_path, _failed_key(mapping)) or "null")
        return float(found["time"]), str(found["reason"])
    except (ValueError, TypeError, KeyError):
        return None


def _say(text):
    print("  ℹ  [events] " + text)


def _warn(text):
    print("  ⚠  [events] " + text)


def _has_history(config_path, mapping):
    """True when the database holds a file or folder row inside the mapping."""
    prefix = (mapping.get("source") or "").rstrip(os.sep) + os.sep
    db = syncdb.SyncDB(syncdb.database_path(config_path))
    try:
        return (any(r["local_path"].startswith(prefix) for r in db.rows())
                or any(r["local_path"].startswith(prefix)
                       for r in db.folders()))
    finally:
        db.close()


def run_mapping(mapping, config_path, cache, exclusions, dry_run=False,
                verbose=False, allow_mass_delete=False, global_ex=None,
                delete=False, full_check=False):
    """Reconcile one two-way mapping, using the events probe to look only at
    the folders that changed. Returns True when the pass completed, False when
    it did not, and ``twoway.PAUSED`` when the probe could not be used and
    nothing was done (never a full listing on its own)."""
    kw = dict(dry_run=dry_run, verbose=verbose,
              allow_mass_delete=allow_mass_delete, global_ex=global_ex,
              delete=delete)
    report = {}
    still = set()           # folders where a deletion waits after this pass

    def run(changes=None, events_read=None, extra=None):
        report.clear()
        done = twoway.sync_mapping(
            mapping, config_path, cache, exclusions, changes=changes,
            events_read=events_read, report=report, extra_folders=extra, **kw)
        still.update(report.get("waiting_folders") or ())
        return done

    def store(complete):
        # An incomplete pass may not have reached every held folder: keep them.
        _store_held(config_path, mapping,
                    still if complete else still | held)
        if complete and report.get("deletions_waiting"):
            _waiting()

    if mapping.get("type") == "file":
        _say(_("Listing the Proton side: a single-file mapping has no folder "
               "to watch."))
        return run()
    if dry_run:
        # The probe keeps its starting point and its bookmark in its data
        # folder, so a dry run does not call it at all.
        _say(_("Listing the whole Proton side: a dry run does not use the "
               "events probe."))
        return run()
    held = _held_folders(config_path, mapping)
    first_walk = not has_state(mapping)
    if first_walk and not full_check:
        failed = _first_failure(config_path, mapping)
        if failed and time.time() - failed[0] < FIRST_WALK_RETRY:
            _pause(_("the events probe's first walk failed ({r}). It is tried "
                     "again after {t}.").format(
                         r=failed[1], t=time.strftime(
                             "%Y-%m-%d %H:%M", time.localtime(
                                 failed[0] + FIRST_WALK_RETRY))), mapping)
            return twoway.PAUSED
    first = read_changes(
        mapping, keep=True,
        timeout=FIRST_WALK_TIMEOUT if first_walk else PROBE_TIMEOUT)
    reason = None
    if not first.ok:
        if first_walk and first.elapsed > SLOW_FAILURE:
            _meta(config_path, _failed_key(mapping), json.dumps(
                {"time": time.time(), "reason": first.reason}))
        if not full_check:
            _pause(_("the events probe could not be used: {r}. It is tried "
                     "again on the next pass.").format(r=first.reason),
                   mapping)
            return twoway.PAUSED
        _warn(_("The events probe could not be used: {r}. Listing the whole "
                "Proton side, as a full check was asked for.").format(
                    r=first.reason))
        reason = ""
    else:
        _meta(config_path, _failed_key(mapping), drop=True)
    if reason is None and first.initialized:
        reason = _("the probe has no starting point yet for this folder")
    elif reason is None and any(_STATE_RESET.search(n) for n in first.notes):
        reason = _("the probe reports that the watched folder was lost or "
                   "must be read again: {n}").format(n=" ".join(first.notes))
    elif reason is None and full_check:
        reason = _("a full check was asked for")
    elif reason is None and _flag(config_path, mapping):
        reason = _("the previous pass did not finish applying the events")
    elif reason is None and not _has_history(config_path, mapping):
        reason = _("nothing is recorded yet for this folder")
    if reason is not None:
        if reason:
            _say(_("Listing the whole Proton side: {r}.").format(r=reason))
        # Set before the listing, cleared only once it completed: a listing
        # that stops halfway must not be forgotten.
        _flag(config_path, mapping, True)
        complete = run()
        store(complete)
        if not complete:
            return complete
        _flag(config_path, mapping, False)  # the listing saw everything
        if first.ok and not first.initialized and first.events_read > 0:
            _acknowledge(mapping, config_path, first, run, store)
        return complete
    complete = run(first.changes, first.events_read, held)
    store(complete)
    if not complete:
        return complete
    if first.events_read > 0:
        _acknowledge(mapping, config_path, first, run, store)
    return complete


def _pause(why, mapping):
    print("  \u23f8 " + _("Two-way sync paused for {s}: {w}").format(
        s=mapping.get("source") or "", w=why))
    twoway._ps._RUN.add("twoway_paused")


def _waiting():
    _say(_("A deletion is waiting for a pass that may apply it; its folder is "
           "looked at again on the next pass."))


def _acknowledge(mapping, config_path, first, run, store):
    """Move the probe's bookmark after a complete pass and apply what it
    reports beyond the first read. A failure or stop in between leaves the
    rescan flag, so the next pass lists everything."""
    _flag(config_path, mapping, True)
    second = read_changes(mapping, keep=False)
    if not second.ok:
        _warn(_("The events could not be marked as read: {r}. The next pass "
                "will list the whole Proton side.").format(r=second.reason))
        return
    if second.initialized:
        _warn(_("The probe started over instead of marking the events as read. "
                "The next pass will list the whole Proton side."))
        return
    extra = [c for c in second.changes if c not in first.changes]
    if extra:
        _say(_("{n} change(s) arrived while the pass ran; applying them."
               ).format(n=len(extra)))
        done = run(extra, second.events_read)
        store(done)
        if not done:
            _warn(_("The late changes were not all applied. The next pass will "
                    "list the whole Proton side."))
            return
    _flag(config_path, mapping, False)

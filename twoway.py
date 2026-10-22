"""Two-way reconcile for one entry of the ``sync_mappings`` key (direction "twoway").

Entries of ``mappings`` stay one-way and never reach this module. Downloads go through the engine's ``run_cli``. A failed
download does not replace the local file and does not rewrite its sync row.
A failed remote listing does not update the sync database for that folder.
"""

__version__ = "1.1.0"

import os
import re
import shutil
import sys
import tempfile
import time

import syncdb


def _load_engine():
    """The running engine.

    ``python proton_sync.py`` executes that file as ``__main__``. Importing
    ``proton_sync`` again would be a second module, with its own counters, so
    a failure recorded here would not change the pass exit code.
    """
    main = sys.modules.get("__main__")
    if main is not None and hasattr(main, "run_cli") and hasattr(main, "get_remote_listing"):
        return main
    import proton_sync
    return proton_sync


_ps = _load_engine()

try:
    from i18n import _
except ImportError:
    def _(s):
        return s


_DOC_TYPES = ("document", "spreadsheet", "proton-doc")
_CONFLICT_MARK = " (proton conflict)"
_CONFLICT_STEM = re.compile(r" \(proton conflict(?: \d+)?\)$")
WEB_DOCUMENT_LINE = (
    "Proton web document. Open it on the web. This copy is not uploaded."
)


def sync_mapping(mapping, config_path, cache, exclusions, dry_run=False,
                 verbose=False, allow_mass_delete=False, global_ex=None,
                 delete=False):
    """Reconcile one twoway mapping. Returns True when the tree completed.

    ``delete`` is the pass's ``--delete`` switch. Without it nothing is
    removed on either side, whatever the mapping says.
    """
    source = mapping.get("source") or ""
    if mapping.get("type") == "file":
        ctx = _context(mapping, config_path, cache, exclusions, dry_run,
                       verbose, allow_mass_delete, source_root=os.path.dirname(source),
                       global_ex=global_ex, delete=delete)
        with ctx.db:
            return _sync_file(source, mapping.get("dest_parent") or "", ctx)
    ctx = _context(mapping, config_path, cache, exclusions, dry_run,
                   verbose, allow_mass_delete, source_root=source,
                   global_ex=global_ex, delete=delete)
    with ctx.db:
        return _sync_dir(source, mapping.get("dest_parent") or "", ctx)


_CONFLICT_TEXT = {
    "local-same": "same local folder as {o}",
    "local-parent": "its local folder contains the one of {o}",
    "local-child": "its local folder is inside the one of {o}",
    "arrival-same": "same Proton folder as {o}: {d}",
    "arrival-inside": "its Proton folder is inside the one of {o}: {d}",
    "arrival-contains": "its Proton folder contains the one of {o}: {d}",
}


def plan(sync_mappings, upload_mappings):
    """Split the ``sync_mappings`` entries into (runnable, skipped).

    skipped is a list of (mapping, log line). Nothing here runs an entry: an
    unknown direction or type, or an overlap with another mapping, only
    produces a line. Checked on every load of the file."""
    import mapping_keys
    skipped, ok = [], []
    for m in sync_mappings:
        if not isinstance(m, dict):
            skipped.append((m, _("Two-way entry skipped: not a mapping object")))
        elif m.get("direction") not in mapping_keys.SYNC_DIRECTIONS:
            skipped.append((m, _("Sync mapping skipped: unknown direction {d!r}: {s}")
                            .format(d=m.get("direction"), s=m.get("source", ""))))
        elif m.get("type") not in mapping_keys.SYNC_TYPES:
            skipped.append((m, _("Sync mapping skipped: unknown type {t!r}: {s}")
                            .format(t=m.get("type"), s=m.get("source", ""))))
        elif not m.get("source") or not m.get("dest_parent"):
            skipped.append((m, _("Sync mapping skipped: source or destination missing: {s}")
                            .format(s=m.get("source", ""))))
        else:
            ok.append(m)
    conflicts = mapping_keys.sync_mapping_conflicts(ok, upload_mappings)
    runnable = []
    for i, m in enumerate(ok):
        hit = conflicts.get(i)
        if hit:
            reason, other, detail = hit
            why = _(_CONFLICT_TEXT[reason]).format(o=other.get("source", "?"), d=detail)
            skipped.append((m, _("Two-way mapping not run: {s} ({w})")
                            .format(s=m["source"], w=why)))
        else:
            runnable.append(m)
    return runnable, skipped


def run_pass(sync_mappings, upload_mappings, config_path, cache, global_ex,
             only_source=None, trigger_name="none", runner=None, dry_run=False,
             verbose=False, allow_mass_delete=False, delete=False,
             full_check=False, blocked=None):
    """Run the two-way pass of one engine run. Returns a list of
    (source, complete, unreadable) for the health report.

    ``runner`` is a trigger's own function; None means no events probe, so
    the whole Proton side would be listed (``sync_mapping``); that is only
    done when ``full_check`` is set (--twoway-full-check), otherwise each
    mapping is paused with one line and counted in ``twoway_paused``.
    ``upload_mappings`` is the full ``mappings`` list, for the overlap check;
    ``blocked`` maps id(mapping) to (reason, other, detail, file) for the
    mappings that overlap a mapping of the other service's file."""
    if runner is None and trigger_name != "none":
        print(_("Two-way trigger {t!r} unknown: listing the whole Proton side")
              .format(t=trigger_name))
    runnable, skipped = plan(sync_mappings, upload_mappings)
    if blocked:
        still = []
        for m in runnable:
            hit = blocked.get(id(m))
            if hit:
                reason, other, detail, where = hit
                why = _(_CONFLICT_TEXT[reason]).format(o=other.get("source", "?"), d=detail)
                skipped.append((m, _("Two-way mapping not run: {s} ({w}, in the "
                                     "mappings file {f})").format(
                                         s=m["source"], w=why, f=where)))
            else:
                still.append(m)
        runnable = still
    for _skipped, line in skipped:
        print("  \u23ed " + line)
    if only_source:
        wanted = {os.path.normpath(s) for s in only_source}
        runnable = [m for m in runnable
                    if os.path.normpath(m["source"]) in wanted]
    out = []
    if runner is None and not full_check:
        for m in runnable:
            print("  \u23f8 " + _("Two-way sync paused for {s}: there is no events "
                                  "probe, so a pass would have to list the whole "
                                  "Proton side. Run with --twoway-full-check to "
                                  "do that once.").format(s=m["source"]))
            _ps._RUN.add("twoway_paused")
        return out
    for m in runnable:
        print(_("\n\u25b6 Two-way mapping: {s}  =>  {d}").format(
            s=m["source"], d=m["dest_parent"]))
        _ps._RUN.mappings_total += 1
        if not os.path.exists(m["source"]):
            print(_("  \u274c Source not found, skipped: {s}").format(s=m["source"]))
            _ps._RUN.add("sources_missing")
            continue
        complete = (runner or sync_mapping)(
            m, config_path, cache, _ps.mapping_exclusions(m, global_ex),
            dry_run=dry_run, verbose=verbose, allow_mass_delete=allow_mass_delete,
            global_ex=global_ex, delete=delete)
        if complete:
            _ps._RUN.add("mappings_complete")
        out.append((m["source"], bool(complete), _ps._take_unreadable()))
        cache.save()
    return out


def sync_tree(mapping, local_dir, remote_parent, config_path, cache, exclusions,
              dry_run=False, verbose=False, allow_mass_delete=False, global_ex=None,
              delete=False):
    """Reconcile one folder of a twoway mapping (a full pass or a subpath)."""
    ctx = _context(mapping, config_path, cache, exclusions, dry_run,
                   verbose, allow_mass_delete,
                   source_root=mapping.get("source") or local_dir,
                   global_ex=global_ex, delete=delete)
    with ctx.db:
        return _sync_dir(local_dir, remote_parent, ctx)


class _Ctx:
    def __init__(self, mapping, db, cache, exclusions, dry_run, verbose,
                 opts, source_root, config_path, delete=False):
        self.mapping = mapping
        self.db = db
        self.cache = cache
        self.exclusions = exclusions
        self.dry_run = dry_run
        self.verbose = verbose
        self.opts = opts
        self.source_root = source_root
        self.config_path = config_path
        self.delete = bool(delete)
        self.hold_queue = None


def _context(mapping, config_path, cache, exclusions, dry_run, verbose,
             allow_mass_delete, source_root, global_ex=None, delete=False):
    opts = _ps.build_delete_opts(
        mapping, source_root, global_ex=global_ex,
        allow_mass_delete=allow_mass_delete, verbose=verbose)
    db = syncdb.SyncDB(syncdb.database_path(config_path))
    return _Ctx(mapping, db, cache, exclusions, dry_run, verbose, opts,
                source_root, config_path, delete=delete)


def _sync_file(local_file, remote_parent, ctx):
    if not ctx.dry_run and not _ps.ensure_remote_path(remote_parent):
        return False
    listing = _ps.get_remote_listing(remote_parent, verbose=ctx.verbose)
    if not listing.ok:
        print("[list-skipped] " + _(
            "Could not list {p} — file skipped this pass "
            "(nothing sent, nothing deleted).").format(p=remote_parent))
        _ps._RUN.add("folders_listing_failed")
        return False
    name = os.path.basename(local_file)
    return _reconcile_file(local_file, remote_parent, listing.get(name), ctx)


def _remote_folder(local_dir, remote_parent, ctx):
    """Remote directory this local directory lists."""
    parent = (remote_parent or "").rstrip("/")
    name = os.path.basename(os.path.normpath(local_dir))
    return parent + "/" + name if parent else "/" + name


def _sync_dir(local_dir, remote_parent, ctx, remote_info=None,
              parent_listed=False):
    """List this folder even when the one-way fingerprint is unchanged.

    ``parent_listed`` says the caller just listed the parent: a ``remote_info``
    of None then means the parent does not contain this folder."""
    remote_folder = _remote_folder(local_dir, remote_parent, ctx)
    print("📂 " + local_dir)
    listing = None
    if remote_info is None and ctx.db.has_synced_under(local_dir):
        if parent_listed:
            _report_folder_gone(local_dir, remote_folder, ctx)
            return False
        # Top folder of the pass: its own listing tells whether it exists.
        listing = _ps.get_remote_listing(remote_folder, verbose=ctx.verbose)
        if not listing.ok and _ps._missing_node_error(listing.error):
            _report_folder_gone(local_dir, remote_folder, ctx)
            return False
        if listing.ok:
            _ps._remember_remote(remote_folder)
    if listing is None or listing.ok:
        if not ctx.dry_run and not _ps.ensure_remote_path(remote_folder):
            return False
        if listing is None:
            listing = _ps.get_remote_listing(remote_folder, verbose=ctx.verbose)
    if not listing.ok:
        print("[list-skipped] " + _(
            "Could not list {p} — folder skipped this pass "
            "(nothing sent, nothing deleted).").format(p=remote_folder))
        _ps._RUN.add("folders_listing_failed")
        return False
    _remember_folder(local_dir, remote_folder, remote_info, ctx)

    try:
        entries = list(os.scandir(local_dir))
    except OSError as exc:
        _ps._note_unreadable(local_dir, exc)
        return False

    local_names = set()
    dir_failed = False
    all_children = True
    outer_queue, ctx.hold_queue = ctx.hold_queue, []
    my_queue = ctx.hold_queue
    n_files = 0
    for entry in entries:
        if _ps._name_excluded(ctx.exclusions, entry.name):
            continue
        local_names.add(entry.name)
        if _is_conflict_copy(entry.name):
            continue
        if entry.is_dir(follow_symlinks=False):
            if not _sync_dir(entry.path, remote_folder, ctx,
                             listing.get(entry.name), parent_listed=True):
                all_children = False
        elif entry.is_file():
            n_files += 1
            if not _reconcile_file(
                    entry.path, remote_folder, listing.get(entry.name), ctx):
                dir_failed = True

    ctx.hold_queue = outer_queue
    if not _flush_holds(my_queue, n_files, remote_folder, ctx):
        dir_failed = True
    if not _remote_only(local_dir, remote_folder, listing, local_names, ctx):
        dir_failed = True

    complete = all_children and not dir_failed
    if ctx.cache is not None and not ctx.dry_run and complete:
        excl_fp = ctx.exclusions.fingerprint() if ctx.exclusions else None
        signature = _ps._local_signature(local_dir, remote_folder, excl_fp)
        if signature is not None:
            ctx.cache.update(local_dir, signature, subtree_complete=True)
            ctx.cache.maybe_save()
    return complete


def _report_folder_gone(local_dir, remote_folder, ctx):
    """This folder is missing on Proton while the database still has synced
    files under it. It was renamed or removed as a whole, so nothing under it
    is held, uploaded or recreated on this pass."""
    print("[folder-missing] " + _(
        "{p}: this folder no longer exists on Proton. "
        "To be safe, nothing was changed in it here.").format(p=local_dir))
    _ps._RUN.add("deletions_refused")
    if not ctx.dry_run:
        known = ctx.db.get_folder(local_dir) or {}
        ctx.db.upsert_folder({
            "local_path": local_dir,
            "remote_path": known.get("remote_path") or remote_folder,
            "remote_node_id": known.get("remote_node_id"),
            "local_seen": 1,
            "remote_seen": 0,
        })


def _remember_folder(local_dir, remote_folder, remote_info, ctx):
    """Baseline row for a folder seen on both sides (no deletion logic yet)."""
    if ctx.dry_run:
        return
    node_id = remote_info.get("node_id") if isinstance(remote_info, dict) else None
    ctx.db.upsert_folder({
        "local_path": local_dir,
        "remote_path": remote_folder,
        "remote_node_id": node_id,
        "local_seen": 1,
        "remote_seen": 1,
    })


def _remote_only(local_dir, remote_folder, listing, local_names, ctx):
    gone = []
    restores = []
    ok = True
    for name, info in listing.items():
        if name in local_names:
            continue
        if _ps._name_excluded(ctx.exclusions, name):
            # Two-way folder: the file may come from another device. An
            # excluded name on Proton is neither downloaded nor trashed,
            # whatever excluded_remote says.
            continue
        local_path = os.path.join(local_dir, name)
        remote_path = remote_folder.rstrip("/") + "/" + name
        rtype = info.get("type") if isinstance(info, dict) else None
        if rtype == "folder":
            if not ctx.dry_run:
                os.makedirs(local_path, exist_ok=True)
            if not _sync_dir(local_path, remote_folder, ctx, info):
                ok = False
            continue
        if rtype in _DOC_TYPES:
            _mark_document(local_path, remote_path, info, ctx)
            continue
        row = ctx.db.get(local_path)
        if row and row.get("state") == "synced":
            if _remote_changed_since(info, row) is False:
                gone.append((local_path, remote_path, info, row))
            else:
                # Removed here, but the Proton copy changed after the last
                # sync, or the listing cannot tell. The newer copy comes back.
                print("[restore] " + _(
                    "deleted here but changed on Proton since, downloading "
                    "it again: {p}").format(p=local_path))
                restores.append((local_path, remote_path, info))
        elif row and row.get("state") == "conflict":
            # The original was removed during a conflict. The Proton version
            # comes back under the original name; any conflict copy stays.
            restores.append((local_path, remote_path, info))
        else:
            restores.append((local_path, remote_path, info))

    if _refusing_mass_delete(gone, listing, ctx):
        print("[delete-guard] " + _(
            "refusing to trash {n} of {t} remote item(s) in {p}").format(
                n=len(gone), t=len(listing), p=remote_folder))
        _ps._RUN.add("deletions_refused")
        # The rows stay as they are (synced): the next pass decides again
        # from scratch, so a refused deletion is never turned into a restore.
        return False

    for local_path, remote_path, info, row in gone:
        if _can_trash(ctx, remote_path):
            if _ps.remote_trash(
                    remote_path, permanent=False, dry_run=ctx.dry_run):
                if not ctx.dry_run:
                    ctx.db.delete(local_path)
            else:
                ok = False
        else:
            if not _deletion_switched_on(ctx):
                # Deletion is simply off: the documented behaviour, the file
                # comes back on the next pass.
                _set_state(row, "pending-down", ctx)
            # Otherwise a guard refused it (mount, source_kind, shared
            # folder): the row stays synced and the next pass decides again,
            # so a refused deletion is never turned into a restore.
            if ctx.opts.get("mount_lost"):
                ok = False

    for local_path, remote_path, info in restores:
        if not _download_over(local_path, remote_path, info, ctx):
            ok = False
    return ok


def _reconcile_file(local_path, remote_folder, remote_info, ctx):
    name = os.path.basename(local_path)
    remote_path = remote_folder.rstrip("/") + "/" + name
    if isinstance(remote_info, dict) and remote_info.get("type") in _DOC_TYPES:
        _mark_document(local_path, remote_path, remote_info, ctx)
        return True
    row = ctx.db.get(local_path)
    if row and row.get("state") == "conflict":
        return _resolve_conflict(local_path, remote_folder, remote_info, row, ctx)

    if not isinstance(remote_info, dict) or remote_info.get("type") not in (None, "file"):
        if row and row.get("state") == "synced":
            return _remote_gone(local_path, remote_folder, remote_info, row, ctx)
        return _upload(local_path, remote_folder, remote_info, ctx)

    action, reason = _ps.download_decision(local_path, remote_info, row)
    if reason == "local-only":
        return _upload(local_path, remote_folder, remote_info, ctx)
    if action == "download":
        return _download_over(local_path, remote_path, remote_info, ctx)
    if action == "conflict":
        return _write_conflict(local_path, remote_path, remote_info, row, ctx)
    if not ctx.dry_run:
        _save_synced(local_path, remote_path, remote_info, ctx)
    return True


def _remember(local_path, remote_path, state, ctx):
    """Record a state without claiming the upload already finished."""
    if ctx.dry_run:
        return
    row = ctx.db.get(local_path)
    if row:
        stored = dict(row)
        stored["state"] = state
        if remote_path:
            stored["remote_path"] = remote_path
        ctx.db.upsert(stored)
        return
    ctx.db.upsert({
        "local_path": local_path,
        "remote_path": remote_path,
        "remote_node_id": None,
        "sha1": None,
        "claimed_size": None,
        "claimed_mtime": None,
        "local_inode": None,
        "local_mtime": None,
        "state": state,
    })


def _upload(local_path, remote_folder, remote_info, ctx):
    if _is_web_marker(local_path):
        return True
    remote_path = remote_folder.rstrip("/") + "/" + os.path.basename(local_path)
    _remember(local_path, remote_path, "pending-up", ctx)
    ok = _ps.upload_batch(
        [local_path], remote_folder, dry_run=ctx.dry_run, verbose=ctx.verbose,
        conflict_mode=ctx.mapping.get("conflict_mode", "replace"))
    if not ok:
        _remember(local_path, remote_path, "error", ctx)
        return False
    if not ctx.dry_run:
        _save_synced(local_path, remote_path, remote_info, ctx)
    return True


def _download_over(local_path, remote_path, remote_info, ctx):
    if ctx.dry_run:
        print("[download] " + _("[DRY-RUN] would download: {p}").format(p=local_path))
        return True
    parent = os.path.dirname(local_path) or "."
    temporary = tempfile.mkdtemp(prefix=".proton-sync-download-", dir=parent)
    try:
        fetched, skipped, reason = _fetch(remote_path, temporary)
        if skipped:
            _mark_document(local_path, remote_path, remote_info, ctx)
            return True
        if fetched is None:
            _download_failed(local_path, reason)
            return False
        os.replace(fetched, local_path)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    print("[download] " + _("downloaded {p}").format(p=local_path))
    _save_synced(local_path, remote_path, remote_info, ctx)
    return True


def _fetch(remote_path, directory):
    """Return (local path or None, skipped_document, reason).

    ``directory`` is a fresh, empty folder. The CLI normally writes the file
    under its remote name; if it picked another name, the one file it wrote
    is used. ``reason`` is the CLI's own message when nothing usable came
    back.
    """
    result = _ps.run_cli(
        ["filesystem", "download", "-f", "skip", remote_path, directory])
    text = (result.stdout or "") + "\n" + (result.stderr or "")
    if "skipped:" in text:
        return None, True, ""
    if result.returncode != 0:
        return None, False, _cli_reason(result)
    name = os.path.basename(remote_path.rstrip("/"))
    path = os.path.join(directory, name)
    if os.path.isfile(path):
        return path, False, ""
    written = []
    for parent, _dirs, files in os.walk(directory):
        written.extend(os.path.join(parent, f) for f in files)
    if len(written) == 1:
        return written[0], False, ""
    return None, False, _cli_reason(result) or _(
        "the Proton CLI finished but wrote {n} files instead of one").format(
            n=len(written))


def _cli_reason(result):
    """Last line the CLI printed on stderr, else stdout, kept short."""
    for stream in (result.stderr, result.stdout):
        lines = [line.strip() for line in (stream or "").splitlines() if line.strip()]
        if lines:
            reason = lines[-1]
            return reason if len(reason) <= 300 else reason[:297] + "..."
    return ""


def _download_failed(local_path, reason):
    if os.path.lexists(local_path):
        text = _("download failed, local file kept: {p}").format(p=local_path)
    else:
        text = _("could not download {p}").format(p=local_path)
    if reason:
        text += " — " + _("Proton CLI said: {r}").format(r=reason)
    print("[download-failed] " + text)
    _ps._RUN.add("files_failed")


def _write_conflict(local_path, remote_path, remote_info, row, ctx):
    """Keep both: the Proton version is saved next to the local file.

    Each copy gets a fresh name, so an older conflict copy is never
    overwritten.
    """
    if ctx.dry_run:
        print("[conflict] " + _(
            "[DRY-RUN] both sides changed, would keep both: {p}").format(p=local_path))
        return True
    parent = os.path.dirname(local_path) or "."
    temporary = tempfile.mkdtemp(prefix=".proton-sync-download-", dir=parent)
    try:
        fetched, skipped, reason = _fetch(remote_path, temporary)
        if skipped:
            _mark_document(local_path, remote_path, remote_info, ctx)
            return True
        if fetched is None:
            _download_failed(local_path, reason)
            return False
        conflict_path = _conflict_path(local_path)
        os.replace(fetched, conflict_path)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    print("[conflict] " + _(
        "both sides changed. Your file is kept; the Proton version is saved "
        "as {c}").format(c=conflict_path))
    _conflict_help(local_path)
    _mark_conflict(local_path, remote_path, remote_info, row, ctx)
    return True


def _conflict_help(local_path):
    print("[conflict] " + _(
        "To finish: keep the version you want under the name {n} and delete "
        "the \"(proton conflict)\" copy. The next pass sends your choice to "
        "Proton.").format(n=os.path.basename(local_path)))


def _resolve_conflict(local_path, remote_folder, remote_info, row, ctx):
    """Finish a conflict once the person has picked a version.

    The row holds the Proton version from when the conflict was found.
    Nothing on Proton is replaced while a conflict copy is still here.
    """
    remote_path = remote_folder.rstrip("/") + "/" + os.path.basename(local_path)
    if not isinstance(remote_info, dict) or remote_info.get("type") not in (None, "file"):
        # The Proton copy is gone. The local file is the only one left.
        print("[conflict-resolved] " + _(
            "no Proton copy any more, sending yours: {p}").format(p=local_path))
        return _upload(local_path, remote_folder, remote_info, ctx)
    remote_sha = _ps._optional_sha1(remote_info.get("sha1"))
    if remote_sha:
        try:
            same = _ps._local_sha1(local_path) == remote_sha
        except OSError:
            same = False
        if same:
            print("[conflict-resolved] " + _(
                "both versions are the same now: {p}").format(p=local_path))
            if not ctx.dry_run:
                _save_synced(local_path, remote_path, remote_info, ctx)
            return True
    if _conflict_copies(local_path):
        print("[conflict] " + _(
            "waiting for you to choose a version: {p}").format(p=local_path))
        _conflict_help(local_path)
        return True
    if _remote_changed_since(remote_info, row) is False:
        print("[conflict-resolved] " + _(
            "conflict copy removed, sending your version: {p}").format(p=local_path))
        return _upload(local_path, remote_folder, remote_info, ctx)
    # Proton changed again after the conflict. Keep that version too.
    return _write_conflict(local_path, remote_path, remote_info, row, ctx)


def _remote_gone(local_path, remote_folder, remote_info, row, ctx):
    """The Proton copy was removed after the last sync.

    An edit made here since then wins and is sent again. An unchanged file
    is moved to the holding folder, never deleted, and only on a pass that
    allows deletions.
    """
    if _ps._local_differs_from_row(local_path, row) is not False:
        print("[restore] " + _(
            "removed on Proton but changed here since, sending it again: "
            "{p}").format(p=local_path))
        return _upload(local_path, remote_folder, remote_info, ctx)
    if not ctx.delete:
        print("[kept] " + _(
            "removed on Proton; local file kept because deletions are off "
            "for this pass: {p}").format(p=local_path))
        return True
    if ctx.hold_queue is not None:
        ctx.hold_queue.append(local_path)
        return True
    return _hold(local_path, ctx)


def _remote_changed_since(remote_info, row):
    """True, False, or None when the listing cannot tell."""
    if not isinstance(remote_info, dict) or not isinstance(row, dict):
        return None
    return _ps._remote_differs_from_row(
        _ps._claimed_size_of(remote_info),
        _ps._optional_sha1(remote_info.get("sha1")),
        _ps._remote_mtime_seconds(remote_info.get("mtime")),
        row)


def _hold(local_path, ctx):
    """Move the local file into <source>/.proton-sync-holding-<user>/.

    The move is os.rename on the same filesystem only. Anything else leaves the
    file where it is, counts one failure and keeps the sync row. Never a copy.
    """
    if ctx.dry_run:
        print("[held] " + _("[DRY-RUN] would move aside: {p}").format(p=local_path))
        return True
    dest = _holding_destination(ctx, local_path)
    dest_dir = os.path.dirname(dest)
    try:
        if _device_of_nearest_parent(dest_dir) != os.lstat(local_path).st_dev:
            raise OSError(
                "the holding folder is on another filesystem")
        os.makedirs(dest_dir, exist_ok=True)
        # A file held earlier under the same name is never replaced.
        dest = _timed_name(dest)
        os.rename(local_path, dest)
    except OSError as exc:
        print("[held-failed] " + _(
            "kept in place, not moved aside: {p} ({e})").format(
                p=local_path, e=exc))
        _ps._RUN.add("files_failed")
        return False
    ctx.db.delete(local_path)
    print("[held] " + local_path + " -> " + dest)
    return True


def _timed_name(dest):
    """``a.txt`` -> ``a~20261022-073300.txt`` (local time); ``-2``, ``-3`` when
    that second is already taken. The extension stays last."""
    stem, ext = os.path.splitext(dest)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    candidate = "{s}~{t}{e}".format(s=stem, t=stamp, e=ext)
    n = 2
    while os.path.lexists(candidate):
        candidate = "{s}~{t}-{n}{e}".format(s=stem, t=stamp, n=n, e=ext)
        n += 1
    return candidate


def _device_of_nearest_parent(path):
    while True:
        try:
            return os.stat(path).st_dev
        except FileNotFoundError:
            parent = os.path.dirname(path)
            if parent == path:
                raise
            path = parent


def _flush_holds(queue, local_count, remote_folder, ctx):
    """Hold the files queued for one folder, unless the mass-delete guard
    refuses. Returns False when something stayed behind."""
    if not queue:
        return True
    if _refusing_mass_delete(queue, local_count, ctx, always=True):
        print("[delete-guard] " + _(
            "refusing to hold {n} of {t} local file(s) in {p}").format(
                n=len(queue), t=local_count, p=remote_folder))
        _ps._RUN.add("deletions_refused")
        return False
    ok = True
    for local_path in queue:
        if not _hold(local_path, ctx):
            ok = False
    return ok


def _deletion_switched_on(ctx):
    return bool(ctx.delete and ctx.mapping.get("allow_delete"))


def _can_trash(ctx, remote_path):
    # Two switches, as for one-way mappings: the pass's --delete and the
    # mapping's allow_delete.
    if not ctx.delete or not ctx.mapping.get("allow_delete"):
        return False
    if (str(remote_path).startswith("/shared-with-me")
            and ctx.mapping.get("shared_delete_confirmed") is not True):
        return False
    if ctx.opts.get("mount_lost"):
        return False
    guard = ctx.opts.get("delete_guard")
    if guard is None:
        return False
    ok, reason = guard()
    if ok:
        return True
    ctx.opts["mount_lost"] = True
    print("[delete-guard] " + _(
        "mount check failed, remote file kept: {r}").format(r=reason))
    _ps._RUN.add("deletions_refused")
    return False


def _refusing_mass_delete(gone, total, ctx, always=False):
    """``total`` is the number of items the folder holds (a listing, or a count).

    ``always``: apply the thresholds even when the mass_delete_guard setting is
    off (used for local holds, which are guarded whatever the setting says).
    """
    if not gone or ctx.opts.get("allow_mass_delete"):
        return False
    if not always and not ctx.opts.get("mass_delete_guard"):
        return False
    n_remote = total if isinstance(total, int) else len(total)
    if not n_remote:
        return False
    max_min = ctx.opts.get("max_delete_min", 20)
    max_ratio = ctx.opts.get("max_delete_ratio", 0.5)
    return len(gone) >= max_min and len(gone) / n_remote > max_ratio


def _save_synced(local_path, remote_path, remote_info, ctx):
    try:
        st = os.stat(local_path)
        sha = _ps._local_sha1(local_path)
    except OSError:
        return
    # Local SHA-1, not the listing from before the upload. The remote copy
    # just received these bytes; storing the old claimed SHA-1 would look
    # like both sides changed on the next pass.
    claimed_size = st.st_size
    claimed_mtime = float(st.st_mtime)
    node_id = None
    if isinstance(remote_info, dict):
        node_id = remote_info.get("node_id")
    ctx.db.upsert({
        "local_path": local_path,
        "remote_path": remote_path,
        "remote_node_id": node_id,
        "sha1": sha,
        "claimed_size": claimed_size,
        "claimed_mtime": claimed_mtime,
        "local_inode": st.st_ino,
        "local_mtime": st.st_mtime,
        "state": "synced",
    })


def _mark_conflict(local_path, remote_path, remote_info, row, ctx):
    """The row remembers the Proton version that was saved as the copy.

    A later pass compares the listing with it. Unchanged means the person's
    choice can be sent. Changed means Proton moved on and gets a new copy.
    """
    info = remote_info if isinstance(remote_info, dict) else {}
    try:
        st = os.stat(local_path)
        inode, mtime = st.st_ino, st.st_mtime
    except OSError:
        inode, mtime = None, None
    ctx.db.upsert({
        "local_path": local_path,
        "remote_path": remote_path,
        "remote_node_id": info.get("node_id") or (row or {}).get("remote_node_id"),
        "sha1": _ps._optional_sha1(info.get("sha1")),
        "claimed_size": _ps._claimed_size_of(info),
        "claimed_mtime": _ps._remote_mtime_seconds(info.get("mtime")),
        "local_inode": inode,
        "local_mtime": mtime,
        "state": "conflict",
    })


def _is_web_marker(local_path):
    """True when this file is the stand-in for a Proton document."""
    try:
        if os.path.getsize(local_path) > 4096:
            return False
        with open(local_path, "r", encoding="utf-8", errors="replace") as handle:
            line = handle.readline(200)
    except OSError:
        return False
    return line.startswith("Proton web document")


def _write_web_marker(local_path):
    """Write the stand-in, or leave a real file that already uses this name."""
    if os.path.isdir(local_path):
        return False
    if os.path.isfile(local_path):
        return _is_web_marker(local_path)
    parent = os.path.dirname(local_path) or "."
    os.makedirs(parent, exist_ok=True)
    temporary = os.path.join(parent, ".proton-sync-marker-tmp")
    text = (
        WEB_DOCUMENT_LINE + "\n"
        "The Proton CLI has no file body for a document or a spreadsheet.\n"
    )
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, local_path)
    except OSError:
        try:
            os.remove(temporary)
        except OSError:
            pass
        return False
    return True


def _mark_document(local_path, remote_path, remote_info, ctx):
    print("[download-skipped] " + _(
        "Proton document or spreadsheet left unchanged: {p}").format(p=remote_path))
    if ctx.dry_run:
        return
    if os.path.isfile(local_path) and not _is_web_marker(local_path):
        return
    if not _write_web_marker(local_path):
        claimed_size = None
        sha = None
        claimed_mtime = None
        node_id = None
        if isinstance(remote_info, dict):
            claimed_size = remote_info.get("claimed_size")
            sha = remote_info.get("sha1")
            claimed_mtime = _ps._remote_mtime_seconds(remote_info.get("mtime"))
            node_id = remote_info.get("node_id")
        ctx.db.upsert({
            "local_path": local_path,
            "remote_path": remote_path,
            "remote_node_id": node_id,
            "sha1": sha,
            "claimed_size": claimed_size,
            "claimed_mtime": claimed_mtime,
            "local_inode": None,
            "local_mtime": None,
            "state": "error",
        })
        return
    _save_synced(local_path, remote_path, remote_info, ctx)


def _set_state(row, state, ctx):
    if ctx.dry_run or not row:
        return
    stored = dict(row)
    stored["state"] = state
    ctx.db.upsert(stored)


def _holding_user():
    try:
        import getpass
        name = getpass.getuser()
    except Exception:
        name = ""
    return re.sub(r"[^A-Za-z0-9._-]", "_", name) or "_"


def _holding_destination(ctx, local_path):
    try:
        rel = os.path.relpath(local_path, ctx.source_root)
    except ValueError:
        rel = os.path.basename(local_path)
    if rel.startswith(".."):
        rel = os.path.basename(local_path)
    return os.path.join(
        ctx.source_root, ".proton-sync-holding-" + _holding_user(), rel)


def _conflict_path(local_path):
    """A conflict-copy name that is not in use yet."""
    directory = os.path.dirname(local_path)
    stem, ext = os.path.splitext(os.path.basename(local_path))
    candidate = os.path.join(directory, stem + _CONFLICT_MARK + ext)
    number = 2
    while os.path.lexists(candidate):
        candidate = os.path.join(
            directory, "{s} (proton conflict {n}){e}".format(s=stem, n=number, e=ext))
        number += 1
    return candidate


def _is_conflict_copy(name):
    stem, _ext = os.path.splitext(name)
    return bool(_CONFLICT_STEM.search(stem))


def _conflict_copies(local_path):
    """Conflict copies of this file that are still in its folder."""
    directory = os.path.dirname(local_path) or "."
    stem, ext = os.path.splitext(os.path.basename(local_path))
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    found = []
    for name in names:
        other_stem, other_ext = os.path.splitext(name)
        if other_ext != ext or not _is_conflict_copy(name):
            continue
        if _CONFLICT_STEM.sub("", other_stem) == stem:
            found.append(os.path.join(directory, name))
    return sorted(found)

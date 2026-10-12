"""File-manager status read from the sync database.

libcloudproviders shows one status per account folder: invalid (0), idle (1),
syncing (2), or error (3). It does not draw a per-file emblem. ``file_status``
still names the sync-database state of one path, so a conflict file is
distinguishable from a synced file. ``directory_status`` maps the same states
to theme emblem names for Dolphin and Nautilus. This module does not call the
Proton CLI and does not change local or remote files.
"""

import os

try:
    from i18n import _
except ImportError:
    def _(text):
        return text

from syncdb import STATES


STATUS_INVALID = 0
STATUS_IDLE = 1
STATUS_SYNCING = 2
STATUS_ERROR = 3

# Theme emblem names (Adwaita and Breeze). An empty string means no mark.
# Dolphin maps these same three names onto its built-in overlays.
EMBLEM_SYNCED = "emblem-default"
EMBLEM_TRANSFERRING = "emblem-synchronizing"
EMBLEM_PROBLEM = "emblem-important"
EMBLEM_UNSYNCED = "emblem-new"
EMBLEMS = {
    "synced": EMBLEM_SYNCED,
    "pending-up": EMBLEM_TRANSFERRING,
    "pending-down": EMBLEM_TRANSFERRING,
    "conflict": EMBLEM_PROBLEM,
    "error": EMBLEM_PROBLEM,
    "unsynced": EMBLEM_UNSYNCED,
}

BUS_NAME = "org.protondrivesync.CloudProviders"
OBJECT_PATH = "/org/protondrivesync/CloudProviders"
ACCOUNT_INTERFACE = "org.freedesktop.CloudProviders.Account"
PROVIDER_INTERFACE = "org.freedesktop.CloudProviders.Provider"
FILE_STATUS_INTERFACE = "org.protondrivesync.FileStatus"
def provider_name():
    """Name Nautilus shows for this unofficial account."""
    return _("Proton Drive Sync")


def emblem_for(state):
    """Theme emblem for a sync-database state, or "" when the file stays unmarked."""
    if not isinstance(state, str):
        return ""
    return EMBLEMS.get(state, "")


def file_status(db, local_path):
    """Sync-database state of ``local_path``, or ``unknown`` when it has no row."""
    if not isinstance(local_path, str) or not local_path:
        return "unknown"
    row = db.get(local_path)
    if row is None:
        normalized = os.path.normpath(local_path)
        if normalized != local_path:
            row = db.get(normalized)
    if row is None:
        real = _resolved(local_path)
        if real:
            row = db.get(real)
    if row is None:
        return "unknown"
    state = row.get("state")
    if state not in STATES:
        return "unknown"
    return state


def rows_under(rows, source):
    """Rows whose local path is ``source`` or a file inside it."""
    if not isinstance(source, str) or not source:
        return []
    root = os.path.normpath(source)
    prefix = root + os.sep
    found = []
    for row in rows:
        path = row.get("local_path") if isinstance(row, dict) else None
        if not isinstance(path, str) or not path:
            continue
        normalized = os.path.normpath(path)
        if normalized == root or normalized.startswith(prefix):
            found.append(row)
    return found


def _is_immediate(normalized, root, prefix):
    """True for ``root`` itself or a direct child, not a nested path."""
    if normalized == root:
        return True
    if not normalized.startswith(prefix):
        return False
    return os.sep not in normalized[len(prefix):]


def _resolved(path):
    """The real path when ``path`` goes through a link, else None.

    ``~/Proton Drive`` shows each synced folder as a link; the database
    holds the real paths.
    """
    if not isinstance(path, str) or not path:
        return None
    real = os.path.realpath(path)
    return real if real != os.path.normpath(path) else None


def path_emblem(db, local_path):
    """Emblem for one path, also when it is reached through a link.

    A file uses its own row. A directory with no row uses the strongest
    mark among the rows inside it, so the mapped folder itself can show a
    status. No row and no child row means no mark.
    """
    found = _path_emblem(db, local_path)
    if found:
        return found
    real = _resolved(local_path)
    return _path_emblem(db, real) if real else ""


def _path_emblem(db, local_path):
    state = file_status(db, local_path)
    own = emblem_for(state)
    if own:
        return own
    if not isinstance(local_path, str) or not local_path:
        return ""
    root = os.path.normpath(local_path)
    prefix = root + os.sep
    rank = {
        EMBLEM_SYNCED: 1,
        EMBLEM_UNSYNCED: 2,
        EMBLEM_TRANSFERRING: 3,
        EMBLEM_PROBLEM: 4,
    }
    best = ""
    for row in db.rows():
        path = row.get("local_path") if isinstance(row, dict) else None
        if not isinstance(path, str) or not path:
            continue
        normalized = os.path.normpath(path)
        if normalized != root and not normalized.startswith(prefix):
            continue
        mark = emblem_for(row.get("state"))
        if rank.get(mark, 0) > rank.get(best, 0):
            best = mark
    return best


def directory_status(db, directory):
    """Emblem name for each immediate child row of ``directory``.

    Keys are normalized absolute paths under ``directory`` as asked, also
    when it is reached through a link. A row with no emblem is omitted so
    clients show no mark. Nested files wait until that folder is opened.
    """
    found = _directory_status(db, directory)
    real = None if found else _resolved(directory)
    if not real:
        return found
    asked = os.path.normpath(directory)
    return {os.path.join(asked, os.path.basename(path)): emblem
            for path, emblem in _directory_status(db, real).items()}


def _directory_status(db, directory):
    if not isinstance(directory, str) or not directory:
        return {}
    root = os.path.normpath(directory)
    prefix = root + os.sep
    found = {}
    for row in db.rows():
        path = row.get("local_path") if isinstance(row, dict) else None
        if not isinstance(path, str) or not path:
            continue
        normalized = os.path.normpath(path)
        if not _is_immediate(normalized, root, prefix):
            continue
        emblem = emblem_for(row.get("state"))
        if emblem:
            found[normalized] = emblem
    return found


def account_status(rows):
    """CloudProviders status code and a stable count string for these rows.

    A conflict or an error is status 3. A pending transfer or a file not yet
    synced, with no conflict, is status 2. Anything else, including an empty
    folder, is idle.
    """
    counts = {state: 0 for state in STATES}
    for row in rows or []:
        state = row.get("state") if isinstance(row, dict) else None
        if state in counts:
            counts[state] += 1
    if counts["conflict"] or counts["error"]:
        code = STATUS_ERROR
    elif counts["pending-up"] or counts["pending-down"] or counts["unsynced"]:
        code = STATUS_SYNCING
    else:
        code = STATUS_IDLE
    details = " ".join(
        "{0}={1}".format(state, counts[state]) for state in STATES
    )
    return code, details

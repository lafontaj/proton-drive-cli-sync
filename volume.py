"""A local folder that is the Proton account, plus its Dolphin place.

The engine still reaches Drive only through the official CLI. This module
writes the mapping, the sidebar entry, and the not-yet-synced row. It does
not upload or download.
"""

import json
import os
from urllib.parse import quote

import syncdb

MY_FILES = "/my-files"
PLACE_ID = "proton-drive-sync-volume"
PLACE_TITLE = "Proton Drive"


def default_local_dir():
    return os.path.expanduser("~/Proton Drive")


def directory_has_entries(path):
    """True when ``path`` exists and contains anything."""
    if not os.path.isdir(path):
        return False
    with os.scandir(path) as entries:
        return any(True for _entry in entries)


def ensure_empty_directory(path):
    """Create ``path`` only when it is missing or already empty.

    A file at that path, or a directory that already has children, is refused
    so an existing tree is not treated as the account.
    """
    if os.path.exists(path) and not os.path.isdir(path):
        raise ValueError("not a directory")
    if directory_has_entries(path):
        raise ValueError("not empty")
    os.makedirs(path, exist_ok=True)


def mapping_for(local_dir):
    """Two-way volume mapping. Deletion stays off."""
    return {
        "type": "folder",
        "source": os.path.abspath(local_dir),
        "dest_parent": MY_FILES,
        "direction": "twoway",
        "volume": True,
        "allow_delete": False,
        "shared_delete_confirmed": False,
    }


def _tracked(mapping):
    """A volume, or the one mapping chosen as the Proton Drive place."""
    if not isinstance(mapping, dict) or mapping.get("direction") != "twoway":
        return False
    return mapping.get("volume") is True or mapping.get("live") is True


def _volume_mapping(mappings, local_path):
    path = os.path.normpath(local_path)
    for mapping in mappings or []:
        if not _tracked(mapping):
            continue
        root = os.path.normpath(mapping.get("source") or "")
        if not root:
            continue
        if path == root or path.startswith(root + os.sep):
            return mapping, root
    return None, None


def _remote_for(mapping, root, local_path):
    rel = os.path.relpath(os.path.normpath(local_path), root)
    dest = (mapping.get("dest_parent") or MY_FILES).rstrip("/")
    if mapping.get("volume") is not True:
        dest = dest + "/" + os.path.basename(root.rstrip("/"))
    if rel in (".", ""):
        return dest
    return dest + "/" + rel.replace(os.sep, "/")


def _live_row(mappings):
    for row in mappings or []:
        if (isinstance(row, dict) and row.get("live") is True
                and row.get("type", "folder") == "folder" and row.get("source")):
            return row
    return None


def confirmed_live(mappings):
    """The folder the person chose in the window, or None.

    Nothing is picked on their behalf. A ``live`` row without
    ``live_confirmed`` was written by an earlier build on its own and is not
    resumed.
    """
    row = _live_row(mappings)
    if row is not None and row.get("live_confirmed") is True:
        return row
    return None


def unconfirmed_live(mappings):
    """A ``live`` row nobody confirmed (left by an earlier build), or None."""
    row = _live_row(mappings)
    if row is not None and row.get("live_confirmed") is not True:
        return row
    return None


def mark_live(mappings, source, allow_delete=None):
    """The Proton Drive place opens this one folder. Other rows lose ``live``.

    Called only after the person confirmed. The chosen folder becomes
    two-way. ``allow_delete`` is their answer: True sends files deleted here
    to the Proton trash, False keeps them on Proton, None leaves the row as
    it is.
    """
    wanted = os.path.normpath(source)
    chosen = None
    for row in mappings or []:
        if not isinstance(row, dict):
            continue
        src = os.path.normpath(row.get("source") or "")
        if row.get("type", "folder") == "folder" and src == wanted:
            row["live"] = True
            row["live_confirmed"] = True
            if row.get("direction") != "twoway":
                row["direction"] = "twoway"
            if allow_delete is True:
                row["allow_delete"] = True
                row["delete_mode"] = "trash"
            elif allow_delete is False:
                row["allow_delete"] = False
            chosen = row
        else:
            row.pop("live", None)
            row.pop("live_confirmed", None)
    return chosen


def clear_live(mappings, allow_delete=None):
    """No folder is live any more. Returns the row that was, or None.

    The row keeps its direction. ``allow_delete=False`` also turns deletion
    off on it.
    """
    was = _live_row(mappings)
    for row in mappings or []:
        if isinstance(row, dict):
            row.pop("live", None)
            row.pop("live_confirmed", None)
    if was is not None and allow_delete is False:
        was["allow_delete"] = False
    return was


def note_local_change(config_path, mappings, local_path):
    """Mark one changed file inside a volume as not yet synced.

    A conflict or an error row is left as it is. A directory path is ignored.
    """
    if not isinstance(local_path, str) or not local_path:
        return False
    if os.path.isdir(local_path):
        return False
    found, root = _volume_mapping(mappings, local_path)
    if found is None:
        return False
    remote = _remote_for(found, root, local_path)
    database = syncdb.database_path(config_path)
    with syncdb.SyncDB(database) as db:
        row = db.get(os.path.normpath(local_path))
        if row and row.get("state") in ("conflict", "error"):
            return False
        if row:
            stored = dict(row)
            stored["state"] = "unsynced"
            stored["remote_path"] = remote
            db.upsert(stored)
            return True
        db.upsert({
            "local_path": os.path.normpath(local_path),
            "remote_path": remote,
            "remote_node_id": None,
            "sha1": None,
            "claimed_size": None,
            "claimed_mtime": None,
            "local_inode": None,
            "local_mtime": None,
            "state": "unsynced",
        })
    return True


def _file_uri(local_dir):
    path = os.path.abspath(local_dir).replace("\\", "/")
    if not path.startswith("/"):
        path = "/" + path
    return "file://" + quote(path, safe="/")


def _bookmark(local_dir):
    href = _file_uri(local_dir)
    return (
        " <bookmark href=\"{href}\">\n"
        "  <title>{title}</title>\n"
        "  <info>\n"
        "   <metadata owner=\"http://freedesktop.org\">\n"
        "    <bookmark:icon name=\"drive-harddisk\"/>\n"
        "   </metadata>\n"
        "   <metadata owner=\"http://www.kde.org\">\n"
        "    <ID>{ident}</ID>\n"
        "    <IsHidden>false</IsHidden>\n"
        "    <isSystemItem>false</isSystemItem>\n"
        "    <icon name=\"drive-harddisk\"/>\n"
        "   </metadata>\n"
        "  </info>\n"
        " </bookmark>\n"
    ).format(href=href, title=PLACE_TITLE, ident=PLACE_ID)


def _inside(path, root):
    """True when ``path`` is ``root`` or somewhere under it."""
    path = os.path.normcase(os.path.abspath(path))
    root = os.path.normcase(os.path.abspath(root)).rstrip(os.sep)
    return path == root or path.startswith(root + os.sep)


def two_way_folders(mappings):
    """Local folders of the two-way mappings, in file order (volumes left out)."""
    found = []
    for mapping in mappings or []:
        if not isinstance(mapping, dict) or mapping.get("volume") is True:
            continue
        if mapping.get("direction") != "twoway" or mapping.get("type", "folder") != "folder":
            continue
        source = mapping.get("source")
        if isinstance(source, str) and source:
            found.append(os.path.abspath(source))
    return found


def _links_record():
    import config
    return os.path.join(config.DATA_DIR, "drive-links.json")


def _read_links(record):
    try:
        with open(record, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    return [p for p in data if isinstance(p, str)] if isinstance(data, list) else []


def ensure_drive_root(mappings, root=None, record=None):
    """Make ``~/Proton Drive`` a folder that shows every two-way folder.

    Like My files on the website, the root holds the synced folders: each
    two-way folder appears inside it as a link named after the folder. A
    link left by an earlier version (``~/Proton Drive`` pointing at one
    folder) is replaced by the real folder; only the link is removed.

    Links this function made are listed in ``record``; a link whose folder
    is no longer two-way is removed, and nothing else in the root is
    touched. When the root is itself synced (a volume, or inside a mapped
    folder), no link is added, because the links would be uploaded.

    Returns the root path. Raises ValueError when the root is a file.
    """
    root = os.path.abspath(root or default_local_dir())
    record = record or _links_record()
    for mapping in mappings or []:
        source = mapping.get("source") if isinstance(mapping, dict) else None
        if isinstance(source, str) and source and _inside(root, source):
            return root
    if os.path.islink(root):
        os.remove(root)
    if os.path.lexists(root) and not os.path.isdir(root):
        raise ValueError("not a directory")
    os.makedirs(root, exist_ok=True)

    wanted = [s for s in two_way_folders(mappings) if not _inside(s, root)]
    ours = []
    for link in _read_links(record):
        if not _inside(link, root) or not os.path.islink(link):
            continue
        target = os.path.abspath(os.path.join(root, os.readlink(link)))
        if target in wanted:
            ours.append(link)
        else:
            os.remove(link)
    shown = {os.path.abspath(os.path.join(root, os.readlink(link))) for link in ours}
    for source in wanted:
        if source in shown:
            continue
        name = os.path.basename(source.rstrip(os.sep)) or "Folder"
        link, n = os.path.join(root, name), 2
        while os.path.lexists(link):
            link = os.path.join(root, "{name} ({n})".format(name=name, n=n))
            n += 1
        os.symlink(source, link)
        ours.append(link)
        shown.add(source)
    parent = os.path.dirname(record)
    if parent:
        os.makedirs(parent, exist_ok=True)
    _replace(record, json.dumps(sorted(ours), indent=1) + "\n")
    return root


def places_path():
    return os.path.expanduser("~/.local/share/user-places.xbel")


def ensure_dolphin_place(local_dir, path=None, add=True):
    """Insert the Proton Drive place. Other bookmarks in the file stay.

    With ``add=False`` an existing place is pointed at ``local_dir`` and a
    missing one (never made, or removed by the person) is not added.
    """
    target = path or places_path()
    if not add and not os.path.exists(target):
        return False
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    bookmark = _bookmark(local_dir)
    if not os.path.exists(target):
        text = (
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<!DOCTYPE xbel>\n"
            "<xbel xmlns:bookmark=\"http://www.freedesktop.org/standards/desktop-bookmarks\""
            " xmlns:mime=\"http://www.freedesktop.org/standards/shared-mime-info\""
            " xmlns:kdepriv=\"http://www.kde.org/kdepriv\" version=\"1.0\">\n"
            + bookmark
            + "</xbel>\n"
        )
        _replace(target, text)
        return True
    with open(target, "r", encoding="utf-8") as handle:
        text = handle.read()
    if PLACE_ID in text:
        href = _file_uri(local_dir)
        start = text.find(PLACE_ID)
        open_at = text.rfind("<bookmark ", 0, start)
        if open_at < 0:
            return True
        href_at = text.find("href=\"", open_at)
        end_quote = text.find("\"", href_at + 6) if href_at >= 0 else -1
        if href_at < 0 or href_at > start or end_quote < 0:
            return True
        text = text[:href_at + 6] + href + text[end_quote:]
        _replace(target, text)
        return True
    close = text.rfind("</xbel>")
    if close < 0 or not add:
        return False
    text = text[:close] + bookmark + text[close:]
    _replace(target, text)
    return True


def _replace(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)


def start_watcher(mappings_path):
    """Install and start the local watcher and the consumer."""
    import realtime_manager
    result = realtime_manager.install_or_update_units(mappings_path, enable=True)
    ok = bool(result[0]) if result else False
    message = result[1] if result and len(result) > 1 else ""
    return ok, message

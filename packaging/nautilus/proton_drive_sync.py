"""Per-file emblems for Nautilus, read from the status bus.

A missing bus name or a D-Bus error leaves the file unmarked. This file does
not import the sync engine and does not change files. Emblem names come from
GetDirectoryStatus, which uses cloudstatus.EMBLEMS.
"""

import os
import time

from gi.repository import GObject, Nautilus

BUS_NAME = "org.protondrivesync.CloudProviders"
OBJECT_PATH = "/org/protondrivesync/CloudProviders"
FILE_STATUS_INTERFACE = "org.protondrivesync.FileStatus"
_CACHE_SECONDS = 2.0


class ProtonDriveSyncInfoProvider(GObject.GObject, Nautilus.InfoProvider):
    """Theme emblems for files that have a sync-database row."""

    def __init__(self):
        super().__init__()
        self._cache = {}

    def update_file_info(self, file_info):
        self._apply(file_info)

    def update_file_info_full(self, provider, handle, closure, file_info):
        self._apply(file_info)
        Nautilus.info_provider_update_complete_invoke(
            closure, provider, handle, Nautilus.OperationResult.COMPLETE)

    def _apply(self, file_info):
        try:
            if file_info.get_uri_scheme() != "file":
                return
            location = file_info.get_location()
            path = location.get_path() if location is not None else None
        except (AttributeError, TypeError):
            return
        if not path:
            return
        emblem = self._lookup(path)
        if emblem:
            file_info.add_emblem(emblem)

    def _lookup(self, path):
        directory = os.path.dirname(os.path.normpath(path))
        if not directory:
            return ""
        now = time.monotonic()
        cached = self._cache.get(directory)
        if cached is None or now - cached[0] > _CACHE_SECONDS:
            self._cache[directory] = (now, _directory_emblems(directory))
            cached = self._cache[directory]
        return cached[1].get(os.path.normpath(path), "")


def _directory_emblems(directory):
    try:
        import dbus
    except ImportError:
        return {}
    try:
        bus = dbus.SessionBus()
        remote = bus.get_object(BUS_NAME, OBJECT_PATH)
        iface = dbus.Interface(remote, FILE_STATUS_INTERFACE)
        found = iface.GetDirectoryStatus(directory)
    except (dbus.DBusException, AttributeError, TypeError, ValueError):
        return {}
    emblems = {}
    for key, value in found.items():
        text = str(value)
        if text:
            emblems[os.path.normpath(str(key))] = text
    return emblems

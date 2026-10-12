"""Session-bus status for file managers, read from the sync database.

The process owns ``org.protondrivesync.CloudProviders`` and exports the
libcloudproviders account interface (one status per mapping folder) plus
``GetFileStatus`` and ``GetDirectoryStatus``. It does not call the Proton CLI.
Nautilus discovers the name from ``packaging/proton-drive-sync-cloud.desktop``
while this process is running. Dolphin does not read this bus itself; its
plugin calls ``GetDirectoryStatus``.
"""

import argparse
import json
import os
import sys

import cloudstatus
import syncdb


def _db_mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def load_mapping_sources(path):
    """Local sources, in file order. Accepts a list or an object with mappings."""
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = data.get("mappings") or []
    else:
        raise ValueError("mappings file must be a list or an object")
    sources = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        source = row.get("source")
        if isinstance(source, str) and source.strip():
            sources.append(source.strip())
    return sources


def serve(mappings_file, data_dir=None):
    """Export provider and account objects until the process is killed."""
    import dbus
    import dbus.service
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib

    sources = load_mapping_sources(mappings_file)
    database = syncdb.database_path(mappings_file, data_dir)

    def open_db():
        return syncdb.SyncDB(database)

    class Account(dbus.service.Object):
        def __init__(self, bus, object_path, source):
            dbus.service.Object.__init__(self, bus, object_path)
            self.source = source
            self.object_path = object_path

        def properties(self):
            with open_db() as db:
                code, details = cloudstatus.account_status(
                    cloudstatus.rows_under(db.rows(), self.source))
            return {
                "Name": dbus.String(cloudstatus.provider_name()),
                "Path": dbus.String(self.source),
                "Icon": dbus.String(""),
                "Status": dbus.Int32(code),
                "StatusDetails": dbus.String(details),
            }

        @dbus.service.method(
            dbus.PROPERTIES_IFACE, in_signature="ss", out_signature="v")
        def Get(self, interface, name):
            props = self.properties()
            if name not in props:
                raise dbus.exceptions.DBusException(
                    "org.freedesktop.DBus.Error.UnknownProperty")
            return props[name]

        @dbus.service.method(
            dbus.PROPERTIES_IFACE, in_signature="s", out_signature="a{sv}")
        def GetAll(self, interface):
            return dbus.Dictionary(self.properties(), signature="sv")

    class Provider(dbus.service.Object):
        def __init__(self, bus, accounts):
            dbus.service.Object.__init__(self, bus, cloudstatus.OBJECT_PATH)
            self.accounts = accounts

        def _managed(self):
            objects = dbus.Dictionary(signature="oa{sa{sv}}")
            provider_props = dbus.Dictionary({
                "Name": dbus.String(cloudstatus.provider_name()),
            }, signature="sv")
            objects[dbus.ObjectPath(cloudstatus.OBJECT_PATH)] = dbus.Dictionary({
                cloudstatus.PROVIDER_INTERFACE: provider_props,
                cloudstatus.FILE_STATUS_INTERFACE: dbus.Dictionary(
                    signature="sv"),
            }, signature="sa{sv}")
            for account in self.accounts:
                objects[dbus.ObjectPath(account.object_path)] = dbus.Dictionary({
                    cloudstatus.ACCOUNT_INTERFACE: dbus.Dictionary(
                        account.properties(), signature="sv"),
                }, signature="sa{sv}")
            return objects

        @dbus.service.method(
            "org.freedesktop.DBus.ObjectManager",
            out_signature="a{oa{sa{sv}}}")
        def GetManagedObjects(self):
            return self._managed()

        @dbus.service.method(
            cloudstatus.FILE_STATUS_INTERFACE, in_signature="s", out_signature="s")
        def GetFileStatus(self, local_path):
            with open_db() as db:
                return cloudstatus.file_status(db, str(local_path))

        @dbus.service.method(
            cloudstatus.FILE_STATUS_INTERFACE, in_signature="s", out_signature="a{ss}")
        def GetDirectoryStatus(self, directory):
            with open_db() as db:
                found = cloudstatus.directory_status(db, str(directory))
            return dbus.Dictionary(found, signature="ss")

        @dbus.service.method(
            cloudstatus.FILE_STATUS_INTERFACE, in_signature="s", out_signature="s")
        def GetPathEmblem(self, local_path):
            with open_db() as db:
                return cloudstatus.path_emblem(db, str(local_path))

        @dbus.service.method(
            dbus.PROPERTIES_IFACE, in_signature="ss", out_signature="v")
        def Get(self, interface, name):
            if name != "Name":
                raise dbus.exceptions.DBusException(
                    "org.freedesktop.DBus.Error.UnknownProperty")
            return dbus.String(cloudstatus.provider_name())

        @dbus.service.method(
            dbus.PROPERTIES_IFACE, in_signature="s", out_signature="a{sv}")
        def GetAll(self, interface):
            return dbus.Dictionary(
                {"Name": dbus.String(cloudstatus.provider_name())}, signature="sv")

        @dbus.service.signal(cloudstatus.FILE_STATUS_INTERFACE, signature="")
        def StatusChanged(self):
            """The sync database changed. File managers should ask again."""

    DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    # The name is released when this object is garbage-collected.
    name_owner = dbus.service.BusName(
        cloudstatus.BUS_NAME, bus, do_not_queue=True)
    accounts = []
    for index, source in enumerate(sources):
        path = "{0}/Account/{1}".format(cloudstatus.OBJECT_PATH, index)
        accounts.append(Account(bus, path, source))
    provider = Provider(bus, accounts)
    provider.name_owner = name_owner
    seen = {"mtime": _db_mtime(database)}

    def _watch_database():
        current = _db_mtime(database)
        if current != seen["mtime"]:
            seen["mtime"] = current
            provider.StatusChanged()
        return True

    GLib.timeout_add_seconds(2, _watch_database)
    GLib.MainLoop().run()


def main(argv):
    parser = argparse.ArgumentParser(
        description="Export sync-database status on the session bus.")
    parser.add_argument("--mappings", required=True)
    parser.add_argument("--data-dir", default=None)
    args = parser.parse_args(argv)
    if not os.path.isfile(args.mappings):
        print("mappings file not found", file=sys.stderr)
        return 1
    try:
        serve(args.mappings, args.data_dir)
    except ImportError:
        print(
            "cloud status needs python3-dbus and PyGObject", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

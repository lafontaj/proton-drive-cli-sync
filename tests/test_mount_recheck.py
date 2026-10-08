"""Mount re-check during a deletion pass.

A refused deletion is logged with the [delete-guard] tag and counted in
deletions_refused, which makes the pass end with code 5. The remote copies
stay put and the uploads continue.
"""

import proton_sync

REMOTE = "/my-files/Backups/Docs"


def test_mount_lost_mid_pass_stops_deletions(fake_drive, local_tree):
    folders = ("a", "b", "c")
    spec = {}
    for name in folders:
        spec["Docs/%s/old.txt" % name] = (b"old", 1_000_000_000)
    src = local_tree(spec)
    root = str(src / "Docs")
    # sync_folder remembers remote folders on the module. This test calls it
    # in-process; clear that set so a later in-process test does not skip
    # creating the same paths in a different fake Drive.
    proton_sync._REMOTE_KNOWN.clear()
    try:
        assert proton_sync.sync_folder(root, "/my-files/Backups", rename_ext=False)
        for name in folders:
            (src / "Docs" / name / "old.txt").unlink()
            local_tree.write("Docs/%s/new.txt" % name, b"new", 1_000_000_100)

        calls = []

        def guard():
            calls.append(1)
            return (True, "") if len(calls) == 1 else (False, "mount gone")

        opts = {"delete_guard": guard, "mount_lost": False}
        proton_sync._RUN.reset()
        complete = proton_sync.sync_folder(
            root, "/my-files/Backups", delete=True, rename_ext=False, delete_opts=opts)
        assert complete is False
        trashed = [n for n in folders if fake_drive.trashed(REMOTE + "/%s/old.txt" % n)]
        assert len(trashed) == 1
        assert opts["mount_lost"] is True
        assert proton_sync._RUN.deletions_refused == 1
        assert proton_sync._RUN.has_failures()
        for name in folders:   # uploads continue after the latch
            assert fake_drive.content(REMOTE + "/%s/new.txt" % name) == b"new"
    finally:
        proton_sync._REMOTE_KNOWN.clear()
        proton_sync._RUN.reset()

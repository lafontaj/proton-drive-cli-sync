"""A refused deletion leaves the row synced; it is never turned into a restore."""

import json
import os


def test_a_refused_deletion_is_not_turned_into_a_restore(
        fake_drive, local_tree, write_mappings, engine):
    src = local_tree({"Docs/a.txt": (b"A", 1_000_000_000)})
    mapping = {"type": "folder", "source": str(src / "Docs"),
               "dest_parent": "/my-files/E", "direction": "twoway",
               "allow_delete": True}
    cfg = write_mappings([], sync_mappings=[mapping])
    assert engine(cfg).returncode == 0
    os.remove(src / "Docs" / "a.txt")
    refused = engine(cfg, "--delete")      # no source_kind: the guard refuses
    assert "[delete-guard]" in refused.stdout, refused.stdout
    data = json.loads(open(cfg).read())
    data["sync_mappings"][0]["source_kind"] = "local"
    open(cfg, "w").write(json.dumps(data))
    allowed = engine(cfg, "--delete")
    assert fake_drive.trashed("/my-files/E/Docs/a.txt"), allowed.stdout
    assert not (src / "Docs" / "a.txt").exists()


def test_a_mass_delete_refusal_is_not_turned_into_a_restore(
        fake_drive, local_tree, write_mappings, engine):
    files = {"Docs/f%02d.txt" % i: (b"x%d" % i, 1_000_000_000)
             for i in range(30)}
    src = local_tree(files)
    engine.update_settings(mass_delete_guard=True)
    mapping = {"type": "folder", "source": str(src / "Docs"),
               "dest_parent": "/my-files/E", "direction": "twoway",
               "allow_delete": True, "source_kind": "local"}
    cfg = write_mappings([], sync_mappings=[mapping])
    assert engine(cfg).returncode == 0
    for i in range(30):
        os.remove(src / "Docs" / ("f%02d.txt" % i))
    refused = engine(cfg, "--delete")
    assert "[delete-guard]" in refused.stdout, refused.stdout
    assert not fake_drive.trashed("/my-files/E/Docs/f00.txt")
    assert not (src / "Docs" / "f00.txt").exists()
    allowed = engine(cfg, "--delete", "--allow-mass-delete")
    assert fake_drive.trashed("/my-files/E/Docs/f00.txt"), allowed.stdout
    assert not (src / "Docs" / "f00.txt").exists()

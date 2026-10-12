"""A full re-check lists each remote folder once, and a failed direct list falls back."""


def _mapping(source, **extra):
    mapping = {
        "type": "folder",
        "source": str(source),
        "dest_parent": "/my-files/Backups",
    }
    mapping.update(extra)
    return mapping


def _json_lists(calls):
    listed = []
    for call in calls:
        if len(call) >= 3 and call[0] == "filesystem" and call[1] == "list" and "-j" in call:
            listed.append(call[2])
    return listed


def _info_paths(calls):
    return [
        call[2] for call in calls
        if len(call) >= 3 and call[0] == "filesystem" and call[1] == "info"
    ]


def test_recheck_lists_each_folder_of_a_five_level_tree_once(
        fake_drive, local_tree, write_mappings, engine):
    src = local_tree({
        "Docs/a/b/c/d/e.txt": (b"deep", 1_000_000_000),
    })
    cfg = write_mappings([_mapping(src / "Docs")])
    first = engine(cfg)
    assert first.returncode == 0, first.stdout + first.stderr

    folders = [
        "/my-files/Backups/Docs",
        "/my-files/Backups/Docs/a",
        "/my-files/Backups/Docs/a/b",
        "/my-files/Backups/Docs/a/b/c",
        "/my-files/Backups/Docs/a/b/c/d",
    ]
    seen = len(fake_drive.calls())
    second = engine(cfg, "--ignore-cache")
    assert second.returncode == 0, second.stdout + second.stderr
    fresh = fake_drive.calls()[seen:]
    listed = _json_lists(fresh)
    for folder in folders:
        assert listed.count(folder) == 1, (folder, listed)
    assert _info_paths(fresh) == []


def test_failed_direct_list_falls_back_instead_of_skipping(
        fake_drive, local_tree, write_mappings, engine):
    src = local_tree({
        "Docs/a/b/c/d/e.txt": (b"deep", 1_000_000_000),
    })
    cfg = write_mappings([_mapping(src / "Docs")])
    first = engine(cfg)
    assert first.returncode == 0, first.stdout + first.stderr

    target = "/my-files/Backups/Docs/a/b/c/d"
    fake_drive.add_fault(cmd="list", match=target, times=1)
    seen = len(fake_drive.calls())
    second = engine(cfg, "--ignore-cache")
    assert second.returncode == 0, second.stdout + second.stderr
    assert "folder skipped" not in second.stdout
    assert fake_drive.content(target + "/e.txt") == b"deep"
    fresh = fake_drive.calls()[seen:]
    assert _json_lists(fresh).count(target) == 2
    assert target in _info_paths(fresh)

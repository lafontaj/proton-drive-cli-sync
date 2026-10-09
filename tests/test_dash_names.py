"""Names that start with a dash.

The engine passes bare names to the CLI (see `_cli_local_args`). Without `--`
the CLI reads `-1_-7.xwmc` as an option and refuses the upload; the same goes
for a folder named `-x` in `create-folder`. The fake CLI rejects unknown
options the way the real one does, so these tests fail without the fix.
"""


def _mapping(source):
    return {"type": "folder", "source": str(source), "dest_parent": "/my-files/Backups"}


def test_fake_cli_rejects_a_dash_name_without_double_dash(fake_drive, tmp_path):
    fake_drive.seed_folder("/my-files/Backups")
    (tmp_path / "-a.txt").write_bytes(b"x")
    res = fake_drive.run("filesystem", "upload", "-f", "replace", "-d", "merge",
                         "-a.txt", "/my-files/Backups", cwd=str(tmp_path))
    assert res.returncode == 1
    assert "Unknown option" in res.stderr
    res = fake_drive.run("filesystem", "upload", "-f", "replace", "-d", "merge", "--",
                         "-a.txt", "/my-files/Backups", cwd=str(tmp_path))
    assert res.returncode == 0, res.stderr
    assert fake_drive.content("/my-files/Backups/-a.txt") == b"x"


def test_files_and_folders_starting_with_a_dash_are_sent(
        fake_drive, local_tree, write_mappings, engine):
    src = local_tree({
        "Docs/cache_1/-1_-7.xwmc": (b"one", 1_000_000_000),
        "Docs/-dossier/-f.txt": (b"two", 1_000_000_000),
        "Docs/normal.txt": (b"three", 1_000_000_000),
    })
    result = engine(write_mappings([_mapping(src / "Docs")]))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Unknown option" not in result.stdout + result.stderr
    assert fake_drive.content("/my-files/Backups/Docs/cache_1/-1_-7.xwmc") == b"one"
    assert fake_drive.content("/my-files/Backups/Docs/-dossier/-f.txt") == b"two"
    assert fake_drive.content("/my-files/Backups/Docs/normal.txt") == b"three"


def test_thumbnail_retry_keeps_skip_thumbnails_before_double_dash(
        fake_drive, local_tree, write_mappings, engine):
    """The retry without thumbnail must put the option BEFORE `--`.

    After `--` the CLI would take `--skip-thumbnails` for a file name: the
    retry would fail and the file would never be sent.
    """
    src = local_tree({"Docs/-photo.tif": (b"image", 1_000_000_000)})
    # The batch and the per-file retry fail on the thumbnail; the third try,
    # without thumbnail, goes through.
    fake_drive.add_fault(cmd="upload", match="-photo.tif", times=2,
                         stderr="ValidationError: Failed to generate thumbnails")
    result = engine(write_mappings([_mapping(src / "Docs")]))
    assert result.returncode == 0, result.stdout + result.stderr
    assert fake_drive.content("/my-files/Backups/Docs/-photo.tif") == b"image"
    last = [c["argv"] for c in fake_drive.upload_cwds()][-1]
    assert "--skip-thumbnails" in last
    assert last.index("--skip-thumbnails") < last.index("--")

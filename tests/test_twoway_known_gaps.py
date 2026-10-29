"""Two known gaps of the two-way reconcile, written as the behaviour we want.

The rename gap is strict xfail: it fails today and must start passing when
node-id rename handling (file renamed on Proton) lands. Remove the marker in
the PR that fixes it. The folder gap was fixed by the folder baseline.
"""

import os

import pytest

import remote_state


@pytest.fixture
def engine(engine):
    """Calling the engine reconciles the sync_mappings entries."""
    class _Reconciling:
        def __call__(self, cfg, *args):
            return engine.reconcile(cfg, *args)
    return _Reconciling()


def _mapping(source, dest, **extra):
    m = {"type": "folder", "source": str(source), "dest_parent": dest}
    m.update(extra)
    return m


def _remote_nodes(fake_drive):
    holder = {}

    def look(state):
        holder["nodes"] = set(state["nodes"].keys())
    fake_drive._update(look)
    return holder["nodes"]


def test_folder_deleted_on_proton_stays_deleted(fake_drive, local_tree,
                                                write_mappings, engine):
    src = local_tree({"Docs/sub/b.txt": (b"BBBB", 1_000_000_000),
                      "Docs/a.txt": (b"A", 1_000_000_000)})
    cfg = write_mappings([_mapping(src / "Docs", "/my-files/Backups",
                                   direction="twoway", allow_delete=True)])
    assert engine(cfg).returncode == 0

    sub = remote_state.normalize("/my-files/Backups/Docs/sub")
    child = remote_state.normalize("/my-files/Backups/Docs/sub/b.txt")

    def delete_on_proton(state):
        state["nodes"].pop(child, None)
        state["nodes"].pop(sub, None)
    fake_drive._update(delete_on_proton)

    assert engine(cfg, "--delete").returncode == 0
    # The folder stays gone on Proton and is removed here once its files are held.
    assert sub not in _remote_nodes(fake_drive)
    assert not (src / "Docs" / "sub").exists()
    # The file itself is kept aside, never destroyed (holding).
    assert (src / "Docs" / "a.txt").exists()


@pytest.mark.xfail(strict=True, reason="renames are not recognised yet: the "
                   "new name is downloaded in full")
def test_file_renamed_on_proton_is_renamed_here(fake_drive, local_tree,
                                                write_mappings, engine):
    src = local_tree({"Docs/a.txt": (b"AAAA", 1_000_000_000)})
    cfg = write_mappings([_mapping(src / "Docs", "/my-files/Backups",
                                   direction="twoway", allow_delete=True)])
    assert engine(cfg).returncode == 0

    old = remote_state.normalize("/my-files/Backups/Docs/a.txt")
    new = remote_state.normalize("/my-files/Backups/Docs/renamed.txt")

    def rename_on_proton(state):
        state["nodes"][new] = state["nodes"].pop(old)
    fake_drive._update(rename_on_proton)

    assert engine(cfg, "--delete").returncode == 0
    assert sorted(os.listdir(src / "Docs")) == ["renamed.txt"]
    assert (src / "Docs" / "renamed.txt").read_bytes() == b"AAAA"
    # Today: the renamed file is fetched again from Proton.
    downloads = [c for c in fake_drive.calls() if c[:2] == ["filesystem", "download"]]
    assert downloads == []

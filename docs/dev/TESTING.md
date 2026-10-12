# Testing

The engine is tested without a Proton account. A fake `proton-drive` executable
stands in for the real CLI. Run the suite on Linux. The engine needs `fcntl`,
so a result from Windows Python does not count. On Windows, WSL is one way to
get a Linux shell.

## Run

From the repository root, on Linux:

```bash
bash scripts/test.sh
bash scripts/test.sh -k baseline -vv
```

From PowerShell: `wsl -- bash scripts/test.sh`.

`scripts/test.sh` refuses to run unless `uname -s` is Linux. The first run
creates `.venv-wsl/` (gitignored) and installs `requirements-dev.txt`. Later
runs reinstall only when that file is newer than the stamp. Extra arguments
are passed to pytest.

The default pytest options skip tests marked `slow` and turn off the cache
provider. `xfail_strict` is on: a test marked `xfail` that starts passing
fails the run.

## Fake CLI

`tests/fakes/fake_proton_drive.py` is the executable (`#!/usr/bin/env python3`).
`PROTON_DRIVE_CLI` points the engine at it. `FAKE_PROTON_STATE` is the JSON
file that holds the remote drive. The engine fixture sets both and fails
immediately if `PROTON_DRIVE_CLI` is unset, instead of searching for a real
binary.

The state file records the account, the `--version` text, nodes, faults,
every argv the fake saw, and the remote paths that were stored. Helpers in
`tests/fakes/remote_state.py` load and save that file. Tests reach it through
the `fake_drive` fixture: `seed_file`, `seed_folder`, `content`, `listing`,
`calls`, `uploads`, `add_fault`, `trashed`.

Supported commands are the ones the engine sends: `--version`,
`filesystem list` (with and without `-j`), `filesystem info`,
`filesystem create-folder`, `filesystem upload`, `filesystem download` and
`filesystem trash`. Anything else exits 2 with `fake: unsupported command`
on stderr.

`filesystem list /` without `-j` is the auth probe. A fault `{"cmd": "auth"}`
makes that probe exit 1. `filesystem list <path> -j` returns a JSON array of
direct children that are not trashed. Fields the engine unwraps (`type`,
`keyAuthor`, `activeRevision`) use `{"ok": true, "value": ...}`. File items
carry `claimedSize`, `claimedModificationTime` and `claimedDigests.sha1`.
`claimedModificationTime` is an ISO-8601 UTC string with milliseconds
(`2001-09-09T01:46:40.000Z`). The state file stores POSIX seconds; the
listing converts them.

Uploads read names relative to the process cwd, which is how the engine
calls the CLI. Glob escapes are undone first (`a[[]b.txt` is the file
`a[b.txt`). An upload of an existing file replaces the bytes and bumps
`revisions`. A per-file fault fails that name only: `- <name>: <error>` on
stdout, `N item(s) failed to upload` on stderr, exit 1, and the other names
in the batch are stored. `filesystem trash` marks the node and its
descendants trashed, and listings hide them.

`filesystem download [-f STRATEGY] <remote...> <localFolder>` writes each
remote file into the folder under its remote name, as the CLI does. An
existing local file needs a strategy (`skip`, `replace`, `keep-both`);
without one the command fails and the local bytes stay. Proton documents
and spreadsheets are skipped. Faults use `cmd="download"`. Modification
times keep their fraction of a second, and a node's `uid` is listed, so
two-way tests can compare dates and node ids.

`add_fault(cmd=..., match=..., mode=..., times=..., stderr=...)` injects a
failure. `times` defaults to every call; `times=1` fires once. `mode` is
`fail` or `partial` (the fake treats them the same: that name fails and the
other names in the batch are stored), `perm` (stderr contains `permission`),
`stderr_text`, or `hang` (the state lock is released, then the process
sleeps; `seconds` defaults to 3600). On a node, `remove_size_meta` omits `claimedSize`
and the `totalStorageSize` fallback, and `remove_digest` omits
`claimedDigests`, so a test can force the missing-metadata paths.

## Writing an engine test

Use the fixtures in `tests/conftest.py`. `_isolate` runs for every test: it
points `HOME` and `XDG_*` at a temporary directory and, if `config` or
`proton_sync` were imported in this process, retargets their path constants.
The session ends by checking that the real `~/.proton-drive-sync` was not
created and that `settings.json` did not appear in the repository root.

`local_tree({"Docs/a.txt": (b"AAAA", 1_000_000_000)})` builds files and sets
their mtime. A number below `10**15` is seconds; a larger number is
nanoseconds. `local_tree.write` updates one file the same way.

`write_mappings([...])` writes a mappings JSON in the new-format object
(`mappings` plus optional global `exclusions`).

`engine(mappings_path, *args)` runs `proton_sync.py` as a subprocess with a
temporary settings file `{"language": "en"}`, the fake CLI, and the isolated
home. It returns `(returncode, stdout, stderr)` on a `CompletedProcess`.
Assert on remote bytes and exit codes. Stable tags such as `[upload-failed]`
and `[auth-failed]` are part of the API and may be asserted. Do not assert
on translated sentences.

Import `proton_sync` in-process only when a test has to observe a helper
the subprocess cannot show, such as whether a file was hashed.
`sync_folder` remembers remote folders on the module (`_REMOTE_KNOWN`): an
in-process test clears it before and after, so the next test starts clean.

`tests/test_twoway.py` drives two-way mappings through the engine.
`tests/test_ui_shell.py` builds the Qt window on the `offscreen` platform
and is skipped when PySide6 cannot be imported.
`tests/test_equal_size.py` covers the v1.17.0 upload rule through the
engine: an equal-size edit is sent, an identical rewrite is not, a file
that still matches the last pass is not read, a failed batch does not
treat an older same-size copy as the new file, and a file edited while
it was excluded is sent once that exclusion is removed.

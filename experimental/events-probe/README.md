# proton-sync-events — experimental event probe

A small, **read-only** command-line tool that reports what changed inside one
Proton Drive folder since its previous run, read from the Drive event stream
instead of walking the whole tree.

It is an **experiment** for the `twoway` branch of proton-drive-cli-sync. It is
not part of a release, it is not used by the application, and it is a
third-party program, **not officially supported by Proton**.

It is built from the sources of the official Proton Drive CLI, in the
[ProtonDriveApps/sdk](https://github.com/ProtonDriveApps/sdk) repository
(MIT licence, see [LICENSE.md](LICENSE.md)), with its own entry point and a
single new command.

## What it keeps to itself

The probe never touches the official CLI's session, cache or event bookmarks:

- **Its own session**, in its own keyring entry
  (`proton-drive-cli-sync/events-probe`). You sign in once with
  `proton-sync-events auth login`, through the browser, as with the CLI.
- **Its own data folder**: `~/.local/share/proton-sync-events`, or the folder
  named by `PROTON_SYNC_EVENTS_DIR`.
- **Its own identity** towards Proton:
  `external-drive-proton_drive_cli_sync@<version>`.

It writes nothing to Drive.

## Commands

```
proton-sync-events auth login
proton-sync-events auth logout
proton-sync-events events changes [-k] [-e] [-j] <folder>
```

- `-j`, `--json`: one JSON object per run (below).
- `-k`, `--keep`: report the changes without advancing the bookmark; the next
  run reports them again.
- `-e`, `--events`: also list every event read, including those that do not
  concern the folder.

The first run on a folder, or a run on another folder than the previous one,
only sets the starting point and reports nothing.

## Output (`--json`)

```json
{"folder": "/my-files/Some folder", "initialized": false, "eventsRead": 12,
 "changes": [
   {"action": "created|modified|revised|moved|removed",
    "type": "file|folder", "path": "relative/to/the/watched/folder",
    "from": "old/path (moves only)", "size": 123,
    "modificationTime": "2026-10-15T20:41:07.000Z", "sha1": "...",
    "reason": "(some actions only)"}
 ],
 "notes": [], "bookmarkAdvanced": true}
```

- Paths are relative to the watched folder. The output carries **no node ids**.
- `modified`: new content (different SHA-1; without SHA-1, size, date or a new
  version). `revised`: a new version with identical content.
- `moved`: renamed or moved inside the folder, with `from`.
- `removed`: trashed, deleted, or moved out of the folder (`reason` says which).
- `bookmarkAdvanced` is false with `--keep`.

## State, and reading safely

The probe keeps its state in its data folder (`changes-state.json`): the event
bookmark, and what it knows of the watched folder's contents. A lock
(`changes.lock`) prevents two runs at once on the same data folder. A network or
server error fails the run without advancing the bookmark.

Without `--keep`, the bookmark advances as soon as the changes are printed. A
program that applies them should therefore:

1. run with `--keep` and apply the changes;
2. run again **without** `--keep` and apply that output too.

The second run reports the same changes again, plus any that arrived in the
meantime, so applying changes must be idempotent. If the program stops between
the two runs, nothing is lost: the next `--keep` run reports everything again.

## Limits

- **One folder per data folder.** Watching another folder resets the state. A
  second folder needs a data folder of its own (`PROTON_SYNC_EVENTS_DIR`); both
  then share the same session in the keyring, so **never run them at the same
  time**: the session renewal is not locked between processes.
- Built and used on Linux only.

## Build

You need [Bun](https://bun.sh) **1.4.2**, the version the CLI requires. An older
version installs the dependencies without error but leaves the patched
`@protontech/crypto` package incomplete, and the build then fails.

1. Clone the SDK repository at the commit these sources were written against:

   ```
   git clone https://github.com/ProtonDriveApps/sdk.git
   cd sdk
   git checkout 8ffae00
   ```

2. Copy the probe into the CLI sources:

   ```
   cp path/to/events-probe/src/proton-sync-events.ts cli/src/
   mkdir -p cli/src/probe
   cp path/to/events-probe/src/probe/*.ts cli/src/probe/
   cp path/to/events-probe/src/credentials/constants.ts cli/src/credentials/constants.ts
   ```

   `credentials/constants.ts` is the only file of the CLI that changes: it gives
   the probe its own keyring entry. Everything else is new.

3. `cli/bun.lock` points at Proton's internal registry. Point it at the public
   one (same versions, same checksums):

   ```
   sed -i 's#https://pkg.protontech.ch/npm/npm-public/#https://registry.npmjs.org/#g' cli/bun.lock
   ```

4. Install the dependencies, in this order:

   ```
   (cd cli && bun install)
   (cd incubating/account/js && bun install)
   (cd client/js && bun install)
   ```

5. Run the tests (28, on a fake Drive):

   ```
   cd cli
   bun test ./src/probe/changesLogic.probe.test.ts
   ```

6. Build a single executable:

   ```
   bun build --compile --bytecode --target=bun-linux-x64-baseline --format=esm --minify --sourcemap=none \
     --define 'APP_VERSION="external-drive-proton_drive_cli_sync@0.4.0-alpha"' \
     --define 'SDK_VERSION="js@git-8ffae00"' --define 'SENTRY_DSN=undefined' \
     src/proton-sync-events.ts --outfile=release-probe/proton-sync-events
   ```

   The `baseline` target also runs on processors without AVX2.

## Removing it

```
proton-sync-events auth logout
rm -rf ~/.local/share/proton-sync-events
```

The session may stay listed on Proton's side until it expires; it can be revoked
from the account's security settings.

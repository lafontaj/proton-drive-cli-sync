🇬🇧 **English** | 🇫🇷 [Français](README_fr.md)

# Proton Drive sync via the official CLI (Linux)

A Python engine plus a graphical interface that drive the **official Proton Drive CLI** to get real folder synchronization to Proton Drive on Linux — the equivalent of `robocopy /MIR`: only new or modified files are sent on each pass, no blind re-uploading. Two complementary modes: **real-time** (folder watching, upload as soon as a file changes) and **scheduled** (periodic full pass that reconciles and acts as a safety net).

Originally built to sync a NAS to Proton Drive for a whole family, but usable for any local or network folder.

> ℹ️ The Proton Drive CLI is a low-level tool (upload, list, delete…) with no continuous sync engine. This project adds the missing "synchronization" layer on top, while Proton's full Linux app is still in the works.

---

## ⚠️ Prerequisite: the Proton Drive CLI (NOT included)

**The official Proton Drive CLI is NOT bundled in this repository.** You must download it directly from Proton:

**➡️ https://proton.me/download/drive/cli/index.html**

Then:

1. Make the binary executable: `chmod +x proton-drive`
2. Place the `proton-drive` binary **in the same folder as this project's `.py` files** (simplest), or point to its path in **⚙ Configuration → Proton Drive CLI** (or via the `PROTON_DRIVE_CLI` environment variable).
3. Authenticate once: `./proton-drive auth login` (opens your browser). The session is stored securely by the system keyring (libsecret / GNOME Keyring).

## Other dependencies

- **Python 3** with **Tkinter** — graphical interface: `sudo apt install python3-tk`
- **PyGObject + libxapp** — system-tray status icon (Cinnamon/MATE/Xfce): `sudo apt install python3-gi gir1.2-xapp-1.0` *(already present on Linux Mint)*
- **pyinotify** — real-time watching: `sudo apt install python3-pyinotify`
- **Zenity** *(optional, recommended)* — native GTK file/folder pickers and calendar: `sudo apt install zenity`. If absent, the app automatically falls back to Tkinter dialogs (less pretty but functional).
- **libsecret / keyring** (GNOME Keyring, active whenever a graphical session runs) — where the CLI stores the Proton session
- **systemd** (user mode, with *linger*) — for the real-time daemons and scheduled passes
- **`nfs-common`** *(only if your source is a NAS mounted over NFS)* — NFS client to mount the shares: `sudo apt install nfs-common`. Not needed if you sync local folders only.

## Quick start

```bash
git clone https://github.com/CapitaineFlamQuebec/proton-drive-cli-sync.git
cd proton-drive-cli-sync

# 1. Place the proton-drive binary here (see above) and authenticate
./proton-drive auth login

# 2. Create your config files from the examples
cp settings.example.json settings.json
cp mappings.example.json mappings-user1.json

# 3. Launch the graphical editor
python3 proton_mapping_editor.py mappings-user1.json
```

From the editor, setting up a folder for the first time usually goes like this:

1. **Add the folder** ("➕ Folder…" button). A Proton destination browser ("🔍 Browse Proton…") saves you from typing paths by hand.
2. **Decide on delete behavior** in the mapping:
   - *Additive* (default): sync never deletes anything on Proton. Safe, no special setting.
   - *Mirror*: enable deletion and choose the mode — **trash** (`trash` — recoverable for 30 days on Proton, recommended) or **permanent** (irreversible). What disappears locally then disappears from Proton.

   > ⚠️ **DANGER — read this before enabling mirror mode.** Mirror makes the Proton destination **identical to the local source**. If the destination folder on Proton **already contains files or folders that are NOT in the local source**, they will be **deleted** (trashed or permanently, depending on the mode) — so that Proton becomes an exact mirror of local. This is not "also upload my local files": it is "make them match exactly". These deletions trigger whenever a mapping is configured as mirror (trash field set): at its priming as well as on later passes. An additive mapping (empty trash field) never deletes anything. **Before enabling mirror on a destination that already holds data, run a `--delete --dry-run`** (see "Delete propagation") to see exactly what would be erased, without deleting anything.
3. **Prime the mapping** ("🌱 Prime the cache" button). Do not skip this: it runs a first full pass that builds the cache and marks the tree as "complete" — which makes the mapping **ready for real-time**. **Priming follows each mapping's own configuration** (its trash field); there is no option to choose at prime time:
   - an **additive** mapping (empty trash field) is uploaded without ever deleting anything;
   - a **mirror** mapping (trash or permanent deletion) reconciles the destination according to its mode, from this very first pass.

   Priming handles stopping/restarting the daemons on its own and shows its progress. **Changing a mapping's vocation later** is done in the mapping editor (via the trash field): switching from additive to mirror resets the mapping's primed state (you will need to prime it again — the editor warns you); switching from mirror to additive requires no re-priming. Tip: decide a mapping's vocation **before** it holds a lot of data, so any re-priming stays quick.
4. **Enable real-time and/or scheduling** if wanted ("⚡ Real-time…" and "⏰ Schedule…" windows).

> 💡 For a risk-free first try, start in additive mode: you watch your files climb to Proton with no possibility of deletion. Switch to mirror later, once comfortable.

---

## How it works

### Change detection (like `robocopy /MIR`)

On each pass, the engine (`proton_sync.py`) compares the local state to a **cache** and sends only what changed (new or modified). Without this cache, the CLI would make one network call per visited folder (~1–2 s each) — on a large tree, a "nothing to do" pass would take hours. With the cache, those passes are near-instant.

- **Local cache**: one signature per folder; an unchanged folder is skipped with no network call.
- **Lock** (`flock`): prevents two passes running at once (real-time vs scheduled) from clashing.
- **Interruption recovery**: Ctrl+C is safe — on the next pass, already-sent files are recognized and skipped.
- **Automatic creation** of missing destination folders on Proton.

### Mappings file format

A mapping describes *which local folder* goes *to which Proton folder*. Two formats are accepted.

**Full format** (recommended — with exclusions):

```json
{
  "exclusions": {
    "names": [".caltrash", "trash", ".Trash-1000"],
    "patterns": ["*.tmp", "~*"]
  },
  "mappings": [
    {
      "type": "folder",
      "source": "/media/nas1/Documents",
      "dest_parent": "/my-files",
      "exclusions": { "names": [".specific-to-this-mapping"] },
      "allow_delete": true,
      "delete_mode": "trash",
      "source_kind": "nfs"
    }
  ]
}
```

**Simple format** (list, still supported):

```json
[
  { "type": "folder", "source": "/home/me/Documents", "dest_parent": "/my-files" }
]
```

Mapping fields:

- `type`: `folder` (all contents, recursive) or `file` (a single file — useful for an encrypted container, explicitly whitelisted so a new container isn't uploaded by mistake).
- `source`: local or network path to sync.
- `dest_parent`: the **parent** folder on Proton where contents land (e.g. `/my-files`, `/my-files/Backups`).
- `allow_delete` *(optional)*: `true`/`false`. Absent = `false` = **additive** mode (never deletes). At `true`, local deletions are propagated to Proton (true mirror).
- `delete_mode` *(optional)*: `"trash"` (Proton trash, recoverable 30 days) or `"permanent"` (irreversible).
- `source_kind` *(optional)*: `"nfs"` or `"local"`. Detected by the GUI. Guard rail: an `nfs` source only deletes if the network mount is alive (a dropped mount never triggers a destructive mirror).

### Exclusions

Two combined mechanisms, applied to **folders and files**, by **name** (never full path), case-insensitive:

- `names`: exact names (e.g. `.caltrash`, `trash`, `.Trash-1000`)
- `patterns`: shell-style glob patterns (e.g. `*.tmp`, `.Trash-*`, `~*`)

Two levels that stack: **global** (all mappings) and **per-mapping** (added on top, for one specific mapping). An excluded folder is not visited at all.

Deliberate choice: hidden files (starting with `.`) are **not** blanket-excluded. A `.config_important` you want to keep is kept; only what is explicitly listed is excluded.

With `--delete`, a file excluded locally but already present on Proton (uploaded before the exclusion was added) is treated as an **orphan** and trashed on the next pass. The exclusion-set fingerprint is part of the cache signature: any change to exclusions forces this reconciliation automatically (the first following pass is longer, then fast skips resume).

`mappings.example.json` ships a **default exclusion list** covering common temporary files (Linux, macOS, Windows) — a good starting point. A per-software catalog is kept in `Temporary-files-exclusions.md`.

### Delete propagation (`--delete`) — optional mirror

By default, sync is **additive**: it never erases anything on Proton. Enabling `--delete` (or `allow_delete: true` on a mapping) turns sync into a **true mirror**: what disappears locally disappears from Proton (trash or permanent per `delete_mode`). Several guard rails surround this sensitive operation: a double condition (`--delete` **and** `allow_delete: true` required together), a check that the network mount is alive for `nfs` sources (a dropped NAS never triggers a destructive mirror), and a tree considered complete before any deletion.

> ⚠️ **Mirror also deletes pre-existing Proton content that is absent locally.** The goal of mirror is for the Proton destination to be **exactly** the local source. So any file or folder already on the Proton destination **but absent from the local source** is treated as an "orphan" and **deleted** (30-day trash or permanent). This applies **from priming onward**, not just for deletions made later. If your Proton destination already holds data you want to keep, **do not enable mirror on it** without checking first.

**Always test first with `--dry-run`.** A `--delete --dry-run` pass shows exactly what would be deleted, **without erasing anything**. Make this a reflex before enabling mirror on a non-empty destination. On the command line:

```bash
python3 proton_sync.py mappings-user1.json --delete --dry-run
```

The `permanent` mode (`delete_mode: "permanent"`) is **irreversible**: prefer `trash` (Proton trash, recoverable 30 days) until you are sure of the behavior.

---

## Graphical interface (`proton_mapping_editor.py`)

- **Mapping editing**: add/modify folders and files, with a **Proton destination browser** ("🔍 Browse Proton…") listing `/my-files` and `/shared-with-me` — no more typing paths by hand.
- **⚙ Configuration** (single button): Proton account (sign-in), CLI path, language, NAS (enable, mount point, identity), extensions, tray icon. Each setting has a "?" help button.
- **Connection indicator** bottom-right: the real connected account ("🔑 Proton: connected — …").
- **Per-mapping "Ready" column** (✅/⏳/—) and **pass journal** (last run, success/failure).
- **System-tray icon** (optional): circular double arrow — purple (active + session OK), grey + X (session expired), grey (daemons stopped). Left-click opens the editor.

## Real-time sync

Folder watching via `inotify`: as soon as a file changes, a **marker** is written and a small daemon (the "consumer") runs the engine on the affected area, with debouncing and de-duplication. Watching is **distributed**: one watcher on the local machine, one on the NAS (for network folders), each writing to a marker queue. Real-time handles the day-to-day (small targeted changes); scheduling builds and reconciles the full tree.

The "⚡ Real-time…" window shows daemon state, delays, marker queues, a live journal, and Install/Update buttons.

## Automatic scheduling

Periodic full passes via **user systemd timers** (with *linger* to survive session logout). Acts as a safety net: whatever real-time might have missed (daemon stopped, mount dropped…) is caught by the scheduled pass. The pass journal in the GUI shows the last run per boot boundary.

## Two system settings to know (real-time daemons and scheduling)

These two points aren't obvious if you're new to systemd, and missing them causes puzzling symptoms. Take a minute to check them.

### 1. Enable *linger* (or the daemons stop at logout)

The daemons (real-time watching, scheduled passes) run as **user systemd services**. By default, a user service **stops when you close your session** and only restarts at the next login. Typical symptom: "sync works while I'm logged in, but stops overnight / when I log out." The fix is to enable *linger*, which lets your services keep running without an open session:

```bash
sudo loginctl enable-linger $USER
```

Do this **once**, on the local machine **and** on the NAS (with the relevant username on each). To verify: `loginctl show-user $USER | grep Linger` should show `Linger=yes`.

### 2. Raise the inotify limit if you watch many folders

Real-time watching "places a sentinel" (*watch*) on each watched folder. The Linux kernel caps the total number of sentinels per user (`fs.inotify.max_user_watches`, often 8192 by default on older systems, much higher on recent ones). On a large tree (thousands of folders), the limit can be hit — watching then becomes incomplete, with no visible error. The app **checks the capacity at startup** and shows it in its journal; if it reports a shortage, raise the limit:

```bash
# Check the current limit
cat /proc/sys/fs/inotify/max_user_watches

# Raise it durably (e.g. 512000) — do this where the watcher runs
echo 'fs.inotify.max_user_watches=512000' | sudo tee /etc/sysctl.d/40-proton-sync.conf
sudo sysctl --system
```

On a NAS with a very large tree, this is the setting to watch first if the NAS watcher struggles to keep up.

## NAS prerequisites: mounts and access (important)

If your source is a NAS, the architecture relies on **two invariants** — without them, real-time watching on the NAS side won't work.

**1. Identical data paths on both sides.** Watched folders must be reachable at the **same path** on the local machine and on the NAS. For example, if a folder is `/media/nas1/Documents` as seen from the local machine (NFS mount), it must also be `/media/nas1/Documents` as seen from the NAS itself (a *bind* mount, or the real path if the watcher runs on the NAS). This is what lets markers written by one watcher be understood by the other: a marker names a path, and that path must mean the same thing everywhere. This is the essence of multi-machine operation.

**2. Write access to the NAS exchange folder.** The `proton-sync/` folder on the NAS (holding `config/` and `queue/`) must be **writable** from the local machine — typically via a dedicated NFS mount (in the reference setup: `/media/home_nas/proton-sync/`). The local machine **pushes** the mappings copy there (so the NAS watcher knows what to watch) and **reads** the markers written by the NAS watcher. Without write access to that folder, the push fails and NAS-side real-time stays silent.

**The NAS watcher runs on the NAS** (`nas_watcher.py`), installed as a systemd service. It needs the same Python files as the local machine (`nas_watcher.py`, `local_watcher.py`, `config.py`, `i18n.py`, `mount_check.py`) in its `proton-sync/` folder — the local machine **syncs them there automatically** on every real-time Install/Update (differential copy). The `.service` file itself is not installed remotely: it is dropped, and if its content changes, a message reminds you of the command to run on the NAS (`sudo systemctl daemon-reload && sudo systemctl restart proton-nas-watch.service`).

> 💡 In **local-only mode** (no NAS), none of this applies: `nas_enabled: false` and the app never tries to reach a NAS.

## Configuration (`settings.json`) and local-only mode

Everything that varies between installations lives in `settings.json` (see `settings.example.json`): language, NAS enable and mount point, CLI path, identity, extension settings, tray icon. In **local-only mode** (`nas_enabled: false`), the app never tries to reach a NAS — the NAS sections are hidden.

The cache, marker queue, and logs live under a single folder: `~/.proton-drive-sync/`.

## NAS identity and marker queues

Each installation has a **stable identity** (`account_name` setting, decoupled from the mappings file name and from the Proton account) that names its config copy and marker queue on the NAS (`mappings-<identity>.json`, `queue/<identity>`). On a fresh install, a neutral unique name (`user1`, `user2`…) is claimed automatically on the NAS with no possible collision. Renaming the mappings file, or switching Proton accounts, therefore no longer breaks anything on the NAS side.

## Internationalization

The interface is available in **six languages**: French, English, German, Spanish, Italian and Portuguese (gettext catalog). The language is chosen in ⚙ Configuration. Compiled `.mo` files are shipped (the app is translated right after cloning); to regenerate after editing translations: `./build_locales.sh`.

**French** and **English** are the reference languages, maintained by the author. **German, Spanish, Italian and Portuguese** are complete but community-contributed translations that may still be improved: corrections and refinements are welcome (see below). Documentation (README, guides) stays in French and English.

### Contributing translations

Translations live in `locale/<language>/LC_MESSAGES/proton-sync.po`. To fix or improve a language: edit the matching `.po` file (each `msgid` is the English string, each `msgstr` its translation), then run `./build_locales.sh` to recompile. Rules to follow: never translate the bracketed tags (`[account-changed]`, `[config]`, `[DRY-RUN]`…), preserve the `{x}` fields and the emojis. Suggestions for corrections or new languages are welcome via an issue or a pull request.

---

## Repository layout

| File | Role |
|---|---|
| `proton_sync.py` | Sync engine (detection, cache, upload, deletions) |
| `proton_mapping_editor.py` | Graphical interface (editing, configuration, control) |
| `realtime_manager.py`, `realtime_consumer.py` | Real-time layer (management + marker consumer) |
| `local_watcher.py`, `nas_watcher.py` | `inotify` watching (local machine / NAS) |
| `schedule_manager.py` | Scheduled passes (systemd timers) |
| `config.py` | Shared settings (`settings.json`) |
| `i18n.py`, `locale/` | Translations (FR, EN, DE, ES, IT, PT) |
| `tray_indicator.py` | System-tray icon |
| `mount_check.py` | Network-mount verification |
| `*.example.json` | Config templates to copy |
| `build_locales.sh`, `build_pdf.py` | Utilities (translations; PDF guides — needs `wkhtmltopdf`) |

## License

MIT — see [LICENSE](LICENSE).

Independent project, not affiliated with Proton AG. "Proton" and "Proton Drive" belong to Proton AG.

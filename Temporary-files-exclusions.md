🇬🇧 English | 🇫🇷 [Français](Temporary-files-exclusions_fr.md)

# Exclusions — known temporary files

Reference of exclusion patterns to use in **Global exclusions** (and, when
needed, per mapping) to avoid sending to Proton temporary files, locks,
application trashes and relics from other operating systems.

---

## How the filtering works (reminder)

- **Exact names**: **literal** name matching (no `*`). Case-insensitive.
  Excludes any folder **or** file bearing exactly that name.
  Examples: `__pycache__`, `.DS_Store`.
- **Patterns**: shell-style globs, `*` matches any sequence of characters (and
  `?` a single character). Examples: `*.tmp`, `.Trash-*`.
- Filtering applies to the **name** (last path segment), not the full path.

> **Classic trap**: an entry containing `*` placed in "Exact names" filters
> **nothing** (it waits for a file literally named `*.tmp`). Patterns always go
> in the **"Patterns"** column.

**Effect at the next `--delete` pass**: a newly excluded file that was already
uploaded to Proton becomes an "orphan" (present remotely, absent from the local
list) → it goes to the **Proton trash** (recoverable for 30 days). That's the
desired cleanup — just don't let it surprise you.

---

## Recommended set — paste as is

### "Exact names" column

```
.windows-serial
__pycache__
lost+found
.directory
.gvfs
.caltrash
.dtrash
thumbnails-digikam.db
.thumbnails
LOST.DIR
.DS_Store
.Spotlight-V100
.Trashes
.fseventsd
.DocumentRevisions-V100
.TemporaryItems
Thumbs.db
ehthumbs.db
desktop.ini
$RECYCLE.BIN
System Volume Information
```

### "Patterns" column

```
.Trash-*
.goutputstream-*
.pikepdf*
.nfs*
.fuse_hidden*
.trashed-*
.pending-*
.~lock.*#
~$*
._*
*~
*.tmp
*.temp
*.swp
*.swo
*.pyc
*.pyo
*.pid
*.lock
*-wal
*-shm
*.db-journal
*.part
*.crdownload
*.partial
```

> This set includes the basics (`.Trash-*`, `.goutputstream-*`, `*.tmp`,
> `*.swp`, `*~`, `*.pyc`, `__pycache__`) **plus** the additions detailed below.
> You can replace the content of both columns wholesale.

---

## Details per application / category

### Linux system & desktop (general)
- `.Trash-*` — freedesktop trashes (`.Trash-1000`…).
- `.goutputstream-*` — GIO atomic writes (xed, gThumb, Pix, exports…).
- `*~` — tilde backup copies (xed and many editors).
- `*.tmp`, `*.temp` — generic temporaries.
- `.fuse_hidden*` — hidden files created by FUSE.
- `.nfs*` — **specific to NFS mounts**: "silly-rename" files NFS creates when a
  still-open file is deleted. Exclude them.
- `lost+found` — recovery folder of ext2/3/4 filesystems (at a volume's root).
  A system artifact, never useful data.
- `.directory` — KDE/Dolphin folder display settings.
- `.gvfs` — old GVFS FUSE mount point (older systems).
- `*.pid` — process PID files (transient).
- `*.lock` — generic application locks. (LibreOffice locks `.~lock.*#` are
  covered separately.)

### Text/code editors (xed, scripts)
- `*.swp`, `*.swo` — Vim swap files.
- `*.pyc`, `*.pyo`, `__pycache__` — Python bytecode.

### Office — LibreOffice (and MS Office relics from copies)
- `.~lock.*#` — LibreOffice open-file lock (`.~lock.document.odt#`).
- `~$*` — Microsoft Office owner/lock files (`~$report.docx`).
- `*.tmp` — save/conversion temporaries (already covered).

### PDF — PDF Arranger, xreader
- `.pikepdf*` — PDF Arranger's atomic-write temporaries.
- xreader leaves no temporary in the folder (annotations stored in
  `~/.local/share/xreader`).

### Images / photos — digiKam, GIMP, gThumb, Pix, xviewer
- `.dtrash` — **digiKam's internal trash** (seen inside your photos).
- `thumbnails-digikam.db` — **thumbnail cache** (can weigh over 1 GB).
  **Derived** data, fully rebuildable from the images; contains **no** cataloging
  whatsoever. On a restore, digiKam regenerates the thumbnails on its own (as you
  browse, or via *Tools → Maintenance → Rebuild Thumbnails*) — the only cost is
  CPU time. Since it changes with every new thumbnail, keeping it means endless
  pointless re-uploads. **Exclude it** (exact name).
- `*-wal`, `*-shm`, `*.db-journal` — SQLite temporaries appearing **next to**
  the digiKam databases. Transient, safe.
- **KEEP at all costs** — these are your real catalog data, not regenerable:
  - `digikam4.db` — albums, keywords/tags, ratings, captions, geolocation,
    **face assignments**. Irreplaceable.
  - `recognition.db` — trained face-recognition model (optional: face tags live
    in `digikam4.db` anyway; without it, digiKam just re-learns automatic
    recognition). Keep it to have recognition operational right after a restore.
  - `similarity.db` — similarity/duplicate search fingerprints (optional,
    regenerable).
- GIMP: few temporaries in the working folder. See "Your call" for `*.xcf~`.
- gThumb / Pix: sometimes store comments in a `.comments` folder.
  **Not excluded by default** (metadata you may want to keep).

### Audio — Audacity, Tenacity, SoundConverter, FFAudioConverter, Puddletag, VLC
- `*-wal`, `*-shm` — SQLite temporaries of `.aup3` projects (Audacity/Tenacity).
- Converters (SoundConverter, FFAudioConverter) write the final file; no notable
  pattern. Puddletag writes tags in place. VLC leaves no temporary in media
  folders.

### E-books — Calibre, Sigil
- `.caltrash` — **Calibre library trash**.
- `*-wal`, `*-shm`, `*.db-journal` — SQLite temporaries of `metadata.db`
  (Calibre).
- Sigil works in a system temp folder (outside the watched tree).

### Password managers / encryption — KeePassXC, VeraCrypt
- **Exclude nothing by extension here.** The `.kdbx` (KeePassXC) and VeraCrypt
  containers are your **real data** — they must be backed up.
- KeePassXC saves atomically (temp then rename). If you enabled "Backup database
  before saving", it creates a `*.old.kdbx` — see "Your call" if you want to
  exclude it.

### Phone / Android (whole-folder copies via SFTP/FolderSync)
- `.thumbnails` — thumbnail folder.
- `.trashed-*` — trashed files (Android 11+).
- `.pending-*` — MediaStore files being written.
- `LOST.DIR` — SD-card recovery folder.
- **Do not exclude** `.nomedia` (an intentional marker, harmless and tiny).

### Relics from other systems (media copies)
- **Apple**: `.DS_Store`, `._*` (AppleDouble forks), `.Spotlight-V100`,
  `.Trashes`, `.fseventsd`, `.DocumentRevisions-V100`, `.TemporaryItems`.
- **Windows**: `Thumbs.db`, `ehthumbs.db`, `desktop.ini`, `$RECYCLE.BIN`,
  `System Volume Information` (and the old `RECYCLER` on ancient media).

### Partial downloads (general)
- `*.part`, `*.crdownload`, `*.partial` — incomplete downloads.

---

## Your call — add only if you're sure

These patterns target files that are **sometimes** intentional. Add them only if
you don't keep this kind of file on purpose.

- `*.bak`, `*.old`, `*.orig` — miscellaneous backups/renames.
- `*.xcf~` — GIMP automatic backup.
- `*.old.kdbx` — KeePassXC backup (if the option is enabled). **Never** `*.kdbx`.
- *core dumps* — a crashing program leaves a `core` or `core.<pid>` file.
  **Avoid** the broad `core.*` pattern: it would catch legitimate files like
  `core.js` or `core.css` (present in source code). If you see one, exclude the
  precise name (e.g. `core.12345`) rather than a generic pattern.

---

## Warnings

- **Never** a pattern that could match a **VeraCrypt** container or a
  **KeePassXC** database: those are data, not temporaries.
- Stay cautious with generic extensions. `*.tmp`, `*.part`, `.nfs*`, `*-wal`,
  `*-shm` are safe. The `*.bak` / `*.old` / `*.orig` suffixes require thought.
- **Global** exclusions are pushed to the NAS automatically on save; they
  therefore apply to both watchers (desktop and NAS).
- Behavior reminder: the **watcher still writes a marker** for an excluded path
  (it is not "exclusion-aware"); the **engine** discards the file — hence an
  occasional `🚫 excluded` in the log, with no upload. Deferred improvement if
  the noise ever becomes bothersome.

---

## Related note: UPPERCASE extensions and Proton previews (not an exclusion)

A neighbour of the "file that chokes on upload" case, but **not** an exclusion
matter (the file must be backed up): the Proton CLI derives the MIME type from the
extension **case-sensitively**. An uppercase extension (`DOC.PDF`, `IMG.JPG`) is
mis-typed → no thumbnail, no preview, no icon in the Proton apps, **silently**.
The engine fixes this at the source by **renaming** the extension to lowercase
(see the README, "Upload robustness, thumbnails and MIME detection"), with a
collision guard and a `~/.proton_sync/renamed-extensions.log`. Disable with
`--no-rename-ext`. Related: TIFF/HEIC/AVIF fail thumbnail generation (image codec)
— the engine then auto-retries with `--skip-thumbnails` (file saved, no built-in
Proton preview; convert to JPEG/PNG for a preview).

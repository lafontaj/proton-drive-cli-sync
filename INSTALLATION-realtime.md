🇬🇧 English | 🇫🇷 [Français](INSTALLATION-realtime.fr.md)

# Installation — Real-time layer

Companion to `INSTALLATION-systemd.md`. The real-time layer adds **three daemons**
on top of the nightly timer already in place: two on the **desktop** (driven from
the GUI) and one on the **NAS** (installed manually, managed on the NAS).

Guiding principle: **systemd everywhere; each machine keeps its own daemons
alive.** The GUI only drives the local daemons (desktop); it **observes** the NAS
watcher through the NFS queue, without SSH.

---

## 1. Desktop — local watcher + consumer (via the GUI)

Nothing to copy by hand. In the mappings editor:

1. Open the user's mappings file (`mappings-user1.json`…).
2. **"⚡ Real-time…"** button.
3. **"💾 Install / Update"**: creates and starts the two `--user` services,
   pointed at the active mappings file:
   - `proton-watch.service`   → `local_watcher.py`
   - `proton-consume.service` → `realtime_consumer.py`

They restart automatically at session login (`WantedBy=default.target`,
`Restart=on-failure`).

Manual equivalent (for reference), once the `.service` files are written to
`~/.config/systemd/user/`:

```bash
systemctl --user daemon-reload
systemctl --user enable --now proton-watch.service proton-consume.service
systemctl --user status proton-consume.service
journalctl --user -u proton-consume.service -n 50 --no-pager
```

### Persistence outside the session (linger)

As with the nightly timer, without **linger** the `--user` daemons stop at
logout. The real-time window shows the linger state and reminds you of the
command (admin, once):

```bash
sudo loginctl enable-linger <user>
```

---

## 2. NAS — NAS watcher (manual, on the NAS)

The NAS watcher runs **on the NAS**, under the `nas` account, as a **system**
service (it must start at boot without an open session). The GUI does not drive
it.

On the NAS:

```bash
# Required files in /home/nasuser/proton-sync/ (copy them together):
#   nas_watcher.py, local_watcher.py (shared helpers), mount_check.py,
#   i18n.py + the locale/ folder (translations; without them, logs in English).
# pyinotify installed (python3-pyinotify or pip).
# Log language: follows the NAS's LANG; to force it:
#   echo '{"language": "fr"}' > /home/nasuser/proton-sync/settings.json
sudo cp proton-nas-watch.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now proton-nas-watch.service
systemctl status proton-nas-watch.service
journalctl -u proton-nas-watch.service -n 50 --no-pager
```

The watcher reads the mapping copies in `/home/nasuser/proton-sync/config/`
(`mappings-user1.json`, `mappings-user2.json`…) — that's what **the GUI pushes**
via *Real-time → ⬆ Push mappings to the NAS*. It hot-reloads these copies, so a
new push is picked up without restarting the service.

---

## 3. Verifying the full chain

1. **GUI → Real-time → ⬆ Push mappings to the NAS**: the indicator turns 🟢 green
   ("Up to date on the NAS").
2. **NAS watcher (observation)**: 🟢 "NAS reachable".
3. Modify a file inside a watched source → a marker appears in the queue, the
   consumer processes it after the debounce delay.
4. **Marker queues**: the counter drops back to 0 once processed.

---

## Reminder — real-time settings

- **Delays**: `~/.proton_sync/realtime.conf` (JSON `debounce_seconds`,
  `cycle_seconds`), written by the GUI, re-read **live** by the consumer on every
  cycle. No restart needed.
- **Queues**: markers in `~/.proton_sync/queue/` (local) and
  `/media/home_nas/proton-sync/queue/<user>/` (NAS over NFS). The GUI counts them
  and can clear them.

---

## Instant detection, delayed sending

Two distinct phases, not to be confused:

- **Detection is instantaneous.** The watchers (inotify) are event-driven: as
  soon as a file moves, the kernel notifies them and the marker is written
  **immediately**. There is no grace delay at this level.
- **The delay is downstream, at consumption.** The `debounce` and the `cycle`
  (default 60 s / 60 s) act on the **consumer**: it groups mature markers and
  **waits** before launching the engine towards Proton.

The debounce is **anchored at the first observation** of a folder, but every new
write into that folder **re-arms** the deadline. In other words, as long as a
folder is actively being modified, it is not considered "stable" and the sync
does not fire: it waits ~`debounce_seconds` **after the last modification**. You
can therefore edit a folder (PDF assembly, batch retouching…) as long as you like
without triggering an upload; the sync only fires once the activity settles.

Practical consequence: seeing dozens of markers pile up while you work is
**normal** — that's the queue filling up while waiting for stabilization. They
then merge into a single sync of the folder (see deduplication below). For a
wider margin, increase `debounce` (GUI section 2); 60 s already covers a normal
editing session since the countdown re-arms on every write.

---

## Who sees what: the two watchers and NFS

Watching is **distributed** between two watchers, and their split comes from a
kernel-level subtlety of NFS.

- The **desktop watcher** (`local_watcher.py`) watches the local sources (ext4)
  **and** the NFS-mounted NAS sources (`/media/nas1…`). All its markers go into
  the **local** queue (`~/.proton_sync/queue/`).
- The **NAS watcher** (`nas_watcher.py`) watches the **NAS's local disk** and
  writes into the **NAS** queue (`/home/nasuser/proton-sync/queue/<user>/`, seen from
  the desktop as `/media/home_nas/proton-sync/queue/<user>/`).

**What the NAS sees of the desktop's NFS writes.** Contrary to popular belief,
the NAS's inotify is not completely blind to writes made by the desktop over NFS.
The NFS server (`nfsd`) executes client requests as genuine local operations on
the NAS disk:

- **Creations, deletions, renames** (*structural*, synchronous operations) →
  **seen** by the NAS's inotify.
- **In-place modifications** of an existing file → **not reliably seen** (the NFS
  client caches data writes and transmits them asynchronously).

It is precisely this last gap — the modifications the NAS misses — that justifies
NFS watching **on the desktop side**: it catches what the NAS cannot see. Rule of
thumb: **the NAS sees the desktop's NFS creations / deletions / renames, but not
in-place modifications; desktop-side watching remains indispensable for those.**

**Double observation, no double upload.** For a structural operation (e.g. PDF
Arranger's atomic write: temp file created then renamed onto the target), **both**
watchers write a marker — the desktop via NFS (local queue) and the NAS locally
(NAS queue). This is not a flaw: the consumer **merges per folder and applies the
debounce**, so these markers resolve into **a single** sync of the affected
folder. No file is transferred twice.

---

## Exclusions in real time

The watchers write a marker for **every** change (they do not apply the
exclusions); the **engine** does the filtering, with a guard dedicated to the
real-time mode (`--subpath`):

- the engine tests **every segment** of the targeted path under the mapping root —
  the target itself (`__pycache__`, `logs`) **and its ancestors** (a target
  `.Trash-1000/info` is skipped because `.Trash-1000` matches `.Trash-*`);
- an excluded path is skipped **cleanly**: no upload, no remote folder creation,
  no deletion;
- the emitted line carries the **stable tag `[subpath-excluded]`** (outside
  translation), which the consumer detects to display "🚫 excluded (name
  filtered) — nothing to sync" instead of an ambiguous "✓ ok".

The log therefore keeps a trace of every filtered attempt (the `→ sync` +
`🚫 excluded` pair) — intentional, for auditing. Removing this noise at the
source (exclusion-aware watchers) remains a deferred improvement.

---

## Daemon language (i18n)

The daemons read the language preference at **startup** (cascade:
`settings.json` → system language → English). After a language change in the GUI
("🌍 Language…"), restart the daemons to apply it:
`systemctl --user restart proton-watch.service proton-consume.service`.
The journal keeps its history in the original language (a mix is normal after a
switch); the **unit descriptions** displayed by systemd ("Started …") are
rewritten in the current language at the next "Install / Update".

---

## Persistence across a reboot

For real-time to restart after a reboot, **three conditions** must hold, in this
order. A single missing link is enough for everything to wait (the daemons run,
but idle).

1. **Network available at boot.** *System* connection profile — the default for
   wired Ethernet. A "for this user only" profile (common with Wi-Fi, key stored
   in the keyring) only comes up at session login. Check:
   `nmcli -f connection.permissions connection show "<name>"` → empty (`--`) =
   system.

2. **NAS mounted at boot.** *This is the main trap, and the cause of a long
   production diagnosis.* If the NAS mounts happen at session login (GVfs/Nemo
   mounts), the daemons start on **empty** mount points and see neither the
   sources nor the marker queue — so nothing syncs until someone opens a
   graphical session.

   **Fix: mount the NFS shares in `/etc/fstab` with `_netdev`** (firm mount at
   boot, after the network). Important: do **not** add `x-systemd.automount`,
   which would only mount at the *first access* — a trap for inotify watchers
   that start before that access.

   ```
   192.168.1.10:/media/nas1  /media/nas1      nfs  _netdev,nofail,rw,hard,proto=tcp,nfsvers=3,exec,auto,acl    0 0
   192.168.1.10:/media/nas2  /media/nas2      nfs  _netdev,nofail,rw,hard,proto=tcp,nfsvers=3,exec,auto,acl    0 0
   192.168.1.10:/home/nasuser    /media/home_nas  nfs  _netdev,nofail,rw,hard,proto=tcp,nfsvers=4.2,exec,auto,acl  0 0
   ```

   Dependent **bind** mounts get
   `bind,x-systemd.requires-mounts-for=/media/nasX` (guarantees ordering: the NFS
   mounts before the bind) and `x-gvfs-hide` (keeps Nemo's sidebar uncluttered,
   especially with two users).

   **Extra net — mount-aware watcher.** Even if a boot race remains (the watcher
   starts before the NFS mount is ready), the local watcher **immediately
   watches** whatever is mounted (the local sources) then **re-scans** the mounts
   — quickly at startup — to add the NAS sources as soon as they appear. It no
   longer stays blind to the NAS for the whole session as before. On the console
   at reboot, you either see the full target list right away, or a ramp-up
   `0 NAS → 🔄 Re-scan: N target(s)` within seconds. It also removes a target
   whose mount goes down (`➖`) and picks it back up if it returns (`➕`).

3. **Keyring unlocked.** The Proton CLI requires the GNOME keyring, which only
   opens at **graphical session login** — *not* a console/TTY login. As long as
   no graphical session is open, the consumer probes authentication
   (`proton_sync.py --check-auth`), finds it locked, writes **a single**
   "⏳ Waiting for session login" line and **keeps** its markers (nothing lost) —
   without launching passes doomed to exit code 2, hence no failure bursts in the
   journal. At login, it **resumes automatically** ("🔓 Session opened —
   resuming") and drains the queue.

**Practical consequence:** after a reboot, **both graphical sessions** (User1 and
User2) must be opened for each queue to drain. Files added in the meantime
(e.g. via SFTP from a phone) pile up in the NAS queue and sync when the relevant
session opens. **This is the intended safety net, not a flaw**: the keyring
protects the Proton credentials at rest. (If a keyring has an *empty* password,
its daemon can sync without a graphical login — more convenient, but credentials
readable at rest. A trade-off to accept explicitly.)

### Checking the chain after a reboot (console, BEFORE login: Ctrl+Alt+F2)

```
# 1. NAS reachable without a session?
ping -c2 192.168.1.10

# 2. NAS actually mounted at boot? (the decisive test)
systemctl --type=mount --all | grep -E 'home_nas|nas1|nas2'   # expected: 3x active/mounted
findmnt /media/home_nas /media/nas1 /media/nas2

# 3. Is the queue visible?
ls /media/home_nas/proton-sync/queue/                          # expected: user1  user2
```

If the three mounts are `active/mounted` and the queue visible **without an open
session**, the mounting is right. All that remains is the keyring, which unlocks
at graphical session login — and the queues drain.

> Diagnostic note: `findmnt` can appear empty right after boot while
> `systemctl --type=mount` correctly shows `active/mounted` — `systemctl` is
> authoritative. And a `#` glued to the start of a command line turns it into a
> comment (the shell ignores it without executing anything).

🇬🇧 English | 🇫🇷 [Français](INSTALLATION-systemd_fr.md)

# Automating the Proton Drive sync with systemd (--user timer)

Schedules the sync once a day at 3:00 am, in the user's session context (hence
with access to the unlocked GNOME keyring).

**Chosen approach**: `systemd --user` timer + graphical session kept permanently
open on the desktop. If the session isn't open when the timer fires, the engine
detects it, exits cleanly (code 2) and retries the next day — no blocking, no
corruption.

> **The easiest path: the GUI's "⏰ Schedule…" window.** It installs and tunes
> the timer in one click — it **generates** `proton-sync.service` + `.timer`,
> reloads systemd and enables the timer — and lets you pick the **frequency**
> (daily, **weekly** with day + time, or hourly) as well as Option A/B. The
> manual procedure below remains valid as a **fallback**, or to understand what
> the GUI does under the hood.
>
> For the **real-time layer** (instant sync via inotify watchers), see
> `INSTALLATION-realtime.md`. With it, this nightly timer mostly serves as a
> **safety net** and is happily set to weekly.

---

## Manual installation (fallback) — for EACH user (User1, then User2)

The commands below must be run **inside the relevant user's session** (logged in
graphically, keyring unlocked). Do not use `sudo` — everything happens at the
user level.

### 1. Copy the service and timer files

```bash
mkdir -p ~/.config/systemd/user
cp proton-sync.service ~/.config/systemd/user/
cp proton-sync.timer   ~/.config/systemd/user/
```

**IMPORTANT for User2**: before copying, edit `proton-sync.service` and replace
`mappings-user1.json` with `mappings-user2.json` (a single line, the `ExecStart`
one).

### 2. Reload systemd and enable the timer

```bash
systemctl --user daemon-reload
systemctl --user enable --now proton-sync.timer
```

### 3. Check the timer is armed

```bash
systemctl --user list-timers proton-sync.timer
```

You should see the next trigger (NEXT) at 3:00 am the following day.

### 4. Test the service immediately (without waiting for 3:00 am)

```bash
systemctl --user start proton-sync.service
```

Then check the result:

```bash
# Service state (active/inactive, exit code)
systemctl --user status proton-sync.service

# Full log of the last pass
journalctl --user -u proton-sync.service -n 50 --no-pager
```

---

## Allowing execution without an active graphical session (linger)

By default, `--user` services stop when the user logs out. Since the session is
kept permanently open on the desktop, this is not strictly necessary — but
enabling "linger" makes the system more robust (the timer survives an accidental
logout, and restarts when the machine boots).

**This command needs admin rights (once, per user)**:

```bash
sudo loginctl enable-linger myuser
sudo loginctl enable-linger user2
```

WARNING: linger keeps the services running even without an open graphical
session. BUT the keyring stays locked until the session is opened. So with
linger alone (no open session), the engine will run but exit cleanly with code 2
(authentication impossible). That's intended: no blocking, just a skipped pass
until the session is reopened.

To check the linger state:

```bash
loginctl show-user myuser | grep Linger
```

---

## Behavior when the session is not open

If the timer fires while the user's session isn't open (keyring locked):

1. The engine runs its authentication test (`filesystem list /`) at startup
2. It fails (keyring locked)
3. The engine prints a clear message and exits with **code 2**
4. systemd treats this as a success (thanks to `SuccessExitStatus=0 2`), so NO
   failure notification, NO service in "failed" state
5. The cache is untouched, the lock is released
6. The next day at 3:00 am (or as soon as the session reopens), a new attempt

To see whether a pass was skipped for this reason:

```bash
journalctl --user -u proton-sync.service | grep -i "authenticat"
```

---

## Collision with real-time (automatic retry)

The scheduled pass and the **real-time consumer** share the lock
`~/.proton_sync.lock`: never two passes in parallel. If the consumer holds the
lock when the timer fires (e.g. a phone backup in the middle of the night woke
up the real-time layer), the scheduled engine exits with **failure (code 1)**.

So that this doesn't skip the whole nightly pass, the service uses `Type=exec`
with `Restart=on-failure` + `RestartSec=120`: systemd **automatically retries
~2 minutes later**, once the consumer has finished and released the lock.
`StartLimitBurst=6` (over `StartLimitIntervalSec=1h`) bounds the attempts to
avoid any loop if the problem persists. Exit code 2 (locked keyring) remains a
success (`SuccessExitStatus=0 2`) and therefore triggers **no** pointless retry.

> These settings are generated automatically by the GUI's "⏰ Schedule…" window
> ("Install / Update" button). After updating the project, replay
> "Install / Update" **for User1 AND for User2** so the new unit replaces the
> old one. Verification:
>
> ```bash
> systemctl --user cat proton-sync.service | grep -E "Type=|Restart=|RestartSec="
> ```
>
> You should see `Type=exec`, `Restart=on-failure`, `RestartSec=120`.

To observe a retry after a real collision:

```bash
journalctl --user -u proton-sync.service --since today --no-pager
```

You'll see a failure ("Another instance… is already running", `status=1`)
followed, ~2 min later, by a new "Starting…" then a successful "Finished…".

---

## Run history (GUI)

The **"📜 Run history…"** button in the Schedule window gives access to the
scheduled service's journal without any manual command:

- **Last run** by default — isolated by its **start boundary** in the journal
  (reliable even after a reboot, unlike systemd's runtime state, wiped on every
  restart);
- **summary** at the top: date + result ("✅ success", "❌ failure (code 1)" =
  lock collision, "⏹ interrupted") — detection markers in FR **and** EN, the
  history stays readable whatever the language;
- **date picker** (calendar) for a specific day.

No file is created: it reads the **systemd journal**, self-limited
(`SystemMaxUse`, automatic purge) — several months of history in practice.
Command-line equivalents:

```
journalctl --user -u proton-sync.service -n 200 --no-pager
journalctl --user -u proton-sync.service --since "2026-07-01" --until "2026-07-01 23:59:59"
```

---

## Changing the time or the frequency

**The easiest is the GUI's "⏰ Schedule…" window** (Frequency menu: Daily /
Weekly / Hourly, + day and time), then "Install / Update". It rewrites the
`OnCalendar` and reloads the timer for you. Equivalent manual method: edit
`~/.config/systemd/user/proton-sync.timer`, change the `OnCalendar=` line, then:

```bash
systemctl --user daemon-reload
systemctl --user restart proton-sync.timer
```

Sample `OnCalendar` values:
- `*-*-* 03:00:00`        -> every day at 3:00 am
- `*-*-* 03,15:00:00`     -> every day at 3:00 am AND 3:00 pm
- `Mon *-*-* 03:00:00`    -> every Monday at 3:00 am
- `*-*-* *:00:00`         -> every hour on the hour

---

## Deletion propagation in the schedule: Option A vs Option B

**Crucial to understand**: setting `allow_delete: true` in the JSON is NOT
enough for the schedule to delete. The engine only propagates deletions when the
`--delete` flag is present on the service's `ExecStart` line. The systemd
service alone therefore decides whether the nightly schedule is additive or a
mirror — regardless of the JSON's content.

### Option A — ADDITIVE schedule (current configuration, recommended)

`ExecStart` WITHOUT `--delete` (the base installation):
```
ExecStart=/usr/bin/python3 %h/Logiciels/Proton-drive/proton_sync.py %h/Logiciels/Proton-drive/mappings-user1.json
```
The 3 am passes send but never delete. Deletions only happen when launched
manually with `--delete` (GUI: "Propagate deletions" checkbox, or command line).
Cautious mode, full control.

### Option B — MIRROR schedule (automatic deletions)

`ExecStart` WITH `--delete`:
```
ExecStart=/usr/bin/python3 %h/Logiciels/Proton-drive/proton_sync.py %h/Logiciels/Proton-drive/mappings-user1.json --delete
```
The 3 am pass becomes a true mirror: what is deleted locally disappears from
Proton (according to each mapping's `delete_mode`, subject to the mount guard).
Safety nets: the several-hour window before 3 am + the 30-day Proton trash
(mappings in `trash` mode).

**To switch A -> B**: edit `~/.config/systemd/user/proton-sync.service`, append
`--delete` at the end of the `ExecStart` line, then:
```bash
systemctl --user daemon-reload
systemctl --user restart proton-sync.timer
```

**Prerequisites before Option B**:
- `mount_check.py` MUST be next to `proton_sync.py` (otherwise deletions are refused)
- Several uneventful manual `--delete` passes beforehand
- Decide separately for User1 and for User2 (each has their own service)

**Current choice: Option A** (additive; manual deletions only).

---

## Monthly deep verification (--verify-hash) — optional

To additionally schedule a monthly SHA1 verification (robocopy /IS equivalent),
create a 2nd service+timer pair, e.g. `proton-verify.service`:

ExecStart with `--verify-hash` added:
```
ExecStart=/usr/bin/python3 %h/Logiciels/Proton-drive/proton_sync.py %h/Logiciels/Proton-drive/mappings-user1.json --verify-hash
```

And a `proton-verify.timer`:
```
OnCalendar=*-*-01 02:00:00     # the 1st of each month at 2:00 am
Persistent=true
```

---

## Unit language (i18n)

The `.service`/`.timer` files are generated with their `Description=` in the
**current language at "Install / Update" time**, then frozen (the nature of
systemd). After a language change in the GUI, redo an "Install / Update"
(Schedule **and** Real-time) to rewrite the descriptions; the journal's
"Started …" lines will then use the new language (the history itself doesn't
change).

---

## Disabling the automation

```bash
systemctl --user disable --now proton-sync.timer
```

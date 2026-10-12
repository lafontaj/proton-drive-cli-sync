"""Watch the chosen folder and ask for a sync after a short quiet period.

The window owns this. A systemd unit is not required for a new file to sync.
A remote listing repeats on its own while that window, or its tray, is alive.
"""

import os
import subprocess
import threading
import time

try:
    from i18n import _
except ImportError:
    def _(s):
        return s

QUIET_SECONDS = 2.0
REMOTE_SECONDS = 30.0


def due(last_event, now, quiet=QUIET_SECONDS):
    """True once ``quiet`` seconds have passed since the latest event."""
    if last_event is None:
        return False
    return (now - last_event) >= quiet


def ignored_edit_name(name):
    """Editor scratch files. A real name still starts a pass."""
    if not name:
        return True
    return name.startswith(".") or name.endswith("~")


class PassFollow:
    """One change during a running pass schedules one pass after it, not a pile."""

    def __init__(self):
        self.again = False

    def request(self, running):
        if running:
            self.again = True
            return False
        return True

    def finished(self):
        if not self.again:
            return False
        self.again = False
        return True


def status_line(folder, last_pass, next_check):
    """Folder, last pass, and the next remote listing. A quiet minute is visible."""
    name = os.path.basename(os.path.normpath(folder or "")) or (folder or "")
    if last_pass:
        last = time.strftime("%H:%M", time.localtime(last_pass))
    else:
        last = _("not yet")
    if next_check:
        nxt = time.strftime("%H:%M", time.localtime(next_check))
    else:
        nxt = _("not scheduled")
    return _("{folder} — last pass {last} — next remote check {next}").format(
        folder=name, last=last, next=nxt)


def short_commit(root):
    """Short git commit for the window title. Empty when git cannot answer."""
    try:
        result = subprocess.run(
            ["git", "-C", root, "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    text = (result.stdout or "").strip()
    if text and all(char in "0123456789abcdef" for char in text):
        return text
    return ""


class LiveSync:
    def __init__(self):
        self._stop = threading.Event()
        self._thread = None
        self._source = None
        self._on_change = None
        self._dirty_at = None

    def follow(self, source, on_change):
        self.stop()
        self._stop = threading.Event()
        self._dirty_at = None
        self._source = source
        self._on_change = on_change
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)

    def _consider(self, event):
        name = os.path.basename(getattr(event, "pathname", "") or "")
        if ignored_edit_name(name):
            return
        self._note()

    def _note(self):
        self._dirty_at = time.monotonic()

    def _run(self):
        stop = self._stop
        source = self._source
        try:
            import pyinotify
        except ImportError:
            self._poll(stop, source)
            return
        manager = pyinotify.WatchManager()
        mask = (
            pyinotify.IN_CLOSE_WRITE | pyinotify.IN_CREATE | pyinotify.IN_DELETE
            | pyinotify.IN_MOVED_TO | pyinotify.IN_MOVED_FROM
        )

        class _Handler(pyinotify.ProcessEvent):
            def process_IN_CLOSE_WRITE(self_inner, event):
                self._consider(event)

            def process_IN_CREATE(self_inner, event):
                self._consider(event)

            def process_IN_DELETE(self_inner, event):
                self._consider(event)

            def process_IN_MOVED_TO(self_inner, event):
                self._consider(event)

            def process_IN_MOVED_FROM(self_inner, event):
                self._consider(event)

        try:
            manager.add_watch(source, mask, rec=True, auto_add=True)
        except Exception:
            self._poll(stop, source)
            return
        notifier = pyinotify.Notifier(manager, _Handler())
        try:
            while not stop.is_set():
                if notifier.check_events(timeout=400):
                    notifier.read_events()
                    notifier.process_events()
                self._pump_once()
        finally:
            notifier.stop()

    def _pump_once(self):
        dirty = self._dirty_at
        if not due(dirty, time.monotonic()):
            return
        self._dirty_at = None
        callback = self._on_change
        if callback is not None:
            callback()

    def _poll(self, stop, source):
        previous = _signature(source)
        while not stop.is_set():
            stop.wait(QUIET_SECONDS)
            current = _signature(source)
            if current != previous:
                previous = current
                callback = self._on_change
                if callback is not None:
                    callback()


def _signature(root):
    count = 0
    newest = 0
    if not root or not os.path.isdir(root):
        return (0, 0)
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            if ignored_edit_name(name):
                continue
            count += 1
            try:
                newest = max(newest, os.path.getmtime(os.path.join(dirpath, name)))
            except OSError:
                pass
    return (count, newest)

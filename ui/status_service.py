"""The file-manager status process, owned by the Qt window.

Dolphin asks this process for overlays. The window starts it and stops it
on Quit. A name that is already owned is left alone.
"""

import os
import subprocess
import sys

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUS_NAME = "org.protondrivesync.CloudProviders"


def close_action(quitting, dirty, confirmed):
    """What closing the window does.

    ``quit`` exits. ``hide`` leaves the process in the tray. ``stay`` keeps
    the window open because the user cancelled the unsaved-changes prompt.
    """
    if quitting:
        return "quit"
    if dirty and not confirmed:
        return "stay"
    return "hide"


def _name_owned(probe):
    if probe is not None:
        return bool(probe())
    try:
        import dbus
        return bool(dbus.SessionBus().name_has_owner(BUS_NAME))
    except Exception:
        return False


class StatusService:
    """One child running ``cloudproviders.py`` for the open mappings file."""

    def __init__(self, probe=None, popen=None):
        self._probe = probe
        self._popen = popen or subprocess.Popen
        self._proc = None
        self._path = None

    def running(self):
        return self._proc is not None and self._proc.poll() is None

    def ensure(self, mappings_path):
        if not isinstance(mappings_path, str) or not os.path.isfile(mappings_path):
            return False
        if self.running() and self._path == mappings_path:
            return True
        if self.running():
            self.stop()
        if _name_owned(self._probe):
            return False
        script = os.path.join(APP_DIR, "cloudproviders.py")
        self._proc = self._popen(
            [sys.executable, script, "--mappings", mappings_path],
            cwd=APP_DIR,
            start_new_session=True,
        )
        self._path = mappings_path
        return True

    def stop(self):
        proc = self._proc
        self._proc = None
        self._path = None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()

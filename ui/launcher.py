"""Lanceurs .desktop et icône de barre. L'exécutable est ``python -m ui``."""

import os
import shlex
import subprocess
import sys

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_NAME = "proton-drive-sync.desktop"


def _quote(arg):
    """Citation Desktop Entry : guillemets si l'argument contient un caractère réservé."""
    if arg and all(c not in ' \t\n"\'\\`$<>~|&;()*?#' for c in arg):
        return arg
    esc = (arg.replace("\\", "\\\\").replace('"', '\\"')
              .replace("`", "\\`").replace("$", "\\$"))
    return '"' + esc + '"'


def _desktop_dir():
    try:
        result = subprocess.run(
            ["xdg-user-dir", "DESKTOP"], capture_output=True, text=True, timeout=5)
        folder = (result.stdout or "").strip()
        if folder and os.path.isdir(folder):
            return folder
    except (OSError, subprocess.SubprocessError):
        pass
    return os.path.expanduser("~/Desktop")


def menu_path():
    return os.path.expanduser("~/.local/share/applications/" + _NAME)


def desktop_path():
    return os.path.join(_desktop_dir(), _NAME)


def content(target_path=None):
    """Fichier .desktop. ``Path`` est le dossier du paquet, pour ``python -m ui``."""
    icon = os.path.join(APP_DIR, "icone.png")
    exec_line = f"{_quote(sys.executable)} -m ui"
    if target_path:
        exec_line += f" {_quote(target_path)}"
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Version=1.0\n"
        "Name=Proton Drive Sync\n"
        "Comment=Folder sync to Proton Drive\n"
        f"Path={APP_DIR}\n"
        f"Exec={exec_line}\n"
        f"Icon={icon}\n"
        "Terminal=false\n"
        "Categories=Network;\n")


def _write(path, text, trusted=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)
    os.chmod(path, 0o755)
    if trusted:
        try:
            subprocess.run(
                ["gio", "set", path, "metadata::trusted", "true"],
                capture_output=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass


def existing_opens_current():
    """True si un lanceur déjà écrit ouvre un fichier de mappings."""
    for path in (menu_path(), desktop_path()):
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    if line.startswith("Exec="):
                        try:
                            parts = shlex.split(line[5:].strip())
                        except ValueError:
                            parts = line[5:].split()
                        return "ui" in parts and len(parts) > parts.index("ui") + 1
        except OSError:
            continue
    return False


def apply_launcher(menu_enabled, desktop_enabled, target_path):
    text = content(target_path)
    try:
        if menu_enabled:
            _write(menu_path(), text)
        elif os.path.exists(menu_path()):
            os.remove(menu_path())
    except OSError:
        pass
    try:
        if desktop_enabled:
            _write(desktop_path(), text, trusted=True)
        elif os.path.exists(desktop_path()):
            os.remove(desktop_path())
    except OSError:
        pass
    try:
        subprocess.run(
            ["update-desktop-database", os.path.dirname(menu_path())],
            capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


def ui_autostart_path():
    return os.path.expanduser("~/.config/autostart/proton-drive-sync-ui.desktop")


def remove_ui_autostart():
    try:
        os.remove(ui_autostart_path())
    except FileNotFoundError:
        pass
    except OSError:
        pass


def install_ui_autostart():
    """Plasma login starts ``python -m ui``. The XApp tray file is not touched.

    Only after the person asked for it: live sync for a folder, or the
    Settings checkbox. Opening the window never writes this file.
    """
    path = ui_autostart_path()
    body = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Proton Drive Sync\n"
        "Comment=Folder sync to Proton Drive\n"
        f"Exec={_quote(sys.executable)} -m ui\n"
        f"Path={APP_DIR}\n"
        f"Icon={os.path.join(APP_DIR, 'icone.png')}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n")
    try:
        _write(path, body)
    except OSError:
        pass


def apply_tray(enabled):
    """Autostart de l'indicateur. Le script reste tray_indicator.py."""
    path = os.path.expanduser("~/.config/autostart/proton-drive-sync-tray.desktop")
    script = os.path.join(APP_DIR, "tray_indicator.py")
    if not enabled:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass
        return
    body = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Proton Drive Sync — status icon\n"
        f"Exec={_quote(sys.executable)} {_quote(script)}\n"
        f"Icon={os.path.join(APP_DIR, 'tray_connected.png')}\n"
        "X-GNOME-Autostart-enabled=true\n")
    try:
        _write(path, body)
    except OSError:
        pass
    try:
        subprocess.Popen(
            [sys.executable, script], cwd=APP_DIR, start_new_session=True)
    except OSError:
        pass

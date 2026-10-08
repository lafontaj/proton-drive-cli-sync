"""Where settings.json and the proton-drive binary live.

config.py and i18n.py both call settings_path(). The CLI order lives only in
resolve_cli(). This module imports neither config nor i18n, so those imports
cannot cycle. Nothing here raises: a missing home, a read-only config
directory, or a failed copy falls back to a path.
"""
__version__ = "1.0.1"   # version propre à CE fichier ; incrémentée quand il change (indépendant de GitHub)

import json
import os
import shutil
import sys

# Posée dans la copie XDG. L'éditeur l'affiche une fois, puis la retire.
# Un démarrage moteur ultérieur ne la touche pas.
SETTINGS_MOVE_NOTICE_KEY = "settings_move_notice_pending"

APP_DIR = os.path.dirname(os.path.abspath(__file__))

_unwritable_said = False


def _(message):
    """Translate when i18n is already loaded. English source otherwise."""
    module = sys.modules.get("i18n")
    translate = getattr(module, "_", None) if module is not None else None
    if callable(translate) and translate is not _:
        try:
            return translate(message)
        except Exception:
            return message
    return message


def xdg_config_home():
    raw = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if raw:
        return raw
    return os.path.join(os.path.expanduser("~"), ".config")


def xdg_settings_path():
    return os.path.join(xdg_config_home(), "proton-drive-sync", "settings.json")


def legacy_settings_path():
    return os.path.join(APP_DIR, "settings.json")


def _say(text):
    try:
        print(text, file=sys.stderr)
    except Exception:
        pass


def _stamp_move_notice(dest):
    """Après une copie réussie, mémorise l'avis pour l'éditeur.

    Un fichier qui n'est pas un objet JSON est laissé tel quel : pas de clé,
    pas d'exception. Un échec d'écriture n'annule pas la copie.
    """
    try:
        with open(dest, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    data[SETTINGS_MOVE_NOTICE_KEY] = True
    tmp = "%s.%d.notice.tmp" % (dest, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=1)
            handle.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, dest)
        os.chmod(dest, 0o600)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def settings_move_notice_pending():
    """True si le fichier de réglages en usage porte encore l'avis."""
    try:
        with open(settings_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get(SETTINGS_MOVE_NOTICE_KEY) is True


def settings_move_notice_text():
    """Les trois points de l'avis : nouvel emplacement, ancien fichier ignoré,
    dossier à ajouter aux sauvegardes. Sans Tk : l'éditeur affiche ce texte."""
    path = settings_path()
    folder = os.path.join(xdg_config_home(), "proton-drive-sync")
    return _(
        "Settings now live in {path}.\n\n"
        "The file beside the scripts is no longer read.\n\n"
        "A backup of the scripts folder no longer includes the configuration. "
        "Add this folder to your backups: {folder}."
    ).format(path=path, folder=folder)


def clear_settings_move_notice():
    """Retire la clé d'avis. Toutes les autres clés restent. False si rien à faire."""
    path = settings_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict) or SETTINGS_MOVE_NOTICE_KEY not in data:
        return False
    del data[SETTINGS_MOVE_NOTICE_KEY]
    tmp = "%s.%d.notice.tmp" % (path, os.getpid())
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, mode=0o700, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=1)
            handle.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
        return True
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def _migrate_legacy(legacy, dest):
    """Copy legacy settings onto dest. Leave legacy in place. Mode 0600."""
    tmp = "%s.%d.tmp" % (dest, os.getpid())
    try:
        os.makedirs(os.path.dirname(dest), mode=0o700, exist_ok=True)
        shutil.copyfile(legacy, tmp)
        os.chmod(tmp, 0o600)
        os.replace(tmp, dest)
        os.chmod(dest, 0o600)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False
    _stamp_move_notice(dest)
    _say(_("Settings copied to {p}. The previous file was left in place.").format(p=dest))
    return True


def settings_path():
    """Path of settings.json for this process.

    1. PROTON_SYNC_SETTINGS, when set and non-empty.
    2. The XDG path, when that file already exists.
    3. A one-time copy of APP_DIR/settings.json into the XDG path, when the
       legacy file exists and the copy succeeds. The legacy file stays.
    4. The legacy file, when the XDG directory cannot be written.
    5. The XDG path (created on the first write).
    """
    global _unwritable_said
    try:
        override = os.environ.get("PROTON_SYNC_SETTINGS", "").strip()
        if override:
            return override
        dest = xdg_settings_path()
        try:
            dest_exists = os.path.isfile(dest)
        except OSError:
            dest_exists = False
        if dest_exists:
            return dest
        legacy = legacy_settings_path()
        try:
            legacy_exists = os.path.isfile(legacy)
        except OSError:
            legacy_exists = False
        if legacy_exists:
            if _migrate_legacy(legacy, dest):
                return dest
            if not _unwritable_said:
                _unwritable_said = True
                _say(_(
                    "Could not write settings to {p}; reading {legacy} instead."
                ).format(p=dest, legacy=legacy))
            return legacy
        return dest
    except Exception:
        try:
            return legacy_settings_path()
        except Exception:
            return "settings.json"


def resolve_cli(app_dir, configured):
    """Path of the proton-drive binary. One order, nowhere else:

    1. PROTON_DRIVE_CLI, when set and non-empty.
    2. `configured` (the proton_cli_path setting).
    3. An executable proton-drive next to the scripts (`app_dir`).
    4. proton-drive on PATH.
    5. app_dir/proton-drive anyway, so an error can name that path.
    """
    env = os.environ.get("PROTON_DRIVE_CLI")
    if env:
        return env
    if isinstance(configured, str) and configured.strip():
        return configured
    bundled = os.path.join(app_dir, "proton-drive")
    if os.path.isfile(bundled) and os.access(bundled, os.X_OK):
        return bundled
    found = shutil.which("proton-drive")
    if found:
        return found
    return bundled

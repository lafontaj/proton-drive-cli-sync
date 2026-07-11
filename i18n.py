#!/usr/bin/env python3
"""
i18n.py — Translation foundation for the Proton Drive sync project (GNU gettext).

Single source of truth for language resolution, shared by the GUI, the engine
and the daemons (no duplicated logic). Import pattern:

    try:
        from i18n import _
    except ImportError:          # standalone deployment (e.g. NAS without i18n)
        def _(s):                # graceful fallback: source strings (English)
            return s

Language resolution order:
  1. Explicit user preference in settings.json ("language": "en" / "fr")
  2. "auto" (or missing) -> operating system language (LC_MESSAGES / LANG)
  3. Fallback -> English (the language of the source msgid strings)

Compiled catalogs are expected under:
    <project dir>/locale/<lang>/LC_MESSAGES/proton-sync.mo
Their absence never breaks anything: gettext falls back to the source strings.
"""
__version__ = "1.0.0"   # version propre à CE fichier ; incrémentée quand il change (indépendant de GitHub)

import gettext
import json
import os

APP_DIR = os.path.dirname(os.path.abspath(__file__))
LOCALE_DIR = os.path.join(APP_DIR, "locale")
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")
DOMAIN = "proton-sync"

SUPPORTED = ("en", "fr", "de", "es", "it", "pt")
SOURCE_LANGUAGE = "en"   # language of the msgid strings in the code (Option B)


# ---------- settings.json (shared, key preserved among others) ----------

def _read_settings():
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def read_language_setting():
    """Return the stored preference: 'en', 'fr' or 'auto' (default)."""
    lang = read_setting("language", "auto")
    return lang if lang in SUPPORTED + ("auto",) else "auto"


def write_language_setting(lang):
    """Persist the preference ('en', 'fr' or 'auto'), preserving other keys.
    Returns True on success."""
    if lang not in SUPPORTED + ("auto",):
        return False
    return write_setting("language", lang)


# ---------- generic settings.json access (any key, shared with config.py) ----------
# config.py builds its typed accessors (nas_enabled, rename_ext_enabled, etc.) on
# top of these two functions, so there is a SINGLE read/write/atomic-write
# mechanism for the whole settings.json file, never duplicated.

def read_setting(key, default=None):
    """Generic settings.json getter for any key."""
    return _read_settings().get(key, default)


def write_setting(key, value):
    """Generic settings.json setter for any key, preserving all other keys.
    Returns True on success (same atomic write as write_language_setting)."""
    data = _read_settings()
    data[key] = value
    try:
        tmp = SETTINGS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, SETTINGS_PATH)
        return True
    except OSError:
        return False


# ---------- language resolution ----------

def system_language():
    """Two-letter OS language code from the environment, or ''."""
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        val = os.environ.get(var, "")
        if val:
            code = val.split(".")[0].split("_")[0].lower()
            if code:
                return code
    return ""


def resolve_language():
    """Effective language: explicit setting > OS language > source language."""
    pref = read_language_setting()
    if pref in SUPPORTED:
        return pref
    sys_lang = system_language()
    return sys_lang if sys_lang in SUPPORTED else SOURCE_LANGUAGE


# ---------- gettext installation ----------

def install():
    """Return the translation function for the resolved language. Never raises:
    missing catalogs fall back to the source (English) strings."""
    lang = resolve_language()
    try:
        tr = gettext.translation(DOMAIN, LOCALE_DIR, languages=[lang],
                                 fallback=True)
    except Exception:
        tr = gettext.NullTranslations()
    return tr.gettext


# ---------- locale environment for external subprocesses ----------

def _available_locales():
    """Set of locales installed on the system (via `locale -a`), lowercase."""
    import subprocess
    try:
        out = subprocess.run(["locale", "-a"], capture_output=True, text=True,
                             timeout=5).stdout
        return {l.strip().lower() for l in out.splitlines() if l.strip()}
    except Exception:
        return set()


def subprocess_env():
    """Environment dict for launching EXTERNAL localized programs (e.g. zenity)
    so that their own UI (calendar, buttons…) follows the language resolved by
    this module rather than the system locale. Returns a copy of os.environ,
    possibly with LC_ALL/LANGUAGE overridden; never raises."""
    env = os.environ.copy()
    lang = resolve_language()
    if lang == system_language():
        return env  # system already matches the chosen language
    if lang == "en":
        # C.UTF-8 is always available and yields English.
        env["LC_ALL"] = "C.UTF-8"
        env["LANGUAGE"] = "en"
        return env
    # Non-English target: pick an installed locale for that language, if any.
    avail = _available_locales()
    # Régions usuelles par langue (première trouvée = utilisée). Générique :
    # toute langue de SUPPORTED est couverte, avec des régions plausibles.
    _REGIONS = {"fr": ("CA", "FR"), "en": ("US", "GB"), "de": ("DE", "AT"),
                "es": ("ES", "MX"), "it": ("IT",), "pt": ("PT", "BR")}
    cands = [f"{lang}.utf8", f"{lang}.UTF-8"]
    for reg in _REGIONS.get(lang, ()):
        cands += [f"{lang}_{reg}.utf8", f"{lang}_{reg}.UTF-8"]
    for cand in cands:
        if cand.lower() in avail:
            env["LC_ALL"] = cand
            env["LANGUAGE"] = lang
            return env
    # No matching locale generated on this system: leave the environment as is
    # (the external tool will follow the system language).
    return env


_ = install()

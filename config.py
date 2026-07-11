#!/usr/bin/env python3
"""
config.py — Single source of truth for per-installation settings and per-user
runtime data directories, shared by the GUI, the engine and the daemons on the
local-machine side (no parallel logic, no duplicated constants).

Two concerns:

  1. Typed settings on top of the SAME settings.json already used by i18n.py
     for the language preference — one file, one atomic-write mechanism, no
     second config format:
       - nas_enabled                   (bool)
       - nas_mount_path                (str)
       - proton_cli_path               (str or None = built-in resolution)
       - rename_ext_enabled            (bool)
       - rename_ext_collision_suffix   (str)

  2. DATA_DIR and its subpaths (cache, queue, logs): a single computed
     location per installation (~/.proton-drive-sync), with a ONE-TIME, safe
     migration from the legacy split locations (~/.proton_sync_cache and
     ~/.proton_sync) the first time this module is imported after the update.
     A rename within the same filesystem does not touch file contents: the
     existing cache is preserved as-is — no re-scan needed.

Import pattern (mirrors i18n.py, for resilience if this file is ever missing
from a deployment):

    try:
        import config as appconfig
    except ImportError:
        appconfig = None   # callers fall back to their own built-in defaults
"""
__version__ = "1.0.0"   # version propre à CE fichier ; incrémentée quand il change (indépendant de GitHub)

import json
import os

try:
    import i18n
    from i18n import _
    _HAS_I18N = True
except ImportError:
    _HAS_I18N = False
    def _(s):
        return s

APP_DIR = os.path.dirname(os.path.abspath(__file__))
_SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")

# ─────────────────────────────────────────────────────────────────────────
#  1) Réglages typés (settings.json)
# ─────────────────────────────────────────────────────────────────────────

DEFAULTS = {
    "nas_enabled": True,
    "nas_mount_path": "/media/home_nas",
    "proton_cli_path": None,          # None = résolution intégrée (voir resolve_proton_cli)
    "rename_ext_enabled": True,
    "rename_ext_collision_suffix": "_ProtonEditExt",
    "tray_enabled": False,            # icône d'état dans la barre des tâches (tray_indicator.py)
    "account_name": None,             # identité NAS stable (None = auto : amorçage intelligent)
    # Correspondance des chemins de DONNÉES entre cette machine (desktop) et le
    # NAS, pour le cas où le NAS voit les mêmes dossiers sous d'autres chemins
    # (ex. Synology : /volume1/... côté NAS vs /media/nas1/... côté desktop).
    # Liste de paires {"local": "<chemin desktop>", "nas": "<chemin NAS>"}.
    # VIDE = chemins identiques des deux côtés (cas d'un NAS Linux monté à
    # l'identique) -> aucune traduction. Utilisée UNIQUEMENT par le watcher NAS
    # (surveillance + écriture des marqueurs) ; poussée avec la config.
    "nas_path_map": [],
}

# Caractères interdits dans le suffixe de collision : casseraient un nom de
# fichier ou un chemin (séparateurs, guillemets).
_SUFFIX_FORBIDDEN = set("/\\\"'")


def _read_raw():
    """Lecture brute de settings.json. Utilise i18n.py si présent (mécanisme
    déjà testé), sinon un repli minimal (même fichier, même format)."""
    if _HAS_I18N:
        return i18n._read_settings()
    try:
        with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_raw(key, value):
    """Écriture atomique d'une clé (préserve les autres). Utilise i18n.py si
    présent, sinon un repli minimal identique à son mécanisme."""
    if _HAS_I18N:
        return i18n.write_setting(key, value)
    data = _read_raw()
    data[key] = value
    try:
        tmp = _SETTINGS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _SETTINGS_PATH)
        return True
    except OSError:
        return False


def get(key, default=None):
    """Valeur d'un réglage : settings.json si la clé y est, sinon le défaut
    connu de ce module, sinon `default`."""
    data = _read_raw()
    if key in data:
        return data[key]
    if key in DEFAULTS:
        return DEFAULTS[key]
    return default


def _put(key, value):
    """Persiste un réglage (préserve les autres clés). Retourne True si OK."""
    return _write_raw(key, value)


# ---------- accesseurs typés (un par réglage) ----------

def nas_enabled():
    return bool(get("nas_enabled"))


def set_nas_enabled(value):
    return _put("nas_enabled", bool(value))


def nas_mount_path():
    v = get("nas_mount_path")
    return v if isinstance(v, str) and v.strip() else DEFAULTS["nas_mount_path"]


def set_nas_mount_path(path):
    path = (path or "").strip()
    if not path:
        return False
    return _put("nas_mount_path", path)


def nas_path_map():
    """Liste de paires de correspondance de chemins de données desktop<->NAS.
    Chaque élément : {"local": "<chemin desktop>", "nas": "<chemin NAS>"}. Filtre
    les entrées mal formées ou vides. Liste vide = aucune traduction."""
    raw = get("nas_path_map")
    out = []
    if isinstance(raw, list):
        for it in raw:
            if not isinstance(it, dict):
                continue
            loc = (it.get("local") or "").strip()
            nas = (it.get("nas") or "").strip()
            if loc and nas:
                out.append({"local": loc, "nas": nas})
    return out


def set_nas_path_map(pairs):
    """Persiste la liste de paires (chacune {local, nas}). Ignore les entrées
    incomplètes. Accepte une liste vide (= pas de traduction)."""
    clean = []
    for it in (pairs or []):
        if not isinstance(it, dict):
            continue
        loc = (it.get("local") or "").strip()
        nas = (it.get("nas") or "").strip()
        if loc and nas:
            clean.append({"local": loc, "nas": nas})
    return _put("nas_path_map", clean)


def _pair_key(local, nas):
    """Clé stable identifiant une paire de correspondance EXACTE. Le verdict n'est
    valable que pour cette clé : dès que local ou nas change, la clé change et le
    verdict mémorisé ne correspond plus (il faut re-tester)."""
    return f"{(local or '').strip()}|{(nas or '').strip()}"


def selftest_verdict(local, nas):
    """Retourne la couleur mémorisée pour cette paire EXACTE ('green'/'yellow'/
    'red') ou None si jamais testée / invalidée. Le stockage est un dict
    {clé: couleur} sous 'nas_path_selftest'."""
    store = get("nas_path_selftest")
    if not isinstance(store, dict):
        return None
    v = store.get(_pair_key(local, nas))
    return v if v in ("green", "yellow", "red") else None


def set_selftest_verdict(local, nas, color):
    """Mémorise la couleur du dernier test pour cette paire EXACTE. color=None
    efface l'entrée (ex. la paire a été éditée -> verdict caduc)."""
    store = get("nas_path_selftest")
    if not isinstance(store, dict):
        store = {}
    else:
        store = dict(store)
    key = _pair_key(local, nas)
    if color in ("green", "yellow", "red"):
        store[key] = color
    else:
        store.pop(key, None)
    return _put("nas_path_selftest", store)


def pair_covering(path, pairs=None):
    """Retourne la paire {local, nas} dont le préfixe 'local' couvre `path`
    (frontière de segment), ou None. Sert à savoir si un mapping dépend d'une
    correspondance déclarée (et donc s'il faut vérifier son verdict de test)."""
    if pairs is None:
        pairs = nas_path_map()
    if not pairs:
        return None
    norm = os.path.normpath(path)
    for it in pairs:
        src = os.path.normpath(it["local"])
        if norm == src or norm.startswith(src.rstrip("/") + os.sep):
            return it
    return None


def translate_path(path, direction, pairs=None):
    """Traduit un chemin d'un référentiel à l'autre par substitution de préfixe.

    direction = "local_to_nas" : desktop -> NAS (ex. /media/nas1/x -> /volume1/x),
                utilisée par le watcher NAS pour savoir quel dossier RÉEL surveiller.
    direction = "nas_to_local" : NAS -> desktop (ex. /volume1/x -> /media/nas1/x),
                utilisée par le watcher NAS pour écrire le marqueur dans le
                référentiel des mappings (compris par le consommateur).

    Applique la PREMIÈRE paire dont le préfixe source correspond (frontière de
    segment : /media/nas1 correspond à /media/nas1 et /media/nas1/..., pas à
    /media/nas10). Si aucune paire ne correspond (ou liste vide), renvoie le
    chemin inchangé — donc l'installation à chemins identiques n'est pas affectée.
    """
    if pairs is None:
        pairs = nas_path_map()
    if not pairs:
        return path
    norm = os.path.normpath(path)
    for it in pairs:
        src = os.path.normpath(it["local"] if direction == "local_to_nas" else it["nas"])
        dst = os.path.normpath(it["nas"] if direction == "local_to_nas" else it["local"])
        if norm == src:
            return dst
        prefix = src.rstrip("/") + os.sep
        if norm.startswith(prefix):
            return os.path.normpath(dst.rstrip("/") + os.sep + norm[len(prefix):])
    return path


def proton_cli_path():
    """Chemin CONFIGURÉ du binaire CLI Proton, ou None si non réglé (dans ce
    cas resolve_proton_cli() applique la résolution par défaut)."""
    v = get("proton_cli_path")
    return v if isinstance(v, str) and v.strip() else None


def set_proton_cli_path(path):
    path = (path or "").strip() or None
    return _put("proton_cli_path", path)


def rename_ext_enabled():
    return bool(get("rename_ext_enabled"))


def set_rename_ext_enabled(value):
    return _put("rename_ext_enabled", bool(value))


def account_name():
    """Identité NAS STABLE de cette installation (nom de la copie de mappings
    poussée et de la file de marqueurs sur le NAS). None = automatique :
    amorcée par le GUI (identité des unités existantes, sinon adresse Proton
    complète à la première connexion). DÉCOUPLÉE du nom du fichier de mappings
    local — le renommer ne change plus rien côté NAS."""
    v = get("account_name")
    return v.strip() if isinstance(v, str) and v.strip() else None


def set_account_name(value):
    return _put("account_name", (value or "").strip() or None)


def tray_enabled():
    return bool(get("tray_enabled"))


def set_tray_enabled(value):
    return _put("tray_enabled", bool(value))


def rename_ext_collision_suffix():
    v = get("rename_ext_collision_suffix")
    if isinstance(v, str) and v.strip() and not (_SUFFIX_FORBIDDEN & set(v)):
        return v
    return DEFAULTS["rename_ext_collision_suffix"]


def validate_collision_suffix(suffix):
    """Valide un suffixe candidat AVANT de le persister. Retourne (ok, raison)
    — raison vide si ok. Pensé pour être affiché tel quel dans le GUI."""
    if not suffix or not suffix.strip():
        return False, _("The suffix cannot be empty.")
    if _SUFFIX_FORBIDDEN & set(suffix):
        return False, _("The suffix cannot contain / \\ \" '.")
    return True, ""


def set_rename_ext_collision_suffix(suffix):
    ok, _reason = validate_collision_suffix(suffix)
    if not ok:
        return False
    return _put("rename_ext_collision_suffix", suffix)


def resolve_proton_cli():
    """Ordre de résolution du binaire CLI Proton, PARTAGÉ par tous les
    fichiers (moteur, GUI, démons) — une seule règle, jamais dupliquée :
      1. Variable d'environnement PROTON_DRIVE_CLI (prioritaire : ne casse
         rien chez qui l'utilise déjà) ;
      2. Réglage 'proton_cli_path' de settings.json (le plus pratique pour un
         usage GUI — pas besoin de manipuler une variable d'environnement) ;
      3. Défaut historique : <dossier d'installation>/proton-drive."""
    env = os.environ.get("PROTON_DRIVE_CLI")
    if env:
        return env
    configured = proton_cli_path()
    if configured:
        return configured
    return os.path.join(APP_DIR, "proton-drive")


def cli_env_value(default_template):
    """Valeur à écrire dans Environment=PROTON_DRIVE_CLI= d'une unité systemd
    GÉNÉRÉE (realtime_manager.py, schedule_manager.py) : le chemin CONFIGURÉ
    (settings.json) s'il est explicitement réglé, sinon `default_template`
    (généralement un gabarit %h, résolu par systemd lui-même à l'exécution —
    PAS par Python ici). Centralisé pour que les deux générateurs d'unités
    partagent EXACTEMENT la même règle, jamais dupliquée."""
    configured = proton_cli_path()
    return configured if configured else default_template


# ─────────────────────────────────────────────────────────────────────────
#  2) Dossier de données unifié (cache, file temps réel, logs)
# ─────────────────────────────────────────────────────────────────────────
# Ancien état, éclaté en 2 dossiers à la racine du $HOME : ~/.proton_sync_cache
# et ~/.proton_sync (+ ~/.proton_sync.lock à part). Fusionnés ici sous un seul
# parent plus lisible. La migration ci-dessous est un RENOMMAGE (même système
# de fichiers que $HOME) : instantané, ne touche PAS le contenu -> aucun
# re-balayage du cache existant.
DATA_DIR = os.path.expanduser("~/.proton-drive-sync")
CACHE_DIR = os.path.join(DATA_DIR, "cache")
QUEUE_DIR = os.path.join(DATA_DIR, "queue")
REALTIME_CONF = os.path.join(DATA_DIR, "realtime.conf")
FAILURES_LOG = os.path.join(DATA_DIR, "failures.log")
RENAMED_LOG = os.path.join(DATA_DIR, "renamed-extensions.log")
LOCK_FILE = os.path.join(DATA_DIR, "proton_sync.lock")
# Battement de cœur du consommateur temps réel (lu par tray_indicator.py) :
# horodatage + état de session, réécrit à chaque cycle. Fichier trop vieux ou
# absent = démons arrêtés.
STATUS_FILE = os.path.join(DATA_DIR, "status.json")

_LEGACY_CACHE_DIR = os.path.expanduser("~/.proton_sync_cache")
_LEGACY_BASE_DIR = os.path.expanduser("~/.proton_sync")
_LEGACY_LOCK_FILE = os.path.expanduser("~/.proton_sync.lock")


def _migrate_data_dirs():
    """Migration UNIQUE et SÛRE des anciens emplacements vers DATA_DIR.

    Idempotente : si DATA_DIR existe déjà, ne fait rien (plusieurs processus
    peuvent démarrer en même temps après une mise à jour — GUI, watchers,
    consommateur — sans se marcher dessus).

    Résiliente : si une étape échoue (permissions...), le programme CONTINUE
    avec les anciens chemins pour cette exécution (rien ne casse) ; la
    migration sera retentée au prochain lancement.

    Un renommage à l'intérieur du même système de fichiers ($HOME) ne touche
    aucun octet du contenu — le cache existant (signatures, complétude...)
    arrive intact à son nouvel emplacement."""
    if os.path.isdir(DATA_DIR):
        return
    try:
        if os.path.isdir(_LEGACY_BASE_DIR):
            os.rename(_LEGACY_BASE_DIR, DATA_DIR)
            print(_("[config] Data folder migrated: {old} -> {new}").format(
                old=_LEGACY_BASE_DIR, new=DATA_DIR))
        else:
            os.makedirs(DATA_DIR, exist_ok=True)
    except OSError as e:
        print(_("[config] ⚠ Could not migrate the data folder ({e}) — "
                "using the legacy paths for this run.").format(e=e))
        return
    try:
        if os.path.isdir(_LEGACY_CACHE_DIR) and not os.path.isdir(CACHE_DIR):
            os.rename(_LEGACY_CACHE_DIR, CACHE_DIR)
        else:
            os.makedirs(CACHE_DIR, exist_ok=True)
    except OSError as e:
        print(_("[config] ⚠ Could not migrate the cache folder ({e}).").format(e=e))
    try:
        if os.path.isfile(_LEGACY_LOCK_FILE) and not os.path.exists(LOCK_FILE):
            os.rename(_LEGACY_LOCK_FILE, LOCK_FILE)
    except OSError:
        pass   # sans conséquence : le verrou sera simplement recréé au bon endroit


_migrate_data_dirs()

# touch

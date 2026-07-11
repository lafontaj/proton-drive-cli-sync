#!/usr/bin/env python3
"""
Gestion du temps réel (couche 5) pour la synchro Proton Drive.

Pendant de schedule_manager.py, mais pour les DÉMONS temps réel plutôt que le
timer nocturne. Encapsule toute la plomberie pour que le GUI n'ait qu'à appeler
des fonctions claires renvoyant (ok, message), plus un status() global.

Périmètre (décidé en conception) :
  - Pilote les DEUX démons de la machine locale via systemctl --user (comme le timer) :
        proton-watch.service     -> local_watcher.py    (surveille le local)
        proton-consume.service   -> realtime_consumer.py (lance le moteur)
  - OBSERVE seulement le watcher NAS : il tourne sur le NAS, sous son propre
    systemd, et le GUI ne fait que lire son activité via la file NFS (pas de SSH,
    pas d'identifiants à distance). On affiche un voyant honnête, sans bouton qui
    prétendrait le contrôler.
  - Règle le délai de debounce/cycle en écrivant ~/.proton_sync/realtime.conf,
    relu À CHAUD par le consommateur à chaque cycle.
  - Pousse la copie des mappings vers le NAS (config/) avec un hash de version,
    et calcule la « dérive » local <-> NAS.
  - Compte et purge les marqueurs des files (locale + NAS).

Tout est centré sur l'utilisateur courant et son fichier de mappings actif.
Conçu pour tourner SANS privilèges (session utilisateur). Le linger (sudo) est
seulement LU et rappelé, jamais modifié ici.
"""
__version__ = "1.1.1"   # version propre à CE fichier ; incrémentée quand il change (indépendant de GitHub)

import os
import re
import json
import glob
import time
import shutil
import hashlib
import getpass
import datetime
import subprocess

# i18n (import guardé : l'absence de i18n.py n'empêche rien — les
# messages restent alors en anglais, la langue source).
try:
    from i18n import _
except ImportError:
    def _(s):
        return s

# Réglages d'installation (chemins, présence NAS...) : une SEULE source de
# vérité partagée par le moteur, le GUI et les démons. Import tolérant : si
# absent, on retombe sur les valeurs historiques (rien ne change pour qui ne
# l'a pas encore).
try:
    import config as appconfig
    _HAS_CONFIG = True
except ImportError:
    _HAS_CONFIG = False

# ─────────────────────────────────────────────────────────────────────────
#  Emplacements (alignés sur les démons des couches 1–4)
# ─────────────────────────────────────────────────────────────────────────
# Dérivé de __file__ (comme i18n.py/config.py) plutôt que recopié en dur :
# installer le dossier entier ailleurs fonctionne sans rien reconfigurer.
APP_DIR = os.path.dirname(os.path.abspath(__file__))
LOCAL_WATCHER = os.path.join(APP_DIR, "local_watcher.py")
CONSUMER = os.path.join(APP_DIR, "realtime_consumer.py")
ENGINE = os.path.join(APP_DIR, "proton_sync.py")

# Binaire proton-drive (CLI officiel). Résolution PARTAGÉE (config.py) : la
# variable d'environnement PROTON_DRIVE_CLI prime, sinon le réglage persistant,
# sinon le défaut dans APP_DIR.
PROTON_CLI = appconfig.resolve_proton_cli() if _HAS_CONFIG else os.environ.get(
    "PROTON_DRIVE_CLI", os.path.join(APP_DIR, "proton-drive"))

# État local du temps réel — dossier de données UNIFIÉ (config.py), avec
# migration automatique et sûre depuis l'ancien ~/.proton_sync (voir config.py).
if _HAS_CONFIG:
    BASE_DIR = appconfig.DATA_DIR
    LOCAL_QUEUE = appconfig.QUEUE_DIR
    CONFIG_FILE = appconfig.REALTIME_CONF
else:
    BASE_DIR = os.path.expanduser("~/.proton_sync")
    LOCAL_QUEUE = os.path.join(BASE_DIR, "queue")
    CONFIG_FILE = os.path.join(BASE_DIR, "realtime.conf")

# Valeurs par défaut : IDENTIQUES à celles du consommateur, pour que l'affichage
# « réglage actuel » soit juste même avant la première écriture du fichier.
DEFAULT_DEBOUNCE_SECONDS = 30
DEFAULT_CYCLE_SECONDS = 30

# NAS via NFS (côté machine locale). Le watcher NAS, lui, voit ces dossiers en local.
# Point de montage CONFIGURABLE (Configuration… dans le GUI) — plus de chemin
# figé pour une autre topologie d'installation.
NAS_MOUNT = appconfig.nas_mount_path() if _HAS_CONFIG else "/media/home_nas"
NAS_BASE = os.path.join(NAS_MOUNT, "proton-sync")
NAS_CONFIG_DIR = os.path.join(NAS_BASE, "config")   # mappings-<user>.json poussés
NAS_QUEUE_DIR = os.path.join(NAS_BASE, "queue")     # queue/<user>/

# systemd --user (mêmes conventions que schedule_manager).
SYSTEMD_USER_DIR = os.path.expanduser("~/.config/systemd/user")
WATCH_NAME = "proton-watch.service"
CONSUME_NAME = "proton-consume.service"
WATCH_PATH = os.path.join(SYSTEMD_USER_DIR, WATCH_NAME)
CONSUME_PATH = os.path.join(SYSTEMD_USER_DIR, CONSUME_NAME)

# %h = home de l'utilisateur (résolu par systemd lui-même, PAS par Python) —
# reste le gabarit par défaut pour les unités générées. Si un binaire CLI est
# explicitement configuré (chemin absolu, pas le défaut), on l'utilise à la
# place du gabarit %h pour que les démons pointent vers le MÊME binaire que le
# GUI/moteur (voir _cli_env_value ci-dessous).
H_ENGINE_DIR = "%h/Logiciels/Proton-drive"
DEFAULT_CLI = "%h/Logiciels/Proton-drive/proton-drive"

# Préfixes des noms de marqueurs déposés par les watchers (cf. marker_filename()).
MARKER_PREFIXES = ("add_", "del_")


def _run(args):
    """Lance une commande et retourne (returncode, stdout, stderr)."""
    try:
        r = subprocess.run(args, capture_output=True, text=True)
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError as e:
        return 127, "", str(e)


# ─────────────────────────────────────────────────────────────────────────
#  Identité : utilisateur « compte » déduit du fichier de mappings
# ─────────────────────────────────────────────────────────────────────────
def user_from_mappings_path(mappings_path):
    """mappings-user1.json -> 'user1'. C'est le nom de COMPTE utilisé côté NAS
    (config/ et queue/), indépendant du login Unix local (qui peut différer,
    ex. 'myuser'). Repli sur le login courant si le motif ne colle pas."""
    base = os.path.basename(mappings_path or "")
    m = re.match(r"mappings-(.+)\.json$", base)
    if m:
        return m.group(1)
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USER") or "user"


# ─────────────────────────────────────────────────────────────────────────
#  1) Réglage du délai (debounce / cycle) — realtime.conf
# ─────────────────────────────────────────────────────────────────────────
def read_config():
    """Lit ~/.proton_sync/realtime.conf. Toujours un dict valide (valeurs par
    défaut si fichier absent/invalide), pour ne jamais faire échouer le GUI."""
    cfg = {
        "debounce_seconds": DEFAULT_DEBOUNCE_SECONDS,
        "cycle_seconds": DEFAULT_CYCLE_SECONDS,
    }
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for k in ("debounce_seconds", "cycle_seconds"):
                v = data.get(k)
                if isinstance(v, (int, float)) and v > 0:
                    cfg[k] = int(v)
    except (OSError, ValueError):
        pass
    return cfg


def write_config(debounce_seconds, cycle_seconds):
    """Écrit realtime.conf de façon atomique. Le consommateur le relit à chaud
    (≤ 1 cycle). Retourne (ok, message)."""
    try:
        debounce_seconds = int(debounce_seconds)
        cycle_seconds = int(cycle_seconds)
    except (TypeError, ValueError):
        return False, _("Invalid delay values (integers expected).")
    if debounce_seconds < 1 or cycle_seconds < 1:
        return False, _("Delays must be at least 1 second.")
    payload = {"debounce_seconds": debounce_seconds, "cycle_seconds": cycle_seconds}
    try:
        os.makedirs(BASE_DIR, exist_ok=True)
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, CONFIG_FILE)
    except OSError as e:
        return False, _("Failed to write the config: {e}").format(e=e)
    return True, _("Delays saved (debounce {d}s, cycle {c}s). Applied at the "
                  "next cycle.").format(d=debounce_seconds, c=cycle_seconds)


# ─────────────────────────────────────────────────────────────────────────
#  2) + 3) Push des mappings vers le NAS + hash de version + dérive
# ─────────────────────────────────────────────────────────────────────────
def _sha256_of_file(path):
    """sha256 du contenu d'un fichier, ou None si illisible."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def nas_reachable():
    """True si le NAS est monté et son dossier proton-sync accessible.

    Coupe NET si le mode local seul est actif (nas_enabled=False) : aucune
    tentative d'E/S sur NAS_MOUNT — pas de « tentative puis échec » répété à
    chaque rafraîchissement, juste une réponse immédiate et honnête."""
    if _HAS_CONFIG and not appconfig.nas_enabled():
        return False
    try:
        if os.path.ismount(NAS_MOUNT):
            return os.path.isdir(NAS_BASE)
        # Repli : certains montages NFS ne se signalent pas comme « mount ».
        return os.path.isdir(NAS_BASE)
    except OSError:
        return False


def ensure_nas_identity(mappings_path):
    """Garantit une identité NAS AVANT le premier usage réel (poussée ou
    installation des services) et la retourne.

    Ordre de résolution :
      1. réglage « account_name » déjà défini -> rien à faire ;
      2. unités systemd existantes -> identité DÉRIVÉE de leur fichier
         (préserve les installations historiques : user1, user2) ;
      3. sinon, ATTRIBUTION d'un nom neutre unique « user{n} » : on scanne le
         NAS (config/mappings-*.json ET queue/*) pour les noms déjà pris, puis
         on RÉCLAME le premier n libre par mkdir de queue/user{n} — mkdir est
         ATOMIQUE, y compris sur NFS : deux postes qui s'amorcent au même
         instant ne peuvent pas gagner le même n (le perdant passe au suivant).
         Le nom est neutre par choix : indépendant du compte Proton (qui peut
         changer — voir le garde-fou [account-changed]) comme du fichier local.

    Mode local seul ou NAS injoignable : retourne l'identité dérivée SANS rien
    persister (l'identité n'a de sens qu'avec un NAS ; l'attribution se fera au
    premier usage réel une fois le NAS disponible)."""
    if _HAS_CONFIG:
        v = appconfig.account_name()
        if v:
            return v
    old_units = read_units_mappings_path()
    if old_units:
        ident = user_from_mappings_path(old_units)
        if _HAS_CONFIG:
            appconfig.set_account_name(ident)
        return ident
    if (_HAS_CONFIG and not appconfig.nas_enabled()) or not nas_reachable():
        return user_from_mappings_path(mappings_path)
    used = set()
    try:
        for p in glob.glob(os.path.join(NAS_CONFIG_DIR, "mappings-*.json")):
            used.add(user_from_mappings_path(p))
        if os.path.isdir(NAS_QUEUE_DIR):
            used.update(os.listdir(NAS_QUEUE_DIR))
    except OSError:
        return user_from_mappings_path(mappings_path)
    n = 1
    while n < 1000:
        name = f"user{n}"
        if name not in used:
            try:
                os.makedirs(NAS_QUEUE_DIR, exist_ok=True)
                os.mkdir(os.path.join(NAS_QUEUE_DIR, name))   # RÉCLAMATION atomique
                if _HAS_CONFIG:
                    appconfig.set_account_name(name)
                return name
            except FileExistsError:
                pass          # perdu la course -> essayer le suivant
            except OSError:
                break         # NAS en écriture impossible -> repli dérivé
        n += 1
    return user_from_mappings_path(mappings_path)


def migrate_nas_identity(old_name, new_name):
    """Migration C : RENOMME sur le NAS tout ce qui porte l'ancienne identité —
    file queue/<old> -> queue/<new>, copie config/mappings-<old>.json (+ sidecar
    .version) -> mappings-<new>.json. Billets et continuité préservés (simple
    rename, aucun contenu touché). Refuse si la cible existe déjà (collision
    avec un autre utilisateur). Retourne (ok, message)."""
    if _HAS_CONFIG and not appconfig.nas_enabled():
        return True, _("Local-only mode is active — nothing to migrate on a NAS.")
    if not nas_reachable():
        return False, _("NAS unreachable (NFS mount missing). Check that "
                        "{mount} is mounted, then try again.").format(mount=NAS_MOUNT)
    old_q = os.path.join(NAS_QUEUE_DIR, old_name)
    new_q = os.path.join(NAS_QUEUE_DIR, new_name)
    old_c = os.path.join(NAS_CONFIG_DIR, f"mappings-{old_name}.json")
    new_c = os.path.join(NAS_CONFIG_DIR, f"mappings-{new_name}.json")
    if os.path.exists(new_q) or os.path.exists(new_c):
        return False, _("The identity “{n}” already exists on the NAS — "
                        "migration refused (possible collision with another "
                        "user). Choose another name.").format(n=new_name)
    try:
        if os.path.isdir(old_q):
            os.rename(old_q, new_q)
        if os.path.exists(old_c):
            os.rename(old_c, new_c)
        old_s, new_s = _version_sidecar(old_c), _version_sidecar(new_c)
        if os.path.exists(old_s):
            os.rename(old_s, new_s)
    except OSError as e:
        return False, _("Migration failed: {e}").format(e=e)
    return True, _("Identity migrated on the NAS: “{a}” → “{b}” (queue and "
                   "mappings copy renamed, markers preserved).").format(
                   a=old_name, b=new_name)


def nas_identity(mappings_path):
    """Identité NAS STABLE : le réglage persistant « account_name »
    (settings.json) s'il est défini, sinon — compatibilité — le nom dérivé du
    fichier (mappings-user1.json -> user1). C'est LA source unique employée pour
    nommer la copie poussée (config/mappings-<identité>.json) et la file
    (queue/<identité>) : renommer le fichier LOCAL ne change plus rien côté
    NAS — plus de file fantôme, une seule queue temps réel par personne."""
    if _HAS_CONFIG:
        v = appconfig.account_name()
        if v:
            return v
    return user_from_mappings_path(mappings_path)


def nas_mapping_target(mappings_path):
    """Chemin où la copie du mapping atterrit sur le NAS. Nom NORMALISÉ sur
    l'identité stable (mappings-<identité>.json), QUEL QUE SOIT le nom du
    fichier local — le watcher NAS découvre les users par glob mappings-*.json,
    et l'identité ne bouge plus."""
    return os.path.join(NAS_CONFIG_DIR, f"mappings-{nas_identity(mappings_path)}.json")


def _version_sidecar(target_path):
    return target_path + ".version"


def push_scripts_to_nas():
    """Copie vers le NAS les scripts dont le WATCHER NAS a besoin, pour que le
    NAS ne soit jamais désynchronisé du poste (un déploiement partiel — ex.
    config.py absent — met le service NAS en échec silencieux). Poussés dans
    NAS_BASE (proton-sync/) : nas_watcher.py, local_watcher.py, config.py,
    i18n.py, mount_check.py + le dossier locale/.

    Le fichier .service, lui, est seulement DÉPOSÉ (nas_watcher.service.new) —
    jamais installé à distance : cela requiert sudo + daemon-reload système sur
    le NAS. Le message de retour rappelle la commande à lancer s'il a changé.

    Silencieux et sans échec bloquant : c'est un confort de déploiement, il ne
    doit jamais empêcher l'installation locale. Retourne (ok, détail) — ok=False
    seulement si le NAS est censé être là mais inaccessible."""
    if _HAS_CONFIG and not appconfig.nas_enabled():
        return True, None                    # mode local seul : rien à pousser
    if not nas_reachable():
        return False, _("scripts not pushed (NAS unreachable)")
    files = ["nas_watcher.py", "local_watcher.py", "config.py", "i18n.py",
             "mount_check.py", "nas_selftest.py", "nas_selftest_watcher.py"]
    pushed = 0
    pushed_names = []                         # noms réellement copiés (pour le message)
    try:
        os.makedirs(NAS_BASE, exist_ok=True)
        for name in files:
            src = os.path.join(APP_DIR, name)
            if not os.path.exists(src):
                continue
            dst = os.path.join(NAS_BASE, name)
            # Copie seulement si différent (taille+contenu) — évite les
            # écritures inutiles à chaque install/update.
            if _sha256_of_file(src) != _sha256_of_file(dst):
                tmp = dst + ".tmp"
                shutil.copyfile(src, tmp)
                os.replace(tmp, dst)
                pushed += 1
                pushed_names.append(name)
        # locale/ (catalogues .mo/.po) : miroir léger.
        locale_pushed = 0
        src_locale = os.path.join(APP_DIR, "locale")
        if os.path.isdir(src_locale):
            for root, _dirs, fnames in os.walk(src_locale):
                rel = os.path.relpath(root, APP_DIR)
                dst_dir = os.path.join(NAS_BASE, rel)
                os.makedirs(dst_dir, exist_ok=True)
                for fn in fnames:
                    s = os.path.join(root, fn); d = os.path.join(dst_dir, fn)
                    if _sha256_of_file(s) != _sha256_of_file(d):
                        shutil.copyfile(s, d)
                        locale_pushed += 1
        if locale_pushed:
            pushed_names.append(_("translation catalogues"))
        # .service : déposé à côté (jamais activé à distance).
        svc_src = os.path.join(APP_DIR, "nas_watcher.service")
        svc_changed = False
        if os.path.exists(svc_src):
            svc_dst = os.path.join(NAS_BASE, "nas_watcher.service.new")
            if _sha256_of_file(svc_src) != _sha256_of_file(svc_dst):
                shutil.copyfile(svc_src, svc_dst)
                svc_changed = True
    except OSError as e:
        return False, _("scripts partly pushed ({e})").format(e=e)
    if svc_changed:
        return True, _("NAS scripts updated: {files}. The service file also "
                       "changed: on the NAS, review proton-sync/"
                       "nas_watcher.service.new, then run "
                       "“sudo systemctl daemon-reload && sudo systemctl restart "
                       "proton-nas-watch.service”.").format(
                           files=", ".join(pushed_names))
    if pushed_names:
        # Lister les fichiers poussés + rappeler le redémarrage du watcher : un
        # script mis à jour n'est actif qu'après relance du service NAS (l'ancien
        # code reste en mémoire sinon). Transparence : l'utilisateur voit
        # EXACTEMENT ce qui a été copié.
        return True, _("NAS scripts updated: {files}.\nTo apply, restart the NAS "
                       "watcher: “sudo systemctl restart "
                       "proton-nas-watch.service”.").format(
                           files=", ".join(pushed_names))
    return True, None                        # déjà à jour : rien à signaler


def push_mappings_to_nas(mappings_path):
    """Copie le fichier de mappings actif vers config/ sur le NAS, et écrit un
    sidecar .version (sha256 + date + hôte). Retourne (ok, message)."""
    ensure_nas_identity(mappings_path)
    if not mappings_path or not os.path.exists(mappings_path):
        return False, _("No active mappings file to push.")
    if _HAS_CONFIG and not appconfig.nas_enabled():
        return False, _("Local-only mode is active (see Configuration…) — "
                        "there is no NAS to push mappings to.")
    if not nas_reachable():
        return False, _("NAS unreachable (NFS mount missing). Check that "
                        "{mount} is mounted, then try again.").format(mount=NAS_MOUNT)
    local_hash = _sha256_of_file(mappings_path)
    if local_hash is None:
        return False, _("Could not read the local mappings file.")
    target = nas_mapping_target(mappings_path)
    try:
        os.makedirs(NAS_CONFIG_DIR, exist_ok=True)
        tmp = target + ".tmp"
        shutil.copyfile(mappings_path, tmp)
        os.replace(tmp, target)
        meta = {
            "sha256": local_hash,
            "pushed_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "host": os.uname().nodename if hasattr(os, "uname") else "",
            "source": mappings_path,
        }
        stmp = _version_sidecar(target) + ".tmp"
        with open(stmp, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
        os.replace(stmp, _version_sidecar(target))
        # Pousser aussi la correspondance de chemins de données (desktop<->NAS),
        # dans le MÊME dossier config/. Le watcher NAS la lit pour surveiller les
        # bons dossiers et écrire les marqueurs dans le référentiel des mappings.
        # Correspondance de chemins PAR UTILISATEUR : nas_path_map-<identité>.json,
        # comme mappings-<identité>.json. Chaque desktop pousse SA propre table
        # (le chemin desktop, ex. /media/nas1, peut différer d'une machine à
        # l'autre) sans écraser celle des autres. Le watcher NAS charge, pour
        # chaque cible, la table de l'utilisateur concerné. Absente/vide = pas de
        # traduction pour cet utilisateur (chemins identiques).
        if _HAS_CONFIG:
            try:
                pm = appconfig.nas_path_map()
                ident = nas_identity(mappings_path)
                pm_name = f"nas_path_map-{ident}.json"
                pmtmp = os.path.join(NAS_CONFIG_DIR, pm_name + ".tmp")
                with open(pmtmp, "w", encoding="utf-8") as f:
                    json.dump(pm, f, indent=2)
                os.replace(pmtmp, os.path.join(NAS_CONFIG_DIR, pm_name))
            except OSError:
                pass   # non bloquant : le push des mappings a réussi
    except OSError as e:
        return False, _("Push to the NAS failed: {e}").format(e=e)
    user = nas_identity(mappings_path)
    return True, _("Mappings for “{user}” pushed to the NAS (version {v}…). "
                  "The NAS watcher reloads them live.").format(
                  user=user, v=local_hash[:10])


def pending_markers_report(new_mappings_path):
    """Décompte des billets (marqueurs) en attente au moment d'un CHANGEMENT de
    fichier de mappings actif : (total, non_couverts).

    total : marqueurs présents dans la file locale ET la file NAS de l'identité
    stable (une seule queue par personne, quel que soit le nom du fichier) ;
    non_couverts : ceux dont le dossier n'est couvert par AUCUN mapping du
    NOUVEAU fichier — ils seront écartés au traitement (ligne « ⊘ » au journal)
    et ne sont rattrapables qu'en rechargeant l'ancien fichier pour un passage
    manuel ou planifié. Le garde-fou du GUI affiche ces chiffres avant de
    confirmer la bascule.

    Réutilise read_markers / find_mapping_for_path du CONSOMMATEUR — aucune
    logique parallèle de lecture de marqueurs ni de correspondance."""
    import realtime_consumer as rc
    queues = [LOCAL_QUEUE]
    if not (_HAS_CONFIG and not appconfig.nas_enabled()):
        queues.append(os.path.join(NAS_QUEUE_DIR, nas_identity(new_mappings_path)))
    try:
        with open(new_mappings_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        new_maps = (raw["mappings"] if isinstance(raw, dict) and "mappings" in raw
                    else raw if isinstance(raw, list) else [])
    except (OSError, ValueError):
        new_maps = []
    total = uncovered = 0
    for _mpath, target, _wd in rc.read_markers(queues):
        if not target:
            continue
        total += 1
        if rc.find_mapping_for_path(target, new_maps) is None:
            uncovered += 1
    return total, uncovered


def drift_state(mappings_path):
    """Compare le mapping local à la copie présente sur le NAS (source de vérité,
    car c'est elle que lit le watcher NAS).

    Retourne un dict :
      state : 'synced' | 'local_newer' | 'nas_missing' | 'nas_unreachable'
      local_hash, nas_hash : sha256 (ou None)
      pushed_at : date du dernier push (depuis le sidecar) ou None
    """
    info = {"state": "nas_unreachable", "local_hash": None,
            "nas_hash": None, "pushed_at": None}
    info["local_hash"] = _sha256_of_file(mappings_path) if mappings_path else None
    if not nas_reachable():
        info["state"] = "nas_unreachable"
        return info
    target = nas_mapping_target(mappings_path) if mappings_path else None
    if not target or not os.path.exists(target):
        info["state"] = "nas_missing"
        return info
    info["nas_hash"] = _sha256_of_file(target)
    # Date du dernier push (informatif), depuis le sidecar s'il existe.
    try:
        with open(_version_sidecar(target), "r", encoding="utf-8") as f:
            meta = json.load(f)
        info["pushed_at"] = meta.get("pushed_at")
    except (OSError, ValueError):
        pass
    if info["local_hash"] and info["local_hash"] == info["nas_hash"]:
        info["state"] = "synced"
    else:
        info["state"] = "local_newer"
    return info


# ─────────────────────────────────────────────────────────────────────────
#  4) Démons (systemd --user) : génération, install, contrôle, état
# ─────────────────────────────────────────────────────────────────────────
def _service_text(description, script, mappings_path):
    """Génère un .service --user simple, qui RESTE actif (Restart=on-failure)
    et redémarre à l'ouverture de session (WantedBy=default.target)."""
    return f"""[Unit]
Description={description}
After=network-online.target

[Service]
Type=simple
Environment=PROTON_DRIVE_CLI={appconfig.cli_env_value(DEFAULT_CLI) if _HAS_CONFIG else DEFAULT_CLI}
ExecStart=/usr/bin/python3 {script} {mappings_path}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


def build_watch_service_text(mappings_path):
    return _service_text(
        _("Local real-time watcher (Proton Drive) — writes markers"),
        os.path.join(H_ENGINE_DIR, "local_watcher.py"), mappings_path)


def build_consume_service_text(mappings_path):
    return _service_text(
        _("Real-time consumer (Proton Drive) — runs the engine on mature folders"),
        os.path.join(H_ENGINE_DIR, "realtime_consumer.py"), mappings_path)


def _write(path, content):
    os.makedirs(SYSTEMD_USER_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def daemon_reload():
    return _run(["systemctl", "--user", "daemon-reload"])


def units_exist():
    return os.path.exists(WATCH_PATH) and os.path.exists(CONSUME_PATH)


def _is_active(unit):
    _rc, out, _err = _run(["systemctl", "--user", "is-active", unit])
    return out.strip() == "active"


def read_units_mappings_path():
    """Extrait le fichier de mappings de l'ExecStart du consommateur (ou None).
    Sert à détecter si les démons installés visent un AUTRE fichier que l'actif."""
    if not os.path.exists(CONSUME_PATH):
        return None
    try:
        with open(CONSUME_PATH, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return None
    m = re.search(r"^ExecStart=.*realtime_consumer\.py\s+(\S+)", content, re.MULTILINE)
    return m.group(1) if m else None


def mappings_ready_count(mappings_path):
    """Retourne (prêts, total) : combien de mappings de type 'folder' sont PRÊTS
    pour le temps réel, c.-à-d. dont la racine (la 'source') est marquée
    subtree_complete dans le cache. Un mapping n'est pleinement utilisable en temps
    réel que si TOUT son arbre a été analysé par un passage complet — ce que la
    complétude de sa racine garantit (la complétude remonte de bas en haut). Un
    mapping non prêt verra ses changements pris en charge par la planification tant
    qu'un passage complet ne l'a pas analysé. (0 si le cache ou les mappings sont
    illisibles — on n'échoue jamais côté GUI.)"""
    if not mappings_path:
        return (0, 0)
    # Racines (sources) des mappings de type 'folder'.
    try:
        with open(mappings_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        maps = raw["mappings"] if isinstance(raw, dict) and "mappings" in raw else raw
        sources = [os.path.normpath(m["source"]) for m in maps
                   if isinstance(m, dict) and m.get("type", "folder") == "folder"
                   and m.get("source")]
    except (OSError, ValueError, KeyError, TypeError):
        return (0, 0)
    total = len(sources)
    if total == 0:
        return (0, 0)
    # Cache correspondant.
    cache_dir = appconfig.CACHE_DIR if _HAS_CONFIG else os.path.expanduser("~/.proton_sync_cache")
    name = os.path.basename(mappings_path).replace(".json", "") + ".cache"
    try:
        with open(os.path.join(cache_dir, name), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return (0, total)
    if not isinstance(data, dict):
        return (0, total)
    ready = 0
    for src in sources:
        entry = data.get(src)
        if isinstance(entry, dict) and entry.get("subtree_complete", False):
            ready += 1
    return (ready, total)


def install_or_update_units(mappings_path, enable=True):
    """Crée/met à jour les 2 services, recharge systemd, et (enable --now) si
    demandé, pour qu'ils tournent maintenant et repartent à l'ouverture de
    session. Retourne (ok, message)."""
    ensure_nas_identity(mappings_path)
    if not mappings_path:
        return False, _("No active mappings file.")
    try:
        _write(WATCH_PATH, build_watch_service_text(mappings_path))
        _write(CONSUME_PATH, build_consume_service_text(mappings_path))
    except OSError as e:
        return False, _("Failed to write the systemd files: {e}").format(e=e)
    rc, out, err = daemon_reload()
    if rc != 0:
        return False, _("daemon-reload failed: {e}").format(e=err or out)
    if enable:
        for unit in (WATCH_NAME, CONSUME_NAME):
            rc, out, err = _run(["systemctl", "--user", "enable", "--now", unit])
            if rc != 0:
                return False, _("Enabling {u} failed: {e}").format(u=unit, e=err or out)
        base = _("Daemons installed and started (auto-restart at session login).")
    else:
        base = _("Daemons installed (not started).")
    # Pousser les scripts vers le NAS pour le garder synchronisé (évite un
    # service NAS en échec après un déploiement partiel). Confort : n'échoue
    # jamais l'installation locale ; on ajoute juste une note si utile.
    _ok, note = push_scripts_to_nas()
    if note:
        base = base + "\n" + note
    return True, base


def start_daemons():
    for unit in (WATCH_NAME, CONSUME_NAME):
        rc, out, err = _run(["systemctl", "--user", "start", unit])
        if rc != 0:
            return False, _("Starting {u} failed: {e}").format(u=unit, e=err or out)
    return True, _("Daemons started.")


def stop_daemons():
    # On arrête le consommateur d'abord, puis le watcher (ordre inverse).
    for unit in (CONSUME_NAME, WATCH_NAME):
        rc, out, err = _run(["systemctl", "--user", "stop", unit])
        if rc != 0:
            return False, _("Stopping {u} failed: {e}").format(u=unit, e=err or out)
    return True, _("Daemons stopped.")


def stop_consumer():
    """Arrête UNIQUEMENT le consommateur (celui qui lance le moteur et prendrait le
    verrou), en laissant le WATCHER actif. Utilisé pendant l'amorçage : le watcher
    continue de déposer des marqueurs pour les vrais changements locaux (mappings
    déjà prêts), qui seront traités dès le retour du consommateur — au lieu d'être
    manqués et repoussés à la planification. Le watcher ne prend pas le verrou et ne
    touche pas au cache : le laisser tourner est sans risque pour l'amorçage."""
    rc, out, err = _run(["systemctl", "--user", "stop", CONSUME_NAME])
    if rc != 0:
        return False, _("Stopping {u} failed: {e}").format(u=CONSUME_NAME, e=err or out)
    return True, _("Consumer stopped (watcher kept running).")


def start_consumer():
    """Redémarre le consommateur (après un amorçage). Le watcher, resté actif, a
    pu accumuler des marqueurs entre-temps : ils seront traités dès ce démarrage."""
    rc, out, err = _run(["systemctl", "--user", "start", CONSUME_NAME])
    if rc != 0:
        return False, _("Starting {u} failed: {e}").format(u=CONSUME_NAME, e=err or out)
    return True, _("Consumer restarted.")


def restart_daemons():
    for unit in (WATCH_NAME, CONSUME_NAME):
        rc, out, err = _run(["systemctl", "--user", "restart", unit])
        if rc != 0:
            return False, _("Restarting {u} failed: {e}").format(u=unit, e=err or out)
    return True, _("Daemons restarted.")


def disable_daemons():
    """Arrête et désactive le démarrage auto (les démons ne repartiront plus
    à l'ouverture de session jusqu'à réactivation)."""
    for unit in (CONSUME_NAME, WATCH_NAME):
        rc, out, err = _run(["systemctl", "--user", "disable", "--now", unit])
        if rc != 0:
            return False, _("Disabling {u} failed: {e}").format(u=unit, e=err or out)
    return True, _("Daemons stopped and autostart disabled.")


# ─────────────────────────────────────────────────────────────────────────
#  Linger (lu seulement ; activation = sudo, hors de portée du GUI)
# ─────────────────────────────────────────────────────────────────────────
def linger_enabled():
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    _rc, out, _err = _run(["loginctl", "show-user", user, "--property=Linger"])
    return "Linger=yes" in out


def linger_command():
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or "<utilisateur>"
    return f"sudo loginctl enable-linger {user}"


def journal_follow_command(lines=500):
    """Commande pour SUIVRE en direct le journal des deux démons (watcher local
    + consommateur), sortie nue (-o cat : juste le message, sans préfixe). Affiche
    d'abord les `lines` dernières lignes pour le contexte (assez pour remonter
    loin même quand beaucoup de marqueurs s'ajoutent), puis suit (-f)."""
    return ["journalctl", "--user",
            "-u", WATCH_NAME, "-u", CONSUME_NAME,
            "-n", str(lines), "-f", "-o", "cat"]


# ─────────────────────────────────────────────────────────────────────────
#  Authentification Proton Drive (session du CLI)
# ─────────────────────────────────────────────────────────────────────────
def check_auth():
    """True si le CLI Proton est authentifié (session valide, trousseau accessible).
    Réutilise EXACTEMENT le préflight du moteur (`proton_sync.py --check-auth`,
    code 0 = OK, code 2 = indisponible) — aucune logique d'auth dupliquée ici. Ne
    prend pas le verrou, ne synchronise rien. Tolérant : en cas d'erreur, retourne
    False (on considère l'auth indisponible plutôt que de faire échouer le GUI)."""
    try:
        env = dict(os.environ)
        env["PROTON_DRIVE_CLI"] = PROTON_CLI
        r = subprocess.run(
            ["python3", ENGINE, "--check-auth"],
            capture_output=True, text=True, timeout=60, env=env)
        return r.returncode == 0
    except Exception:
        return False


def auth_login_command():
    """Commande de connexion Proton (authentification par NAVIGATEUR : le CLI
    ouvre le navigateur et attend ; aucun mot de passe ne transite par ce logiciel).
    Le GUI lance cette commande et diffuse sa sortie (URL de secours + message de
    succès) sans jamais manipuler d'identifiant."""
    return [PROTON_CLI, "auth", "login"]


def auth_logout():
    """Déconnecte la session Proton du CLI (utile pour TESTER le flux de reconnexion,
    ou pour repartir propre). Retourne (ok, message)."""
    try:
        r = subprocess.run([PROTON_CLI, "auth", "logout"],
                           capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            return True, _("Signed out of Proton.")
        return False, (r.stderr or r.stdout or _("Sign-out failed.")).strip()
    except Exception as e:
        return False, str(e)


# ─────────────────────────────────────────────────────────────────────────
#  Observation du watcher NAS (pas de contrôle : pas de SSH)
# ─────────────────────────────────────────────────────────────────────────
def _count_markers(queue_dir):
    """Compte les marqueurs (add_/del_) d'une file, et l'âge du plus récent."""
    count = 0
    newest_mtime = None
    if not os.path.isdir(queue_dir):
        return 0, None
    try:
        for name in os.listdir(queue_dir):
            if name.endswith(".tmp"):
                continue
            if name.startswith(MARKER_PREFIXES):
                count += 1
                try:
                    mt = os.path.getmtime(os.path.join(queue_dir, name))
                    if newest_mtime is None or mt > newest_mtime:
                        newest_mtime = mt
                except OSError:
                    pass
    except OSError:
        return 0, None
    return count, newest_mtime


def nas_observe():
    """État observable du côté NAS, via la file NFS (aucun SSH). Retourne :
      reachable      : NAS monté et accessible
      marker_count   : marqueurs en attente, toutes files utilisateurs confondues
      last_activity  : « il y a … » du marqueur NAS le plus récent, ou None
    """
    res = {"reachable": nas_reachable(), "marker_count": 0, "last_activity": None}
    if not res["reachable"]:
        return res
    newest = None
    total = 0
    # queue/<user>/ pour chaque sous-dossier présent.
    try:
        subdirs = [os.path.join(NAS_QUEUE_DIR, d)
                   for d in os.listdir(NAS_QUEUE_DIR)
                   if os.path.isdir(os.path.join(NAS_QUEUE_DIR, d))]
    except OSError:
        subdirs = []
    for d in subdirs:
        c, mt = _count_markers(d)
        total += c
        if mt is not None and (newest is None or mt > newest):
            newest = mt
    res["marker_count"] = total
    if newest is not None:
        res["last_activity"] = _human_age(time.time() - newest)
    return res


def _human_age(seconds):
    if seconds < 0:
        seconds = 0
    if seconds < 90:
        return f"{int(seconds)} s"
    if seconds < 5400:
        return f"{int(seconds / 60)} min"
    if seconds < 172800:
        return f"{int(seconds / 3600)} h"
    return f"{int(seconds / 86400)} j"


# ─────────────────────────────────────────────────────────────────────────
#  5) Files de marqueurs : comptage + nettoyage
# ─────────────────────────────────────────────────────────────────────────
def queue_dirs_for(mappings_path):
    """Les files concernées par l'utilisateur actif : locale (machine locale) + NAS.
    Le sous-dossier NAS est résolu par le NOM DE COMPTE (mappings-user1.json ->
    user1), pas par le login Unix — c'est ce nom qu'emploie le watcher NAS."""
    user = nas_identity(mappings_path)
    return {
        "local": LOCAL_QUEUE,
        "nas": os.path.join(NAS_QUEUE_DIR, user),
        "user": user,
    }


def count_queues(mappings_path):
    """Retourne {local: n, nas: n, nas_reachable: bool, user: ...}."""
    dirs = queue_dirs_for(mappings_path)
    local_n, _x = _count_markers(dirs["local"])
    reachable = nas_reachable()
    nas_n, _x = _count_markers(dirs["nas"]) if reachable else (0, None)
    return {"local": local_n, "nas": nas_n,
            "nas_reachable": reachable, "user": dirs["user"]}


def _purge_markers(queue_dir):
    """Supprime les marqueurs (add_/del_ + .tmp) d'une file. Retourne le nombre
    de fichiers retirés. N'efface QUE des marqueurs reconnus, jamais autre chose."""
    removed = 0
    if not os.path.isdir(queue_dir):
        return 0
    try:
        names = os.listdir(queue_dir)
    except OSError:
        return 0
    for name in names:
        is_marker = name.startswith(MARKER_PREFIXES)
        is_tmp = name.endswith(".tmp") and (
            name[:-4].startswith(MARKER_PREFIXES) or name == ".tmp")
        if not (is_marker or is_tmp):
            continue
        try:
            os.remove(os.path.join(queue_dir, name))
            removed += 1
        except OSError:
            pass
    return removed


def clean_queues(mappings_path, include_local=True, include_nas=True):
    """Purge les marqueurs en attente. Retourne (ok, message)."""
    dirs = queue_dirs_for(mappings_path)
    total = 0
    notes = []
    if include_local:
        n = _purge_markers(dirs["local"])
        total += n
        notes.append(f"locale : {n}")
    if include_nas:
        if nas_reachable():
            n = _purge_markers(dirs["nas"])
            total += n
            notes.append(f"NAS ({dirs['user']}) : {n}")
        else:
            notes.append("NAS : ignorée (injoignable)")
    return True, _("{n} marker(s) removed — ").format(n=total) + ", ".join(notes) + "."


# ─────────────────────────────────────────────────────────────────────────
#  Vue d'ensemble pour le GUI
# ─────────────────────────────────────────────────────────────────────────
def status(mappings_path):
    """Agrège tout l'état nécessaire à la fenêtre temps réel."""
    cfg = read_config()
    queues = count_queues(mappings_path)
    # La DÉRIVE NAS (section 3) doit porter sur le fichier que les services
    # surveillent RÉELLEMENT (celui qui est poussé vers le NAS), PAS sur le
    # fichier actuellement ouvert dans l'éditeur — sinon, éditer un autre
    # fichier afficherait un faux « local modifié » et « Pousser » écraserait la
    # copie NAS avec le mauvais fichier. On retombe sur le fichier courant si
    # aucun service n'est installé (rien n'est encore surveillé).
    units_path = read_units_mappings_path()
    drift_path = units_path if units_path else mappings_path
    return {
        "scripts_present": os.path.exists(LOCAL_WATCHER) and os.path.exists(CONSUMER),
        "units_exist": units_exist(),
        "watch_active": _is_active(WATCH_NAME),
        "consume_active": _is_active(CONSUME_NAME),
        "units_mappings_path": units_path,
        "linger": linger_enabled(),
        "debounce_seconds": cfg["debounce_seconds"],
        "cycle_seconds": cfg["cycle_seconds"],
        "drift": drift_state(drift_path),
        "drift_path": drift_path,
        "nas": nas_observe(),
        "queues": queues,
        "active_mappings_path": mappings_path,
    }


# ─────────────────────────────────────────────────────────────────────────
#  Self-test des correspondances de chemins NAS (chantier v1.5.0) — PILOTE
#  DESKTOP (Protocole 1). Voir nas_selftest.py (verdict) et
#  nas_selftest_watcher.py (côté watcher).
# ─────────────────────────────────────────────────────────────────────────
def run_selftest(local_prefix, nas_prefix, timeout=20.0, poll=0.4):
    """Teste UNE correspondance de chemin (local_prefix <-> nas_prefix) de bout
    en bout, sans jamais toucher au moteur de synchro. Retourne (couleur, message)
    via nas_selftest.verdict().

    Protocole 1 (desktop pilote) :
      1. dépose une demande dans NAS_BASE/selftest/request-<id>.json ;
      2. écrit un témoin DISTANT dans son propre montage (local_prefix/…) — c'est
         la phase A vue du desktop ;
      3. attend la réponse reply-<id>.json (le watcher a fait B puis observé A) ;
      4. complète les observations (remote_witness_written) et appelle verdict().

    Invariant : purement additif, aucune écriture dans les files de marqueurs,
    aucun appel moteur. En cas d'absence de réponse -> timed_out (jaune).
    """
    import uuid
    try:
        import nas_selftest
    except Exception:
        return ("yellow", _("Self-test module unavailable."))

    test_id = uuid.uuid4().hex[:12]
    sdir = os.path.join(NAS_BASE, "selftest")
    req_path = os.path.join(sdir, f"request-{test_id}.json")
    reply_path = os.path.join(sdir, f"reply-{test_id}.json")
    ready_path = os.path.join(sdir, f"ready-{test_id}")
    remote_name = f".proton-selftest-A-{test_id}"

    # Le dossier de test, côté DESKTOP, est le préfixe local lui-même ; côté NAS,
    # c'est le préfixe NAS (le watcher y posera un watch temporaire). Le témoin
    # distant est écrit à la racine du montage local.
    local_witness_path = os.path.join(local_prefix, remote_name)

    obs = nas_selftest.SelfTestObservation(
        local_prefix=local_prefix, nas_prefix=nas_prefix)

    # 1) Déposer la demande.
    try:
        os.makedirs(sdir, exist_ok=True)
        req = {
            "id": test_id,
            "local_prefix": local_prefix,
            "nas_prefix": nas_prefix,
            "nas_test_dir": nas_prefix,          # le watcher teste ce dossier NAS
            "remote_witness": remote_name,
            # Les fichiers de contrôle (ready/reply) vivent dans le dossier
            # selftest/ du montage PARTAGÉ, vu à des chemins différents des deux
            # côtés (/media/home_nas côté desktop, /home/nas côté NAS). On envoie
            # donc les NOMS SEULS ; le watcher les résout avec SON propre chemin
            # de base (son selftest/), et le desktop lit au sien. Même fichier
            # physique, chemins d'accès différents.
            "reply_name": f"reply-{test_id}.json",
            "ready_name": f"ready-{test_id}",
        }
        tmp = req_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(req, f)
        os.replace(tmp, req_path)
    except OSError:
        # Impossible d'écrire la demande sur le montage partagé : NAS injoignable.
        obs.timed_out = True
        return nas_selftest.verdict(obs, _)

    # 2) Attendre le signal READY (watch temporaire posé + phase B faite), PUIS
    #    écrire le témoin distant — sinon l'écriture arriverait avant le watch et
    #    l'inotify ne la capterait pas (faux jaune). Si pas de ready dans le
    #    délai, le watcher est probablement absent -> on écrit quand même (la
    #    présence physique restera testable) et le timeout tranchera.
    ready_deadline = time.time() + timeout
    ready_name = f"ready-{test_id}"
    while time.time() < ready_deadline:
        try:
            if ready_name in os.listdir(sdir):
                break
        except OSError:
            if os.path.exists(ready_path):
                break
        time.sleep(poll)
    try:
        with open(local_witness_path, "w", encoding="utf-8") as f:
            f.write("selftest-A")
        obs.remote_witness_written = True
    except OSError:
        obs.remote_witness_written = False

    # 3) Attendre la réponse (le watcher fait B puis observe A). Comme pour le
    #    témoin côté NAS, os.path.exists peut renvoyer un résultat CACHÉ sur NFS
    #    et rater le reply-* fraîchement créé par le NAS. On reliste le dossier
    #    (invalide le cache d'attributs) et on teste par le nom.
    reply = None
    reply_name = f"reply-{test_id}.json"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            present = reply_name in os.listdir(sdir)
        except OSError:
            present = os.path.exists(reply_path)
        if present:
            try:
                with open(reply_path, "r", encoding="utf-8") as f:
                    reply = json.load(f)
                break
            except (OSError, ValueError):
                pass
        time.sleep(poll)

    # 4) Nettoyer le témoin distant et les fichiers de test.
    for p in (local_witness_path, reply_path, req_path, ready_path):
        try:
            os.remove(p)
        except OSError:
            pass

    if reply is None:
        obs.timed_out = True
        return nas_selftest.verdict(obs, _)

    # Fusionner les observations du watcher avec ce que le desktop sait.
    obs.nas_dir_exists = bool(reply.get("nas_dir_exists"))
    obs.local_witness_written = bool(reply.get("local_witness_written"))
    obs.local_inotify_caught = bool(reply.get("local_inotify_caught"))
    obs.remote_witness_seen_on_nas = bool(reply.get("remote_witness_seen_on_nas"))
    obs.remote_inotify_caught = bool(reply.get("remote_inotify_caught"))
    # marker_written : non testé dans ce circuit (le témoin est isolé du moteur) ;
    # on considère la chaîne « watcher » validée si l'inotify distant a capté.
    obs.marker_written = obs.remote_inotify_caught

    return nas_selftest.verdict(obs, _)

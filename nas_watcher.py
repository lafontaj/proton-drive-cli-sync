#!/usr/bin/env python3
"""
Watcher inotify NAS (couche 4) pour la synchro Proton Drive.

Tourne SUR LE NAS (Ubuntu). Surveille les dossiers sources des mappings de TOUS
les utilisateurs (user1, user2...), en local ext4 (où inotify fonctionne), et
dépose un marqueur dans la file de l'utilisateur concerné. Le consommateur
(couche 2), côté machine locale, lit ces marqueurs via NFS et lance le moteur.

Différences avec le watcher local (couche 3) :
- Multi-utilisateur : lit les copies de mappings poussées par le GUI dans
  config/ (mappings-user1.json, mappings-user2.json...). Surveille l'union des
  sources NAS, et AIGUILLE chaque marqueur vers la file du bon utilisateur
  (déduit du chemin : les sources sont partitionnées par propriétaire).
- Les chemins sont identiques NAS<->machine locale (/media/nas1/...), donc le marqueur
  contient le chemin tel quel, directement compris par le consommateur (machine locale).
- Rattrapage au démarrage : un marqueur par mapping racine, SANS suppression
  (delete=false), car inotify ne voit pas l'existant et le démarrage est un
  moment d'incertitude (les suppressions manquées sont laissées au filet hebdo).
- Rechargement à chaud : surveille les copies de mappings et repose ses watches
  quand elles changent.

La logique pure (sélection des cibles, aiguillage, rattrapage) est testable sans
pyinotify. Le branchement pyinotify est testé sur le NAS.
"""
__version__ = "1.0.0"   # version propre à CE fichier ; incrémentée quand il change (indépendant de GitHub)

import os
import sys
import threading

# i18n (import guardé : sans i18n.py, messages en anglais — langue source).
try:
    from i18n import _
except ImportError:
    def _(s):
        return s
import json
import glob
import time

# Réutilise les helpers de marqueurs de la couche 3 (même format, même logique).
try:
    from local_watcher import (marker_for_event, event_concerns_target,
                               marker_filename, write_marker)
except ImportError:
    # Si local_watcher n'est pas importable (déploiement séparé), on duplique le
    # minimum nécessaire. En pratique, les deux fichiers sont déployés ensemble.
    import hashlib

    def marker_for_event(event_path, is_delete, is_dir):
        p = os.path.normpath(event_path)
        if is_delete:
            return os.path.dirname(p), True
        return (p if is_dir else os.path.dirname(p)), False

    def event_concerns_target(event_path, target):
        if target["type"] == "file":
            return os.path.basename(os.path.normpath(event_path)) == target["file_name"]
        return True

    def marker_filename(target_dir, is_delete):
        h = hashlib.sha1(target_dir.encode("utf-8", "replace")).hexdigest()[:12]
        kind = "del" if is_delete else "add"
        ts = time.strftime("%Y%m%d-%H%M%S")
        us = int((time.time() % 1) * 1_000_000)
        return f"{kind}_{h}_{ts}_{us:06d}"

    def write_marker(queue_dir, target_dir, is_delete):
        os.makedirs(queue_dir, exist_ok=True)
        name = marker_filename(target_dir, is_delete)
        final = os.path.join(queue_dir, name)
        tmp = final + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps({"path": target_dir, "delete": bool(is_delete)}))
        os.replace(tmp, final)
        return final


# Emplacements côté NAS. BASE_DIR = dossier « proton-sync/ » où résident
# config/ et queue/ : par défaut, le dossier de CE script (déployé dans
# proton-sync/ sur le NAS). Surchargeable sans toucher au code via
# --config-dir/--queue-dir (voir argparse plus bas), pour toute autre
# installation côté NAS.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(BASE_DIR, "config")
QUEUE_DIR = os.path.join(BASE_DIR, "queue")


# ─────────────────────────────────────────────────────────────────────────
#  Chargement des mappings multi-utilisateurs
# ─────────────────────────────────────────────────────────────────────────
def discover_users(config_dir=CONFIG_DIR):
    """Découvre les utilisateurs d'après les fichiers mappings-<user>.json
    présents dans config/. Retourne {user: chemin_du_json}."""
    users = {}
    for path in glob.glob(os.path.join(config_dir, "mappings-*.json")):
        name = os.path.basename(path)
        # mappings-user1.json -> user1
        user = name[len("mappings-"):-len(".json")]
        if user:
            users[user] = path
    return users


def _load_mappings_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return []
    if isinstance(raw, dict) and "mappings" in raw:
        return raw["mappings"]
    if isinstance(raw, list):
        return raw
    return []


# ─────────────────────────────────────────────────────────────────────────
#  Correspondance des chemins de données desktop <-> NAS
# ─────────────────────────────────────────────────────────────────────────
# Le watcher NAS voit peut-être les dossiers de données sous d'autres chemins que
# ceux écrits dans les mappings (cas Synology : /volume1/... côté NAS vs
# /media/nas1/... dans les mappings, référentiel desktop). La table de
# correspondance, poussée par le desktop dans config/nas_path_map-<user>.json,
# deux traductions symétriques :
#   • local -> NAS : pour SURVEILLER le bon dossier réel sur le NAS ;
#   • NAS -> local : pour écrire le marqueur dans le référentiel des mappings
#     (celui que le consommateur comprend, côté desktop).
# La substitution est réimplémentée ici (le watcher ne dépend pas de config.py) :
# c'est un simple remplacement de préfixe à frontière de segment. Table absente
# ou vide = aucune traduction (installation à chemins identiques, cas Linux
# monté pareil des deux côtés).
def load_path_maps(config_dir=CONFIG_DIR):
    """Charge les tables de correspondance PAR UTILISATEUR depuis
    config/nas_path_map-<user>.json. Retourne {user: [ {local, nas}, ... ]}.
    Chaque desktop pousse sa propre table (son montage peut différer). Un
    utilisateur sans fichier (ou fichier vide/illisible) n'a pas d'entrée ->
    aucune traduction pour lui."""
    maps = {}
    for path in glob.glob(os.path.join(config_dir, "nas_path_map-*.json")):
        base = os.path.basename(path)
        # nas_path_map-<user>.json -> user
        user = base[len("nas_path_map-"):-len(".json")]
        if not user:
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            continue
        pairs = []
        if isinstance(raw, list):
            for it in raw:
                if isinstance(it, dict):
                    loc = (it.get("local") or "").strip()
                    nas = (it.get("nas") or "").strip()
                    if loc and nas:
                        pairs.append({"local": loc, "nas": nas})
        if pairs:
            maps[user] = pairs
    return maps


def _translate_path(path, direction, pairs):
    """Traduit un chemin par substitution de préfixe à frontière de segment.
    direction : 'local_to_nas' ou 'nas_to_local'. Aucune paire correspondante
    (ou liste vide) -> chemin inchangé."""
    if not pairs or not path:
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


def build_targets(users_mappings, path_maps=None):
    """Construit la liste des cibles à surveiller, avec leur propriétaire.

    users_mappings : {user: [mappings...]}
    path_maps : tables de correspondance PAR UTILISATEUR {user: [ {local, nas} ]}.
    Retourne une liste de dicts :
      {"watch_dir": ..., "type": "folder"|"file", "file_name": ...,
       "user": <user>, "mapping_source": <source du mapping>}

    `watch_dir` est le chemin RÉEL à surveiller sur le NAS : la `source` du
    mapping (référentiel desktop) traduite local->NAS via la table de CET
    utilisateur. Sans table (ou chemins identiques), watch_dir == source.
    `mapping_source` reste le chemin desktop d'origine : il sert à écrire le
    marqueur dans le référentiel des mappings (après re-traduction NAS->local du
    chemin d'événement).

    Seules les sources NAS sont retenues — identifiées par le champ 'source_kind'
    == 'nfs' DÉJÀ PRÉSENT dans chaque mapping (rempli à la création via
    mount_check.detect_source_kind, poussé avec le mapping comme le reste).
    """
    if path_maps is None:
        path_maps = {}
    targets = []
    for user, mappings in users_mappings.items():
        pairs = path_maps.get(user, [])
        for m in mappings:
            source = m.get("source", "")
            mtype = m.get("type", "folder")
            if m.get("source_kind") != "nfs":
                continue
            # Chemin réel à surveiller sur le NAS (desktop -> NAS), table de CET user.
            watch_real = _translate_path(os.path.normpath(source),
                                         "local_to_nas", pairs)
            if mtype == "folder":
                targets.append({
                    "watch_dir": watch_real,
                    "type": "folder", "file_name": None,
                    "user": user, "mapping_source": os.path.normpath(source),
                })
            # Les mappings de type 'file' (ex. conteneurs VeraCrypt) sont
            # VOLONTAIREMENT exclus du temps réel : ce sont de gros fichiers qu'on
            # ne sauvegarde que démontés/stables, ce que fait le balayage (filet
            # hebdomadaire). Les surveiller en temps réel risquerait d'uploader un
            # état incohérent. Ils restent donc couverts par le balayage complet.
    return targets


# ─────────────────────────────────────────────────────────────────────────
#  Aiguillage : à quel(s) utilisateur(s) appartient un chemin ?
# ─────────────────────────────────────────────────────────────────────────
def users_for_path(path, targets):
    """Retourne l'ensemble des utilisateurs dont un mapping couvre ce chemin.

    Les chemins étant partitionnés par propriétaire, ce sera en pratique 0 ou 1
    utilisateur. Mais on gère le cas multiple (robustesse) : si un jour deux
    utilisateurs partageaient un dossier, le marqueur irait dans les deux files.
    """
    np = os.path.normpath(path)
    found = set()
    for t in targets:
        wd = t["watch_dir"]
        if np == wd or (np + "/").startswith(wd.rstrip("/") + "/"):
            # Pour une cible 'file', vérifier que le chemin concerne le fichier.
            if t["type"] == "file" and not event_concerns_target(path, t):
                continue
            found.add(t["user"])
    return found


def queue_for_user(user, queue_dir=QUEUE_DIR):
    return os.path.join(queue_dir, user)


def emit_marker(path, is_delete, is_dir, targets, queue_dir=QUEUE_DIR, log=None,
                path_maps=None):
    """Calcule le marqueur pour un événement et le dépose dans la file de chaque
    utilisateur concerné. Retourne la liste des fichiers-marqueurs écrits.

    `path` et `targets` sont en référentiel NAS (ce que voit inotify). Le chemin
    ÉCRIT dans le marqueur est retraduit NAS->local via la table de CHAQUE
    utilisateur concerné (`path_maps` = {user: [pairs]}), pour que le consommateur
    de cet utilisateur (côté desktop) le reconnaisse comme la source d'un mapping."""
    if path_maps is None:
        path_maps = {}
    target_dir, want_delete = marker_for_event(path, is_delete, is_dir)
    # L'aiguillage se fait sur le chemin de l'ÉVÉNEMENT (pas le target_dir, qui
    # peut être un parent en cas de suppression) — mais comme le parent est dans
    # le même mapping, on aiguille sur le target_dir, plus robuste pour le delete.
    users = users_for_path(target_dir, targets)
    if not users:
        # Le parent peut être hors mapping si on a remonté trop haut (ex. delete
        # à la racine d'un mapping). On retombe sur l'aiguillage par le chemin
        # d'origine de l'événement.
        users = users_for_path(path, targets)
    written = []
    for user in users:
        # Chemin à INSCRIRE dans le marqueur : référentiel desktop de CET
        # utilisateur (retraduction NAS->local avec SA table).
        marker_path = _translate_path(target_dir, "nas_to_local",
                                      path_maps.get(user, []))
        qd = queue_for_user(user, queue_dir)
        try:
            p = write_marker(qd, marker_path, want_delete)
            written.append(p)
            if log:
                tag = "DEL" if want_delete else "ADD"
                log(f"  {tag} [{user}] {path} -> {marker_path}")
        except OSError as e:
            if log:
                log(_("  ⚠ marker write ({u}) failed: {e}").format(u=user, e=e))
    if not users and log:
        log(_("  ⊘ no user for {p} (ignored)").format(p=path))
    return written


# ─────────────────────────────────────────────────────────────────────────
#  Rattrapage au démarrage (SANS suppression)
# ─────────────────────────────────────────────────────────────────────────
def startup_catchup(targets, queue_dir=QUEUE_DIR, log=None):
    """Dépose un marqueur de rattrapage (delete=False) par mapping racine, pour
    chaque utilisateur. Force le moteur à revérifier l'existant (qu'inotify ne
    voit pas au démarrage). SANS suppression : les suppressions manquées sont
    laissées au filet hebdomadaire (démarrage = moment d'incertitude).

    On déduplique par (user, mapping_source) pour ne pas multiplier les
    marqueurs si plusieurs cibles partagent une racine.
    """
    seen = set()
    count = 0
    for t in targets:
        # Pour un mapping 'folder', la cible de rattrapage est sa source ; pour un
        # mapping 'file', c'est le dossier parent (watch_dir). On déduplique par
        # (user, target_dir) EFFECTIF, pour ne pas déposer deux marqueurs si deux
        # mappings 'file' partagent le même dossier parent (ex. deux conteneurs
        # VeraCrypt dans /media/nas1/Conteneurs).
        # target_dir = chemin ÉCRIT dans le marqueur (référentiel desktop, celui
        # des mappings) ; watch_real = chemin RÉEL sur le NAS (pour tester
        # l'existence). Sans table de correspondance, les deux sont identiques.
        if t["type"] == "folder":
            target_dir = t["mapping_source"]   # référentiel desktop
            watch_real = t["watch_dir"]        # référentiel NAS
        else:
            target_dir = t["watch_dir"]
            watch_real = t["watch_dir"]
        key = (t["user"], target_dir)
        if key in seen:
            continue
        seen.add(key)
        if not os.path.isdir(watch_real):
            if log:
                log(_("  ⚠ catch-up: folder missing, skipped: {p}").format(p=target_dir))
            continue
        qd = queue_for_user(t["user"], queue_dir)
        try:
            write_marker(qd, target_dir, is_delete=False)
            count += 1
            if log:
                log(_("  ⟳ catch-up [{u}] {p}").format(u=t["user"], p=target_dir))
        except OSError as e:
            if log:
                log(_("  ⚠ catch-up failed ({u}): {e}").format(u=t["user"], e=e))
    return count


# ─────────────────────────────────────────────────────────────────────────
#  Branchement pyinotify (testé sur le NAS)
# ─────────────────────────────────────────────────────────────────────────
def _log_orphan_queues(config_dir, queue_dir, log):
    """Signale (SANS y toucher) toute file queue/<user> qui n'a plus de
    mappings-<user>.json correspondant dans config/ — typiquement le résidu
    d'un ANCIEN nom de fichier de mappings abandonné côté poste de travail.
    Ses marqueurs ne seront jamais consommés ; le ménage se fait depuis le
    GUI (garde-fou de changement de nom) — prudence ici : une file sans
    config peut aussi être une installation en cours."""
    try:
        users = set(discover_users(config_dir))
        for name in sorted(os.listdir(queue_dir)):
            qd = os.path.join(queue_dir, name)
            if not os.path.isdir(qd) or name in users:
                continue
            try:
                n = sum(1 for f in os.listdir(qd)
                        if os.path.isfile(os.path.join(qd, f)))
            except OSError:
                continue
            if n:
                log(_("⚠ Orphan queue (no matching mappings file): {d} "
                      "({n} marker(s)) — left untouched.").format(d=qd, n=n))
    except OSError:
        pass


def load_all_targets(config_dir=CONFIG_DIR, log=None):
    """Charge les mappings de tous les utilisateurs et construit les cibles.
    Retourne (targets, path_maps) : les tables de correspondance PAR UTILISATEUR
    sont chargées ici pour être réutilisées à l'émission des marqueurs sans les
    relire à chaque événement."""
    users = discover_users(config_dir)
    if not users and log:
        log(_("No mappings copy in {d} "
            "(the GUI must push them there).").format(d=config_dir))
    users_mappings = {u: _load_mappings_file(p) for u, p in users.items()}
    path_maps = load_path_maps(config_dir)
    if path_maps and log:
        total = sum(len(v) for v in path_maps.values())
        log(_("  Path maps: {n} correspondence(s) across {u} user(s) "
              "desktop<->NAS.").format(n=total, u=len(path_maps)))
    return build_targets(users_mappings, path_maps), path_maps


def run_watcher(config_dir=CONFIG_DIR, queue_dir=QUEUE_DIR, log=None,
                do_catchup=True):
    """Lance la surveillance inotify côté NAS. Bloquant."""
    import pyinotify

    if log is None:
        def log(msg):
            ts = time.strftime("%H:%M:%S")
            print(f"[{ts}] {msg}", flush=True)

    targets, path_maps = load_all_targets(config_dir, log=log)
    _log_orphan_queues(config_dir, queue_dir, log)
    if not targets:
        log(_("No NAS target to watch. The NAS watcher has nothing to do."))
        return

    # Vérif de la marge de watches (alerte si insuffisant).
    _check_watch_capacity(targets, log)

    log(_("NAS watcher started — {n} target(s):").format(n=len(targets)))
    for t in targets:
        kind = ("fichier " + t["file_name"]) if t["type"] == "file" else "dossier"
        log(f"   • [{t['user']}] {t['watch_dir']}  ({kind})")

    # Rattrapage au démarrage (sans suppression).
    if do_catchup:
        n = startup_catchup(targets, queue_dir, log=log)
        log(_("Startup catch-up: {n} marker(s) written.").format(n=n))

    mask = (pyinotify.IN_CLOSE_WRITE | pyinotify.IN_MOVED_TO |
            pyinotify.IN_MOVED_FROM | pyinotify.IN_DELETE |
            pyinotify.IN_CREATE)

    wm = pyinotify.WatchManager()

    # Self-test (chantier v1.5.0) : registre passif des événements + thread qui
    # traite les demandes de test. PUREMENT ADDITIF — n'influence pas l'émission
    # des marqueurs. Import tolérant : si le module manque, le watcher marche pareil.
    _st_registry = None
    _st_stop = None
    _st_thread = None
    try:
        import nas_selftest_watcher as _stw
        _st_registry = _stw.EventRegistry()
    except Exception:
        _stw = None

    def _is_selftest_witness(pathname):
        """True si le chemin est un fichier lié au self-test (témoin OU fichier de
        contrôle du canal). Ces fichiers ne doivent jamais générer de marqueur de
        synchro. Couvre : les témoins `.proton-selftest-*` (dans les dossiers de
        données testés) et les fichiers de contrôle request-/ready-/reply- (dans
        le dossier selftest/ du montage partagé, qui peut se retrouver sous un
        watch temporaire quand on teste le montage partagé lui-même)."""
        base = os.path.basename(pathname or "")
        if base.startswith(".proton-selftest-"):
            return True
        if "/selftest/" in pathname or pathname.endswith("/selftest"):
            if (base.startswith("request-") or base.startswith("ready-")
                    or base.startswith("reply-")):
                return True
        return False

    class Handler(pyinotify.ProcessEvent):
        def _emit(self, event):
            # Alimenter le registre self-test (passif), puis, si c'est un témoin
            # de test, NE PAS émettre de marqueur (isolation : un témoin ne doit
            # jamais entrer dans le flux de synchro).
            if _st_registry is not None:
                _st_registry.note(event.pathname)
            if _is_selftest_witness(event.pathname):
                return
            is_delete = bool(event.mask & (pyinotify.IN_DELETE | pyinotify.IN_MOVED_FROM))
            is_dir = bool(event.mask & pyinotify.IN_ISDIR)
            emit_marker(event.pathname, is_delete, is_dir, targets,
                        queue_dir=queue_dir, log=log, path_maps=path_maps)

        def _emit_create(self, event):
            # Un CREATE de témoin self-test doit AUSSI être noté (l'inotify l'a vu)
            # mais jamais émis. Les dossiers créés continuent d'être émis normalement.
            if _st_registry is not None:
                _st_registry.note(event.pathname)
            if _is_selftest_witness(event.pathname):
                return
            if event.mask & pyinotify.IN_ISDIR:
                self._emit(event)

        process_IN_CLOSE_WRITE = _emit
        process_IN_MOVED_TO = _emit
        process_IN_MOVED_FROM = _emit
        process_IN_DELETE = _emit
        process_IN_CREATE = _emit_create

    handler = Handler()
    notifier = pyinotify.Notifier(wm, handler)

    # Poser les watches récursifs sur chaque dossier surveillé (dédupliqués).
    watched_dirs = sorted({t["watch_dir"] for t in targets})
    for wd in watched_dirs:
        if not os.path.isdir(wd):
            log(_("   ⚠ folder missing, not watched: {p}").format(p=wd))
            continue
        wm.add_watch(wd, mask, rec=True, auto_add=True)

    log(_("Watching. (Ctrl+C to stop.)"))

    # Démarrer le thread self-test (traite les demandes déposées par le desktop
    # dans NAS_BASE/selftest/). Isolé, tolérant : s'il échoue, le watcher continue.
    if _stw is not None and _st_registry is not None:
        try:
            nas_base = os.path.dirname(os.path.normpath(config_dir))  # .../proton-sync
            sdir = _stw.selftest_dir(nas_base)
            os.makedirs(sdir, exist_ok=True)

            # Callbacks pour poser/retirer un watch TEMPORAIRE (test découplé d'un
            # mapping). add_watch retourne le dict {path: wd} de pyinotify ; on le
            # passe tel quel à rm_watch pour retirer.
            def _st_add_watch(path):
                if not os.path.isdir(path):
                    return None
                return wm.add_watch(path, mask, rec=True, auto_add=True)

            def _st_rm_watch(token):
                if not token:
                    return
                for wd in token.values():
                    if wd and wd > 0:
                        try:
                            wm.rm_watch(wd)
                        except Exception:
                            pass

            _st_stop = threading.Event()
            _st_thread = threading.Thread(
                target=_stw.poll_requests,
                args=(nas_base, _st_registry),
                kwargs={"log": log, "stop_event": _st_stop,
                        "add_watch": _st_add_watch, "rm_watch": _st_rm_watch},
                daemon=True)
            _st_thread.start()
            log(_("  Self-test listener active (correspondence checks)."))
        except Exception:
            pass   # jamais bloquer le watcher pour le self-test

    try:
        notifier.loop()
    finally:
        if _st_stop is not None:
            _st_stop.set()


def _check_watch_capacity(targets, log):
    """Compte approximativement les sous-dossiers à surveiller et compare à la
    limite système. Alerte si la marge semble insuffisante."""
    try:
        with open("/proc/sys/fs/inotify/max_user_watches") as f:
            limit = int(f.read().strip())
    except (OSError, ValueError):
        return
    # Estimation grossière : compter les dossiers sous chaque watch_dir.
    total = 0
    for wd in {t["watch_dir"] for t in targets}:
        if not os.path.isdir(wd):
            continue
        try:
            for _root, dirs, _files in os.walk(wd):
                total += len(dirs)
        except OSError:
            pass
    if total > limit * 0.8:
        log(_("⚠ WARNING: ~{t} subfolders to watch, system "
            "limit = {l}. Watches may be missed. "
            "Increase fs.inotify.max_user_watches.").format(t=total, l=limit))
    else:
        log(_("Watch capacity OK (~{t} folders / limit {l}).").format(t=total, l=limit))


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Watcher inotify NAS pour la synchro Proton Drive")
    parser.add_argument("--config-dir", default=CONFIG_DIR,
                        help=f"Dossier des copies de mappings (défaut {CONFIG_DIR})")
    parser.add_argument("--queue-dir", default=QUEUE_DIR,
                        help=f"Dossier des files par utilisateur (défaut {QUEUE_DIR})")
    parser.add_argument("--no-catchup", action="store_true",
                        help="Ne pas déposer de marqueurs de rattrapage au démarrage")
    args = parser.parse_args()
    run_watcher(config_dir=args.config_dir, queue_dir=args.queue_dir,
                do_catchup=not args.no_catchup)


if __name__ == "__main__":
    main()

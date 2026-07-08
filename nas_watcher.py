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
import os
import sys

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


def build_targets(users_mappings):
    """Construit la liste des cibles à surveiller, avec leur propriétaire.

    users_mappings : {user: [mappings...]}
    Retourne une liste de dicts :
      {"watch_dir": ..., "type": "folder"|"file", "file_name": ...,
       "user": <user>, "mapping_source": <source du mapping>}
    Seules les sources NAS sont retenues — identifiées par le champ
    'source_kind' == 'nfs' DÉJÀ PRÉSENT dans chaque mapping (rempli à la
    création via mount_check.detect_source_kind, poussé avec le mapping comme
    le reste). Aucune liste de préfixes à maintenir côté NAS : la même
    information circule déjà d'un bout à l'autre du projet, on la réutilise
    telle quelle plutôt que de la dupliquer sous une autre forme.
    """
    targets = []
    for user, mappings in users_mappings.items():
        for m in mappings:
            source = m.get("source", "")
            mtype = m.get("type", "folder")
            if m.get("source_kind") != "nfs":
                continue
            if mtype == "folder":
                targets.append({
                    "watch_dir": os.path.normpath(source),
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


def emit_marker(path, is_delete, is_dir, targets, queue_dir=QUEUE_DIR, log=None):
    """Calcule le marqueur pour un événement et le dépose dans la file de chaque
    utilisateur concerné. Retourne la liste des fichiers-marqueurs écrits."""
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
        qd = queue_for_user(user, queue_dir)
        try:
            p = write_marker(qd, target_dir, want_delete)
            written.append(p)
            if log:
                tag = "DEL" if want_delete else "ADD"
                log(f"  {tag} [{user}] {path} -> {target_dir}")
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
        target_dir = t["mapping_source"] if t["type"] == "folder" else t["watch_dir"]
        key = (t["user"], target_dir)
        if key in seen:
            continue
        seen.add(key)
        if not os.path.isdir(target_dir):
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
    """Charge les mappings de tous les utilisateurs et construit les cibles."""
    users = discover_users(config_dir)
    if not users and log:
        log(_("No mappings copy in {d} "
            "(the GUI must push them there).").format(d=config_dir))
    users_mappings = {u: _load_mappings_file(p) for u, p in users.items()}
    return build_targets(users_mappings)


def run_watcher(config_dir=CONFIG_DIR, queue_dir=QUEUE_DIR, log=None,
                do_catchup=True):
    """Lance la surveillance inotify côté NAS. Bloquant."""
    import pyinotify

    if log is None:
        def log(msg):
            ts = time.strftime("%H:%M:%S")
            print(f"[{ts}] {msg}", flush=True)

    targets = load_all_targets(config_dir, log=log)
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

    class Handler(pyinotify.ProcessEvent):
        def _emit(self, event):
            is_delete = bool(event.mask & (pyinotify.IN_DELETE | pyinotify.IN_MOVED_FROM))
            is_dir = bool(event.mask & pyinotify.IN_ISDIR)
            emit_marker(event.pathname, is_delete, is_dir, targets,
                        queue_dir=queue_dir, log=log)

        def _emit_create(self, event):
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
    notifier.loop()


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

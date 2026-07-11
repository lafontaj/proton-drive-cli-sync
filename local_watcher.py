#!/usr/bin/env python3
"""
Watcher inotify LOCAL de la machine locale (couche 3) pour la synchro Proton Drive.

Surveille les dossiers LOCAUX (sur ext4 de la machine locale) déclarés dans les mappings
de l'utilisateur courant, et dépose un marqueur dans la file locale
(~/.proton_sync/queue/) à chaque changement détecté. Le démon consommateur
(couche 2) prend ensuite le relais.

Décisions de conception (validées) :
- Ne surveille QUE les sources locales (ext4). Le classement local/NAS se fait
  via mount_check.detect_source_kind() — robuste, récupère même les mappings
  sans source_kind déclaré. Les sources NAS sont surveillées par la couche 4
  (démon inotify côté NAS).
- Marqueur = petit JSON {"path": dossier, "delete": bool}.
    * Ajout/modif    -> marqueur sur le DOSSIER du fichier touché, delete=False.
    * Suppression    -> marqueur sur le DOSSIER PARENT du chemin supprimé,
                        delete=True (option 1 : remonter au parent pour qu'un
                        dossier entier supprimé soit bien constaté par le moteur).
- Cas du mapping de type 'file' (ex. prefs.js) : on surveille le dossier parent
  du fichier et on ne réagit qu'aux événements concernant ce fichier précis.
- Surveillance récursive avec auto-ajout des nouveaux sous-dossiers (pyinotify
  rec=True, auto_add=True).

La PARTIE LOGIQUE (marker_for_event, classify, ...) est pure et testable sans
pyinotify. La PARTIE BRANCHEMENT (WatchManager, boucle d'événements) nécessite
pyinotify et de vrais dossiers — testée sur la machine locale.
"""
__version__ = "1.0.0"   # version propre à CE fichier ; incrémentée quand il change (indépendant de GitHub)

import os
import sys

# i18n (import guardé : sans i18n.py, messages en anglais — langue source).
try:
    from i18n import _
except ImportError:
    def _(s):
        return s
import json
import hashlib
import glob
import time

# Réglages d'installation (dossier de données unifié) : une SEULE source de
# vérité partagée par le moteur, le GUI et les démons. Import tolérant : si
# absent, on retombe sur l'ancien emplacement.
try:
    import config as appconfig
    _HAS_CONFIG = True
except ImportError:
    _HAS_CONFIG = False

# Dossier de file locale (dossier de données unifié, config.py — migration
# automatique et sûre depuis l'ancien ~/.proton_sync, voir config.py).
if _HAS_CONFIG:
    BASE_DIR = appconfig.DATA_DIR
    LOCAL_QUEUE = appconfig.QUEUE_DIR
else:
    BASE_DIR = os.path.expanduser("~/.proton_sync")
    LOCAL_QUEUE = os.path.join(BASE_DIR, "queue")

# mount_check est requis pour classer local vs NAS.
try:
    import mount_check
    _HAS_MOUNT_CHECK = True
except ImportError:
    _HAS_MOUNT_CHECK = False


# ─────────────────────────────────────────────────────────────────────────
#  Chargement des mappings & sélection des sources LOCALES
# ─────────────────────────────────────────────────────────────────────────
def load_mappings(config_path):
    """Charge la liste des mappings depuis le JSON (gère les deux formats)."""
    with open(config_path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    if isinstance(raw, dict) and "mappings" in raw:
        return raw["mappings"]
    if isinstance(raw, list):
        return raw
    return []


def source_kind_of(source_path):
    """Retourne le 'kind' du système de fichiers portant la source :
    'local', 'nfs' ou 'missing'. Utilise mount_check si disponible."""
    if not _HAS_MOUNT_CHECK:
        return "local"  # sans mount_check, on suppose local (pas de garde NFS)
    info = mount_check.detect_source_kind(source_path)
    return info.get("kind", "missing")


def is_local_source(source_path):
    """Conservé pour compatibilité : True si la source est LOCALE (ext4...)."""
    return source_kind_of(source_path) == "local"


def select_targets(mappings):
    """À partir des mappings, retourne les cibles à surveiller par le watcher
    machine locale : TOUS les dossiers 'folder', qu'ils soient LOCAUX (ext4) ou NAS
    (/media/nas1...). Raison : la machine locale voit ses propres écritures, y compris
    celles qu'il fait dans les dossiers NAS via NFS (testé). Chaque cible note
    son 'kind' (local/nfs) pour appliquer la protection anti-faux-delete
    seulement sur les dossiers NAS.

    Chaque cible :
      {"watch_dir": ..., "type": "folder", "file_name": None, "kind": "local"|"nfs"}

    Les mappings de type 'file' restent EXCLUS du temps réel (balayage).
    """
    targets = []
    for m in mappings:
        source = m.get("source", "")
        mtype = m.get("type", "folder")
        if mtype != "folder":
            continue
        kind = source_kind_of(source)
        if kind == "missing":
            # Source introuvable au démarrage : on ignore (sera reprise au
            # rechargement si elle réapparaît). Évite de surveiller un fantôme.
            continue
        targets.append({"watch_dir": os.path.normpath(source),
                        "type": "folder", "file_name": None, "kind": kind})
    return targets


# Alias rétrocompatible : l'ancien nom pointe vers la nouvelle fonction.
def select_local_targets(mappings):
    """DEPRECATED : conservé pour compatibilité. Utilise select_targets, qui
    surveille désormais local ET NAS."""
    return select_targets(mappings)


# ─────────────────────────────────────────────────────────────────────────
#  Logique pure : événement -> marqueur (chemin + delete)
# ─────────────────────────────────────────────────────────────────────────
def marker_for_event(event_path, is_delete, is_dir):
    """Calcule le (target_dir, delete) à inscrire dans un marqueur pour un
    événement donné. Logique pure, testable sans pyinotify.

    - is_delete=False (ajout/modif d'un fichier) :
        target = dossier CONTENANT le fichier (event_path est le fichier).
        Si l'événement concerne un dossier (création de dossier), target = ce
        dossier lui-même (son contenu sera synchronisé).
    - is_delete=True (suppression/déplacement-hors) :
        target = dossier PARENT du chemin supprimé (option 1), pour que le moteur
        constate l'absence de l'élément et la propage. delete=True.
    """
    p = os.path.normpath(event_path)
    if is_delete:
        # On remonte toujours au parent : que ce soit un fichier ou un dossier
        # supprimé, son parent existe encore et le moteur y constatera l'absence.
        return os.path.dirname(p), True
    else:
        if is_dir:
            # Dossier créé/modifié : on cible ce dossier (son contenu).
            return p, False
        # Fichier ajouté/modifié : on cible son dossier contenant.
        return os.path.dirname(p), False


def event_concerns_target(event_path, target):
    """Pour un mapping de type 'file', ne réagir qu'aux événements concernant le
    fichier précis. Pour 'folder', tout événement sous le watch_dir compte."""
    if target["type"] == "file":
        return os.path.basename(os.path.normpath(event_path)) == target["file_name"]
    return True


def _marker_prefix(target_dir, is_delete):
    """Préfixe de nom encodant l'IDENTITÉ d'un marqueur : hash du chemin + type
    (add/del). Deux marqueurs de même préfixe = même dossier ET même flag delete.
    Un 'add' et un 'del' du même dossier ont des préfixes DIFFÉRENTS."""
    h = hashlib.sha1(target_dir.encode("utf-8", "replace")).hexdigest()[:12]
    kind = "del" if is_delete else "add"
    return f"{kind}_{h}_"


def marker_filename(target_dir, is_delete):
    """Nom de fichier unique pour un marqueur. Le nom n'a pas de sens fonctionnel
    (le contenu JSON fait foi) ; il doit juste être unique et sans caractères
    problématiques. On combine le préfixe d'identité + horodatage."""
    ts = time.strftime("%Y%m%d-%H%M%S")
    # microsecondes pour éviter les collisions dans la même seconde
    us = int((time.time() % 1) * 1_000_000)
    return f"{_marker_prefix(target_dir, is_delete)}{ts}_{us:06d}"


def write_marker(queue_dir, target_dir, is_delete):
    """Écrit un marqueur JSON {path, delete} dans la file, de façon atomique
    (tmp + rename). Crée la file si besoin. Logique pure (filesystem).

    Dédoublonnage à l'écriture : si un marqueur STRICTEMENT identique (même
    dossier ET même flag delete) attend déjà dans la file, on n'en dépose pas un
    second — le nom encode hash(chemin)+type, donc un simple glob du préfixe
    suffit (pas besoin de lire les contenus). Cela évite l'accumulation quand le
    consommateur est en retard (ex. watcher NAS qui redémarre et redépose les
    marqueurs de rattrapage par-dessus une file non vidée). SÛR : un marqueur =
    « regarde ce dossier » ; un seul suffit, l'état réel du disque au traitement
    fait foi, aucune donnée n'est perdue. Un 'add' et un 'del' du même dossier ne
    se dédoublonnent PAS entre eux (préfixes différents ; le consommateur applique
    « delete l'emporte » à la consommation). Retourne le chemin écrit, ou None si
    un marqueur identique était déjà en attente."""
    os.makedirs(queue_dir, exist_ok=True)
    prefix = _marker_prefix(target_dir, is_delete)
    if glob.glob(os.path.join(queue_dir, prefix + "*")):
        return None  # marqueur identique déjà en attente
    name = f"{prefix}{time.strftime('%Y%m%d-%H%M%S')}_{int((time.time() % 1) * 1_000_000):06d}"
    final = os.path.join(queue_dir, name)
    tmp = final + ".tmp"
    payload = json.dumps({"path": target_dir, "delete": bool(is_delete)})
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(payload)
    os.replace(tmp, final)
    return final


# ─────────────────────────────────────────────────────────────────────────
#  Branchement pyinotify (testé sur la machine locale, pas en sandbox)
# ─────────────────────────────────────────────────────────────────────────
def _folder_sources(mappings):
    """Sources de type 'folder' (les seules concernées par le temps réel)."""
    return [m.get("source", "") for m in mappings
            if m.get("type", "folder") == "folder"]


def _missing_sources(mappings):
    """Liste des sources 'folder' pas encore accessibles (montage NAS pas prêt,
    ou dossier absent). Sert à cadencer le ré-scan : tant qu'il en reste au tout
    début (course au boot), on sonde vite ; ensuite on ralentit."""
    return [s for s in _folder_sources(mappings) if source_kind_of(s) == "missing"]


def _diff_targets(new_targets, watched, isdir=os.path.isdir):
    """Réconciliation PURE (sans effet de bord, donc testable) entre l'ensemble
    déjà surveillé (`watched`) et les nouvelles cibles.

    Retourne (new_by_dir, added, removed) :
      • new_by_dir = index watch_dir -> [targets] reconstruit ;
      • added   = dossiers PRÉSENTS (isdir) dans new_targets mais pas surveillés ;
      • removed = dossiers surveillés mais absents des new_targets présents
                  (montage tombé, dossier supprimé, ou source retirée du mapping).
    """
    new_by_dir = {}
    for t in new_targets:
        new_by_dir.setdefault(t["watch_dir"], []).append(t)
    present = set(d for d in new_by_dir if isdir(d))
    added = present - set(watched)
    removed = set(watched) - present
    return new_by_dir, added, removed


def run_watcher(config_path, log=None, rescan_interval=30, fast_interval=3,
                fast_window=120):
    """Lance la surveillance inotify des dossiers locaux ET NAS (via les montages
    de la machine locale). Bloquant. À lancer dans la session de l'utilisateur.

    Robuste aux montages, sans jamais retarder la couverture des sources déjà
    disponibles :
      • surveille IMMÉDIATEMENT ce qui est monté au démarrage (les sources
        locales dès t=0) ;
      • NIVEAU 1 (course au boot) — tant qu'il reste des sources non montées et
        pendant les fast_window premières secondes, ré-scanne VITE (fast_interval
        s) pour capter les montages NAS dès qu'ils apparaissent (quelques
        secondes après le boot) ;
      • NIVEAU 2 (régime permanent) — ensuite, ré-scanne toutes les
        rescan_interval s pour prendre en compte un montage qui APPARAÎT (source
        reprise) ou DISPARAÎT (démontée / retirée du mapping), et recharge les
        mappings au passage. Chaque changement est journalisé (➕ / ➖ / 🔄).
    """
    import pyinotify

    if log is None:
        def log(msg):
            ts = time.strftime("%H:%M:%S")
            print(f"[{ts}] {msg}", flush=True)

    mappings = load_mappings(config_path)
    # Énumération immédiate : on surveille ce qui est monté MAINTENANT (les
    # locales tout de suite). Les sources NAS pas encore montées sont reprises
    # par le ré-scan (rapide au début), donc rien n'est perdu ni retardé côté
    # local.
    targets = select_targets(mappings)

    os.makedirs(LOCAL_QUEUE, exist_ok=True)

    mask = (pyinotify.IN_CLOSE_WRITE | pyinotify.IN_MOVED_TO |
            pyinotify.IN_MOVED_FROM | pyinotify.IN_DELETE |
            pyinotify.IN_CREATE)
    wm = pyinotify.WatchManager()

    # État partagé, muté EN PLACE pour que le Handler voie toujours l'index à
    # jour après un ré-scan (les closures pointent sur ces mêmes objets).
    by_dir = {}          # watch_dir -> [targets]
    watched = set()      # watch_dirs effectivement sous surveillance inotify

    def _kind_for_dir(best_dir):
        ts = by_dir.get(best_dir, [])
        return ts[0]["kind"] if ts else "local"

    class Handler(pyinotify.ProcessEvent):
        def _emit(self, event):
            path = event.pathname
            best_dir = None
            best_len = -1
            np = os.path.normpath(path)
            for wd in by_dir:
                if np == wd or (np + "/").startswith(wd.rstrip("/") + "/"):
                    if len(wd) > best_len:
                        best_dir, best_len = wd, len(wd)
            if best_dir is None:
                return
            relevant = False
            for t in by_dir[best_dir]:
                if event_concerns_target(path, t):
                    relevant = True
                    break
            if not relevant:
                return

            is_delete = bool(event.mask & (pyinotify.IN_DELETE | pyinotify.IN_MOVED_FROM))
            is_dir = bool(event.mask & pyinotify.IN_ISDIR)
            target_dir, want_delete = marker_for_event(path, is_delete, is_dir)

            # PROTECTION anti-faux-delete : pour un dossier NAS, avant d'émettre
            # un marqueur de SUPPRESSION, vérifier que le montage est sain. Si le
            # NAS est tombé (montage non sain), on IGNORE l'événement delete
            # plutôt que de produire un faux marqueur. Les dossiers locaux (ext4)
            # ne sont pas concernés (pas de montage réseau à valider).
            if want_delete and _kind_for_dir(best_dir) == "nfs":
                if _HAS_MOUNT_CHECK:
                    info = mount_check.detect_source_kind(target_dir)
                    healthy = (info.get("kind") == "nfs" and info.get("readable"))
                    if not healthy:
                        log(_("  ⊘ DELETE ignored (NAS mount unhealthy): {p}").format(p=path))
                        return

            try:
                write_marker(LOCAL_QUEUE, target_dir, want_delete)
                tag = "DEL" if want_delete else "ADD"
                log(_("  {t} {p} -> marker on {d}").format(t=tag, p=path, d=target_dir))
            except OSError as e:
                log(_("  ⚠ marker write failed: {e}").format(e=e))

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

    def _counts():
        nl = sum(1 for d in watched if by_dir.get(d) and by_dir[d][0]["kind"] == "local")
        nn = sum(1 for d in watched if by_dir.get(d) and by_dir[d][0]["kind"] == "nfs")
        return nl, nn

    def _apply_targets(new_targets, first=False):
        """Réconcilie l'ensemble surveillé avec new_targets : pose les watches des
        cibles APPARUES (dossier présent), retire celles DISPARUES (montage tombé
        ou source retirée du mapping). Retourne True si quelque chose a changé."""
        new_by_dir, added, removed = _diff_targets(new_targets, watched)

        # Mettre à jour l'index AVANT de toucher aux watches (le Handler s'en sert).
        by_dir.clear()
        by_dir.update(new_by_dir)

        for d in sorted(added):
            try:
                wm.add_watch(d, mask, rec=True, auto_add=True)
                watched.add(d)
                if not first:
                    log(_("  ➕ target added: [{k}] {d}").format(k=new_by_dir[d][0]["kind"], d=d))
            except Exception as e:
                log(_("  ⚠ cannot watch {d}: {e}").format(d=d, e=e))

        for d in sorted(removed):
            try:
                wd = wm.get_wd(d)
                if wd is not None:
                    wm.rm_watch(wd, rec=True, quiet=True)
            except Exception:
                pass
            watched.discard(d)
            log(_("  ➖ target removed (mount down or source deleted): {d}").format(d=d))

        return bool(added or removed)

    # ── Pose initiale ──
    _apply_targets(targets, first=True)
    nl, nn = _counts()
    log(_("Local watcher started — {n} target(s) "
        "({l} local, {m} NAS):").format(n=len(watched), l=nl, m=nn))
    for d in sorted(watched):
        log(f"   • [{by_dir[d][0]['kind']}] {d}")
    pending = _missing_sources(mappings)
    if pending:
        log(_("   ⏳ {n} source(s) not mounted yet — picked up "
            "automatically as soon as they appear:").format(n=len(pending)))
        for s in pending:
            log(f"      • {s}")
    log(_("Watching — mounts re-scanned every {s}s "
        "(faster at startup). (Ctrl+C to stop.)").format(s=rescan_interval))

    # ── Boucle : événements inotify + ré-scan à cadence adaptative ──
    #  Rapide (fast_interval) tant qu'il manque des montages et pendant les
    #  fast_window premières secondes (course au boot) ; sinon régime tranquille
    #  (rescan_interval) pour détecter un montage tombé/repris en cours de session.
    started = time.monotonic()
    last_rescan = started
    try:
        while True:
            if notifier.check_events(timeout=1000):   # timeout en ms
                notifier.read_events()
                notifier.process_events()

            now = time.monotonic()
            fast = _missing_sources(mappings) and (now - started) < fast_window
            interval = fast_interval if fast else rescan_interval
            if now - last_rescan >= interval:
                last_rescan = now
                # Recharger les mappings (la config a pu changer) ; en cas de
                # lecture ratée ou vide, on GARDE les précédents pour ne pas
                # effacer toutes les cibles sur une erreur transitoire.
                try:
                    fresh = load_mappings(config_path)
                except Exception:
                    fresh = None
                if fresh:
                    mappings = fresh
                if _apply_targets(select_targets(mappings)):
                    nl, nn = _counts()
                    log(_("  🔄 Re-scan: {n} target(s) watched "
                        "({l} local, {m} NAS).").format(n=len(watched), l=nl, m=nn))
    except KeyboardInterrupt:
        pass
    finally:
        notifier.stop()


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Watcher inotify local (machine locale) pour la synchro Proton Drive")
    parser.add_argument("config", help="Fichier JSON de mappings de cet utilisateur")
    args = parser.parse_args()
    run_watcher(args.config)


if __name__ == "__main__":
    main()

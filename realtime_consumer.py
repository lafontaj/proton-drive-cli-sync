#!/usr/bin/env python3
"""
Démon consommateur temps réel (couche 2) pour la synchro Proton Drive.

Lit les files de marqueurs (locale + NAS via NFS), accumule les chemins de
dossiers touchés, applique un délai de debounce (regroupe les rafales, laisse
les écritures finir), déduplique par dossier, puis lance le moteur
`proton_sync.py --subpath ...` sur chaque dossier mûr.

Principes (décidés en conception) :
- Démon permanent « détendu » : cycle de sondage tranquille (~30 s), pas de
  surveillance temps-réel des files. Le sondage par os.listdir contourne le fait
  qu'inotify ne fonctionne pas sur NFS (la file NAS est sur NFS).
- Marqueur = chemin du dossier touché, rien d'autre. Le mapping est déduit par
  correspondance de préfixe avec les 'source' des mappings -> la vérité reste
  côté mappings/moteur.
- Debounce daté par PREMIÈRE OBSERVATION (horloge locale de la machine locale), robuste aux
  décalages d'horloge NAS<->machine locale.
- Bloquant séquentiel : un dossier à la fois, on attend la fin du moteur. Les
  producteurs (inotify) continuent d'écrire dans la file sans entrave pendant ce
  temps (découplage par fichiers sur disque).
- Robuste : marqueur parasite / dossier disparu / hors mapping -> ignoré
  proprement. Survit aux redémarrages (marqueurs sur disque).

Un démon par utilisateur (sa session, son trousseau, ses mappings).
"""
import os
import sys

# i18n (import guardé : sans i18n.py, messages en anglais — langue source).
try:
    from i18n import _
except ImportError:
    def _(s):
        return s
import csv
import json
import time
import subprocess
import threading

# Réglages d'installation (dossier de données unifié, présence NAS...) : une
# SEULE source de vérité partagée par le moteur, le GUI et les démons. Import
# tolérant : si absent, on retombe sur les anciens emplacements/valeurs.
try:
    import config as appconfig
    _HAS_CONFIG = True
except ImportError:
    _HAS_CONFIG = False

# Dossier parent unifié (dossier de données unifié, config.py — migration
# automatique et sûre depuis l'ancien ~/.proton_sync, voir config.py).
if _HAS_CONFIG:
    BASE_DIR = appconfig.DATA_DIR
    LOCAL_QUEUE = appconfig.QUEUE_DIR
    CONFIG_FILE = appconfig.REALTIME_CONF
else:
    BASE_DIR = os.path.expanduser("~/.proton_sync")
    LOCAL_QUEUE = os.path.join(BASE_DIR, "queue")
    CONFIG_FILE = os.path.join(BASE_DIR, "realtime.conf")


# File NAS (via NFS). <user> est résolu au lancement. Point de montage
# CONFIGURABLE (voir Configuration… dans le GUI), plus de chemin figé pour une
# autre topologie d'installation.
def _nas_queue_for(user):
    mount = appconfig.nas_mount_path() if _HAS_CONFIG else "/media/home_nas"
    return f"{mount}/proton-sync/queue/{user}"


def _user_from_config(config_path):
    """Identité NAS de cette installation. PRIORITÉ au réglage persistant
    « account_name » (settings.json) — l'identité STABLE, découplée du nom du
    fichier ; repli (compatibilité) sur le nom dérivé : mappings-user1.json ->
    user1. Le login Unix local peut différer (ex. 'myuser' pour 'user1') :
    s'appuyer sur $USER ferait lire la mauvaise file NAS. NB : les démons ne
    résolvent JAMAIS l'adresse Proton eux-mêmes (session parfois verrouillée à
    leur démarrage) — ils lisent le réglage, semé par le GUI."""
    if _HAS_CONFIG:
        v = appconfig.account_name()
        if v:
            return v
    import re
    base = os.path.basename(config_path or "")
    m = re.match(r"mappings-(.+)\.json$", base)
    if m:
        return m.group(1)
    return os.environ.get("USER") or os.environ.get("LOGNAME") or "user"

# Le moteur (même dossier que ce script, par convention du projet) — dérivé de
# __file__ et non recopié en dur : installer le dossier ailleurs fonctionne.
ENGINE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proton_sync.py")

DEFAULT_CYCLE_SECONDS = 30      # rythme de sondage du démon
DEFAULT_DEBOUNCE_SECONDS = 30   # délai de calme avant de traiter un dossier
# Un dossier « froid » (cache pas encore bâti par un passage complet) est mis de
# côté : on garde ses marqueurs mais on ne relance le moteur qu'au plus une fois
# toutes les COLD_RECHECK_SECONDS, au cas où une planification l'aurait consolidé
# entre-temps. Évite des centaines de sondes d'auth pendant l'attente.
COLD_RECHECK_SECONDS = 1800     # 30 min


# ─────────────────────────────────────────────────────────────────────────
#  Configuration (délai réglable, écrit par le GUI en couche 5)
# ─────────────────────────────────────────────────────────────────────────
def load_config():
    """Lit la config du démon. Retourne un dict avec au moins 'debounce_seconds'
    et 'cycle_seconds'. Tolérant : si le fichier manque ou est invalide, on
    retombe sur les valeurs par défaut (le démon n'échoue jamais là-dessus)."""
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
                    cfg[k] = float(v)
    except (OSError, ValueError):
        pass  # fichier absent ou invalide : valeurs par défaut
    return cfg


# ─────────────────────────────────────────────────────────────────────────
#  Lecture des files de marqueurs
# ─────────────────────────────────────────────────────────────────────────
def read_markers(queue_dirs):
    """Lit tous les marqueurs présents dans les dossiers de file donnés.

    Un marqueur est un fichier dont le CONTENU est le chemin du dossier touché
    (une ligne). On retourne une liste de tuples (marker_file_path, target_dir).
    Les marqueurs illisibles ou vides sont signalés pour suppression (target_dir
    = None) afin que l'appelant les nettoie.
    """
    out = []
    for qdir in queue_dirs:
        try:
            names = os.listdir(qdir)
        except OSError:
            continue  # file absente (ex. NAS non monté) : on saute, sans échouer
        for name in names:
            mpath = os.path.join(qdir, name)
            if not os.path.isfile(mpath):
                continue
            try:
                with open(mpath, "r", encoding="utf-8") as f:
                    content = f.read().strip()
            except OSError:
                out.append((mpath, None, False))  # illisible -> à nettoyer
                continue
            target, want_delete = _parse_marker(content)
            out.append((mpath, target, want_delete))
    return out


def _parse_marker(content):
    """Décode le contenu d'un marqueur. Format attendu : un petit JSON
    {"path": "...", "delete": true/false}. Tolérant : si le contenu est juste un
    chemin brut (texte sur une ligne), on l'interprète comme un ajout
    (delete=False) — robustesse et rétrocompatibilité. Retourne (path, delete).
    Retourne (None, False) si rien d'exploitable."""
    if not content:
        return None, False
    # Essai JSON d'abord.
    try:
        data = json.loads(content)
        if isinstance(data, dict):
            path = data.get("path")
            want_delete = bool(data.get("delete", False))
            return (path or None), want_delete
    except ValueError:
        pass
    # Repli : contenu = chemin brut sur la première ligne -> ajout.
    first_line = content.splitlines()[0].strip() if content else ""
    return (first_line or None), False


# ─────────────────────────────────────────────────────────────────────────
#  Correspondance chemin -> mapping (la vérité reste côté mappings)
# ─────────────────────────────────────────────────────────────────────────
def find_mapping_for_path(path, mappings):
    """Trouve le mapping (type 'folder') dont la 'source' est un préfixe du
    chemin donné. Retourne le mapping, ou None si aucun ne correspond.

    On choisit le préfixe le PLUS LONG si plusieurs sources sont préfixes (cas
    de mappings imbriqués), pour attribuer au mapping le plus spécifique.
    """
    norm = os.path.normpath(path)
    best = None
    best_len = -1
    for m in mappings:
        if m.get("type") != "folder":
            continue
        src = os.path.normpath(m["source"])
        if norm == src or (norm + "/").startswith(src.rstrip("/") + "/"):
            if len(src) > best_len:
                best = m
                best_len = len(src)
    return best


# ─────────────────────────────────────────────────────────────────────────
#  État de debounce (daté par première observation, horloge locale)
# ─────────────────────────────────────────────────────────────────────────
class DebounceState:
    """Suit, pour chaque dossier touché, l'instant de PREMIÈRE observation et le
    nombre de marqueurs en attente. Un dossier est « mûr » quand il est calme
    (aucun nouveau marqueur) depuis `debounce_seconds`.

    On date à la première observation avec l'horloge locale -> robuste aux
    décalages d'horloge entre NAS et machine locale.
    """

    def __init__(self):
        # dossier -> {"first_seen": ts, "last_seen": ts, "markers": set(paths),
        #             "want_delete": bool}
        self.pending = {}
        # dossier « froid » -> horodatage monotone de la dernière constatation.
        # Un dossier froid (cache pas encore bâti par un passage complet) est mis
        # de côté : on CONSERVE ses marqueurs mais on ne relance pas le moteur à
        # chaque cycle (ça multiplierait les sondes d'auth inutiles pendant des
        # heures). On ne réessaie qu'après COLD_RECHECK_SECONDS, au cas où une
        # planification aurait consolidé le dossier entre-temps.
        self.cold = {}

    def observe(self, target_dir, marker_path, now, want_delete=False):
        """Enregistre un marqueur. IMPORTANT : ne met à jour last_seen que si le
        marqueur est NOUVEAU (jamais vu). Sinon, relire les mêmes marqueurs sur
        disque à chaque cycle repousserait le debounce à l'infini et le dossier
        ne mûrirait jamais. Retourne True si c'était un nouveau marqueur.

        Règle de FUSION pour want_delete : si AU MOINS UN marqueur du dossier
        porte delete=True, le passage se fera avec propagation des suppressions
        (un passage 'avec delete' fait aussi les ajouts, donc il englobe tout)."""
        e = self.pending.get(target_dir)
        if e is None:
            self.pending[target_dir] = {
                "first_seen": now, "last_seen": now,
                "markers": {marker_path},
                "want_delete": bool(want_delete),
            }
            return True
        # want_delete est "collant" : une fois vrai, il le reste (fusion).
        if want_delete:
            e["want_delete"] = True
        if marker_path in e["markers"]:
            return False  # déjà connu : ne PAS repousser last_seen
        e["last_seen"] = now
        e["markers"].add(marker_path)
        return True

    def mature(self, debounce_seconds, now):
        """Retourne la liste des dossiers mûrs (calmes depuis debounce_seconds)."""
        ready = []
        for target_dir, e in self.pending.items():
            if now - e["last_seen"] >= debounce_seconds:
                ready.append(target_dir)
        return ready

    def markers_for(self, target_dir):
        e = self.pending.get(target_dir)
        return set(e["markers"]) if e else set()

    def want_delete_for(self, target_dir):
        e = self.pending.get(target_dir)
        return bool(e["want_delete"]) if e else False

    def clear(self, target_dir):
        self.pending.pop(target_dir, None)

    def is_cold_recent(self, target_dir, now, recheck_seconds):
        """True si ce dossier a été constaté froid il y a MOINS de
        recheck_seconds -> on le laisse de côté sans relancer le moteur."""
        ts = self.cold.get(target_dir)
        return ts is not None and (now - ts) < recheck_seconds

    def mark_cold(self, target_dir, now):
        """Marque le dossier froid. Retourne True si c'est une NOUVELLE
        constatation (pour ne journaliser qu'une fois, pas à chaque re-vérif)."""
        was_known = target_dir in self.cold
        self.cold[target_dir] = now
        return not was_known

    def clear_cold(self, target_dir):
        self.cold.pop(target_dir, None)


# ─────────────────────────────────────────────────────────────────────────
#  Lancement du moteur sur un sous-chemin
# ─────────────────────────────────────────────────────────────────────────
def run_engine_subpath(mapping, target_dir, config_path, dry_run=False,
                       want_delete=False, runner=None):
    """Lance le moteur en mode --subpath sur le dossier donné. `runner` permet
    d'injecter un faux lanceur pour les tests. Retourne (code, sortie)."""
    cmd = [sys.executable, ENGINE, config_path,
           "--subpath", target_dir,
           "--mapping-source", mapping["source"]]
    if want_delete:
        cmd.append("--delete")
    if dry_run:
        cmd.append("--dry-run")
    if runner is not None:
        return runner(cmd)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return 1, str(e)


def keyring_ready(config_path, runner=None):
    """True si le trousseau est déverrouillé (le CLI peut s'authentifier).

    Réutilise EXACTEMENT le préflight du moteur via `proton_sync.py --check-auth`
    (code 0 = OK, code 2 = verrouillé) — aucune logique d'authentification n'est
    dupliquée ici, donc pas de risque de divergence avec le moteur. La sonde ne
    prend pas le verrou et ne synchronise rien.

    On ne l'appelle QUE lorsqu'il y a du travail (un dossier mûr) : au repos, on
    ne sonde pas, pour ne pas générer d'appel CLI inutile à chaque cycle.
    """
    cmd = [sys.executable, ENGINE, config_path, "--check-auth"]
    if runner is not None:
        return runner(cmd) == 0
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
        return r.returncode == 0
    except Exception:
        return False


def lock_free(config_path, runner=None):
    """True si le verrou est libre (aucun autre passage en cours).

    Réutilise EXACTEMENT le même flock que le moteur via `proton_sync.py
    --check-lock` (code 0 = libre, code 1 = tenu) — aucune logique de verrou n'est
    dupliquée ici. La sonde relâche le verrou aussitôt et ne synchronise rien.

    Comme keyring_ready, on ne l'appelle QUE lorsqu'il y a du travail, pour éviter
    de lancer un sous-processus inutile à chaque cycle au repos.
    """
    cmd = [sys.executable, ENGINE, config_path, "--check-lock"]
    if runner is not None:
        return runner(cmd) == 0
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
        return r.returncode == 0
    except Exception:
        # En cas d'échec de la sonde elle-même, on considère le verrou occupé
        # (prudence : ne pas foncer et empiler des échecs).
        return False


def process_ready(state, target_dir, mappings, config_path, log, runner=None):
    """Traite UN dossier mûr : trouve son mapping, lance le moteur, nettoie les
    marqueurs. Robuste : dossier disparu / hors mapping -> marqueurs nettoyés et
    on passe. Retourne True si une synchro a été lancée, False sinon."""
    markers = state.markers_for(target_dir)
    want_delete = state.want_delete_for(target_dir)

    # Dossier disparu entre-temps ? On nettoie et on ignore.
    # NOTE : avec l'option 1 (le producteur remonte au PARENT pour les
    # suppressions), un dossier 'disparu' ici ne devrait normalement concerner
    # que des ajouts dont le dossier a été supprimé juste après — cas rare. La
    # suppression du dossier lui-même est signalée via un marqueur sur son parent
    # (qui, lui, existe encore), donc elle sera bien propagée par CE parent.
    if not os.path.isdir(target_dir):
        log(_("  ⊘ folder vanished, skipped: {p}").format(p=target_dir))
        _cleanup(markers)
        state.clear(target_dir)
        return False

    mapping = find_mapping_for_path(target_dir, mappings)
    if mapping is None:
        log(_("  ⊘ no mapping for: {p} (marker ignored)").format(p=target_dir))
        _cleanup(markers)
        state.clear(target_dir)
        return False

    flag = _(" [with deletions]") if want_delete else ""
    log(_("  → sync: {p}{f}").format(p=target_dir, f=flag))
    code, output = run_engine_subpath(mapping, target_dir, config_path,
                                      want_delete=want_delete, runner=runner)
    if code == 0:
        # Succès : on nettoie les marqueurs traités.
        _cleanup(markers)
        state.clear(target_dir)
        state.clear_cold(target_dir)   # au cas où il était froid : il est chaud maintenant
        # Le moteur a-t-il SAUTÉ ce sous-chemin parce que son nom est filtré ?
        # On relaie simplement son signal (chaîne « sous-chemin exclu » émise par
        # le garde-fou de proton_sync.py) plutôt que de rejuger l'exclusion ici —
        # une seule source de vérité (le moteur décide, le consommateur affiche).
        # Ainsi l'utilisateur voit noir sur blanc que son filtre agit, au lieu
        # d'un « ✓ ok » ambigu qui laisse croire à une synchro.
        if "[subpath-excluded]" in output or "sous-chemin exclu" in output:
            log(_("    🚫 excluded (name filtered) — nothing to sync"))
        else:
            log(_("    ✓ ok"))
        return True
    elif code == 3:
        # Dossier FROID : cache pas encore bâti par un passage complet. Le moteur
        # n'a rien fait et a délégué à la planification. On CONSERVE les marqueurs
        # (ils seront traités dès qu'un passage complet aura consolidé le dossier)
        # mais on retire l'entrée de debounce et on met le dossier « de côté » pour
        # ne pas relancer le moteur à chaque cycle (re-vérif au plus toutes les
        # COLD_RECHECK_SECONDS). On journalise la nature FROIDE à CHAQUE re-vérif
        # (et pas seulement la première) : sinon les re-vérifications ne montrent
        # que le « → sync » optimiste (écrit avant le lancement), ce qui laisse
        # croire à tort à une synchro sur un mapping non amorcé. `mark_cold` remet
        # le compteur de re-vérif à zéro ; on ignore désormais sa valeur de retour.
        state.clear(target_dir)
        state.mark_cold(target_dir, time.monotonic())
        log(_("    ⏳ cold folder — deferred to the scheduled pass "
              "(real-time does not build the cache)"))
        return False
    elif code == 4:
        # COMPTE Proton changé : le cache appartient à l'ancien compte — le
        # moteur REFUSE (rien touché, voir [account-changed]). Marqueurs
        # CONSERVÉS ; les relances sont espacées par le mécanisme « froid »
        # (pas de spam), et le cycle remonte l'état « account » (une seule
        # ligne d'attente au journal, icône d'attention dans la barre des
        # tâches). Résolution : Amorcer/Réinitialiser depuis le GUI.
        state.clear(target_dir)
        state.mark_cold(target_dir, time.monotonic())
        state.account_flag = True
        log(_("    ⛔ account changed — engine refused (cache belongs to the "
              "previous account); markers kept"))
        return False
    else:
        # Échec : on NE nettoie PAS les marqueurs (ils seront retentés au
        # prochain cycle). On retire juste l'entrée de debounce pour réobserver.
        state.clear(target_dir)
        log(_("    ✗ failure (code {c}) — markers kept for retry").format(c=code))
        if output:
            for line in output.strip().splitlines()[:3]:
                log(f"      {line}")
        return False


def _cleanup(marker_paths):
    for p in marker_paths:
        try:
            os.remove(p)
        except OSError:
            pass


# ─────────────────────────────────────────────────────────────────────────
#  Boucle principale du démon
# ─────────────────────────────────────────────────────────────────────────
def _default_log(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def run_once(state, queue_dirs, mappings, config_path, debounce_seconds, now,
             log, runner=None, auth_check=None, lock_check=None):
    """Un cycle : lire les marqueurs, mettre à jour le debounce, traiter les
    dossiers mûrs (séquentiellement). Facteur testable de la boucle.

    Retourne un statut : "idle" (rien à faire), "locked" (dossiers mûrs mais
    trousseau verrouillé), "busy" (dossiers mûrs mais le verrou est tenu par un
    autre passage), "account" (le moteur refuse : compte Proton changé — voir
    [account-changed]), ou "done" (au moins un dossier traité).

    `auth_check` et `lock_check` (optionnels) sont appelés UNE SEULE FOIS par
    cycle, et seulement s'il y a des dossiers mûrs :
      • auth_check() False -> "locked" (on ne lance rien qui échouerait en code 2) ;
      • lock_check() False -> "busy"   (on ne lance rien qui échouerait en code 1).
    Dans les deux cas, les marqueurs restent en file et les entrées de debounce
    restent mûres -> tout sera traité tel quel dès que la voie sera libre.
    """
    markers = read_markers(queue_dirs)
    for mpath, target, want_delete in markers:
        if target is None:
            _cleanup([mpath])  # marqueur illisible/vide : on nettoie
            continue
        state.observe(os.path.normpath(target), mpath, now, want_delete=want_delete)

    ready = state.mature(debounce_seconds, now)
    if not ready:
        return "idle"

    # Il y a du travail : on vérifie l'authentification puis le verrou, une fois
    # pour tout le cycle. Bloqué -> on ne traite rien (marqueurs et debounce
    # conservés, donc aucun événement perdu).
    if auth_check is not None and not auth_check():
        return "locked"
    if lock_check is not None and not lock_check():
        return "busy"

    state.account_flag = False
    processed_any = False
    for target_dir in ready:
        # Dossier froid constaté récemment : on garde ses marqueurs mais on ne
        # relance pas le moteur (il ressortirait froid) avant la prochaine
        # re-vérification. La planification le consolidera ; ensuite il passera.
        if state.is_cold_recent(target_dir, now, COLD_RECHECK_SECONDS):
            continue
        if process_ready(state, target_dir, mappings, config_path, log,
                         runner=runner):
            processed_any = True
    # « account » ne l'emporte que si RIEN n'a abouti ce cycle (si le moteur a
    # refusé pour changement de compte, c'est de toute façon le cas pour tous).
    if not processed_any and getattr(state, "account_flag", False):
        return "account"
    return "done"


def _count_ready_mappings(config_path):
    """(prêts, total) mappings de type 'folder' dont la racine est subtree_complete
    dans le cache. Un mapping n'est utilisable en temps réel que si tout son arbre
    a été analysé par un passage complet (la complétude de la racine le garantit).
    Tolérant aux erreurs (retourne (0, N) ou (0, 0) plutôt que d'échouer)."""
    try:
        with open(config_path, "r", encoding="utf-8") as f:
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
    # Cache au NOUVEL emplacement unifié (config.py) — sinon, après migration,
    # ce compte lirait l'ancien dossier vide et afficherait toujours 0.
    cache_dir = appconfig.CACHE_DIR if _HAS_CONFIG else os.path.expanduser("~/.proton_sync_cache")
    name = os.path.basename(config_path).replace(".json", "") + ".cache"
    try:
        with open(os.path.join(cache_dir, name), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return (0, total)
    if not isinstance(data, dict):
        return (0, total)
    ready = sum(1 for src in sources
                if isinstance(data.get(src), dict)
                and data[src].get("subtree_complete", False))
    return (ready, total)


def _write_status(auth_ok, cycle_seconds, mappings_path=None):
    """Battement de cœur pour l'icône de barre des tâches (tray_indicator.py) :
    horodatage + état de session + fichier de mappings ACTIF (écriture atomique).
    L'indicateur en déduit trois états : fichier frais + auth_ok -> connecté ;
    frais + not auth_ok -> session expirée ; vieux/absent -> démons arrêtés. Le
    chemin des mappings permet au clic gauche d'ouvrir l'éditeur sur le bon fichier.
    NB : l'état de session reflète ce que le consommateur SAIT — la sonde d'auth
    n'a lieu que lorsqu'il y a du travail (pour éviter la contention de trousseau) ;
    au repos, le dernier état connu persiste."""
    path = appconfig.STATUS_FILE if _HAS_CONFIG else os.path.join(BASE_DIR, "status.json")
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "auth_ok": bool(auth_ok),
                       "cycle_seconds": int(cycle_seconds),
                       "mappings_path": (os.path.abspath(mappings_path)
                                         if mappings_path else None)}, f)
        os.replace(tmp, path)
    except OSError:
        pass   # informatif seulement : ne doit jamais gêner le cycle


class _Heartbeat:
    """Bat le status.json à intervalle RÉGULIER depuis un thread dédié, quoi que
    fasse la boucle principale.

    Sans ça, le battement n'était réécrit qu'ENTRE les cycles (après run_once) :
    un passage long — p. ex. des centaines de sous-dossiers .git/objects à
    synchroniser d'affilée — pouvait bloquer run_once plusieurs minutes, laissant
    le battement vieillir au-delà du seuil « arrêté » du systray. L'icône passait
    alors au gris « démons arrêtés » alors que le démon était simplement TRÈS
    occupé. Le thread écrit le battement toutes les ~`interval` secondes en
    utilisant le dernier état publié par la boucle (auth_ok, cycle, mappings) ;
    l'affichage reste donc juste même pendant un très long passage."""

    def __init__(self, cycle_seconds, mappings_path, interval=20):
        self._lock = threading.Lock()
        self._auth_ok = True
        self._cycle = int(cycle_seconds)
        self._mappings_path = mappings_path
        self._interval = max(5, int(interval))
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        _write_status(self._auth_ok, self._cycle, self._mappings_path)  # battement immédiat
        self._thread.start()

    def update(self, auth_ok=None, cycle_seconds=None):
        """Publie le dernier état connu (appelé par la boucle à chaque cycle).
        Le thread s'en sert pour ses écritures régulières."""
        with self._lock:
            if auth_ok is not None:
                self._auth_ok = bool(auth_ok)
            if cycle_seconds is not None:
                self._cycle = int(cycle_seconds)

    def beat_now(self):
        """Écrit le battement immédiatement avec l'état courant (utile juste
        après un cycle pour ne pas attendre l'intervalle du thread)."""
        with self._lock:
            _write_status(self._auth_ok, self._cycle, self._mappings_path)

    def _run(self):
        # Réveil fin (1 s) pour pouvoir s'arrêter vite, mais on n'écrit qu'aux
        # multiples de l'intervalle — l'écriture est atomique et négligeable.
        elapsed = 0
        while not self._stop.wait(1):
            elapsed += 1
            if elapsed >= self._interval:
                elapsed = 0
                with self._lock:
                    _write_status(self._auth_ok, self._cycle, self._mappings_path)

    def stop(self):
        self._stop.set()


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Démon consommateur temps réel (synchro Proton Drive)")
    parser.add_argument("config", help="Fichier JSON de mappings de cet utilisateur")
    parser.add_argument("--once", action="store_true",
                        help="Fait un seul cycle puis sort (utile pour test/debug)")
    args = parser.parse_args()

    user = _user_from_config(args.config)
    os.makedirs(LOCAL_QUEUE, exist_ok=True)
    # Mode local seul (nas_enabled=False) : on ne sonde même pas la file NAS —
    # coupure nette plutôt qu'une tentative-puis-échec à chaque cycle.
    nas_on = (appconfig.nas_enabled() if _HAS_CONFIG else True)
    queue_dirs = [LOCAL_QUEUE] + ([_nas_queue_for(user)] if nas_on else [])

    log = _default_log
    state = DebounceState()

    log(_("Real-time daemon started (user {u}).").format(u=user))
    log(_("  Watched queues: {q}").format(q=queue_dirs))
    log(_("  Engine: {p}").format(p=ENGINE))

    # Compte informatif : combien de mappings sont prêts pour le temps réel
    # (arbre entièrement analysé). Les autres nécessitent un passage complet
    # (planification/manuel) avant que le temps réel puisse les synchroniser —
    # d'ici là, leurs changements sont pris en charge par la planification.
    _ready, _total = _count_ready_mappings(args.config)
    if _total and _ready < _total:
        log(_("  • {r}/{t} mapping(s) ready for real-time. The others need a full "
              "pass (Schedule) before real-time can sync them.").format(r=_ready, t=_total))
    elif _total:
        log(_("  • {r}/{t} mapping(s) ready for real-time.").format(r=_ready, t=_total))

    waiting_reason = None  # None / "locked" (trousseau) / "busy" (verrou tenu)
    # Thread de battement dédié : garde le status.json frais MÊME pendant un
    # passage long (run_once peut durer plusieurs minutes sur beaucoup de
    # sous-dossiers). Sans lui, le systray passait à tort au gris « arrêté ».
    hb = _Heartbeat(cycle_seconds=load_config()["cycle_seconds"],
                    mappings_path=args.config)
    hb.start()
    while True:
        cfg = load_config()  # relu à chaque cycle -> délai modifiable à chaud
        # Charger les mappings frais à chaque cycle (la vérité reste à jour).
        try:
            with open(args.config, "r", encoding="utf-8") as f:
                raw = json.load(f)
            mappings = raw["mappings"] if isinstance(raw, dict) and "mappings" in raw else raw
            if not isinstance(mappings, list):
                mappings = []
        except (OSError, ValueError, KeyError):
            mappings = []

        status = run_once(state, queue_dirs, mappings, args.config,
                          cfg["debounce_seconds"], time.monotonic(), log,
                          auth_check=lambda: keyring_ready(args.config),
                          lock_check=lambda: lock_free(args.config))

        # Trois motifs d'attente possibles, chacun signalé par UNE SEULE ligne
        # tant qu'il dure (au lieu d'un échec par source et par cycle) :
        # trousseau verrouillé ("locked"), verrou tenu par un autre passage
        # ("busy"), ou compte Proton changé ("account", moteur en refus —
        # résolution par Amorcer/Réinitialiser). "idle" ne change rien (on n'a
        # pas sondé). "done" après une attente = reprise. On ne re-signale que
        # si le motif change.
        if status in ("locked", "busy", "account"):
            if waiting_reason != status:
                if status == "locked":
                    log(_("⏳ Proton authentication unavailable (expired session "
                          "or locked keyring) — markers kept, no pass launched. "
                          "Run “proton-drive auth login” (or the “Sign in to "
                          "Proton” button) to renew the session."))
                elif status == "account":
                    log(_("⛔ Proton account changed — the cache belongs to the "
                          "previous account. Markers kept; prime the cache (or "
                          "reset the mappings) from the GUI to resume."))
                else:
                    log(_("⏳ Waiting: another pass holds the lock "
                          "(manual or scheduled run) — markers kept, "
                          "no pass launched."))
                waiting_reason = status
        elif status == "done":
            if waiting_reason == "locked":
                log(_("🔓 Session opened — resuming processing."))
            elif waiting_reason == "busy":
                log(_("🔓 Lock released — resuming processing."))
            elif waiting_reason == "account":
                log(_("🔓 Account matter resolved — resuming processing."))
            waiting_reason = None

        # Battement de cœur : publier l'état courant au thread dédié (qui écrit
        # à intervalle régulier, y compris pendant un long run_once) et forcer un
        # battement immédiat maintenant que le cycle est terminé.
        hb.update(auth_ok=(waiting_reason not in ("locked", "account")),
                  cycle_seconds=cfg["cycle_seconds"])
        hb.beat_now()

        if args.once:
            hb.stop()
            break
        time.sleep(cfg["cycle_seconds"])


if __name__ == "__main__":
    main()

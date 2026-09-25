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
__version__ = "1.6.0"   # version propre à CE fichier ; incrémentée quand il change (indépendant de GitHub)

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

# nas_reachable() : sonde NAS NON BLOQUANTE (TCP port 2049 via /proc/mounts,
# chantier C) — réutilisée telle quelle pour éviter toute divergence de logique.
# Import guardé : sans realtime_manager, on considère le NAS joignable (repli
# historique, comportement inchangé pour qui n'a pas le module).
try:
    from realtime_manager import nas_reachable as _nas_reachable
    _HAS_NAS_PROBE = True
except ImportError:
    _HAS_NAS_PROBE = False

    def _nas_reachable():
        return True

# nas_scripts_stale() : détection LECTURE SEULE d'un déploiement de scripts NAS
# en attente (écart de contenu poste↔disque NAS). Import guardé : sans le module,
# on considère qu'il n'y a pas d'écart (repli neutre, aucune fausse alerte).
try:
    from realtime_manager import nas_scripts_stale as _nas_scripts_stale
    _HAS_SCRIPTS_PROBE = True
except ImportError:
    _HAS_SCRIPTS_PROBE = False

    def _nas_scripts_stale():
        return False

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

# Ancienneté au-delà de laquelle une RACINE de mapping restée froide devient un
# avertissement visible (barre des tâches). Une racine froide veut dire qu'aucun
# passage complet ne l'a jamais analysée : le temps réel ne la couvre pas.
#
# POURQUOI attendre, et pourquoi deux heures. Une racine peut être froide pour
# une raison parfaitement normale — un mapping qu'on vient d'ajouter et dont le
# passage planifié n'a pas encore eu lieu. Avertir tout de suite reviendrait à
# crier au loup. Deux heures laissent passer sans bruit le cas courant (un gros
# envoi en cours peut occuper le moteur longtemps) tout en restant très en deçà
# du délai d'un passage planifié hebdomadaire.
#
# Ne s'applique QU'aux racines. Un dossier illisible a une cause certaine et se
# signale immédiatement ; un sous-dossier froid ordinaire se débloque tout seul
# dès que son parent est parcouru et ne se signale pas du tout.
COLD_ALERT_SECONDS = 7200       # 2 h


# ─────────────────────────────────────────────────────────────────────────
#  Dossiers illisibles — lecture de l'état publié par le moteur
# ─────────────────────────────────────────────────────────────────────────
HEALTH_FILE = (appconfig.HEALTH_FILE if _HAS_CONFIG
               else os.path.expanduser("~/.proton-drive-sync/health.json"))

_HEALTH_CACHE = {"mtime": None, "paths": frozenset()}


def health_unreadable():
    """Dossiers illisibles publiés par le dernier passage COMPLET du moteur.

    Relu seulement quand le fichier a changé (comparaison de mtime) : cette
    fonction est appelée à chaque cycle du démon, il ne faut pas qu'elle relise
    un fichier inchangé toutes les 30 secondes.

    Tolérant par construction : fichier absent, tronqué ou d'un format futur ->
    ensemble vide. Un démon de sauvegarde ne s'arrête jamais sur un fichier
    d'observation."""
    try:
        mtime = os.path.getmtime(HEALTH_FILE)
    except OSError:
        _HEALTH_CACHE["mtime"] = None
        _HEALTH_CACHE["paths"] = frozenset()
        return _HEALTH_CACHE["paths"]
    if mtime == _HEALTH_CACHE["mtime"]:
        return _HEALTH_CACHE["paths"]
    paths = set()
    try:
        with open(HEALTH_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for entry in (data.get("mappings") or {}).values():
            for p in (entry.get("unreadable") or []):
                if isinstance(p, str):
                    paths.add(p)
    except (OSError, ValueError, AttributeError):
        paths = set()
    _HEALTH_CACHE["mtime"] = mtime
    _HEALTH_CACHE["paths"] = frozenset(paths)
    return _HEALTH_CACHE["paths"]


def parse_unreadable(output):
    """Extrait les chemins des lignes « [unreadable] » émises par le moteur.

    Le moteur écrit l'étiquette et le chemin SEULS sur leur ligne, la raison
    traduite venant sur la ligne suivante : tout ce qui suit l'étiquette est
    donc le chemin, quels que soient les espaces ou les deux-points qu'il
    contient, et quelle que soit la langue."""
    marque = "[unreadable] "
    trouves = []
    for ligne in (output or "").splitlines():
        i = ligne.find(marque)
        if i >= 0:
            chemin = ligne[i + len(marque):].strip()
            if chemin and chemin not in trouves:
                trouves.append(chemin)
    return trouves


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
def read_markers(queue_dirs, selftest_log=None):
    """Lit tous les marqueurs présents dans les dossiers de file donnés.

    Un marqueur est un fichier dont le CONTENU est le chemin du dossier touché
    (une ligne). On retourne une liste de tuples (marker_file_path, target_dir,
    want_delete). Les marqueurs illisibles ou vides sont signalés pour suppression
    (target_dir = None) afin que l'appelant les nettoie.

    ÉTANCHÉITÉ DU SELF-TEST : un marqueur de test de correspondance NAS
    ({"selftest": true, ...}) est INTERCEPTÉ ICI, au point d'entrée unique, AVANT
    toute logique de synchro. Il est traité (réponse écrite dans son canal de
    retour) puis supprimé, et n'est JAMAIS ajouté à la liste des marqueurs
    normaux — le moteur ne le voit donc jamais. C'est le garde-fou central : un
    marqueur de test ne peut pas déclencher de synchro/suppression.
    """
    out = []
    for qdir in queue_dirs:
        try:
            names = os.listdir(qdir)
        except OSError:
            continue  # file absente (ex. NAS non monté) : on saute, sans échouer
        for name in names:
            # Ignorer les fichiers temporaires d'écriture atomique : le watcher
            # (local ET NAS) écrit « <nom>.tmp » puis os.replace vers le nom
            # final — l'os.replace n'est pas instantané via NFS. Lu à mi-écriture,
            # un « .tmp » donnait un JSON partiel → repli « chemin brut » → cible
            # fantaisiste → nettoyage : le consommateur pouvait SUPPRIMER le .tmp
            # avant le os.replace du watcher, qui échouait alors et perdait le
            # marqueur jusqu'au filet hebdomadaire. (_count_markers, côté GUI,
            # excluait déjà les .tmp ; read_markers ne le faisait pas.)
            if name.endswith(".tmp"):
                continue
            mpath = os.path.join(qdir, name)
            if not os.path.isfile(mpath):
                continue
            try:
                with open(mpath, "r", encoding="utf-8") as f:
                    content = f.read().strip()
            except OSError:
                out.append((mpath, None, False))  # illisible -> à nettoyer
                continue
            # ---- Interception self-test AVANT toute interprétation de 'path' ----
            if _is_selftest_marker(content):
                _handle_selftest_marker(mpath, content, log=selftest_log)
                continue   # jamais ajouté à la liste normale (étanche)
            target, want_delete = _parse_marker(content)
            out.append((mpath, target, want_delete))
    return out


def _is_selftest_marker(content):
    """True si le contenu est un marqueur de self-test ({"selftest": true}).
    Vérifié AVANT toute exploitation d'un éventuel champ 'path' — la détection
    doit primer pour garantir l'étanchéité vis-à-vis du moteur."""
    if not content:
        return False
    try:
        data = json.loads(content)
    except ValueError:
        return False
    return isinstance(data, dict) and data.get("selftest") is True


def _handle_selftest_marker(marker_path, content, log=None):
    """Traite un marqueur de self-test SANS jamais toucher au moteur : écrit la
    confirmation dans le fichier de réponse indiqué (canal de retour desktop<->NAS)
    puis supprime le marqueur de test.

    Format attendu du marqueur : {"selftest": true, "id": "<uuid>",
    "reply": "<chemin absolu du fichier de réponse>"}. Le champ 'reply' pointe
    vers un fichier (sur le montage partagé) que le desktop surveille. On y écrit
    {"id": "<uuid>", "seen": true, "ts": <epoch>} de façon atomique.

    Ce marqueur NE CONTIENT PAS de champ 'path' exploitable par le moteur ; même
    s'il en contenait un, il n'est jamais transmis (intercepté en amont).
    """
    try:
        data = json.loads(content)
    except ValueError:
        data = {}
    test_id = data.get("id")
    reply_path = data.get("reply")
    # Écrire la réponse (le desktop l'attend pour conclure « inotify a capté »).
    if isinstance(reply_path, str) and reply_path:
        try:
            payload = {"id": test_id, "seen": True, "ts": time.time()}
            tmp = reply_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp, reply_path)
            if log:
                log(_("  🔎 self-test marker acknowledged (id {i}).").format(i=test_id))
        except OSError as e:
            if log:
                log(_("  ⚠ self-test reply write failed: {e}").format(e=e))
    # Nettoyer le marqueur de test (jamais laissé traîner).
    try:
        os.remove(marker_path)
    except OSError:
        pass


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
# Nombre de reports consécutifs avant de commencer à journaliser qu'un dossier
# ne se stabilise pas. Volontairement PAS un plafond : un dossier qui n'arrête
# pas de changer ne doit pas être synchronisé de force avec un fichier partiel.
# Il ne bloque personne (le consommateur passe au suivant) et le passage planifié
# le rattrape de toute façon.
STABILITY_LOG_AFTER = 3

# Lignes de la sortie du moteur recopiées au journal en cas d'échec. Les échecs
# d'authentification (code 2) ont droit à plus : leur message est plus long et
# sa ligne la plus utile (« CLI detail ») arrive en cinquième position.
ENGINE_OUTPUT_LINES = 3
ENGINE_OUTPUT_LINES_AUTH = 8


def _folder_signature(path):
    """Empreinte du CONTENU immédiat d'un dossier : {nom: (taille, mtime)}.

    NON récursive : un marqueur porte sur le dossier qui contient le fichier
    touché, donc ce seul niveau suffit. Un fichier en cours d'écriture dans un
    sous-dossier relève de son propre marqueur, donc de sa propre vérification.

    Retourne None si le dossier n'est pas lisible (NAS reparti, dossier
    supprimé). Un échec de sonde ne doit JAMAIS bloquer : l'appelant traite
    None comme « je ne sais pas » et laisse passer, comportement d'avant ce
    mécanisme.

    On retient taille ET date. Un agent de sauvegarde conserve souvent la date
    de la source : la pose de cette date définitive en fin de transfert est
    elle-même un changement détectable — un report de plus, puis la stabilité.
    """
    try:
        sig = {}
        for entry in os.scandir(path):
            try:
                if entry.is_file():
                    st = entry.stat()
                    sig[entry.name] = (st.st_size, st.st_mtime)
            except OSError:
                continue      # fichier disparu entre-temps : simplement absent
        return sig
    except OSError:
        return None


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
        # dossier froid -> horodatage monotone de la PREMIÈRE constatation,
        # JAMAIS rafraîchi tant que le dossier reste froid.
        #
        # POURQUOI un second dictionnaire. `self.cold` est réécrit à chaque
        # re-vérification (c'est un « vu froid pour la dernière fois », qui sert
        # à espacer les relances) : mesurer une ancienneté dessus donnerait
        # toujours moins de COLD_RECHECK_SECONDS, et le seuil ne mesurerait rien.
        # Les deux entrées naissent et disparaissent ensemble.
        self.cold_since = {}
        # Racines de mapping constatées froides parce qu'AUCUN passage complet
        # ne les a jamais analysées ([subpath-cold-root]). Distinct d'un
        # sous-dossier froid ordinaire, qui se débloque dès que son parent est
        # parcouru : une racine, elle, n'a pas de parent à attendre.
        self.cold_roots = set()
        # Dossiers dont la LECTURE a échoué pendant un passage temps réel
        # (étiquette [unreadable] du moteur). Cause nommée, donc publiable tout
        # de suite — sans attendre le prochain passage complet, qui peut être à
        # une semaine si la planification est hebdomadaire.
        self.unreadable = set()

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
            # PREMIER relevé de l'empreinte, pris ICI et non à maturité : les
            # deux mesures se retrouvent ainsi séparées par la fenêtre de
            # debounce, gratuitement. Prendre les deux à maturité coûterait un
            # cycle de latence sur TOUTES les synchros, y compris celles qui
            # n'en ont aucun besoin.
            self.pending[target_dir] = {
                "first_seen": now, "last_seen": now,
                "markers": {marker_path},
                "want_delete": bool(want_delete),
                "sig": _folder_signature(target_dir),
                "defers": 0,
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

    def is_settled(self, target_dir, now):
        """Le contenu du dossier a-t-il cessé de changer ?

        Compare l'empreinte relevée à l'apparition du dossier dans la file avec
        celle d'aujourd'hui. Identiques -> on peut lancer. Différentes -> le
        dossier est encore en train d'être écrit : on mémorise la NOUVELLE
        empreinte, on repousse `last_seen` (le dossier repart pour une fenêtre
        de debounce, ce qui espace naturellement la mesure suivante) et on rend
        False.

        POURQUOI. Un agent externe qui dépose d'abord un petit fichier puis un
        gros arme le debounce sur le petit : le moteur partait pendant que le
        gros s'écrivait encore, et téléversait une version tronquée. Mesuré en
        production : envoi lancé 12 minutes avant la fin réelle de l'écriture
        d'un fichier de 2 Gio, puis renvoi complet une fois le vrai contenu
        détecté — 11 minutes de transfert entièrement perdues.

        Augmenter le debounce ne corrige rien (la fenêtre d'écriture dépasse
        largement toute valeur raisonnable) et une quarantaine par âge non plus
        (l'agent conserve la date d'origine, le fichier paraît donc ancien alors
        qu'il grossit encore).

        LIMITE ASSUMÉE : une taille stable sur une fenêtre de debounce n'est pas
        une PREUVE que l'écriture est finie — un transfert qui bafouille plus
        longtemps que la fenêtre passerait pour terminé. C'est une heuristique,
        et elle reste très supérieure à l'état antérieur, qui partait sans
        aucune vérification. Le filet de `upload_batch` (relecture du distant et
        ré-essai des manquants) subsiste par-dessus.

        Empreinte illisible (NAS reparti, dossier supprimé) -> on ne bloque
        PAS : on rend True et la suite du traitement gère l'absence.
        """
        e = self.pending.get(target_dir)
        if e is None:
            return True
        now_sig = _folder_signature(target_dir)
        if now_sig is None or e.get("sig") is None:
            return True          # sonde impossible : on ne bloque jamais dessus
        if now_sig == e["sig"]:
            return True
        e["sig"] = now_sig
        e["last_seen"] = now     # repart pour une fenêtre de debounce
        e["defers"] = e.get("defers", 0) + 1
        return False

    def defers_for(self, target_dir):
        e = self.pending.get(target_dir)
        return e.get("defers", 0) if e else 0

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

    def mark_cold(self, target_dir, now, is_root=False):
        """Marque le dossier froid. Retourne True si c'est une NOUVELLE
        constatation (pour ne journaliser qu'une fois, pas à chaque re-vérif).

        `is_root` : le moteur a répondu [subpath-cold-root], c'est-à-dire que la
        CIBLE est la racine d'un mapping jamais analysé entièrement. Aucun
        parcours parent ne viendra la débloquer — seul un amorçage ou un passage
        planifié le peut. C'est la seule forme de froid dont on sache nommer la
        cause, donc la seule qu'on publiera."""
        was_known = target_dir in self.cold
        self.cold[target_dir] = now
        if not was_known:
            # Première constatation : c'est CE moment qu'on mesure ensuite.
            self.cold_since[target_dir] = now
        if is_root:
            self.cold_roots.add(target_dir)
        else:
            self.cold_roots.discard(target_dir)
        return not was_known

    def note_unreadable(self, paths):
        """Mémorise des dossiers signalés illisibles par le moteur."""
        self.unreadable.update(paths)

    def known_unreadable(self):
        """Tout ce qu'on sait d'illisible : ce que le temps réel a constaté, plus
        ce que le dernier passage COMPLET a publié.

        Les deux sources sont nécessaires. Le temps réel ne voit que les dossiers
        qui ont reçu un événement ; le passage complet voit tout le parc mais
        peut dater d'une semaine si la planification est hebdomadaire."""
        return self.unreadable | health_unreadable()

    def blocking_unreadable(self, parent_dir):
        """Un dossier illisible situé SOUS `parent_dir`, ou None.

        Tant qu'il en existe un, `parent_dir` ne peut pas être marqué complet —
        donc aucun de ses enfants ne sera repris en temps réel. C'est ce qui
        transforme un dossier cassé en gel de tout le voisinage."""
        if not parent_dir:
            return None
        prefixe = parent_dir.rstrip("/") + "/"
        for p in sorted(self.known_unreadable()):
            if p == parent_dir or p.startswith(prefixe):
                return p
        return None

    def clear_unreadable(self, target_dir):
        """Un passage a réussi sur ce dossier : ce qu'on croyait illisible en
        dessous ne l'est plus. On retire la cible et sa descendance.

        Ne jamais laisser un échec se figer : sans cet oubli, une permission
        rétablie laisserait l'alerte allumée jusqu'au redémarrage du démon."""
        prefixe = target_dir.rstrip("/") + "/"
        for d in [p for p in self.unreadable
                  if p == target_dir or p.startswith(prefixe)]:
            self.unreadable.discard(d)

    def alert_paths(self, now, cold_seconds):
        """Ce qu'il y a à signaler, avec une cause nommée pour chaque entrée.

        Retourne (illisibles, racines_froides). Rien d'autre n'est publié : un
        dossier froid ordinaire se débloque tout seul dès que son parent est
        parcouru, et une alerte dont on ne sait pas dire la cause ne dit à
        personne quoi faire — elle use l'avertissement pour rien.

        `cold_seconds` ne s'applique qu'aux racines : le temps d'attendre qu'un
        passage planifié fasse son travail avant de déranger l'utilisateur.
        Un dossier illisible, lui, a une cause certaine : on le signale tout de
        suite."""
        racines = sorted(d for d in self.cold_roots
                         if (now - self.cold_since.get(d, now)) >= cold_seconds)
        return sorted(self.known_unreadable()), racines

    def clear_cold(self, target_dir):
        """Oublie l'état froid de CE dossier, et de TOUTE sa descendance.

        POURQUOI la descendance. `sync_folder` est récursif : une synchro
        réussie sur un dossier a parcouru et mis en cache tout ce qui est en
        dessous. Les descendants marqués froids ne le sont donc PLUS à cet
        instant — l'information est devenue fausse, on la retire.

        Sans cela, ils restaient écartés par `is_cold_recent()` pendant
        COLD_RECHECK_SECONDS (30 min) alors que leur parent venait de les
        indexer. Constaté en production le 28 août : une archive décompressée
        pose un marqueur par dossier ; ceux traités AVANT la racine partent en
        « dossier froid », la racine réussit 5 minutes plus tard et indexe tout,
        mais ces quatre-là attendaient encore 25 minutes. Leur sort ne tenait
        qu'à l'ORDRE de lecture des marqueurs.

        Le délai de 30 min garde tout son sens pour le vrai cas froid : celui
        où aucun parcours parent n'a eu lieu.

        Si l'invalidation se trompe (le parcours du parent n'a pas tout indexé),
        le dossier repart en code 3 et se remarque froid : le coût maximal est
        un lancement de moteur inutile.
        """
        self.cold.pop(target_dir, None)
        self.cold_since.pop(target_dir, None)
        self.cold_roots.discard(target_dir)
        prefixe = target_dir.rstrip("/") + "/"
        for d in [k for k in self.cold if k.startswith(prefixe)]:
            self.cold.pop(d, None)
            self.cold_since.pop(d, None)
            self.cold_roots.discard(d)


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


# ─────────────────────────────────────────────────────────────────────────
#  Marqueurs « en vol » (inflight/)
# ─────────────────────────────────────────────────────────────────────────
INFLIGHT_DIRNAME = "inflight"


def _inflight_dir(queue_dir):
    return os.path.join(queue_dir, INFLIGHT_DIRNAME)


def _move_markers_inflight(marker_paths, log=None):
    """Déplace les marqueurs dans le sous-dossier `inflight/` de LEUR PROPRE file,
    juste avant de lancer le moteur. Retourne la liste des nouveaux chemins.

    POURQUOI. `write_marker` dédoublonne sur la présence d'un marqueur dans la
    file : tant que celui qu'on est en train de traiter y reste, un changement
    survenant PENDANT la synchro ne dépose aucun marqueur, et le nettoyage de fin
    efface ensuite celui qui l'avait fait écarter — le changement est oublié
    jusqu'au prochain passage planifié. En sortant les marqueurs de la file avant
    de lancer le moteur, il n'y a plus rien à dédoublonner : l'événement crée son
    propre marqueur, qui survit au nettoyage.

    Un même dossier peut porter des marqueurs venant de PLUSIEURS files (locale
    ET NAS) : chacun est donc déplacé dans le `inflight/` de sa file d'origine.

    `read_markers` ignore ce sous-dossier sans rien changer : il teste
    `os.path.isfile` sur chaque entrée, et un dossier n'est pas un fichier.

    Un déplacement qui échoue (NAS disparu entre la lecture et le déplacement)
    n'interrompt RIEN : on journalise et on garde le chemin d'origine. C'est
    exactement le comportement d'avant ce mécanisme, donc aucune régression — la
    synchro a bien lieu, seule la fenêtre de dédoublonnage reste ouverte.
    """
    moved = []
    for src in marker_paths:
        qdir = os.path.dirname(src)
        dst_dir = _inflight_dir(qdir)
        dst = os.path.join(dst_dir, os.path.basename(src))
        try:
            os.makedirs(dst_dir, exist_ok=True)
            os.replace(src, dst)
            moved.append(dst)
        except OSError as e:
            if log:
                log(_("    ⚠  could not set marker aside ({e}) — continuing")
                    .format(e=e))
            moved.append(src)
    return moved


def _restore_markers(marker_paths, log=None):
    """Ramène des marqueurs de `inflight/` vers leur file, pour qu'ils soient
    relus au prochain cycle.

    INDISPENSABLE : trois des quatre sorties de `process_ready` CONSERVENT les
    marqueurs (dossier froid, compte changé, échec) en comptant sur `read_markers`
    pour les retrouver dans la file. Les laisser dans `inflight/` les rendrait
    invisibles à jamais : le dossier froid ne serait jamais repris, l'échec jamais
    rejoué. On corrigerait un marqueur avalé de temps en temps en en perdant trois
    catégories entières.
    """
    for src in marker_paths:
        d = os.path.dirname(src)
        if os.path.basename(d) != INFLIGHT_DIRNAME:
            continue  # déjà dans la file (déplacement qui avait échoué)
        dst = os.path.join(os.path.dirname(d), os.path.basename(src))
        try:
            os.replace(src, dst)
        except OSError as e:
            if log:
                log(_("    ⚠  could not put marker back ({e}) — it will be "
                      "recovered at next startup").format(e=e))


def recover_inflight(queue_dirs, log=None):
    """Ramène dans leur file les marqueurs restés « en vol », au DÉMARRAGE du
    consommateur. Sert uniquement au cas où le processus est mort brutalement
    (arrêt machine, SIGKILL) entre le déplacement et la fin du traitement.

    On ramène plutôt qu'on ne supprime : un marqueur ramené à tort ne coûte
    qu'une synchro inutile, alors que l'oublier coûterait un changement perdu.
    Même principe que le `startup_catchup` du watcher NAS.
    """
    total = 0
    for qdir in queue_dirs:
        d = _inflight_dir(qdir)
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            try:
                os.replace(os.path.join(d, name), os.path.join(qdir, name))
                total += 1
            except OSError:
                pass
    if total and log:
        log(_("↩ {n} marker(s) recovered from an interrupted run.").format(n=total))
    return total


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
    # Les marqueurs sortent de la file AVANT le lancement : un changement
    # survenant pendant la synchro dépose alors son propre marqueur au lieu
    # d'être écarté par le dédoublonnage (cf. _move_markers_inflight).
    markers = _move_markers_inflight(markers, log=log)
    code, output = run_engine_subpath(mapping, target_dir, config_path,
                                      want_delete=want_delete, runner=runner)
    if code == 0:
        # Succès : on nettoie les marqueurs traités.
        _cleanup(markers)
        state.clear(target_dir)
        state.clear_cold(target_dir)   # au cas où il était froid : il est chaud maintenant
        # Le passage a réussi : ce qu'on croyait illisible sous cette cible ne
        # l'est plus. On oublie d'abord (ne jamais figer un échec résolu), puis
        # on reprend ce que CE passage vient de constater — un passage peut très
        # bien réussir globalement tout en butant sur un sous-dossier.
        state.clear_unreadable(target_dir)
        state.note_unreadable(parse_unreadable(output))
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
        _restore_markers(markers, log=log)
        state.clear(target_dir)
        est_racine = "[subpath-cold-root]" in output
        state.mark_cold(target_dir, time.monotonic(), is_root=est_racine)
        # Le message annonçait « différé au passage planifié » : c'était vrai
        # tant que la reprise attendait COLD_RECHECK_SECONDS (30 min), donc
        # souvent la nuit. Depuis que clear_cold() invalide la descendance, la
        # reprise a lieu au cycle SUIVANT l'indexation du parent — mesuré à
        # 2 min 39 s en production le 28 août, contre 30 min auparavant. Le
        # passage planifié n'est plus qu'un filet de dernier recours, quand
        # aucun parcours parent n'a lieu. Le message promettait donc pire que
        # ce qui se produit.
        # La racine d'un mapping n'a AUCUN parent qui sera parcouru : lui
        # promettre une reprise « dès que son parent sera indexé » annonce
        # quelque chose qui n'arrivera jamais (constaté le 1er septembre :
        # incus-exports répétait ce message chaque minute après un amorçage
        # échoué, alors que seul un nouveau passage complet pouvait le
        # débloquer). Le moteur distingue les deux cas par tag stable.
        #
        # Troisième cas, ajouté après l'incident de septembre : le parent EXISTE
        # et sera reparcouru, mais il ne pourra JAMAIS devenir complet parce
        # qu'un dossier sous lui est illisible. La complétude remonte de bas en
        # haut : un seul dossier qu'on ne peut pas lister suffit à ce que rien
        # au-dessus ne soit jamais marqué complet. Promettre ici « dès que son
        # parent sera indexé » envoie l'utilisateur attendre une reprise qui
        # n'arrivera pas — c'est ce qui a laissé passer 14 heures. Le dossier
        # fautif n'est en général PAS un ancêtre de la cible mais un VOISIN :
        # il suffit qu'il partage le même parent pour que ce parent ne soit
        # jamais complet. Quand on en connaît un, on le nomme, et on nomme le
        # geste qui débloque.
        if est_racine:
            log(_("    ⏳ this mapping has never been fully analysed — run "
                  "“Prime the cache”, or wait for the scheduled pass"))
        else:
            bloquant = state.blocking_unreadable(os.path.dirname(target_dir))
            if bloquant:
                log(_("    ⛔ cold folder — it will NOT be picked up on its own: "
                      "a neighbouring folder cannot be read, so their common "
                      "parent can never be marked complete. Fix the permissions "
                      "on: {p}").format(p=bloquant))
            else:
                log(_("    ⏳ cold folder — will be picked up as soon as its parent "
                      "folder is indexed"))
        return False
    elif code == 4:
        # COMPTE Proton changé : le cache appartient à l'ancien compte — le
        # moteur REFUSE (rien touché, voir [account-changed]). Marqueurs
        # CONSERVÉS ; les relances sont espacées par le mécanisme « froid »
        # (pas de spam), et le cycle remonte l'état « account » (une seule
        # ligne d'attente au journal, icône d'attention dans la barre des
        # tâches). Résolution : Amorcer/Réinitialiser depuis le GUI.
        _restore_markers(markers, log=log)
        state.clear(target_dir)
        state.mark_cold(target_dir, time.monotonic())
        state.account_flag = True
        log(_("    ⛔ account changed — engine refused (cache belongs to the "
              "previous account); markers kept"))
        return False
    else:
        # Échec : on NE nettoie PAS les marqueurs (ils seront retentés au
        # prochain cycle) — mais il faut les RAMENER dans la file, sinon
        # `read_markers` ne les verrait plus et le ré-essai n'aurait jamais lieu.
        _restore_markers(markers, log=log)
        state.clear(target_dir)
        log(_("    ✗ failure (code {c}) — markers kept for retry").format(c=code))
        if output:
            # Combien de lignes de la sortie moteur on recopie au journal.
            # Le message d'échec d'authentification (code 2) en fait SEPT, dont
            # la ligne « CLI detail : … » — la SEULE qui dise ce que le CLI a
            # réellement répondu, donc la seule qui permette de distinguer un
            # trousseau verrouillé d'une panne de service Proton. L'ancienne
            # limite de 3 la coupait systématiquement : on ne voyait que la
            # cause SUPPOSÉE, jamais la cause réelle (constaté en production le
            # 30 juillet pendant une panne Proton).
            limit = ENGINE_OUTPUT_LINES_AUTH if code == 2 else ENGINE_OUTPUT_LINES
            for line in output.strip().splitlines()[:limit]:
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
             log, runner=None, auth_check=None, lock_check=None,
             on_lock_acquired=None):
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
    markers = read_markers(queue_dirs, selftest_log=log)
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

    # Auth + verrou OK : on VA traiter. Signaler la reprise MAINTENANT (avant le
    # passage), pour que « verrou libéré » précède les lignes de synchro plutôt
    # que d'arriver après coup (le statut « done » ne revient qu'en fin de passage).
    if on_lock_acquired is not None:
        on_lock_acquired()

    state.account_flag = False
    processed_any = False
    for target_dir in ready:
        # Dossier froid constaté récemment : on garde ses marqueurs mais on ne
        # relance pas le moteur (il ressortirait froid) avant la prochaine
        # re-vérification. La planification le consolidera ; ensuite il passera.
        if state.is_cold_recent(target_dir, now, COLD_RECHECK_SECONDS):
            continue
        # Le dossier change-t-il encore ? Si oui on le laisse mûrir davantage :
        # ses marqueurs sont CONSERVÉS et il ne bloque personne — la boucle
        # passe simplement au dossier suivant, comme pour un dossier « froid ».
        if not state.is_settled(target_dir, now):
            n = state.defers_for(target_dir)
            if n == STABILITY_LOG_AFTER or (n > STABILITY_LOG_AFTER and n % 10 == 0):
                log(_("  ⏳ {p} is still being written — sync postponed "
                      "({n} time(s) so far)").format(p=target_dir, n=n))
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


def _write_status(auth_ok, cycle_seconds, mappings_path=None,
                  nas_scripts_stale=False, unreadable=None, cold_roots=None):
    """Battement de cœur pour l'icône de barre des tâches (tray_indicator.py) :
    horodatage + état de session + fichier de mappings ACTIF (écriture atomique).
    L'indicateur en déduit trois états : fichier frais + auth_ok -> connecté ;
    frais + not auth_ok -> session expirée ; vieux/absent -> démons arrêtés. Le
    chemin des mappings permet au clic gauche d'ouvrir l'éditeur sur le bon fichier.
    NB : l'état de session reflète ce que le consommateur SAIT — la sonde d'auth
    n'a lieu que lorsqu'il y a du travail (pour éviter la contention de trousseau) ;
    au repos, le dernier état connu persiste.

    nas_scripts_stale : True si un déploiement de scripts vers le NAS est en
    attente (écart de contenu poste↔disque NAS). Le systray en fait un 4e état
    (avertissement « ! », moins prioritaire que expired/stopped) et la fenêtre
    Temps réel le reflète. Défaut False (rétrocompatible : les lecteurs qui
    ignorent la clé se comportent comme avant).

    unreadable / cold_roots : ce qu'il y a à SIGNALER, avec une cause nommée.
    `unreadable` = dossiers dont la lecture échoue (leur contenu n'est pas
    sauvegardé) ; `cold_roots` = racines de mapping qu'aucun passage complet n'a
    jamais analysées depuis assez longtemps pour que ce soit anormal. Rien
    d'autre n'est publié : une alerte dont on ne sait pas dire la cause
    n'apprend rien à l'utilisateur et finit par être ignorée. Défaut vide,
    rétrocompatible."""
    path = appconfig.STATUS_FILE if _HAS_CONFIG else os.path.join(BASE_DIR, "status.json")
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "auth_ok": bool(auth_ok),
                       "cycle_seconds": int(cycle_seconds),
                       "mappings_path": (os.path.abspath(mappings_path)
                                         if mappings_path else None),
                       "nas_scripts_stale": bool(nas_scripts_stale),
                       "unreadable": list(unreadable or []),
                       "cold_roots": list(cold_roots or [])}, f)
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
        self._nas_scripts_stale = False
        # Ce qu'il y a à signaler, avec sa cause. Transporté par le battement :
        # l'indicateur de la barre des tâches ne lit qu'un fichier, et il reste
        # juste même pendant un passage très long (c'est tout l'intérêt de ce
        # thread).
        self._unreadable = []
        self._cold_roots = []
        self._interval = max(5, int(interval))
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        _write_status(self._auth_ok, self._cycle, self._mappings_path,
                      self._nas_scripts_stale, self._unreadable,
                      self._cold_roots)                            # battement immédiat
        self._thread.start()

    def update(self, auth_ok=None, cycle_seconds=None, nas_scripts_stale=None,
               unreadable=None, cold_roots=None):
        """Publie le dernier état connu (appelé par la boucle à chaque cycle).
        Le thread s'en sert pour ses écritures régulières."""
        with self._lock:
            if auth_ok is not None:
                self._auth_ok = bool(auth_ok)
            if cycle_seconds is not None:
                self._cycle = int(cycle_seconds)
            if nas_scripts_stale is not None:
                self._nas_scripts_stale = bool(nas_scripts_stale)
            if unreadable is not None:
                self._unreadable = list(unreadable)
            if cold_roots is not None:
                self._cold_roots = list(cold_roots)

    def beat_now(self):
        """Écrit le battement immédiatement avec l'état courant (utile juste
        après un cycle pour ne pas attendre l'intervalle du thread)."""
        with self._lock:
            _write_status(self._auth_ok, self._cycle, self._mappings_path,
                          self._nas_scripts_stale, self._unreadable,
                          self._cold_roots)

    def _run(self):
        # Réveil fin (1 s) pour pouvoir s'arrêter vite, mais on n'écrit qu'aux
        # multiples de l'intervalle — l'écriture est atomique et négligeable.
        elapsed = 0
        while not self._stop.wait(1):
            elapsed += 1
            if elapsed >= self._interval:
                elapsed = 0
                with self._lock:
                    _write_status(self._auth_ok, self._cycle, self._mappings_path,
                                  self._nas_scripts_stale, self._unreadable,
                                  self._cold_roots)

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

    log = _default_log
    state = DebounceState()

    # État NAS DYNAMIQUE (réévalué à chaque cycle) : le switch nas_enabled
    # (intention de l'utilisateur) combiné à la joignabilité réelle du NAS pilote
    # si on surveille la file NAS. Trois états :
    #   A. nas_enabled=False           -> pas de NAS voulu : file NAS jamais lue.
    #   B. nas_enabled=True + joignable -> normal : file NAS lue.
    #   C. nas_enabled=True + absent    -> suspendu : file NAS ignorée le temps de
    #      l'absence, reprise au retour (les marqueurs restent sur le NAS ; le
    #      startup_catchup du watcher NAS rebalaie tout à son redémarrage).
    # _nas_state mémorise l'état courant pour ne journaliser qu'aux TRANSITIONS.
    _nas_state = None      # None (pas encore évalué) / "off" / "on" / "absent"

    log(_("Real-time daemon started (user {u}).").format(u=user))
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

    # Détection d'écart de scripts NAS (alerte systray + fenêtre Temps réel) :
    # évaluée AU DÉMARRAGE (moment d'un déploiement — le consumer vient d'être
    # redémarré) puis toutes les ~NAS_SCRIPTS_CHECK_INTERVAL secondes — PAS à
    # chaque cycle (sha256 de ~15 fichiers via NFS). _nas_scripts_stale() est
    # LECTURE SEULE et déjà gardée (renvoie False en mode local ou NAS absent),
    # donc sûre et non bloquante à appeler ici.
    NAS_SCRIPTS_CHECK_INTERVAL = 300           # 5 min
    _last_scripts_check = None                 # monotonic du dernier contrôle
    _scripts_stale = False                     # dernier verdict connu (publié au battement)

    # Rapatriement des marqueurs « en vol » restés d'un arrêt brutal (coupure,
    # SIGKILL) entre leur mise à l'écart et la fin du traitement. La file NAS
    # n'est ajoutée que si elle est réellement lisible ; si le NAS est absent au
    # démarrage, ses marqueurs seront rapatriés au prochain lancement, et son
    # propre startup_catchup couvre l'intervalle.
    _startup_queues = [LOCAL_QUEUE]
    try:
        _nas_q = _nas_queue_for(user)
        if _nas_q and os.path.isdir(_nas_q):
            _startup_queues.append(_nas_q)
    except Exception:
        pass
    recover_inflight(_startup_queues, log=log)

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

        # ── État NAS dynamique : recalculé À CHAQUE CYCLE ────────────────────
        # Le switch nas_enabled (intention) + la joignabilité réelle décident si
        # on surveille la file NAS. On ne sonde le NAS QUE si l'utilisateur en
        # veut un (nas_enabled=True) — un utilisateur sans NAS ne sonde jamais.
        # Trois états, journalisés UNIQUEMENT aux transitions (pas à chaque cycle).
        want_nas = (appconfig.nas_enabled() if _HAS_CONFIG else True)
        if not want_nas:
            new_state = "off"
        elif _nas_reachable():
            new_state = "on"
        else:
            new_state = "absent"

        if new_state != _nas_state:
            # Transition : journaliser une seule ligne claire.
            if new_state == "on" and _nas_state == "absent":
                log(_("▶ NAS back online — resuming NAS mappings."))
            elif new_state == "absent":
                log(_("⏸ NAS unreachable — NAS mappings suspended, "
                      "local mode active. Local mappings keep syncing."))
            elif new_state == "off" and _nas_state is not None:
                log(_("NAS disabled — local mode only."))
            _nas_state = new_state

        # File NAS incluse seulement dans l'état « on » (NAS voulu ET joignable).
        # État « absent » (C.1) : file NAS IGNORÉE le temps de l'absence — les
        # marqueurs restent sur le NAS et seront repris au retour (le
        # startup_catchup du watcher NAS rebalaie tout à son redémarrage).
        queue_dirs = [LOCAL_QUEUE]
        if new_state == "on":
            queue_dirs.append(_nas_queue_for(user))

        # ── Écart de scripts NAS (throttlé ~5 min) ───────────────────────────
        # Recalcule au plus toutes les 5 min (et une fois au démarrage). Le
        # verdict s'auto-efface : dès que les scripts concordent (après un push),
        # le contrôle suivant repasse à False. Publié au battement plus bas.
        now_m = time.monotonic()
        if (_last_scripts_check is None
                or (now_m - _last_scripts_check) >= NAS_SCRIPTS_CHECK_INTERVAL):
            _last_scripts_check = now_m
            _scripts_stale = _nas_scripts_stale()

        # Callback appelé par run_once JUSTE AVANT le traitement (verrou obtenu).
        # Si on sortait d'une attente, on annonce la reprise MAINTENANT — avant
        # les lignes de synchro — plutôt qu'après coup. Ne signale rien si on ne
        # sortait pas d'une attente (cycle normal).
        def _announce_resume(_wr=waiting_reason):
            # Annonce de reprise SEULEMENT pour les attentes dont l'obtention du
            # verrou + auth SUFFIT à conclure la reprise (trousseau/verrou). Le cas
            # « account » est volontairement EXCLU : auth+verrou sont orthogonaux
            # au changement de compte, donc les avoir ne prouve PAS que le compte
            # est réglé — le moteur va re-refuser en code 4 tant que l'utilisateur
            # n'a pas ré-amorcé. Annoncer « Account matter resolved » ici était
            # faux ET répété à chaque cycle. La vraie résolution est confirmée par
            # le « ✓ Pass finished » quand le passage aboutit enfin (status done).
            if _wr == "locked":
                log(_("🔓 Session opened — starting the pass."))
            elif _wr == "busy":
                log(_("🔓 Lock released — starting the pass."))

        status = run_once(state, queue_dirs, mappings, args.config,
                          cfg["debounce_seconds"], time.monotonic(), log,
                          auth_check=lambda: keyring_ready(args.config),
                          lock_check=lambda: lock_free(args.config),
                          on_lock_acquired=_announce_resume)

        # Trois motifs d'attente possibles, chacun signalé par UNE SEULE ligne
        # tant qu'il dure (au lieu d'un échec par source et par cycle) :
        # trousseau verrouillé ("locked"), verrou tenu par un autre passage
        # ("busy"), ou compte Proton changé ("account", moteur en refus —
        # résolution par Amorcer/Réinitialiser). "idle" ne change rien (on n'a
        # pas sondé). "done" après une attente = passage terminé. On ne re-signale
        # que si le motif change.
        if status in ("locked", "busy", "account"):
            if waiting_reason != status:
                if status == "locked":
                    # DEUX causes, jamais une seule affirmée. `keyring_ready()`
                    # passe par `--check-auth`, qui exécute `filesystem list /` :
                    # un VRAI appel réseau. Une panne ou un refus du serveur
                    # ("Too many server errors") le fait échouer exactement comme
                    # un trousseau verrouillé. Constaté le 1er septembre : le
                    # message est apparu deux fois pendant que Proton refusait des
                    # envois, puis la sonde a repassé seule — sans aucune action
                    # de l'utilisateur, qu'on envoyait pourtant se reconnecter.
                    # Même correction que le message du moteur (27 août) : on
                    # n'affirme pas une cause qu'on ne connaît pas.
                    log(_("⏳ Proton unreachable — markers kept, no pass launched. "
                          "Either this user's session is not open (locked "
                          "keyring), or Proton itself is temporarily unavailable. "
                          "If your session is open, wait for the next cycle: a "
                          "temporary outage clears on its own. Otherwise run "
                          "“proton-drive auth login” (or the “Sign in to Proton” "
                          "button)."))
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
            # La reprise a déjà été annoncée AVANT le passage (via _announce_resume).
            # Ici, si on sortait d'une attente, on confirme la FIN du passage.
            if waiting_reason in ("locked", "busy", "account"):
                log(_("✓ Pass finished."))
            waiting_reason = None

        # Battement de cœur : publier l'état courant au thread dédié (qui écrit
        # à intervalle régulier, y compris pendant un long run_once) et forcer un
        # battement immédiat maintenant que le cycle est terminé.
        # Ce qu'il y a à signaler, calculé à chaque cycle : les dossiers
        # illisibles (cause certaine, signalés tout de suite) et les racines de
        # mapping froides depuis plus de COLD_ALERT_SECONDS (cause nommée elle
        # aussi : aucun passage complet ne les a analysées).
        _illisibles, _racines_froides = state.alert_paths(time.monotonic(),
                                                          COLD_ALERT_SECONDS)
        hb.update(auth_ok=(waiting_reason not in ("locked", "account")),
                  cycle_seconds=cfg["cycle_seconds"],
                  nas_scripts_stale=_scripts_stale,
                  unreadable=_illisibles,
                  cold_roots=_racines_froides)
        hb.beat_now()

        if args.once:
            hb.stop()
            break
        time.sleep(cfg["cycle_seconds"])


if __name__ == "__main__":
    main()

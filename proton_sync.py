#!/usr/bin/env python3
"""
Moteur de synchro Proton Drive (NAS -> Proton, à sens unique).

Lit un fichier JSON de mappings (voir proton_mapping_editor.py) et, pour
chaque entrée, n'envoie que les fichiers nouveaux ou modifiés — en
s'appuyant sur la taille (et la date de modification si disponible)
plutôt que de tout réenvoyer comme le ferait `upload --conflict-strategy
replace` seul.

Cache local : pour éviter un appel `filesystem list` côté Proton sur
chaque sous-dossier à chaque passage (très coûteux sur une arborescence
profonde), un cache JSON local stocke une empreinte de chaque dossier
synchronisé avec succès. Au passage suivant, si l'empreinte locale n'a
pas changé, on saute l'appel CLI. Le cache vit dans
~/.proton_sync_cache/<nom_du_mapping>.cache et n'est qu'un raccourci :
le supprimer force un passage complet (équivalent à --ignore-cache).

Usage :
    python3 proton_sync.py mappings-user1.json --dry-run -v   # test, rien n'est envoyé
    python3 proton_sync.py mappings-user1.json                # exécution réelle
    python3 proton_sync.py mappings-user1.json --ignore-cache # ignore le cache pour ce passage
    python3 proton_sync.py mappings-user1.json --verify-hash  # vérif SHA1, ignore le cache

Variable d'environnement :
    PROTON_DRIVE_CLI   chemin vers le binaire proton-drive
                        (par défaut : ~/Logiciels/Proton-drive/proton-drive)
"""
__version__ = "1.3.1"   # version propre à CE fichier ; incrémentée quand il change (indépendant de GitHub)

import argparse
import atexit
import datetime
import fcntl
import fnmatch
import hashlib
import json
import os
import signal
import subprocess
import sys
import time

# i18n (import guardé : sans i18n.py, messages en anglais — langue source).
try:
    from i18n import _
except ImportError:
    def _(s):
        return s

# Détection du type de source (nfs/local) pour le garde-fou de suppression.
# Le moteur cherche mount_check.py dans son propre dossier.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import mount_check
    _HAS_MOUNT_CHECK = True
except ImportError:
    _HAS_MOUNT_CHECK = False

# Détection du type de source (nfs/local) pour le garde-fou de suppression.
# Le moteur cherche mount_check.py (et config.py) dans son propre dossier.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import mount_check
    _HAS_MOUNT_CHECK = True
except ImportError:
    _HAS_MOUNT_CHECK = False

# Réglages d'installation (chemins, présence NAS, extensions...) : une SEULE
# source de vérité partagée par le moteur, le GUI et les démons. Repli sûr si
# config.py est absent (déploiement minimal) : mêmes valeurs qu'avant son
# introduction, rien ne change pour qui ne l'a pas encore.
try:
    import config as appconfig
    _HAS_CONFIG = True
except ImportError:
    _HAS_CONFIG = False

if _HAS_CONFIG:
    CLI = appconfig.resolve_proton_cli()
    LOCK_FILE = appconfig.LOCK_FILE
    CACHE_DIR = appconfig.CACHE_DIR
    FAILURES_LOG = appconfig.FAILURES_LOG
    RENAMED_LOG = appconfig.RENAMED_LOG
else:
    CLI = os.environ.get(
        "PROTON_DRIVE_CLI",
        os.path.expanduser("~/Logiciels/Proton-drive/proton-drive"),
    )
    # Verrou pour empêcher deux exécutions simultanées sous le même compte
    # Linux. Placé sous le home plutôt que /tmp/ pour que chaque utilisateur
    # (un par utilisateur) ait son propre verrou.
    LOCK_FILE = os.path.expanduser("~/.proton_sync.lock")
    # Répertoire des fichiers de cache. Un cache par fichier de mappings,
    # indexé par le nom du JSON (chaque utilisateur a le sien).
    CACHE_DIR = os.path.expanduser("~/.proton_sync_cache")
    # Journal DÉDIÉ des échecs d'upload (option #2) : chaque fichier qui
    # refuse de monter (même après ré-essai individuel) y est consigné, une
    # ligne par échec, horodatage + chemin + raison. But : relire SEULEMENT
    # les échecs sans dérouler tout le journal.
    FAILURES_LOG = os.path.expanduser("~/.proton_sync/failures.log")
    # Journal DÉDIÉ des renommages d'extension (majuscule -> minuscule).
    RENAMED_LOG = os.path.expanduser("~/.proton_sync/renamed-extensions.log")


def log_rename(src_path, dst_path):
    """Consigne un renommage d'extension (best-effort, n'interrompt jamais la
    synchro)."""
    try:
        os.makedirs(os.path.dirname(RENAMED_LOG), exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(RENAMED_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {src_path}  ->  {dst_path}\n")
    except OSError:
        pass


def log_failure(local_path, remote_parent, reason, kind="FAIL"):
    """Consigne au journal dédié (best-effort) un événement notable d'upload. Une
    ligne par fichier, avec la RAISON exacte renvoyée par le CLI. `kind` :
      - "FAIL"     -> échec réel (fichier PAS sur Proton) ;
      - "NO-THUMB" -> fichier bien téléversé mais SANS vignette (codec image
                      manquant : TIFF/HEIC/AVIF…). Gardé au journal pour garder la
                      trace de la raison et savoir quels fichiers n'ont pas d'aperçu
                      Proton."""
    try:
        os.makedirs(os.path.dirname(FAILURES_LOG), exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = (reason or "").strip().replace("\n", " ⏎ ")
        tag = "⚠ NO-THUMBNAIL" if kind == "NO-THUMB" else "❌ FAIL"
        with open(FAILURES_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {tag}  {local_path}  =>  {remote_parent}  :  {line}\n")
    except OSError:
        pass


def _is_thumbnail_error(reason):
    """True si l'échec vient de la génération de VIGNETTE / d'un codec image
    manquant (TIFF/HEIC/AVIF, etc.). Dans ce cas, l'upload SANS vignette réussit
    (le fichier est parfaitement sauvegardable ; seul l'aperçu Proton coince).
    On matche les termes stables du CLI, indépendants de la langue."""
    low = (reason or "").lower()
    return ("thumbnail" in low) or ("format not supported" in low)


def _is_vanished_error(reason):
    """True si l'échec vient de la DISPARITION du fichier entre le listing et
    l'upload (supprimé/déplacé pendant le traitement) : ENOENT / « no such file ».
    Ce n'est PAS un vrai échec — le fichier n'existe simplement plus. À distinguer
    d'un fichier présent mais illisible/corrompu (lui, reste un échec à consigner)."""
    low = (reason or "").lower()
    return ("enoent" in low or "no such file" in low)


# Valeur intégrée par défaut si config.py est absent ou le réglage vide/invalide.
_EXT_COLLISION_SUFFIX_DEFAULT = "_ProtonEditExt"


def _normalize_uppercase_ext(local_dir, entries, exclusions, dry_run=False, verbose=False,
                             collision_suffix=_EXT_COLLISION_SUFFIX_DEFAULT):
    """Renomme les fichiers (enfants DIRECTS de local_dir) dont l'extension finale
    contient des majuscules -> extension en minuscule, base du nom inchangée
    (IMG_1949.JPG -> IMG_1949.jpg, DOC.PDF -> DOC.pdf).

    Pourquoi : le CLI Proton détecte le type MIME à partir de l'extension de façon
    SENSIBLE À LA CASSE ; une extension majuscule est mal typée (octet-stream), ce
    qui casse la vignette (images), l'aperçu et l'icône (PDF, etc.). Normaliser la
    SOURCE règle le problème à la racine ET garde le cache cohérent (le distant
    portera le même nom minuscule que le local — pas de divergence, pas de ré-upload
    en boucle, pas d'orphelin).

    Sûreté :
      - dossiers et fichiers EXCLUS : jamais touchés (on ne modifie pas ce qu'on ne
        sauvegarde pas) ;
      - COLLISION : si la cible minuscule existe déjà (fichier distinct sur un FS
        sensible à la casse), on n'écrase JAMAIS — on insère `collision_suffix`
        (configurable, cf. config.py) avant l'extension, puis un compteur si
        nécessaire ;
      - dry-run : n'écrit rien, se contente d'annoncer les renommages.

    Retourne le nombre de renommages RÉELLEMENT effectués (0 en dry-run)."""
    renamed = 0
    for entry in entries:
        try:
            if entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        name = entry.name
        if exclusions and exclusions.is_excluded(name):
            continue
        base, ext = os.path.splitext(name)
        if not ext or ext == ext.lower():
            continue   # pas d'extension, ou déjà minuscule
        low_ext = ext.lower()
        target = base + low_ext
        # Garde-fou anti-collision : ne jamais écraser une cible existante.
        if os.path.exists(os.path.join(local_dir, target)):
            n = 0
            while True:
                suffix = collision_suffix + ("" if n == 0 else f"_{n}")
                target = base + suffix + low_ext
                if not os.path.exists(os.path.join(local_dir, target)):
                    break
                n += 1
        dst_path = os.path.join(local_dir, target)
        if dry_run:
            print(_("    ✎ [DRY-RUN] would fix uppercase extension: {a} -> {b}").format(
                a=entry.path, b=target))
            continue
        try:
            os.rename(entry.path, dst_path)
            renamed += 1
            # On affiche le chemin COMPLET de la source (et non le seul nom) : ça
            # permet au GUI d'extraire le dossier local et de le faire défiler en
            # mode épuré (voir que ça travaille), sans citer de chemin distant.
            print(_("    ✎ fixed uppercase extension: {a} -> {b}").format(
                a=entry.path, b=target))
            log_rename(entry.path, dst_path)
        except OSError as e:
            print(_("    ⚠  Could not rename {a}: {e}").format(a=name, e=e))
    return renamed


class Cache:
    """Cache des empreintes de dossiers déjà synchronisés avec succès.

    Pour chaque dossier local visité, on stocke une entrée :
      {
        "sig": { dir_mtime, files, remote_folder },   # l'empreinte locale
        "delete_synced": bool                          # le distant a-t-il été
                                                       # réconcilié (orphelins
                                                       # supprimés) lors d'un
                                                       # passage --delete ?
      }

    L'empreinte `sig` détecte tout changement local (ajout/suppression/
    renommage d'un enfant via dir_mtime ; modification de contenu via le mtime
    des fichiers). Le drapeau `delete_synced` mémorise si la propagation des
    suppressions a déjà été faite pour cet état.

    Pourquoi le drapeau : une suppression locale change TOUJOURS l'empreinte.
    Donc si l'empreinte est inchangée ET que le distant a déjà été réconcilié
    (delete_synced=True), on peut sauter l'appel CLI même en mode --delete : il
    ne peut pas y avoir de nouvel orphelin. Mais si un dossier a été mis en cache
    par un passage SANS --delete (delete_synced=False), une suppression locale
    survenue avant ce passage n'a peut-être jamais été propagée — il faut donc
    vérifier le distant au prochain --delete, même si l'empreinte est "fraîche".

    RÉTROCOMPATIBILITÉ : les anciens caches stockaient directement la signature
    comme valeur (sans enveloppe). _entry() gère les deux formats.

    ESTAMPILLE DE COMPTE : la clé réservée "__meta__" (jamais un chemin — les
    chemins locaux commencent tous par « / ») mémorise l'adresse du compte
    Proton pour lequel ce cache a été bâti. Un cache SANS estampille (anciens
    caches) est valide tel quel et se fait estampiller à la prochaine
    sauvegarde — aucun ré-amorçage requis. Voir le garde-fou de changement de
    compte dans main().

    Le cache n'est JAMAIS une source de vérité — juste un raccourci. En cas de
    doute, le supprimer force un re-scan complet.
    """

    def __init__(self, path):
        self.path = path
        self.data = {}
        self.dirty = False
        self._last_save = 0.0   # horodatage de la dernière écriture disque
        self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(_("⚠  Cache corrupted or unreadable ({e}) — starting fresh.").format(e=e))
            self.data = {}

    def save(self):
        if not self.dirty:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)
        self.dirty = False
        self._last_save = time.monotonic()

    def maybe_save(self, min_interval=10.0):
        """Écrit le cache sur disque SI au moins `min_interval` secondes se sont
        écoulées depuis la dernière écriture (et si le cache a changé). Appelée au
        fil d'un long parcours pour que la progression survive à une interruption
        (SIGTERM d'un `systemctl stop`, reboot, collision de verrou) : sans elle,
        les entrées delete_synced écrites par sous-dossier restent en MÉMOIRE et
        sont perdues si le processus est tué avant la fin du mapping. Le coût d'une
        interruption passe ainsi de « tout le mapping » à « ~min_interval s »."""
        if self.dirty and (time.monotonic() - self._last_save) >= min_interval:
            self.save()

    @staticmethod
    def _entry(raw):
        """Normalise une valeur de cache (ancien ou nouveau format) en
        (signature, delete_synced). Ancien format = la signature directement."""
        if isinstance(raw, dict) and "sig" in raw:
            return raw.get("sig"), bool(raw.get("delete_synced", False))
        # Ancien format : la valeur EST la signature ; jamais réconcilié pour delete.
        return raw, False

    def is_fresh(self, local_dir, current_signature):
        """True si l'empreinte locale correspond à celle en cache (côté upload)."""
        cached_sig, _ds = self._entry(self.data.get(local_dir))
        return cached_sig == current_signature

    def is_delete_synced(self, local_dir, current_signature):
        """True si l'empreinte correspond ET que le distant a déjà été réconcilié
        (orphelins supprimés) pour cet état. Permet de sauter le dossier en mode
        --delete sans risquer de manquer un orphelin."""
        cached_sig, dsync = self._entry(self.data.get(local_dir))
        return cached_sig == current_signature and dsync

    def cached_excl(self, local_dir):
        """Empreinte d'exclusions actuellement stockée pour ce dossier (ou None).
        Sert au mode TEMPS RÉEL (Option A) : en réutilisant cette empreinte au lieu
        de l'empreinte courante, un simple changement d'exclusions ne périme PAS
        delete_synced sur le chemin temps réel (qui doit rester instantané). La
        réconciliation des orphelins nouvellement exclus reste portée par les
        passages COMPLETS planifiés, qui utilisent l'empreinte courante et voient
        donc la différence."""
        cached_sig, _ds = self._entry(self.data.get(local_dir))
        if isinstance(cached_sig, dict):
            return cached_sig.get("excl")
        return None

    def subtree_complete(self, local_dir):
        """True si ce dossier ET TOUTE SA DESCENDANCE ont été analysés jusqu'au
        bout par un passage complet (planifié/manuel), sans interruption. C'est le
        signal « prêt pour le temps réel » : un dossier marqué complet est
        entièrement fiable, on peut y synchroniser un changement ciblé sans risquer
        de déclencher l'analyse d'un gros sous-arbre encore inconnu.

        Distinct de « a une entrée de cache » : un dossier peut avoir une empreinte
        (vu au moins une fois) tout en ayant des sous-dossiers jamais analysés
        (consolidation partielle héritée, ou interrompue) — auquel cas il n'est PAS
        complet. C'est cette distinction qui empêche le temps réel de repartir dans
        un long parcours sur une racine « tiède ».

        Rétrocompatible : une entrée sans le champ (ancien format, ou dossier
        jamais consolidé) est lue comme NON complète -> le temps réel diffère à la
        planification tant qu'un passage complet n'a pas marqué l'arbre. C'est le
        comportement voulu (le cache hérité est traité comme non fiable jusqu'à une
        reconsolidation complète)."""
        raw = self.data.get(local_dir)
        return bool(isinstance(raw, dict) and raw.get("subtree_complete", False))

    def purge_subtree(self, local_dir):
        """Retire du cache TOUTES les entrées dont le chemin est `local_dir` ou
        situé dessous (récursif). Utilisé par la réinitialisation d'un mapping :
        le sous-arbre redevient « inconnu » -> l'État repasse en ⏳ et TOUT type de
        passage (normal, planifié, temps réel) le retraite entièrement — l'état est
        donc cohérent par construction et auto-cicatrisant (aucune entrée « complète »
        périmée ne subsiste pour égarer un passage). Écriture IMMÉDIATE (pas de
        maybe_save throttlé) : l'état ⏳ doit être persistant dès le départ, même si
        le passage suivant est interrompu très tôt. Retourne le nombre d'entrées
        retirées."""
        base = os.path.normpath(local_dir)
        prefix = base.rstrip("/") + "/"
        removed = 0
        for k in list(self.data.keys()):
            nk = os.path.normpath(k)
            if nk == base or (nk + "/").startswith(prefix):
                del self.data[k]
                removed += 1
        if removed:
            self.dirty = True
            self.save()
        return removed

    META_KEY = "__meta__"

    def account(self):
        """Adresse du compte pour lequel ce cache a été bâti (None si
        cache jamais estampillé — anciens caches, ou compte inconnu)."""
        m = self.data.get(self.META_KEY)
        return m.get("account") if isinstance(m, dict) else None

    def set_account(self, email):
        """Estampille le cache avec l'adresse du compte connecté."""
        if not email:
            return
        m = self.data.get(self.META_KEY)
        if not isinstance(m, dict):
            m = {}
            self.data[self.META_KEY] = m
        if m.get("account") != email:
            m["account"] = email
            self.dirty = True

    def update(self, local_dir, signature, delete_synced=None, subtree_complete=None):
        """Met à jour l'entrée. Si delete_synced est None, on conserve la valeur
        existante (utile pour un passage sans --delete qui ne doit pas prétendre
        avoir réconcilié les suppressions). Si True/False, on l'impose.
        subtree_complete suit la même logique (None = conserver l'existant si
        l'empreinte n'a pas changé, sinon repartir de False)."""
        cached_sig, existing_dsync = self._entry(self.data.get(local_dir))
        raw = self.data.get(local_dir)
        existing_complete = bool(isinstance(raw, dict)
                                 and raw.get("subtree_complete", False))
        if delete_synced is None:
            # Conserver l'état de réconciliation seulement si l'empreinte est la
            # même ; si l'empreinte a changé, on repart de False (non réconcilié).
            new_dsync = existing_dsync if cached_sig == signature else False
        else:
            new_dsync = delete_synced
        if subtree_complete is None:
            new_complete = existing_complete if cached_sig == signature else False
        else:
            new_complete = subtree_complete
        new_val = {"sig": signature, "delete_synced": new_dsync,
                   "subtree_complete": new_complete}
        if self.data.get(local_dir) != new_val:
            self.data[local_dir] = new_val
            self.dirty = True

    def invalidate(self, local_dir):
        if local_dir in self.data:
            del self.data[local_dir]
            self.dirty = True


class Exclusions:
    """Décide si un fichier ou dossier doit être exclu de la synchro.

    Deux mécanismes combinés :
      - names  : liste de noms EXACTS (ex. ".caltrash", "trash", ".Trash-1000").
                 Comparaison insensible à la casse.
      - patterns : liste de motifs glob façon shell (ex. "*trash*", "*.tmp",
                 ".Trash-*"). Comparaison insensible à la casse via fnmatch.

    L'exclusion s'applique au NOM de l'entrée (pas au chemin complet), donc
    "trash" exclut tout dossier/fichier nommé exactement "trash", où qu'il soit.
    Un dossier exclu n'est pas visité du tout (son contenu est ignoré).

    Les exclusions globales (valant pour tous les mappings) et les exclusions
    propres à un mapping sont fusionnées : une entrée est exclue si elle
    correspond à l'une OU l'autre.
    """

    def __init__(self, names=None, patterns=None):
        # On normalise en minuscules pour une comparaison insensible à la casse.
        self.names = set(n.lower() for n in (names or []))
        self.patterns = [p.lower() for p in (patterns or [])]

    def merged_with(self, other):
        """Retourne une nouvelle Exclusions combinant self (global) + other (mapping)."""
        if other is None:
            return self
        combined_names = self.names | other.names
        combined_patterns = self.patterns + other.patterns
        e = Exclusions()
        e.names = combined_names
        e.patterns = combined_patterns
        return e

    def is_excluded(self, name):
        """True si 'name' (nom d'un fichier ou dossier) doit être exclu."""
        low = name.lower()
        if low in self.names:
            return True
        for pat in self.patterns:
            if fnmatch.fnmatch(low, pat):
                return True
        return False

    def fingerprint(self):
        """Empreinte stable et compacte du jeu d'exclusions effectif (noms +
        motifs). Injectée dans la signature de cache pour qu'un CHANGEMENT
        d'exclusions périme la réconciliation : un fichier nouvellement exclu mais
        déjà présent sur Proton sera alors re-détecté comme orphelin au prochain
        passage --delete (sinon le dossier parent, inchangé localement, resterait
        sauté par le cache et l'orphelin ne serait jamais nettoyé)."""
        payload = "\n".join(sorted(self.names)) + "\u0000" + "\n".join(sorted(self.patterns))
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]

    def __bool__(self):
        return bool(self.names or self.patterns)


def load_config(path):
    """Charge le fichier de config et retourne (mappings, global_exclusions).

    Accepte DEUX formats, pour rétrocompatibilité :

    1. Ancien format — une simple liste de mappings :
       [ {type, source, dest_parent}, ... ]

    2. Nouveau format — un objet avec exclusions globales optionnelles :
       {
         "exclusions": { "names": [...], "patterns": [...] },
         "mappings": [ {type, source, dest_parent, exclusions: {...}}, ... ]
       }

    Dans le nouveau format, chaque mapping peut avoir sa propre clé "exclusions"
    (noms + motifs) qui s'ajoute aux exclusions globales pour ce mapping.
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        # Ancien format : liste simple, aucune exclusion globale.
        return data, Exclusions()

    if isinstance(data, dict):
        mappings = data.get("mappings", [])
        ex = data.get("exclusions", {}) or {}
        global_ex = Exclusions(ex.get("names"), ex.get("patterns"))
        return mappings, global_ex

    raise ValueError("Format de config non reconnu (ni liste, ni objet).")


def mapping_exclusions(mapping, global_ex):
    """Retourne les exclusions effectives pour un mapping : global + propres au mapping."""
    ex = mapping.get("exclusions") or {}
    local_ex = Exclusions(ex.get("names"), ex.get("patterns"))
    return global_ex.merged_with(local_ex)



def _local_signature(local_dir, remote_folder, excl_fp=None):
    """Empreinte rapide d'un dossier local : mtime du dossier + (nom,taille,mtime)
    de chaque fichier direct (non-récursif). Les sous-dossiers ont chacun leur
    propre entrée de cache, donc on ne les inclut pas ici.

    `excl_fp` = empreinte du jeu d'exclusions effectif (Exclusions.fingerprint()).
    L'inclure dans la signature fait qu'un changement d'exclusions périme le cache
    de ce dossier → il sera re-listé et re-réconcilié (côté suppressions) au
    prochain passage, ce qui permet de nettoyer les orphelins nouvellement exclus
    déjà présents sur Proton."""
    try:
        dir_mtime = os.path.getmtime(local_dir)
    except OSError:
        return None
    files = []
    try:
        for entry in os.scandir(local_dir):
            if entry.is_file(follow_symlinks=False):
                try:
                    st = entry.stat(follow_symlinks=False)
                    files.append([entry.name, st.st_size, st.st_mtime])
                except OSError:
                    return None  # erreur de lecture -> empreinte invalide
    except OSError:
        return None
    files.sort()
    return {
        "dir_mtime": dir_mtime,
        "files": files,
        "remote_folder": remote_folder,
        "excl": excl_fp,
    }

# --- Noms de champs réels confirmés sur la vraie sortie 'filesystem list -j' ---
# totalStorageSize = taille CHIFFRÉE stockée (overhead de chiffrement, ne correspond
# jamais exactement à la taille locale) -> ne pas utiliser pour comparer.
# activeRevision.value.claimedSize = vraie taille du fichier original -> à utiliser.
# activeRevision.value.claimedDigests.sha1 = hash du contenu original -> vérif optionnelle.
REMOTE_NAME_KEYS = ("name",)


def _extract_remote_meta(item):
    """Extrait (size, mtime, sha1) en privilégiant les champs 'claimed*' (fichier
    original), avec repli sur les champs de premier niveau si activeRevision est
    absent (ex. ancienne version du CLI, ou structure différente)."""
    size = item.get("totalStorageSize")
    mtime = item.get("modificationTime")
    sha1 = None
    active_rev = _unwrap(item.get("activeRevision"))
    if isinstance(active_rev, dict):
        if active_rev.get("claimedSize") is not None:
            size = active_rev.get("claimedSize")
        if active_rev.get("claimedModificationTime") is not None:
            mtime = active_rev.get("claimedModificationTime")
        digests = active_rev.get("claimedDigests")
        if isinstance(digests, dict):
            sha1 = digests.get("sha1")
    return size, mtime, sha1


def run_cli(args, json_output=False):
    cmd = [CLI] + list(args)
    if json_output:
        cmd.append("-j")
    return subprocess.run(cmd, capture_output=True, text=True)




def check_auth():
    """Vérifie que le CLI peut s'authentifier (trousseau déverrouillé, session
    ouverte). Retourne (True, None) si OK, (False, message) sinon.

    Cas typique d'échec : la tâche planifiée se déclenche alors que la session
    graphique de l'utilisateur n'est pas ouverte, donc le trousseau GNOME est
    verrouillé et le CLI ne peut pas lire ses identifiants. On veut alors sortir
    proprement et vite, sans rien tenter d'autre.

    On utilise `filesystem list /` comme test inoffensif : ça ne touche à rien,
    c'est rapide, et ça force le CLI à accéder au trousseau pour s'authentifier.
    """
    res = run_cli(["filesystem", "list", "/"])
    if res.returncode == 0:
        return True, None
    err = (res.stderr or res.stdout or "").strip()
    return False, err


def check_auth_settled():
    """check_auth TOLÉRANT au faux négatif transitoire : un premier « non » est
    revérifié après 2,5 s — même principe que la sonde fiabilisée du GUI
    (_check_auth_settled). Un VRAI échec (trousseau verrouillé, session
    expirée), lui, persiste sur les deux essais.

    Utilisée UNIQUEMENT pour la vérification de DÉBUT DE PASSAGE : elle ferme
    la fenêtre où un passage lancé par un démon subit un faux négatif s'il
    démarre à l'instant exact où une sonde du GUI tient le trousseau (les
    sondes du GUI sont sérialisées ENTRE ELLES, mais le moteur est un autre
    processus). La sonde --check-auth, elle, reste volontairement INSTANTANÉE
    (check_auth simple) : le consommateur s'en sert comme lecture rapide de
    l'état du trousseau à chaque cycle."""
    ok, err = check_auth()
    if ok:
        return True, None
    time.sleep(2.5)
    return check_auth()


def get_account_email():
    """Adresse du COMPTE Proton réellement connecté, déduite des métadonnées
    d'auteur (keyAuthor/nameAuthor) que 'filesystem list -j' attache à chaque
    élément — observées en production, ce sont les champs qui portent l'adresse
    du compte pour les éléments qu'il a créés. On liste /my-files (contenu créé
    par le compte) et on prend l'adresse MAJORITAIRE parmi les auteurs — robuste
    même si quelques éléments proviennent d'un partage. Retourne None si
    indéterminable (ex. Drive vide) — l'appelant affiche alors l'état sans nom.
    Sert au témoin de connexion du GUI : le VRAI compte, pas un nom dérivé du
    fichier de mappings."""
    counts = {}
    for root in ("/my-files", "/"):
        data, _err = cli_json(["filesystem", "list", root])
        for item in data or []:
            for key in ("keyAuthor", "nameAuthor"):
                v = _unwrap(item.get(key))
                if isinstance(v, str) and "@" in v:
                    counts[v] = counts.get(v, 0) + 1
        if counts:
            return max(counts, key=counts.get)
    return None


def cli_json(args):
    result = run_cli(args, json_output=True)
    if result.returncode != 0:
        return None, result.stderr.strip()
    try:
        return json.loads(result.stdout), None
    except json.JSONDecodeError as e:
        return None, f"JSON invalide ({e}). Sortie brute : {result.stdout[:300]!r}"


def _unwrap(value):
    """Le CLI enveloppe certains champs en {'ok': True, 'value': ...} — on déballe si besoin."""
    if isinstance(value, dict) and "value" in value:
        return value.get("value")
    return value


def _first(d, keys, default=None):
    for k in keys:
        if k in d and d[k] is not None:
            return _unwrap(d[k])
    return default


def remote_exists(path):
    data, _err = cli_json(["filesystem", "info", path])
    return data is not None


def _already_exists_error(stderr):
    """True si l'erreur du CLI signale que la cible existe déjà — quelle que soit
    la langue. Un `create-folder` sur un dossier déjà présent n'est PAS une vraie
    erreur, juste une redondance à ignorer silencieusement. On matche les
    formulations connues (anglais + français, avec et sans accent) plutôt que la
    seule chaîne anglaise, qui laissait passer l'avertissement sur un CLI en
    français (« … existe déjà. »)."""
    s = (stderr or "").lower()
    return any(h in s for h in (
        "already exists",   # CLI en anglais
        "existe déjà",      # CLI en français (tel que renvoyé)
        "existe deja",      # français sans accent (robustesse d'encodage)
    ))


def ensure_remote_path(path):
    """Crée récursivement chaque segment manquant du chemin distant `path`."""
    parts = [p for p in path.strip("/").split("/") if p]
    current = ""
    for part in parts:
        parent = current if current else "/"
        current = f"{current}/{part}" if current else f"/{part}"
        # Emplacements de PREMIER NIVEAU de Proton Drive (« My files »,
        # « Shared with me », « Photos », « Devices ») : racines VIRTUELLES fixes,
        # NON créables (le CLI renvoie « Path "/" is not supported ») et toujours
        # présentes. On ne tente donc pas de les créer — on descend simplement
        # dedans. Corrige l'avertissement parasite sur une destination
        # « /shared-with-me/… » (ex. un dossier de travail collaboratif, un par
        # personne, sur un partage commun).
        if parent == "/":
            continue
        if not remote_exists(current):
            res = run_cli(["filesystem", "create-folder", parent, part])
            # « existe déjà » (dans n'importe quelle langue) = dossier présent =
            # succès silencieux. On n'avertit que sur une VRAIE erreur (permission,
            # quota, nom invalide…), dont le message ne matche pas ce filtre.
            if res.returncode != 0 and not _already_exists_error(res.stderr):
                print(_("    ⚠  Could not create {p}: {e}").format(p=current, e=res.stderr.strip()))
    return current


def get_remote_listing(remote_path, verbose=False):
    """Retourne {nom: {"size", "mtime", "sha1", "type"}} pour un dossier distant.
    Le champ "type" ("file"/"folder") permet de choisir la bonne opération de
    suppression (trash/delete) et de descendre dans les dossiers orphelins."""
    data, err = cli_json(["filesystem", "list", remote_path])
    if data is None:
        if verbose:
            print(_("    (nothing found at {p}: {e})").format(p=remote_path, e=err))
        return {}
    # Dump JSON du premier élément : outil de mise au point (format de réponse du
    # CLI). Inutile en usage normal -> conditionné à PROTON_SYNC_DEBUG pour ne PLUS
    # polluer le mode Détaillé, tout en restant récupérable si Proton change le
    # format de 'filesystem list -j'.
    if verbose and data and os.environ.get("PROTON_SYNC_DEBUG"):
        print(_("    [debug] first raw element received from 'filesystem list -j':"))
        print("   ", json.dumps(data[0], indent=2, default=str)[:600].replace("\n", "\n    "))
    listing = {}
    for item in data:
        name = _first(item, REMOTE_NAME_KEYS)
        if not name:
            continue
        size, mtime, sha1 = _extract_remote_meta(item)
        # Le type peut être enveloppé {ok, value} comme les autres champs.
        rtype = _unwrap(item.get("type"))
        listing[name] = {"size": size, "mtime": mtime, "sha1": sha1, "type": rtype}
    return listing


def _local_sha1(path, chunk_size=1024 * 1024):
    import hashlib
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


def needs_upload(local_path, remote_info, verbose=False, verify_hash=False):
    if remote_info is None:
        return True
    try:
        local_size = os.path.getsize(local_path)
    except OSError:
        return True
    remote_size = remote_info.get("size")
    if remote_size is None:
        # Champ taille introuvable dans la réponse -> comparaison impossible,
        # on choisit de réenvoyer plutôt que de risquer de manquer un changement.
        if verbose:
            print(_("    (remote size unknown for {f}, re-sending to be safe)").format(f=os.path.basename(local_path)))
        return True
    if int(remote_size) != local_size:
        return True
    if verify_hash and remote_info.get("sha1"):
        local_hash = _local_sha1(local_path)
        if local_hash != remote_info["sha1"]:
            if verbose:
                print(_("    (same size but different content: {f})").format(f=os.path.basename(local_path)))
            return True
    return False


def _glob_escape_local_path(path):
    """Échappe les métacaractères glob dans un chemin local destiné au CLI Proton.

    Le CLI `proton-drive filesystem upload` applique une expansion de motif (glob)
    sur ses arguments de fichiers locaux. Un nom contenant { } [ ] * ? est alors
    interprété comme un motif et, ne correspondant à aucun fichier réel, échoue
    avec « No paths matched » (cas typique : les extensions Thunderbird nommées
    avec un UUID entre accolades, ex. {43ed69b5-...}.xpi).

    Solution : envelopper chaque métacaractère dans une classe glob [c], qui
    désigne le caractère LITTÉRAL. Confirmé en pratique : le CLI accepte
    « [{]43ed...[}].xpi » et uploade correctement le fichier « {43ed...}.xpi ».

    On n'échappe QUE le chemin transmis au CLI ; l'affichage et la logique
    interne (cache, comparaisons) continuent d'utiliser le vrai nom.
    """
    specials = "*?[]{}"
    out = []
    for ch in path:
        if ch in specials:
            out.append(f"[{ch}]")
        else:
            out.append(ch)
    return "".join(out)


def _upload_one(local_path, remote_parent, skip_thumbnails=False):
    """Téléverse UN seul fichier. Retourne (ok: bool, message: str). Sert à
    l'isolation des échecs (ré-essai fichier par fichier) et n'est appelé qu'après
    l'échec d'un lot — jamais en régime normal.

    Le message capture la VRAIE raison : le détail par fichier (ex.
    « - X.tiff: ValidationError: Failed to generate thumbnails … ») est renvoyé par
    le CLI dans STDOUT, tandis que STDERR ne donne qu'un compteur générique
    (« 1 item(s) failed to upload »). On garde les DEUX, stdout d'abord."""
    cli_path = _glob_escape_local_path(local_path)
    cmd = ["filesystem", "upload", "-f", "replace", "-d", "merge"]
    if skip_thumbnails:
        cmd.append("--skip-thumbnails")
    cmd += [cli_path, remote_parent]
    res = run_cli(cmd)
    parts = [p for p in ((res.stdout or "").strip(), (res.stderr or "").strip()) if p]
    return res.returncode == 0, " | ".join(parts)


def _emit_progress(**fields):
    """Émet une ligne de progression machine-lisible sur stdout, préfixée
    @@PROGRESS, que le GUI intercepte pour alimenter sa barre de progression
    (et retire du journal). Non traduite (protocole interne, pas destiné à
    l'utilisateur directement). Robuste : n'échoue jamais.

    Champs utilisés au Temps 1 :
      state=start|done   — début / fin d'un envoi de lot
      files=<n>          — nombre de fichiers du lot (si state=start)
      bytes=<n>          — taille totale du lot en octets (si state=start)
    """
    try:
        parts = " ".join(f"{k}={v}" for k, v in fields.items())
        print("@@PROGRESS " + parts, flush=True)
    except Exception:
        pass


def _sum_sizes(paths):
    """Somme des tailles (octets) des chemins existants. Ignore les erreurs
    (fichier disparu entre-temps) — la somme reste indicative."""
    total = 0
    for p in paths:
        try:
            total += os.path.getsize(p)
        except OSError:
            pass
    return total


def upload_batch(local_paths, remote_parent, dry_run=False, verbose=False):
    """Retourne True si tout s'est bien passé (y compris si la liste est vide ou en
    dry-run), False en cas d'échec d'upload.

    Sur échec du LOT : le CLI ne rapporte qu'un compteur (« N item(s) failed »)
    sans nommer le fautif. On ISOLE donc (option #1) : on relit le distant pour
    savoir ce qui est bien monté, puis on ré-essaie individuellement CE qui manque
    encore. Les fichiers déjà montés par le lot sont sautés (pas de renvoi inutile).
    Chaque échec résiduel est nommé précisément (chemin + raison du CLI), affiché
    ET consigné dans le journal dédié (option #2). Un fichier qui repasse au
    ré-essai individuel est réellement monté (pas juste diagnostiqué)."""
    if not local_paths:
        return True
    if dry_run:
        for p in local_paths:
            print(_("    [DRY-RUN] would upload: {p}").format(p=p))
        return True
    # Échapper les métacaractères glob pour le CLI (sinon les noms à accolades,
    # crochets, etc. échouent avec « No paths matched »).
    cli_paths = [_glob_escape_local_path(p) for p in local_paths]
    cmd = ["filesystem", "upload", "-f", "replace", "-d", "merge"] + cli_paths + [remote_parent]
    # Progression (Temps 1) : signaler le lot en cours (nb de fichiers + taille
    # totale) AVANT l'envoi groupé, puis la fin APRÈS. Le GUI affiche un indicateur
    # discret « Envoi en cours — N fichiers, X Go » pendant ce temps.
    #
    # NOTE : le suivi PAR FICHIER (pourcentage intra-fichier) a été évalué puis
    # écarté. Le CLI n'émet son pourcentage que vers un vrai terminal (TTY) ; dès
    # que sa sortie est lue via un pipe (ce que fait forcément le moteur), il
    # supprime la progression. La capter exigerait un pseudo-terminal (pty) au
    # parsing fragile — disproportionné tant que le CLI n'offre pas d'option de
    # progression machine-lisible. Voir CONTEXTE.md.
    _emit_progress(state="start", files=len(local_paths), bytes=_sum_sizes(local_paths))
    try:
        res = run_cli(cmd)
    finally:
        _emit_progress(state="done")
    if res.returncode == 0:
        print(_("    ✅ {n} file(s) sent to {p}").format(n=len(local_paths), p=remote_parent))
        if verbose and res.stdout.strip():
            print("      " + res.stdout.strip().replace("\n", "\n      "))
        return True

    # ── Le lot a échoué : isolation par fichier (option #1) ────────────────
    print(_("    ⚠  Batch upload to {p} reported a failure — isolating the "
            "culprit file(s)…").format(p=remote_parent))
    if verbose and res.stdout.strip():
        print("      " + res.stdout.strip().replace("\n", "\n      "))

    # Relire le distant : ce qui est déjà présent (bonne taille) a réussi dans le
    # lot -> inutile de le renvoyer. On ne ré-essaie QUE ce qui manque encore.
    remote_after = get_remote_listing(remote_parent, verbose=False)
    failures = []
    recovered = 0
    no_thumb = 0
    vanished = 0
    for p in local_paths:
        # Fichier disparu AVANT même de tenter l'upload (supprimé/déplacé entre le
        # listing et maintenant) : bénin, on l'ignore silencieusement (pas un échec,
        # rien au journal, le dossier pourra se mettre en cache).
        if not os.path.exists(p):
            vanished += 1
            if verbose:
                print(_("      – vanished before upload (deleted meanwhile), skipped: {p}").format(p=p))
            continue
        info = remote_after.get(os.path.basename(p))
        if not needs_upload(p, info, verbose=False):
            continue   # déjà monté correctement par le lot
        ok, why = _upload_one(p, remote_parent)
        if not ok and _is_vanished_error(why):
            # Disparu PENDANT l'upload (course avec une suppression concurrente) :
            # bénin aussi. À NE PAS confondre avec un fichier présent mais corrompu.
            vanished += 1
            if verbose:
                print(_("      – vanished during upload (deleted meanwhile), skipped: {p}").format(p=p))
            continue
        if not ok and _is_thumbnail_error(why):
            # Auto-récupération ciblée (option #1) : l'échec vient de la VIGNETTE
            # (codec image manquant), pas de l'upload. On re-téléverse SANS vignette
            # -> le fichier est sauvegardé (sans aperçu Proton). Ne se déclenche
            # QUE sur cette signature ; les vrais échecs restent des échecs.
            print(_("      ⚠ thumbnail/codec issue — retrying without thumbnail: {p}").format(p=p))
            ok2, why2 = _upload_one(p, remote_parent, skip_thumbnails=True)
            if ok2:
                no_thumb += 1
                print(_("      ✓ uploaded WITHOUT thumbnail (no Proton preview): {p}").format(p=p))
                # Trace persistante de la raison (le journal doit la garder).
                log_failure(p, remote_parent, why, kind="NO-THUMB")
                continue
            why = why2   # échec réel même sans vignette -> on tombe dans le cas échec
            ok = False
        if ok:
            recovered += 1
            if verbose:
                print(_("      ✓ recovered on individual retry: {p}").format(p=p))
        else:
            failures.append(p)
            print(_("    ❌ upload failed: {p}").format(p=p))
            print(_("    ❌   reason: {e}").format(e=why or _("(no message from the CLI)")))
            log_failure(p, remote_parent, why)

    if recovered:
        print(_("    ✅ {n} file(s) recovered on individual retry.").format(n=recovered))
    if no_thumb:
        print(_("    ⚠  {n} file(s) uploaded WITHOUT thumbnail (image format needs an "
                "OS codec — saved anyway, logged in {log}).").format(n=no_thumb, log=FAILURES_LOG))
    if vanished:
        print(_("    – {n} file(s) vanished (deleted meanwhile) — skipped, not a failure.").format(n=vanished))
    if failures:
        print(_("    ❌ {n} file(s) still failing (see the failures log: {log}).").format(
            n=len(failures), log=FAILURES_LOG))
        return False
    # Tout a fini par passer (le lot échouait mais chaque fichier monte seul, ou a
    # légitimement disparu). Les fichiers disparus ne bloquent pas la complétude.
    return True


def remote_trash(remote_path, permanent=False, dry_run=False):
    """Envoie un élément distant à la corbeille Proton (ou le supprime
    définitivement si permanent=True). Retourne True si OK."""
    action = "delete" if permanent else "trash"
    if dry_run:
        verbe = _("would delete PERMANENTLY") if permanent else _("would move to trash")
        print(f"    [DRY-RUN] {verbe} : {remote_path}")
        return True
    res = run_cli(["filesystem", action, remote_path])
    if res.returncode != 0:
        print(_("    ❌ {a} of {p} failed: {e}").format(a=action, p=remote_path, e=res.stderr.strip()))
        return False
    label = _("permanently deleted") if permanent else _("sent to trash")
    print(f"    🗑  {label} : {remote_path}")
    return True


def _count_remote_recursive(remote_path):
    """Compte récursivement le nombre d'éléments (fichiers + dossiers) sous un
    dossier distant. Sert à journaliser l'ampleur d'une suppression de dossier
    orphelin. Best-effort : en cas d'erreur, retourne ce qui a pu être compté."""
    total = 0
    listing = get_remote_listing(remote_path)
    for name, info in listing.items():
        total += 1
        if info.get("type") == "folder":
            total += _count_remote_recursive(remote_path.rstrip("/") + "/" + name)
    return total


def delete_orphans(local_dir, remote_folder, remote_items, local_names,
                   delete_mode="trash", dry_run=False, verbose=False):
    """Supprime sur Proton les éléments présents dans `remote_items` mais absents
    localement (`local_names`). Les dossiers orphelins sont supprimés en entier
    (Proton gère la récursion via trash/delete sur le dossier).

    `delete_mode` : "trash" (corbeille) ou "permanent" (définitif).
    Retourne le nombre d'éléments supprimés (ou qui le seraient en dry-run).
    """
    permanent = (delete_mode == "permanent")
    n_deleted = 0
    for name, info in remote_items.items():
        if name in local_names:
            continue  # existe encore localement -> on garde
        remote_path = remote_folder.rstrip("/") + "/" + name
        rtype = info.get("type")
        if rtype == "folder":
            # Dossier entier disparu localement : on le supprime en entier.
            n_sub = _count_remote_recursive(remote_path) if verbose else None
            if verbose and n_sub is not None:
                print(_("    (orphan folder: {n} — ~{c} remote element(s))").format(n=name, c=n_sub))
            if remote_trash(remote_path, permanent=permanent, dry_run=dry_run):
                n_deleted += 1
        else:
            if remote_trash(remote_path, permanent=permanent, dry_run=dry_run):
                n_deleted += 1
    return n_deleted


def _delete_guard_ok(source_path, source_kind, verbose=False):
    """Vérifie via mount_check qu'une suppression est sûre pour cette source.
    Retourne (ok, raison). Si mount_check est absent, refuse par prudence."""
    if not _HAS_MOUNT_CHECK:
        return False, ("module mount_check.py absent — suppression refusée par "
                       "sécurité (place mount_check.py à côté de proton_sync.py)")
    return mount_check.source_is_safe_for_delete(source_path, source_kind, verbose=verbose)


def _wipe_mapping_remote(mapping, dry_run=False, verbose=False):
    """Envoie à la CORBEILLE (jamais définitif, quel que soit delete_mode) le
    dossier distant d'un mapping, en préalable à une reconstruction (option de la
    réinitialisation). Retourne True si le distant est propre à l'issue (vidé ou
    déjà absent), False si le vidage a été refusé ou a échoué.

    Sécurité : conditionné par le MÊME garde-fou de montage que les suppressions.
    Le vidage précède le re-téléversement ; si la source locale n'est pas saine
    (non montée), on REFUSE — sinon on effacerait le distant sans pouvoir le
    reconstruire. Idempotent : si le dossier distant n'existe pas (déjà vidé, ou
    mapping jamais téléversé), c'est un no-op silencieux (pas une erreur)."""
    source = mapping["source"]
    ok, raison = _delete_guard_ok(source, mapping.get("source_kind"), verbose=verbose)
    if not ok:
        print(_("  ⛔ Remote wipe REFUSED (mount guard): {r}").format(r=raison))
        print(_("     Nothing deleted remotely — this mapping was NOT reset."))
        return False
    remote_folder = (mapping["dest_parent"].rstrip("/") + "/"
                     + os.path.basename(source.rstrip("/")))
    if not remote_exists(remote_folder):
        print(_("  ♻ No remote folder to wipe (already absent): {p}").format(p=remote_folder))
        return True
    print(_("  🗑  Wiping remote folder to TRASH: {p}").format(p=remote_folder))
    # Toujours corbeille (permanent=False) : état transitoire, filet de 30 j si le
    # re-téléversement échoue. L'utilisateur purgera la corbeille lui-même après
    # avoir vérifié le succès.
    return remote_trash(remote_folder, permanent=False, dry_run=dry_run)


def sync_folder(local_dir, remote_parent, dry_run=False, verbose=False, verify_hash=False,
                cache=None, ignore_cache=False, exclusions=None,
                delete=False, delete_mode="trash", realtime=False, rename_ext=True,
                collision_suffix=_EXT_COLLISION_SUFFIX_DEFAULT):
    # Retourne True si CE dossier ET toute sa descendance ont été analysés
    # jusqu'au bout sans échec (subtree_complete) ; False sinon. Cette valeur
    # « remonte » de bas en haut : un parent n'est complet que si tous ses enfants
    # non exclus le sont. Elle est stockée dans le cache (champ subtree_complete)
    # et sert de feu vert au temps réel (voir sync_subpath).
    folder_name = os.path.basename(local_dir.rstrip("/"))
    remote_folder = remote_parent.rstrip("/") + "/" + folder_name

    # Ligne de PROGRESSION : le dossier local en cours de balayage, une par dossier,
    # dans l'ordre exact où le moteur descend l'arbre (tous les dossiers, y compris
    # ceux sautés par cache). C'est la SEULE chose que l'épuré affiche par dossier —
    # le GUI n'a plus rien à deviner. Pas de _() : uniquement un emoji + un chemin,
    # rien à traduire.
    print("📂 " + local_dir)

    # Liste des entrées locales (faite une fois pour les deux branches cache hit/miss).
    try:
        entries = list(os.scandir(local_dir))
    except OSError as e:
        print(_("  ❌ Could not read {p}: {e}").format(p=local_dir, e=e))
        return False   # illisible -> sous-arbre non complet

    # Normalisation des extensions MAJUSCULES -> minuscules sur les fichiers directs
    # (avant tout : la signature de cache et l'upload doivent voir les noms finaux).
    # Un seul point d'injection ici -> couvre manuel, amorçage/réinitialisation ET
    # temps réel (sync_subpath passe par sync_folder). Activable/désactivable via
    # config.py (réglage persistant) ou --no-rename-ext (surcharge ponctuelle).
    if rename_ext:
        n_ren = _normalize_uppercase_ext(local_dir, entries, exclusions,
                                         dry_run=dry_run, verbose=verbose,
                                         collision_suffix=collision_suffix)
        if n_ren:
            # Des fichiers ont été renommés -> re-scanner pour repartir des noms finaux.
            try:
                entries = list(os.scandir(local_dir))
            except OSError as e:
                print(_("  ❌ Could not read {p}: {e}").format(p=local_dir, e=e))
                return False

    # Ensemble des noms locaux NON exclus (sert à détecter les orphelins distants).
    local_names = set()
    for entry in entries:
        if exclusions and exclusions.is_excluded(entry.name):
            continue
        local_names.add(entry.name)

    # Tentative de saut via le cache.
    #
    # Mode normal (sans --delete) : on saute si l'empreinte locale est inchangée
    # (is_fresh). Le cache évite l'appel `filesystem list`, coûteux.
    #
    # Mode --delete : on ne peut sauter QUE si, en plus de l'empreinte inchangée,
    # le dossier a déjà été réconcilié côté distant lors d'un passage --delete
    # précédent (delete_synced). Raison : une suppression locale change toujours
    # l'empreinte ; donc empreinte inchangée + déjà réconcilié => aucun orphelin
    # possible, saut sûr. En revanche, un dossier mis en cache par un passage
    # SANS --delete n'est pas "delete_synced" : il faut vérifier le distant au
    # moins une fois en mode --delete pour propager d'éventuelles suppressions
    # antérieures. Résultat : le 1er passage --delete vérifie tout (plus lent),
    # les suivants sautent les dossiers inchangés (rapides).
    # Empreinte d'exclusions injectée dans la signature.
    #   - Passage COMPLET (planifié/manuel) : empreinte COURANTE -> un changement
    #     d'exclusions périme delete_synced et force la réconciliation (nettoyage
    #     des orphelins nouvellement exclus). C'est le rôle des passages complets.
    #   - Passage TEMPS RÉEL (--subpath, realtime=True) : empreinte DÉJÀ EN CACHE
    #     pour ce dossier (Option A). Ainsi un changement d'exclusions ne périme
    #     pas delete_synced ici -> pas de réconciliation lourde en plein temps
    #     réel (qui bloquait le verrou et provoquait des collisions). Le filtrage
    #     des NOUVEAUX uploads, lui, reste immédiat (il ne dépend pas du cache mais
    #     de is_excluded, évalué à chaque passage). Une vraie suppression locale
    #     change le CONTENU de l'empreinte et reste donc propagée normalement.
    if realtime and cache is not None:
        excl_fp = cache.cached_excl(local_dir)
    else:
        excl_fp = exclusions.fingerprint() if exclusions else None
    signature = (_local_signature(local_dir, remote_folder, excl_fp)
                 if cache is not None else None)
    if delete:
        can_skip = (
            cache is not None
            and not ignore_cache
            and not verify_hash
            and signature is not None
            and cache.is_delete_synced(local_dir, signature)
        )
    else:
        can_skip = (
            cache is not None
            and not ignore_cache
            and not verify_hash
            and signature is not None
            and cache.is_fresh(local_dir, signature)
        )

    if can_skip:
        if verbose:
            tag = "cache valide + delete réconcilié" if delete else "cache valide"
            print(_("  ⚡ {t}, skipping the CLI call for {p}").format(t=tag, p=local_dir))
        all_children_complete = True
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                if exclusions and exclusions.is_excluded(entry.name):
                    if verbose:
                        print(_("  🚫 excluded (folder): {p}").format(p=entry.path))
                    continue
                child_complete = sync_folder(
                    entry.path, remote_folder, dry_run=dry_run, verbose=verbose,
                    verify_hash=verify_hash, cache=cache, ignore_cache=ignore_cache,
                    exclusions=exclusions, delete=delete, delete_mode=delete_mode,
                    realtime=realtime, rename_ext=rename_ext,
                    collision_suffix=collision_suffix)
                if not child_complete:
                    all_children_complete = False
        # Le dossier lui-même est valide (cache frais) ; sa complétude de sous-arbre
        # = tous ses enfants complets. On met à jour le champ (permet de « promouvoir »
        # à complet un dossier hérité/interrompu dont les enfants viennent de finir).
        # Hors dry-run : le dry-run ne touche jamais au cache.
        if cache is not None and not dry_run and signature is not None:
            cache.update(local_dir, signature,
                         subtree_complete=all_children_complete)
            cache.maybe_save()
        return all_children_complete

    # Chemin normal : on s'assure que le dossier distant existe, puis on liste.
    if not dry_run:
        ensure_remote_path(remote_folder)

    remote_items = get_remote_listing(remote_folder, verbose=verbose)

    to_upload = []
    had_failure = False
    all_children_complete = True
    for entry in entries:
        if exclusions and exclusions.is_excluded(entry.name):
            if verbose:
                kind = "dossier" if entry.is_dir(follow_symlinks=False) else "fichier"
                print(_("    🚫 excluded ({k}): {p}").format(k=kind, p=entry.path))
            continue
        if entry.is_dir(follow_symlinks=False):
            child_complete = sync_folder(
                entry.path, remote_folder, dry_run=dry_run, verbose=verbose,
                verify_hash=verify_hash, cache=cache, ignore_cache=ignore_cache,
                exclusions=exclusions, delete=delete, delete_mode=delete_mode,
                realtime=realtime, rename_ext=rename_ext,
                    collision_suffix=collision_suffix)
            if not child_complete:
                all_children_complete = False
        else:
            info = remote_items.get(entry.name)
            if needs_upload(entry.path, info, verbose=verbose, verify_hash=verify_hash):
                to_upload.append(entry.path)
            elif verbose:
                print(_("    ⏭  unchanged: {p}").format(p=entry.path))

    upload_ok = upload_batch(to_upload, remote_folder, dry_run=dry_run, verbose=verbose)
    if not upload_ok:
        had_failure = True

    # --- Propagation des suppressions (si activée pour ce mapping) ---
    deleted_something = False
    if delete:
        n = delete_orphans(local_dir, remote_folder, remote_items, local_names,
                           delete_mode=delete_mode, dry_run=dry_run, verbose=verbose)
        deleted_something = bool(n)

    # --- Mise à jour du cache ---
    # Hors dry-run et si pas d'échec d'upload :
    #   - en mode --delete, ce dossier vient d'être réconcilié côté distant
    #     (orphelins traités) -> on le marque delete_synced=True. Au prochain
    #     passage --delete, s'il n'a pas changé, il sera sauté (rapide).
    #   - hors --delete, on met à jour l'empreinte mais SANS prétendre avoir
    #     réconcilié les suppressions (delete_synced laissé tel quel / False),
    #     pour qu'un futur --delete vérifie quand même le distant.
    #   - subtree_complete : ce dossier est complet SSI il a été traité sans échec
    #     ET tous ses enfants non exclus sont complets. C'est le signal « prêt pour
    #     le temps réel ». En cas d'échec (had_failure), on ne l'écrit pas (le
    #     dossier n'est pas mis en cache du tout dans ce cas -> non complet).
    subtree_ok = (not had_failure) and all_children_complete
    if cache is not None and not dry_run and not had_failure and signature is not None:
        if delete:
            cache.update(local_dir, signature, delete_synced=True,
                         subtree_complete=subtree_ok)
        else:
            cache.update(local_dir, signature, delete_synced=None,
                         subtree_complete=subtree_ok)
        # Persistance périodique : écrit la progression sur disque au fil de l'eau
        # (throttlée) pour qu'une interruption ne perde que ~quelques secondes de
        # travail, au lieu de tout le mapping. Sans dry-run (le dry-run ne touche
        # jamais au cache).
        cache.maybe_save()
    return subtree_ok


def sync_folder_guarded(mapping, local_dir, remote_parent, dry_run=False, verbose=False,
                        verify_hash=False, cache=None, ignore_cache=False, exclusions=None,
                        delete=False, rename_ext=True,
                        collision_suffix=_EXT_COLLISION_SUFFIX_DEFAULT):
    """Enveloppe sync_folder en appliquant le GARDE-FOU de suppression.

    Si --delete est demandé ET que ce mapping autorise la suppression
    (allow_delete), on vérifie d'abord que la source est saine (montage NFS
    vivant pour une source 'nfs', etc.). Si le garde-fou refuse, on désactive la
    suppression pour ce mapping (les uploads continuent) et on journalise.
    """
    mapping_delete = delete and bool(mapping.get("allow_delete"))
    mode = mapping.get("delete_mode", "trash")

    if mapping_delete:
        source_kind = mapping.get("source_kind")
        ok, raison = _delete_guard_ok(local_dir, source_kind, verbose=verbose)
        if not ok:
            print(_("  ⚠  Deletions disabled for this mapping: {r}").format(r=raison))
            print(_("      (uploads continue normally)"))
            mapping_delete = False
        else:
            label = _("permanent") if mode == "permanent" else _("to trash")
            print(_("  🗑  Deletion propagation ACTIVE ({l}) for this mapping").format(l=label))

    sync_folder(local_dir, remote_parent, dry_run=dry_run, verbose=verbose,
                verify_hash=verify_hash, cache=cache, ignore_cache=ignore_cache,
                exclusions=exclusions, delete=mapping_delete, delete_mode=mode,
                rename_ext=rename_ext, collision_suffix=collision_suffix)


def sync_file(local_file, remote_parent, dry_run=False, verbose=False, verify_hash=False,
              cache=None, ignore_cache=False, exclusions=None):
    # Pour un fichier unique, le coût de l'appel `list` du dossier parent est
    # déjà minime (un seul list pour un seul fichier à vérifier). On garde la
    # logique simple sans cache.
    if exclusions and exclusions.is_excluded(os.path.basename(local_file)):
        if verbose:
            print(_("    🚫 excluded (file): {p}").format(p=local_file))
        return
    if not dry_run:
        ensure_remote_path(remote_parent)
    remote_items = get_remote_listing(remote_parent, verbose=verbose)
    info = remote_items.get(os.path.basename(local_file))
    if needs_upload(local_file, info, verbose=verbose, verify_hash=verify_hash):
        upload_batch([local_file], remote_parent, dry_run=dry_run, verbose=verbose)
    elif verbose:
        print(_("    ⏭  unchanged: {p}").format(p=local_file))


def _remote_parent_for_subpath(mapping, subpath):
    """Calcule le parent distant correspondant à un sous-dossier local.

    Le mapping synchronise `source` vers `dest_parent/basename(source)` (c'est
    ce que fait sync_folder : il ajoute le nom du dossier au parent). Pour un
    sous-dossier `source/a/b/c`, le dossier `c` doit donc être placé sous
    `dest_parent/basename(source)/a/b`. On renvoie ce parent distant ; sync_folder
    y ajoutera lui-même le segment final `c`.

    Retourne (remote_parent, None) en cas de succès, ou (None, raison) si le
    sous-chemin n'est pas valide (hors du mapping, etc.).
    """
    source = os.path.normpath(mapping["source"])
    sub = os.path.normpath(subpath)

    # Sécurité : le sous-chemin DOIT être à l'intérieur de la source du mapping.
    # On compare segment par segment pour éviter qu'un préfixe trompeur passe
    # (ex. /media/nas1/Doc vs /media/nas1/Documents).
    if sub != source:
        prefix = source.rstrip("/") + "/"
        if not (sub + "/").startswith(prefix):
            return None, f"sous-chemin hors du mapping : {sub} n'est pas sous {source}"

    # Portion relative entre la source et le sous-chemin (ex. "a/b/c", ou "" si
    # le sous-chemin EST la racine du mapping).
    rel = os.path.relpath(sub, source)
    if rel == ".":
        rel = ""

    # Racine distante du mapping = dest_parent/basename(source).
    source_base = os.path.basename(source.rstrip("/"))
    remote_root = mapping["dest_parent"].rstrip("/") + "/" + source_base

    if not rel:
        # Le sous-chemin est la racine du mapping : parent distant = dest_parent
        # (sync_folder ajoutera basename(source) -> remote_root). On reproduit
        # exactement le comportement d'un mapping entier.
        return mapping["dest_parent"], None

    # Sinon, le parent distant du sous-dossier = remote_root + (rel sans son
    # dernier segment). sync_folder ajoutera le dernier segment lui-même.
    rel_parent = os.path.dirname(rel)  # "" si rel n'a qu'un segment
    if rel_parent:
        remote_parent = remote_root + "/" + rel_parent
    else:
        remote_parent = remote_root
    return remote_parent, None


def _first_excluded_segment(mapping, subpath, exclusions):
    """Retourne le premier segment EXCLU du sous-chemin (relatif à la racine du
    mapping), ou None. On teste chaque niveau SOUS la racine — pas seulement le
    dernier — pour attraper un ANCÊTRE exclu.

    Exemple : cible `.../Photographies/.Trash-1000/info`, racine du mapping
    `.../Photographies`. Le dernier segment est `info` (anodin), mais l'ancêtre
    `.Trash-1000` matche le motif `.Trash-*` → on doit sauter. Ne tester que le
    basename (ancienne version) laissait passer ce cas : on entrait SOUS la
    corbeille sans qu'aucun segment testé ne matche.

    On ne teste pas les segments de la racine elle-même (pour ne pas exclure par
    accident un dossier parent légitime du mapping)."""
    if not exclusions:
        return None
    try:
        source = os.path.normpath(mapping["source"])
    except (KeyError, TypeError):
        return None
    sub = os.path.normpath(subpath)
    rel = os.path.relpath(sub, source)
    if rel in (".", ""):
        # Le sous-chemin EST la racine du mapping : on teste juste son nom.
        base = os.path.basename(sub)
        return base if exclusions.is_excluded(base) else None
    if rel.startswith(".."):
        # Hors du mapping (ne devrait pas arriver) : on ne juge rien ici.
        return None
    for seg in rel.split(os.sep):
        if seg and exclusions.is_excluded(seg):
            return seg
    return None


def sync_subpath(mapping, subpath, dry_run=False, verbose=False, verify_hash=False,
                 cache=None, ignore_cache=False, exclusions=None, delete=False,
                 rename_ext=True, collision_suffix=_EXT_COLLISION_SUFFIX_DEFAULT):
    """Synchronise UN sous-dossier précis d'un mapping (mode temps réel).

    sync_folder étant récursive, le sous-dossier ET son sous-arbre sont traités.

    delete : interrupteur MAÎTRE de la propagation des suppressions pour ce
    passage (passé par le consommateur temps réel quand l'événement source était
    une suppression). La suppression ne se produit QUE si delete=True ET que le
    mapping a allow_delete=True ET que le garde-fou de montage valide la source
    (mêmes règles que sync_folder_guarded). Le mode (corbeille/définitif) est
    celui du mapping ('delete_mode'). Si le garde-fou refuse, on désactive la
    suppression pour ce passage mais on continue les uploads.
    """
    if not os.path.isdir(subpath):
        print(_("  ❌ Subpath not found or not a folder: {p}").format(p=subpath))
        return

    # Garde-fou d'exclusion sur le SOUS-CHEMIN. sync_folder ne teste les
    # exclusions que sur les ENFANTS d'un dossier parcouru ; un sous-chemin ciblé
    # DIRECTEMENT par le watcher (ex. __pycache__, ou .Trash-1000/info) passerait
    # donc au travers. On teste ici CHAQUE segment du sous-chemin sous la racine
    # du mapping : si l'un d'eux est exclu (nom exact OU motif) — la cible ou un
    # de ses ancêtres — on saute proprement (ni upload, ni création distante, ni
    # suppression). C'est ce qui aligne le temps réel sur le parcours batch, qui
    # saute déjà ces dossiers en descendant.
    seg = _first_excluded_segment(mapping, subpath, exclusions)
    if seg is not None:
        print("  🚫 [subpath-excluded] " + _("subpath excluded (“{s}” filtered), skipped: {p}").format(s=seg, p=subpath))
        return

    # Garde-fou temps réel : le temps réel synchronise le QUOTIDIEN (changements
    # ciblés) mais ne BÂTIT jamais l'index d'un gros sous-arbre encore inconnu —
    # ce travail exhaustif revient à la planification. On autorise le traitement
    # SEULEMENT si l'endroit visé est déjà entièrement analysé :
    #   - si le sous-chemin visé EST la racine du mapping (cas typique d'un marqueur
    #     de rattrapage) -> on exige que cette racine soit `subtree_complete` ;
    #   - sinon -> on exige que le PARENT du sous-chemin soit complet. Ainsi un
    #     dossier NOUVELLEMENT créé (jamais analysé) dans un mapping déjà complet
    #     est traité en temps réel (son parent est complet), tandis qu'un dossier
    #     dans un mapping encore partiellement analysé est différé.
    # Si l'endroit de référence n'est pas complet, on DÉLÈGUE à la planification :
    # sortie en code 3 (« pas encore analysé — différé »), le consommateur CONSERVE
    # le marqueur et réessaiera ; dès qu'un passage complet aura analysé l'arbre, il
    # deviendra traitable. `--ignore-cache` court-circuite ce garde-fou.
    if cache is not None and not ignore_cache:
        source = os.path.normpath(mapping.get("source", ""))
        target = os.path.normpath(subpath)
        ref = target if target == source else os.path.dirname(target)
        if not cache.subtree_complete(ref):
            print("  ⏳ [subpath-cold] " + _("folder not fully indexed yet "
                  "(cache not built by a full pass), deferred to scheduling: {p}")
                  .format(p=subpath))
            return "cold"

    remote_parent, raison = _remote_parent_for_subpath(mapping, subpath)
    if remote_parent is None:
        print(f"  ❌ {raison}")
        return

    # Double interrupteur : delete (du passage) ET allow_delete (du mapping).
    mapping_delete = delete and bool(mapping.get("allow_delete"))
    mode = mapping.get("delete_mode", "trash")

    if mapping_delete:
        # Garde-fou de montage STRICT avant toute suppression (même logique que
        # sync_folder_guarded) : si la source n'est pas saine (NAS tombé, montage
        # effondré), on désactive la suppression mais on laisse les uploads.
        source_kind = mapping.get("source_kind")
        ok, raison_gf = _delete_guard_ok(subpath, source_kind, verbose=verbose)
        if not ok:
            print(_("  ⚠  Deletions disabled for this subpath: {r}").format(r=raison_gf))
            print(_("      (uploads continue normally)"))
            mapping_delete = False
        else:
            label = _("permanent") if mode == "permanent" else _("to trash")
            print(_("  🗑  Deletion propagation ACTIVE ({l}) for this subpath").format(l=label))

    print(_("  ↪ subpath: {s}  =>  {d}").format(s=subpath, d=remote_parent))
    sync_folder(subpath, remote_parent, dry_run=dry_run, verbose=verbose,
                verify_hash=verify_hash, cache=cache, ignore_cache=ignore_cache,
                exclusions=exclusions, delete=mapping_delete, delete_mode=mode,
                realtime=True, rename_ext=rename_ext,
                collision_suffix=collision_suffix)


def main():
    # Forcer l'écriture immédiate des messages (pas de mise en mémoire tampon).
    # Ainsi, quand la sortie est redirigée vers un fichier journal via `tee`,
    # les messages y apparaissent en temps réel plutôt que par gros blocs.
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass  # vieux Python ou flux non reconfigurable : sans gravité

    parser = argparse.ArgumentParser(description="Moteur de synchro Proton Drive (NAS -> Proton, sens unique)")
    parser.add_argument("config", nargs="?", default=None,
                        help="Fichier JSON de mappings (optionnel pour les sondes "
                             "--check-auth / --check-lock qui ne lisent pas les mappings)")
    parser.add_argument("--dry-run", action="store_true", help="Affiche ce qui serait fait, sans rien transférer")
    parser.add_argument(
        "--verify-hash", action="store_true",
        help="Vérifie aussi le contenu par SHA1 (plus lent, lit chaque fichier en entier). "
             "Équivalent du /IS mensuel de robocopy — à utiliser occasionnellement, pas au quotidien. "
             "Ignore aussi le cache local.",
    )
    parser.add_argument(
        "--ignore-cache", action="store_true",
        help="Ignore le cache local pour ce passage (force la revérification complète "
             "côté Proton). Le cache reste à jour à la fin si le passage réussit.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument(
        "--delete", action="store_true",
        help="Interrupteur MAÎTRE de la propagation des suppressions. Sans ce flag, "
             "AUCUNE suppression n'est faite (comportement additif, défaut sûr). Avec "
             "ce flag, chaque mapping qui a 'allow_delete: true' propage vers Proton "
             "les suppressions locales (ce qui est sur Proton mais absent localement "
             "est supprimé). Le mode de suppression (corbeille ou définitif) est celui "
             "défini dans chaque mapping ('delete_mode'). Un garde-fou vérifie d'abord "
             "que la source est saine (montage NFS vivant) ; sinon les suppressions "
             "sont désactivées pour ce mapping.",
    )
    parser.add_argument(
        "--subpath", metavar="CHEMIN",
        help="Mode TEMPS RÉEL : synchronise uniquement ce sous-dossier (et son "
             "sous-arbre) au lieu de tous les mappings. Nécessite --mapping-source "
             "pour identifier le mapping auquel ce sous-chemin appartient. Purement "
             "additif (ne propage jamais les suppressions). Utilisé par le "
             "déclencheur inotify ; sans cette option, le moteur traite tous les "
             "mappings normalement.",
    )
    parser.add_argument(
        "--mapping-source", metavar="CHEMIN",
        help="Avec --subpath : le champ 'source' du mapping concerné (sert à "
             "retrouver le mapping et à calculer la destination distante).",
    )
    parser.add_argument(
        "--check-auth", action="store_true",
        help="Sonde UNIQUEMENT l'authentification (le trousseau est-il "
             "déverrouillé ?) puis sort : code 0 = OK, code 2 = verrouillé. Ne "
             "prend pas le verrou, ne synchronise rien, ne touche pas au cache. "
             "Utilisé par le consommateur temps réel pour éviter de lancer des "
             "passages voués au code 2 quand la session n'est pas ouverte.",
    )
    parser.add_argument(
        "--only-source", action="append", default=None, metavar="CHEMIN",
        help="Restreint le passage COMPLET aux mappings dont la 'source' est "
             "listée (répétable). Sert à l'amorçage ciblé du cache depuis le GUI : "
             "on consolide seulement les mappings choisis, en gardant la sémantique "
             "passage complet (marque subtree_complete). Sans cette option : tous "
             "les mappings sont traités.",
    )
    parser.add_argument(
        "--reset-source", action="append", default=None, metavar="CHEMIN",
        help="RÉINITIALISE le(s) mapping(s) dont la 'source' est listée (répétable) : "
             "purge d'abord les entrées de cache du sous-arbre (le mapping retombe "
             "en attente / non complété), PUIS reconstruit via un passage --delete "
             "ciblé — identique à l'amorçage, donc le cache ressort armé "
             "(subtree_complete + delete_synced). Idempotent : rejouable autant de "
             "fois que voulu. Sert à repartir propre après un effacement du contenu "
             "distant (ex. fichiers au mauvais type MIME téléversés hors CLI). "
             "Implique --delete et restreint le passage à ces mappings (pas besoin "
             "de --only-source).",
    )
    parser.add_argument(
        "--wipe-remote", action="store_true",
        help="Avec --reset-source UNIQUEMENT : avant de reconstruire, envoie à la "
             "CORBEILLE (jamais définitif, quel que soit delete_mode) le dossier "
             "distant de chaque mapping réinitialisé. Sous garde-fou de montage "
             "(refusé si la source locale n'est pas saine). Idempotent : sans effet "
             "si le dossier distant est déjà absent. La corbeille Proton (30 j) sert "
             "de filet ; purge-la toi-même après avoir vérifié le re-téléversement.",
    )
    parser.add_argument(
        "--accept-account-change", action="store_true",
        help="Accepte explicitement un CHANGEMENT de compte Proton : si le cache "
             "a été bâti pour un autre compte que celui connecté, il est écarté "
             "et reconstruit pour le nouveau compte au fil du passage (les "
             "destinations seront (re)créées sur le nouveau Drive). Sans ce "
             "drapeau, le moteur REFUSE le passage (code 4) pour éviter un "
             "re-téléversement massif involontaire. Passé automatiquement par "
             "les actions « Amorcer le cache » et « Réinitialiser » du GUI.")
    parser.add_argument(
        "--no-rename-ext", action="store_true",
        help="DÉSACTIVE la normalisation des extensions pour CE passage, quel que "
             "soit le réglage persistant (config.py / GUI). Par défaut, le moteur "
             "renomme les fichiers source dont l'extension finale contient des "
             "majuscules -> extension en minuscule (IMG.JPG -> IMG.jpg, DOC.PDF -> "
             "DOC.pdf), pour que Proton détecte le bon type MIME (vignette, aperçu, "
             "icône) et que le cache reste cohérent (local = distant). En cas de "
             "collision avec une cible existante, on n'écrase jamais (suffixe "
             "configurable, voir rename_ext_collision_suffix dans settings.json). "
             "Ne touche pas aux dossiers ni aux fichiers exclus. Chaque renommage "
             "est journalisé (renamed-extensions.log).",
    )
    parser.add_argument(
        "--check-lock", action="store_true",
        help="Sonde UNIQUEMENT le verrou : un autre passage tourne-t-il déjà ? "
             "Sort code 0 = verrou libre, code 1 = verrou tenu. Ne synchronise "
             "rien, ne touche pas au cache, relâche immédiatement le verrou. "
             "Utilisé par le consommateur temps réel pour éviter de lancer des "
             "passages voués à l'échec code 1 quand un lancement manuel/planifié "
             "tient le verrou.",
    )
    args = parser.parse_args()

    # --wipe-remote n'a de sens qu'avec --reset-source (il vide le distant AVANT la
    # reconstruction). Le refuser seul évite un effacement distant hors du cadre
    # sécurisé de la réinitialisation.
    if args.wipe_remote and not args.reset_source:
        print(_("❌ --wipe-remote requires --reset-source."))
        sys.exit(2)

    # Normalisation des extensions : réglage PERSISTANT (config.py / GUI), avec
    # --no-rename-ext comme surcharge ponctuelle qui force TOUJOURS l'arrêt pour
    # ce passage, quel que soit le réglage. Résolu une seule fois ici, propagé
    # à tous les appels du passage.
    if _HAS_CONFIG:
        effective_rename_ext = appconfig.rename_ext_enabled() and not args.no_rename_ext
        effective_collision_suffix = appconfig.rename_ext_collision_suffix()
    else:
        effective_rename_ext = not args.no_rename_ext
        effective_collision_suffix = _EXT_COLLISION_SUFFIX_DEFAULT

    # Sonde d'authentification pure (--check-auth) : réutilise EXACTEMENT le même
    # test que le passage normal (check_auth ci-dessus), mais sans prendre le
    # verrou (c'est une simple lecture) et sans rien synchroniser. Placée AVANT
    # l'acquisition du verrou, exprès : sinon la sonde échouerait dès qu'un vrai
    # passage tourne, ce qui n'a rien à voir avec l'état du trousseau.
    if args.check_auth:
        if not os.path.exists(CLI):
            print(_("❌ proton-drive binary not found at {p}").format(p=CLI))
            sys.exit(2)
        ok, _err = check_auth()
        sys.exit(0 if ok else 2)

    # Sonde de verrou pure (--check-lock) : teste EXACTEMENT le même flock, sur le
    # même LOCK_FILE, que l'acquisition réelle ci-dessous — mais le relâche aussitôt
    # et ne synchronise rien. code 0 = libre, code 1 = tenu par un autre passage.
    if args.check_lock:
        probe_fp = open(LOCK_FILE, "w")
        try:
            fcntl.flock(probe_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit(1)          # verrou tenu par un autre passage
        else:
            fcntl.flock(probe_fp, fcntl.LOCK_UN)   # libre : on relâche tout de suite
            probe_fp.close()
            sys.exit(0)

    # À partir d'ici, on fait un VRAI passage : le fichier de mappings est requis
    # (les sondes --check-auth / --check-lock ci-dessus sont déjà sorties sans lui).
    if not args.config:
        print(_("❌ A mappings file is required (config).") )
        sys.exit(2)

    # Verrou : empêche qu'une seconde instance démarre alors qu'une première
    # tourne encore (ex. cron qui se déclenche pendant un passage manuel, ou
    # deux planifications qui se chevauchent). flock est libéré automatiquement
    # par l'OS à la fin du processus, propre ou brutale (kill, Ctrl+C, crash) —
    # pas de risque de verrou orphelin.
    lock_fp = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(_("❌ Another instance of proton_sync.py is already running."))
        print(_("   Lock: {p}").format(p=LOCK_FILE))
        print(_("   (If you are sure no other instance is running, delete this file.)"))
        sys.exit(1)

    if not os.path.exists(CLI):
        print(_("❌ proton-drive binary not found at {p}").format(p=CLI))
        print(_("   Adjust the PROTON_DRIVE_CLI environment variable if needed."))
        sys.exit(1)

    # Vérification d'authentification AVANT tout traitement. Si le trousseau
    # n'est pas déverrouillé (session non ouverte au moment d'un déclenchement
    # automatique), on sort proprement avec un code distinct (2) et un message
    # clair, sans rien tenter d'uploader, sans toucher au cache. Le verrou est
    # libéré automatiquement à la sortie, et la tâche planifiée réessaiera plus
    # tard (ou dès que la session sera rouverte). Sonde TOLÉRANTE (reprise
    # 2,5 s) : un faux négatif transitoire — ex. une sonde du GUI qui tient le
    # trousseau à l'instant précis du démarrage — ne fait plus échouer le
    # passage ; un vrai verrouillage, lui, persiste sur les deux essais.
    ok, auth_err = check_auth_settled()
    if not ok:
        print("⚠ [auth-failed] " + _("Proton Drive authentication impossible — pass skipped."))
        print(_("   Likely cause: the graphical session is not open, so the"))
        print(_("   secrets keyring is locked and the CLI cannot read its"))
        print(_("   credentials."))
        if auth_err:
            print(_("   CLI detail: {e}").format(e=auth_err))
        print(_("   (This is not a serious error: the next run will retry."))
        print(_("    Open this user's session to unlock the keyring.)"))
        sys.exit(2)

    mappings, global_ex = load_config(args.config)

    # Le cache est indexé par le nom du fichier de mappings (sans son chemin).
    # Comme ça, déplacer le JSON ne casse pas le cache.
    cache_name = os.path.basename(args.config).replace(".json", "") + ".cache"
    cache_path = os.path.join(CACHE_DIR, cache_name)
    cache = Cache(cache_path)
    # Sauvegarder le cache à la sortie (normale, Ctrl+C, exception non gérée).
    # En cas de kill -9 on perd les màj de ce passage, mais le cache du passage
    # précédent reste intact (écriture atomique via tmp+rename).
    atexit.register(cache.save)

    # ── GARDE-FOU changement de COMPTE Proton ──────────────────────────────
    # Le cache décrit le Drive du compte qui l'a bâti (estampille __meta__).
    # Si le compte connecté a CHANGÉ, faire confiance au cache produirait une
    # sauvegarde silencieusement incomplète (« déjà à jour » pour des dossiers
    # absents du nouveau Drive) — et l'ignorer re-téléverserait TOUT sans
    # prévenir (heures de transfert déclenchées par un timer). Par défaut, le
    # moteur REFUSE donc le passage (code 4, rien touché) ; l'acceptation
    # explicite (--accept-account-change, passée par Amorcer/Réinitialiser du
    # GUI) écarte l'ancien cache et repart proprement sur le nouveau compte.
    # Cache jamais estampillé (anciens caches) : estampillé avec le compte
    # courant, sans ré-amorçage — installations existantes intactes.
    current_account = get_account_email()
    stamped_account = cache.account()
    if stamped_account and current_account and stamped_account != current_account:
        if args.accept_account_change:
            print("⚠ [account-changed] " + _("Account change accepted "
                  "({a} → {b}) — the previous cache is discarded and will be "
                  "rebuilt for the new account.").format(
                  a=stamped_account, b=current_account))
            cache.data = {Cache.META_KEY: {"account": current_account}}
            cache.dirty = True
        else:
            print("⚠ [account-changed] " + _("The connected Proton account has "
                  "changed: this cache was built for {a}, but the session is "
                  "now {b}.").format(a=stamped_account, b=current_account))
            print(_("   Nothing was synced or modified — the cache describes "
                    "the OLD account's Drive."))
            print(_("   To proceed on the NEW account: run “Prime the cache” "
                    "(or reset the mappings). The destinations will be "
                    "(re)created on the new Drive."))
            sys.exit(4)
    elif current_account and not stamped_account:
        cache.set_account(current_account)

    # Sauvegarde sur SIGTERM/SIGINT. `atexit` ne s'exécute PAS sur un SIGTERM par
    # défaut (le processus est tué sans lever d'exception) — or SIGTERM est
    # justement ce qu'envoient `systemctl stop`, un reboot, ou l'arrêt du service
    # consommateur. Sans ce handler, la progression écrite en mémoire pendant un
    # long parcours (delete_synced par sous-dossier) serait perdue. On écrit le
    # cache puis on quitte proprement (systemd laisse ~90 s avant le SIGKILL, très
    # largement le temps d'un os.replace atomique).
    def _save_and_exit(signum, frame):
        # Sauver la progression, et ne confirmer QUE si l'écriture a réussi (ne
        # jamais promettre à tort). En cas d'échec d'écriture, on quitte sans
        # message trompeur.
        saved = False
        try:
            cache.save()
            saved = True
        except Exception:
            saved = False
        if saved:
            try:
                print("\n" + _("⏹ Interrupted — cache progress saved: resuming will "
                      "skip what is already done (no restart from zero)."), flush=True)
            except Exception:
                pass
        # 128 + n : convention shell pour « terminé par le signal n ».
        os._exit(128 + signum)
    signal.signal(signal.SIGTERM, _save_and_exit)
    signal.signal(signal.SIGINT, _save_and_exit)

    mode = _("DRY-RUN (nothing will be transferred)") if args.dry_run else _("real")
    extras = []
    if args.verify_hash:
        extras.append("verify-hash")
    if args.ignore_cache:
        extras.append("ignore-cache")
    if args.delete:
        extras.append("delete")
    extras_str = f" [{', '.join(extras)}]" if extras else ""
    print(_("== Proton Drive sync — {n} entry(ies) — {m}{x} ==").format(n=len(mappings), m=mode, x=extras_str))
    n_known = sum(1 for k in cache.data if k != Cache.META_KEY)
    print(_("   Cache: {p} ({n} known folder(s))").format(p=cache_path, n=n_known))
    if n_known > 0 and not args.ignore_cache and not args.dry_run and not args.reset_source:
        # Cache déjà peuplé : rassurer que le travail enregistré ne sera pas refait
        # (le moteur SAUTE les dossiers connus/inchangés — il ne reprend pas à un
        # curseur). Couvre notamment le cas d'une coupure de courant, où le message
        # d'interruption n'a pas pu s'afficher. (Pas pour une réinitialisation, qui
        # va justement purger le cache des mappings visés.)
        print(_("   ↺ Resuming on an existing cache — work already recorded won't "
                "be redone (unchanged folders are skipped)."))
    if global_ex:
        n_names = len(global_ex.names)
        n_pat = len(global_ex.patterns)
        print(_("   Global exclusions: {n} name(s), {p} pattern(s)").format(n=n_names, p=n_pat))
    # ── Restriction éventuelle à certains mappings (--only-source) ──────────
    # Amorçage ciblé du cache : on ne garde que les mappings choisis, tout en
    # gardant la sémantique passage complet (marque subtree_complete). Sans
    # l'option, on traite tous les mappings.
    # IMPORTANT : appliquée AVANT le message « mode SUPPRESSION » ci-dessous, pour
    # que le comptage allow_delete porte sur la PORTÉE RÉELLE du passage. Sinon,
    # amorcer un seul mapping additif afficherait quand même l'avertissement en
    # comptant les mappings miroir du reste du fichier (bug constaté).
    if args.only_source:
        wanted = {os.path.normpath(s) for s in args.only_source}
        kept = [m for m in mappings if os.path.normpath(m.get("source", "")) in wanted]
        skipped = len(mappings) - len(kept)
        mappings = kept
        print(_("   ⟳ Restricted to {n} selected mapping(s)"
                " (of {t}).").format(n=len(mappings), t=len(mappings) + skipped))

    if args.delete:
        n_del = sum(1 for m in mappings if m.get("allow_delete"))
        n_perm = sum(1 for m in mappings
                     if m.get("allow_delete") and m.get("delete_mode") == "permanent")
        # N'avertir QUE si au moins un mapping de la PORTÉE (après --only-source)
        # est réellement en miroir. --delete peut être passé globalement
        # (amorçage/reset) alors que tous les mappings visés sont additifs : dans
        # ce cas rien ne sera supprimé, et afficher « mode ELIMINATION actif »
        # serait trompeur et inutilement alarmant.
        if n_del > 0:
            print(_("   ⚠  DELETION mode active — {n} mapping(s) with allow_delete").format(n=n_del)
                  + (_(", including {n} PERMANENT").format(n=n_perm) if n_perm else ""))
            if not _HAS_MOUNT_CHECK:
                print(_("   ⚠  mount_check.py missing: ALL deletions will be "
                  "refused (safety guard)."))

    # ── Mode TEMPS RÉEL (--subpath) ────────────────────────────────────────
    # Si --subpath est fourni, on synchronise UNIQUEMENT ce sous-dossier dans le
    # mapping identifié par --mapping-source, puis on sort. Tout le reste de la
    # logique (boucle sur tous les mappings) est inchangé et ne s'exécute pas
    # dans ce mode. C'est purement additif : sans --subpath, comportement normal.
    if args.subpath:
        if not args.mapping_source:
            print(_("❌ --subpath requires --mapping-source to identify the mapping."))
            sys.exit(1)
        target_source = os.path.normpath(args.mapping_source)
        target = None
        for m in mappings:
            if os.path.normpath(m["source"]) == target_source:
                target = m
                break
        if target is None:
            print(_("❌ No mapping with source = {s}").format(s=args.mapping_source))
            sys.exit(1)
        if target["type"] != "folder":
            print(_("❌ --subpath only applies to 'folder' mappings "
                  "(this one is '{t}').").format(t=target["type"]))
            sys.exit(1)

        print(_("== REAL-TIME sync — subpath of a mapping — {m} ==").format(m=mode))
        print(_("   Mapping: {s}  =>  {d}").format(s=target["source"], d=target["dest_parent"]))
        if args.delete:
            print(_("   ⚠  Deletion propagation requested (--delete)"))
        eff_ex = mapping_exclusions(target, global_ex)
        result = sync_subpath(target, args.subpath, dry_run=args.dry_run,
                     verbose=args.verbose, verify_hash=args.verify_hash,
                     cache=cache, ignore_cache=args.ignore_cache, exclusions=eff_ex,
                     delete=args.delete, rename_ext=effective_rename_ext,
                     collision_suffix=effective_collision_suffix)
        cache.save()
        if result == "cold":
            # Sous-dossier froid : rien n'a été traité, la planification prendra
            # le relais. Code 3 = signal dédié pour le consommateur (conserver le
            # marqueur, journaliser, ne pas compter comme un échec).
            sys.exit(3)
        print("\n" + _("Done."))
        return
    # ── Fin du mode temps réel ─────────────────────────────────────────────

    # ── Réinitialisation ciblée (--reset-source [+ --wipe-remote]) ──────────
    # 1) restreint le passage aux mappings choisis ; 2) purge leur cache (retour en
    # ⏳) ; 3) vide optionnellement leur dossier distant (corbeille, sous garde-fou) ;
    # 4) force --delete pour que la reconstruction ressorte un cache ARMÉ
    # (subtree_complete + delete_synced), exactement comme l'amorçage. Idempotent.
    if args.reset_source:
        wanted = {os.path.normpath(s) for s in args.reset_source}
        kept = [m for m in mappings if os.path.normpath(m.get("source", "")) in wanted]
        skipped = len(mappings) - len(kept)
        mappings = kept
        print(_("   ♻ RESET restricted to {n} selected mapping(s)"
                " (of {t}).").format(n=len(mappings), t=len(mappings) + skipped))
        # La reconstruction active --delete globalement, MAIS le moteur le filtre
        # par mapping (`mapping_delete = delete and allow_delete`) : un mapping
        # additif ne supprime rien et n'arme pas delete_synced (inutile), un
        # mapping miroir réconcilie son distant selon SON delete_mode et ressort
        # armé. Chaque mapping est donc reconstruit selon sa propre vocation.
        args.delete = True
        for m in mappings:
            src = m.get("source", "")
            # (a) Purge du cache du sous-arbre -> le mapping repasse en ⏳ tout de
            #     suite (écriture immédiate), donc plus aucune entrée « complète »
            #     périmée ne peut égarer un passage concurrent.
            n_purged = cache.purge_subtree(src)
            print(_("   ♻ {n} cache entry(ies) purged: {s}").format(
                  n=n_purged, s=os.path.basename(src.rstrip("/"))))
            # (b) Vidage distant optionnel (corbeille), avant reconstruction. Ne
            #     s'applique qu'aux mappings de type dossier ; un mapping fichier
            #     n'a pas de « dossier distant » propre à vider.
            if args.wipe_remote and m.get("type") == "folder":
                _wipe_mapping_remote(m, dry_run=args.dry_run, verbose=args.verbose)

    for i, m in enumerate(mappings, 1):
        # En-tête par mapping : indique l'entrée en cours (source => destination).
        # Rétabli ici — la boucle normale ne l'affichait plus, contrairement au
        # mode --subpath (qui imprime « Mapping : ... »). Le compteur i/n aide à
        # suivre la progression sur un long passage.
        print(_("\n▶ Mapping {i}/{n} : {s}  =>  {d}").format(i=i, n=len(mappings), s=m["source"], d=m["dest_parent"]))
        if not os.path.exists(m["source"]):
            print(_("  ❌ Source not found, skipped: {s}").format(s=m["source"]))
            continue
        # Exclusions effectives pour ce mapping = globales + propres au mapping.
        eff_ex = mapping_exclusions(m, global_ex)
        if m["type"] == "folder":
            # sync_folder_guarded applique le garde-fou de montage avant toute
            # suppression. Le mode de suppression (corbeille/définitif) est celui
            # déclaré dans le mapping ('delete_mode') — il fait foi.
            sync_folder_guarded(m, m["source"], m["dest_parent"],
                                dry_run=args.dry_run, verbose=args.verbose,
                                verify_hash=args.verify_hash, cache=cache,
                                ignore_cache=args.ignore_cache, exclusions=eff_ex,
                                delete=args.delete, rename_ext=effective_rename_ext,
                                collision_suffix=effective_collision_suffix)
        else:
            sync_file(m["source"], m["dest_parent"], dry_run=args.dry_run, verbose=args.verbose,
                      verify_hash=args.verify_hash, cache=cache, ignore_cache=args.ignore_cache,
                      exclusions=eff_ex)
        # Checkpoint après chaque entrée du mapping : si la machine plante ou
        # qu'on reçoit un kill -9 plus tard, on garde au moins le travail des
        # mappings déjà entièrement traités. L'écriture atomique (tmp+rename)
        # garantit qu'on ne se retrouve jamais avec un cache corrompu.
        cache.save()

    print("\n" + _("Done."))


if __name__ == "__main__":
    main()

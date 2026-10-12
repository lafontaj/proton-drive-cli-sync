"""Fichier de mappings, sans interface.

Même règles que l'éditeur Tk : les deux formes JSON, clés inconnues
conservées à l'édition, écriture atomique. Les widgets ne réécrivent pas
cette politique, ils appellent ce module.
"""

import json
import os
import posixpath

from mapping_keys import carry_unknown_keys

try:
    from i18n import _
except ImportError:
    def _(s):
        return s

try:
    import config as appconfig
except ImportError:
    appconfig = None

try:
    from proton_sync import Exclusions as _EXCL
except Exception:
    _EXCL = None

WRITABLE_ROOTS = ("/my-files", "/shared-with-me")


def source_key(path):
    """Identité d'un chemin Linux (source ou clé de cache).

    posixpath, pas os.path : sous Windows, os.path.normpath transforme
    « /data » en « \\data » et le cache ne correspond plus.
    """
    text = (path or "").strip()
    if not text:
        return ""
    return posixpath.normpath(text)


def source_is_absolute(path):
    """Vrai pour un chemin Linux (« /… ») ou un chemin absolu local."""
    text = (path or "").strip()
    return text.startswith("/") or os.path.isabs(text)


def mapping_remote_path(dest_parent, source):
    """Chemin distant réellement écrit : ``dest_parent/<nom de la source>``.

    None si l'un des deux champs est vide. Même calcul que le moteur, pour
    que l'avertissement de suppression ne vise pas le dossier parent entier.
    """
    dest = (dest_parent or "").strip().rstrip("/")
    src = (source or "").strip().rstrip("/")
    if not dest or not src:
        return None
    return dest + "/" + os.path.basename(src)


def proton_location(mapping):
    """Où le dossier ou le fichier se trouve sur Proton Drive, pas son parent.

    Un dossier créé sur le site doit l'être ici pour revenir sur l'ordinateur.
    Un volume deux sens est ``dest_parent`` lui-même.
    """
    dest = (mapping.get("dest_parent") or "").strip()
    if mapping.get("direction") == "twoway" and mapping.get("volume") is True:
        return dest.rstrip("/") or dest
    return mapping_remote_path(dest, mapping.get("source", "")) or dest


def kind_label(mapping):
    """Libellé de la colonne Type : le sens se lit sans ouvrir Edit."""
    if mapping.get("direction") == "twoway":
        return _("Two-way")
    if mapping.get("type") == "file":
        return _("File, upload only")
    return _("Upload only")


class DocumentError(Exception):
    """Fichier illisible ou refus d'une édition. Le message est déjà traduit."""


def cache_dir():
    """Dossier des caches. Lu à l'appel, pas figé à l'import."""
    if appconfig is not None:
        return appconfig.CACHE_DIR
    return os.path.expanduser("~/.proton-drive-sync/cache")


def cache_path_for(config_path, directory=None):
    """Même règle que le moteur : <cache>/<nom sans .json>.cache."""
    directory = cache_dir() if directory is None else directory
    name = os.path.basename(config_path).replace(".json", "") + ".cache"
    return os.path.join(directory, name)


def read_cache(path):
    """Dict du cache, ou None s'il n'existe pas. None ≠ cache vide."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def cache_account(cache_data):
    """Compte estampillé dans __meta__.account, ou None."""
    if not isinstance(cache_data, dict):
        return None
    meta = cache_data.get("__meta__")
    return meta.get("account") if isinstance(meta, dict) else None


def atomic_write_json(path, payload):
    """Écriture tmp + fsync + os.replace. Les lecteurs voient l'ancien ou le nouveau."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def empty_exclusions():
    return {"names": [], "patterns": []}


def exclusions_active(exclusions):
    ex = exclusions or {}
    return bool(ex.get("names") or ex.get("patterns"))


def parse_lines(text):
    """Une entrée par ligne, lignes vides ignorées."""
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def destination_ok(dest):
    """Vrai seulement sous /my-files ou /shared-with-me, à la frontière d'un segment."""
    folded = (dest or "").strip().rstrip("/") or "/"
    return any(folded == root or folded.startswith(root + "/") for root in WRITABLE_ROOTS)


def edit_error(source, dest, allow_delete, source_kind):
    """Message d'erreur, ou None si les champs peuvent être enregistrés.

    N'exige pas que la source existe : un NAS non monté reste légitime.
    """
    source = (source or "").strip()
    dest = (dest or "").strip()
    if not source:
        return _("Enter a source.")
    if not source_is_absolute(source):
        return _(
            "The source must be an ABSOLUTE path (starting with “/”).\n"
            "Use “Browse…” or fix the path you typed.")
    if not dest:
        return _("Enter a destination.")
    if not destination_ok(dest):
        return _(
            "The destination must be a Proton Drive folder under\n"
            "“/my-files” or “/shared-with-me”.\n"
            "Use “Browse Proton…” to pick it.")
    if allow_delete and source_kind not in ("nfs", "local"):
        return _(
            "You enabled deletion: confirm the source type "
            "(NFS or local) before saving.")
    return None


def confirm_kind(dest, allow_delete, delete_mode):
    """Confirmation exigée avant d'enregistrer, ou None.

    ``permanent`` : suppression définitive. ``shared`` : miroir dans un
    dossier qui appartient à quelqu'un d'autre.
    """
    if not allow_delete:
        return None
    if delete_mode == "permanent":
        return "permanent"
    if (dest or "").strip().rstrip("/").startswith("/shared-with-me"):
        return "shared"
    return None


def backup_blurb(mappings):
    """Sidebar line. Saved mappings without the key stay upload-only."""
    rows = [row for row in mappings or [] if isinstance(row, dict)]
    if any(row.get("direction") == "twoway" for row in rows):
        return _("Two-way is on for those mappings only")
    if rows:
        return _("Upload only. Two-way can be turned on per mapping.")
    return _("Folder sync to Proton Drive")


def build_mapping(old, m_type, source, dest, conflict_mode, allow_delete,
                  delete_mode, source_kind, direction="upload",
                  shared_delete_confirmed=False, excluded_remote=None,
                  edit_delete_limits=False, max_delete_min=None,
                  max_delete_ratio=None):
    """Dict prêt à remplacer ``old``. Les clés hors dialogue sont reportées."""
    new_m = {
        "type": m_type,
        "source": source.strip(),
        "dest_parent": dest.strip(),
    }
    if old and old.get("exclusions"):
        new_m["exclusions"] = old["exclusions"]
    if conflict_mode == "revision":
        new_m["conflict_mode"] = "revision"
    if allow_delete:
        new_m["allow_delete"] = True
        new_m["delete_mode"] = delete_mode or "trash"
        new_m["source_kind"] = source_kind
    if old:
        carry_unknown_keys(old, new_m)
    if direction == "twoway":
        new_m["direction"] = "twoway"
    else:
        new_m.pop("direction", None)
    if shared_delete_confirmed:
        new_m["shared_delete_confirmed"] = True
    else:
        new_m.pop("shared_delete_confirmed", None)
    # Clé absente = "prune" (le défaut). La case écrit "keep", décochée la retire.
    if excluded_remote == "keep":
        new_m["excluded_remote"] = "keep"
    elif excluded_remote == "prune":
        new_m.pop("excluded_remote", None)
    if edit_delete_limits:
        if max_delete_min is None:
            new_m.pop("max_delete_min", None)
        else:
            new_m["max_delete_min"] = max_delete_min
        if max_delete_ratio is None:
            new_m.pop("max_delete_ratio", None)
        else:
            new_m["max_delete_ratio"] = max_delete_ratio
    return new_m


def exclusion_fingerprint(mapping, global_exclusions):
    """Empreinte effective, ou None si le moteur d'exclusions est absent."""
    if _EXCL is None:
        return None
    try:
        glob = global_exclusions or {}
        merged_global = _EXCL(glob.get("names"), glob.get("patterns"))
        local = mapping.get("exclusions") or {}
        merged_local = _EXCL(local.get("names"), local.get("patterns"))
        effective = merged_global.merged_with(merged_local)
        return effective.fingerprint() if effective else None
    except Exception:
        return None


def ready_state(mapping, cache_data, global_exclusions):
    """``ready``, ``pending`` ou ``na`` (fichier, pas d'arbre)."""
    if mapping.get("type") != "folder":
        return "na"
    data = cache_data if isinstance(cache_data, dict) else {}
    src = source_key(mapping.get("source", ""))
    entry = data.get(src)
    if not isinstance(entry, dict) or not entry.get("subtree_complete"):
        return "pending"
    sig = entry.get("sig")
    cached = sig.get("excl") if isinstance(sig, dict) else None
    if exclusion_fingerprint(mapping, global_exclusions) == cached:
        return "ready"
    return "pending"


def subtree_keys(cache_data, source):
    """Clés du sous-arbre (racine + descendants), hors __meta__."""
    base = source_key(source)
    prefix = base.rstrip("/") + "/"
    found = []
    for key in cache_data:
        if key == "__meta__":
            continue
        normalized = source_key(key)
        if normalized == base or (normalized + "/").startswith(prefix):
            found.append(key)
    return found


def clear_primed(cache_path, source):
    """Retire subtree_complete sur le sous-arbre. Les autres mappings restent."""
    data = read_cache(cache_path)
    if not data:
        return False
    changed = False
    for key in subtree_keys(data, source):
        entry = data.get(key)
        if isinstance(entry, dict) and entry.get("subtree_complete"):
            entry["subtree_complete"] = False
            changed = True
    if changed:
        atomic_write_json(cache_path, data)
    return changed


def _read_global(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return empty_exclusions()
    if isinstance(raw, dict):
        ex = raw.get("exclusions") or {}
        return {
            "names": list(ex.get("names", []) or []),
            "patterns": list(ex.get("patterns", []) or []),
        }
    return empty_exclusions()


def _file_has_mappings(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return False
    if isinstance(raw, list):
        return len(raw) > 0
    if isinstance(raw, dict):
        return len(raw.get("mappings", []) or []) > 0
    return False


def _normalized_exclusions(exclusions):
    ex = exclusions or {}
    return {
        "names": sorted(ex.get("names", []) or []),
        "patterns": sorted(ex.get("patterns", []) or []),
    }


def mappings_invalidated_by_globals(config_path, new_globals, exclude_source,
                                    directory=None):
    """Sources prêtes dont l'empreinte casserait sous ``new_globals``."""
    cache = read_cache(cache_path_for(config_path, directory))
    if not cache:
        return []
    try:
        with open(config_path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return []
    rows = raw if isinstance(raw, list) else (
        raw.get("mappings", []) if isinstance(raw, dict) else [])
    skip = source_key(exclude_source) if exclude_source else None
    hit = []
    for mapping in rows:
        if mapping.get("type") != "folder":
            continue
        src = source_key(mapping.get("source", ""))
        if src == skip:
            continue
        entry = cache.get(src)
        if not (isinstance(entry, dict) and entry.get("subtree_complete")):
            continue
        sig = entry.get("sig")
        stored = sig.get("excl") if isinstance(sig, dict) else None
        if exclusion_fingerprint(mapping, new_globals) != stored:
            hit.append(mapping.get("source", ""))
    return hit


def plan_move(src_path, dest_path, mapping, global_exclusions, directory=None):
    """Décide si le déplacement est permis, sans rien écrire.

    ``reason`` quand ``ok`` est faux : same_file, other_account, unknown_identity.
    """
    if os.path.normpath(dest_path) == os.path.normpath(src_path):
        return {"ok": False, "reason": "same_file"}
    src_cache = read_cache(cache_path_for(src_path, directory))
    dest_cache_path = cache_path_for(dest_path, directory)
    dest_cache = read_cache(dest_cache_path)
    src_account = cache_account(src_cache)
    dest_account = cache_account(dest_cache)
    dest_exists = os.path.exists(dest_path)
    if dest_cache is not None and dest_account:
        if src_account and dest_account != src_account:
            return {
                "ok": False, "reason": "other_account",
                "src_account": src_account, "dest_account": dest_account,
            }
    elif dest_exists and _file_has_mappings(dest_path) and dest_cache is None:
        return {"ok": False, "reason": "unknown_identity"}

    src_excl = _normalized_exclusions(global_exclusions)
    dest_excl = _normalized_exclusions(
        _read_global(dest_path) if dest_exists else empty_exclusions())
    dest_has_excl = bool(dest_excl["names"] or dest_excl["patterns"])
    dest_has_maps = dest_exists and _file_has_mappings(dest_path)
    same = src_excl == dest_excl
    copy_excl = False
    notice = None
    impacted = []
    if not same:
        if not dest_has_excl and not dest_has_maps:
            copy_excl = True
            if src_excl["names"] or src_excl["patterns"]:
                notice = "copy"
        else:
            impacted = mappings_invalidated_by_globals(
                dest_path, src_excl, mapping.get("source", ""), directory)
            copy_excl = True
            if impacted:
                notice = "impact"
    return {
        "ok": True,
        "copy_excl": copy_excl,
        "notice": notice,
        "impacted": impacted,
        "src_account": src_account,
        "src_excl": src_excl,
    }


def apply_move(src_path, dest_path, mapping, src_account, copy_excl, src_excl,
               directory=None):
    """Ajoute le mapping au fichier destination et déplace son cache."""
    dest_doc = {"exclusions": empty_exclusions(), "mappings": []}
    if os.path.exists(dest_path):
        try:
            with open(dest_path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
            if isinstance(raw, list):
                dest_doc = {"exclusions": empty_exclusions(), "mappings": raw}
            elif isinstance(raw, dict):
                dest_doc = raw
                dest_doc.setdefault("mappings", [])
                dest_doc.setdefault("exclusions", empty_exclusions())
        except (OSError, ValueError):
            pass
    if copy_excl and src_excl is not None:
        dest_doc["exclusions"] = {
            "names": list(src_excl.get("names", [])),
            "patterns": list(src_excl.get("patterns", [])),
        }
    dest_doc["mappings"].append(mapping)
    atomic_write_json(dest_path, dest_doc)

    if copy_excl and src_excl is not None:
        _invalidate_impacted(dest_path, mapping.get("source", ""), src_excl, directory)

    if mapping.get("type") != "folder":
        return
    src_cache_path = cache_path_for(src_path, directory)
    src_cache = read_cache(src_cache_path)
    if not src_cache:
        return
    keys = subtree_keys(src_cache, mapping.get("source", ""))
    if not keys:
        return
    dest_cache_path = cache_path_for(dest_path, directory)
    dest_cache = read_cache(dest_cache_path)
    if dest_cache is None:
        dest_cache = {}
    if src_account and "__meta__" not in dest_cache:
        dest_cache["__meta__"] = {"account": src_account}
    for key in keys:
        dest_cache[key] = src_cache[key]
    parent = os.path.dirname(dest_cache_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    atomic_write_json(dest_cache_path, dest_cache)
    for key in keys:
        del src_cache[key]
    atomic_write_json(src_cache_path, src_cache)


def _invalidate_impacted(config_path, exclude_source, new_globals, directory):
    targets = set(source_key(s) for s in mappings_invalidated_by_globals(
        config_path, new_globals, exclude_source, directory))
    if not targets:
        return
    cache_path = cache_path_for(config_path, directory)
    cache = read_cache(cache_path)
    if not cache:
        return
    changed = False
    for target in targets:
        for key in subtree_keys(cache, target):
            entry = cache.get(key)
            if isinstance(entry, dict) and entry.get("subtree_complete"):
                entry["subtree_complete"] = False
                changed = True
    if changed:
        atomic_write_json(cache_path, cache)


class Document:
    """Mappings ouverts dans l'éditeur. ``dirty`` suit les modifications non écrites."""

    def __init__(self):
        self.path = None
        self.mappings = []
        self.global_exclusions = empty_exclusions()
        self.dirty = False

    def load(self, path):
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, list):
            mappings = data
            global_ex = empty_exclusions()
        elif isinstance(data, dict):
            mappings = data.get("mappings", [])
            ex = data.get("exclusions", {}) or {}
            global_ex = {
                "names": list(ex.get("names", []) or []),
                "patterns": list(ex.get("patterns", []) or []),
            }
        else:
            raise DocumentError(_("The file must contain a list or an object."))
        if not isinstance(mappings, list):
            raise DocumentError(_("The file must contain a list or an object."))
        for entry in mappings:
            if not isinstance(entry, dict) or not all(
                    key in entry for key in ("type", "source", "dest_parent")):
                raise DocumentError(
                    _("Each entry must have: type, source, dest_parent."))
        self.mappings = mappings
        self.global_exclusions = global_ex
        self.path = path
        self.dirty = False

    def payload(self):
        """Liste simple s'il n'y a aucune exclusion, sinon l'objet."""
        has_global = exclusions_active(self.global_exclusions)
        has_mapping = any(m.get("exclusions") for m in self.mappings)
        if has_global or has_mapping:
            return {
                "exclusions": {
                    "names": self.global_exclusions.get("names", []),
                    "patterns": self.global_exclusions.get("patterns", []),
                },
                "mappings": self.mappings,
            }
        return self.mappings

    def save(self, path=None):
        target = path or self.path
        if not target:
            raise DocumentError(_("Open or save a mappings file first."))
        atomic_write_json(target, self.payload())
        self.path = target
        self.dirty = False
        if appconfig is not None:
            appconfig.set_last_mappings_path(os.path.abspath(target))
        return target

    def cache_data(self):
        if not self.path:
            return {}
        return read_cache(cache_path_for(self.path)) or {}

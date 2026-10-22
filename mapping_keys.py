"""Pure helpers for mapping dictionaries (no GUI imports, testable without a display)."""
__version__ = "1.3.0"   # version propre à CE fichier ; incrémentée quand il change (indépendant de GitHub)

import os

# Keys the mapping dialog builds itself on every save. A key in this set that the
# dialog leaves out was deliberately switched off by the user and must NOT come back.
# excluded_remote est édité par la case du dialogue : décochée, la clé ne revient pas.
DIALOG_KEYS = frozenset({
    "type", "source", "dest_parent", "exclusions",
    "conflict_mode", "allow_delete", "delete_mode", "source_kind",
    "excluded_remote",
})


def carry_unknown_keys(old, new):
    """Copy to `new` every key of `old` that the dialog does not manage
    (future keys...). Returns `new`. Keys already present in `new` are never
    overwritten. excluded_remote is a dialog key: an unchecked box must not
    come back from the previous mapping."""
    if not old:
        return new
    for key, value in old.items():
        if key not in DIALOG_KEYS and key not in new:
            new[key] = value
    return new


# ── Dossiers d'arrivée sur Proton ────────────────────────────────────────────
# Un mapping dépose son contenu dans dest_parent/<nom de la source> : son
# « dossier d'arrivée ». Deux mappings dont les dossiers d'arrivée sont égaux,
# ou dont l'un est à l'intérieur de l'autre, se marchent dessus : quand les
# suppressions sont propagées, le moteur met à la corbeille tout ce qui, sur
# Proton, n'a pas d'équivalent dans SA source — donc le contenu de l'autre.
# Ce calcul unique sert au moteur (suppressions retenues) et à l'éditeur
# (avertissement au chargement, refus d'un nouveau conflit).

def arrival_folder(mapping):
    """Dossier du Drive où le mapping dépose son contenu, ou None si la source
    ou la destination manque. Même calcul que le moteur :
    dest_parent sans « / » final, plus le nom de la source."""
    dest = (mapping.get("dest_parent") or "").strip().rstrip("/")
    src = (mapping.get("source") or "").strip().rstrip("/")
    if not dest or not src:
        return None
    name = src.rsplit("/", 1)[-1]
    if not name:
        return None
    parts = [p for p in (dest + "/" + name).split("/") if p and p != "."]
    return "/" + "/".join(parts)


def _inside(inner, outer):
    return inner.startswith(outer + "/")


def conflict_kind(a, b):
    """Relation entre deux dossiers d'arrivée : "same", "a_in_b", "b_in_a"
    ou None (aucun conflit). Comparaison à frontière de segment :
    /x/Doc n'est pas dans /x/Documents."""
    if a is None or b is None:
        return None
    if a == b:
        return "same"
    if _inside(a, b):
        return "a_in_b"
    if _inside(b, a):
        return "b_in_a"
    return None


def arrival_conflicts(mappings):
    """Toutes les paires en conflit d'une liste de mappings.

    Retourne une liste de (outer, inner, kind, outer_folder, inner_folder),
    où outer et inner sont des INDICES dans `mappings` :
      - kind "same" : même dossier d'arrivée (outer < inner, ordre du fichier) ;
      - kind "nested" : le dossier d'arrivée de inner est dans celui de outer.
    """
    folders = [arrival_folder(m) if isinstance(m, dict) else None for m in mappings]
    out = []
    for i in range(len(mappings)):
        for j in range(i + 1, len(mappings)):
            kind = conflict_kind(folders[i], folders[j])
            if kind == "same":
                out.append((i, j, "same", folders[i], folders[j]))
            elif kind == "b_in_a":
                out.append((i, j, "nested", folders[i], folders[j]))
            elif kind == "a_in_b":
                out.append((j, i, "nested", folders[j], folders[i]))
    return out


def deletion_holds(mappings):
    """Indices des mappings dont les suppressions doivent être retenues, avec,
    pour chacun, l'indice d'un mapping en conflit (le premier trouvé).

    Les DEUX mappings de chaque paire, imbriquée comme identique. Pour une
    paire imbriquée, celui qui contient l'autre jetterait son contenu ; mais
    celui qui est à l'intérieur peut aussi jeter des fichiers de l'autre :
    quand la source englobante a un sous-dossier du même nom, les deux
    envoient dans le même dossier distant (démontré sur le faux Drive)."""
    holds = {}
    for outer, inner, _kind, _fo, _fi in arrival_conflicts(mappings):
        holds.setdefault(outer, inner)
        holds.setdefault(inner, outer)
    return holds


def first_arrival_conflict(candidate, others):
    """Premier conflit entre `candidate` et la liste `others`, ou None.
    Retourne (other, kind, candidate_folder, other_folder), kind valant
    "same", "inside" (le candidat arrive dans le dossier de l'autre) ou
    "contains" (l'autre arrive dans le dossier du candidat)."""
    mine = arrival_folder(candidate)
    if mine is None:
        return None
    for other in others:
        if not isinstance(other, dict):
            continue
        theirs = arrival_folder(other)
        kind = conflict_kind(mine, theirs)
        if kind is None:
            continue
        return (other, {"same": "same", "a_in_b": "inside", "b_in_a": "contains"}[kind],
                mine, theirs)
    return None


# ── Clés de premier niveau du fichier de mappings ────────────────────────────
# L'éditeur ne gère que "exclusions" et "mappings". Une version plus récente
# peut écrire d'autres clés dans le même fichier (ex. "sync_mappings") : elles
# doivent survivre à un enregistrement fait par cette version, sans que
# celle-ci en tienne compte autrement.

FILE_KEYS = frozenset({"exclusions", "mappings"})


def unknown_top_level(document):
    """Clés de premier niveau que l'éditeur ne gère pas, dans l'ordre du
    fichier. Un fichier au format liste n'en a pas."""
    if not isinstance(document, dict):
        return {}
    return {k: v for k, v in document.items() if k not in FILE_KEYS}


# ── Conflits ENTRE deux fichiers de mappings ─────────────────────────────────
# Le passage planifié et le temps réel peuvent tourner sur deux fichiers
# différents. Deux mappings, un dans chaque fichier, qui arrivent dans le même
# dossier du Drive (ou l'un dans l'autre) se mettent à la corbeille l'un
# l'autre exactement comme deux mappings d'un même fichier. Exception : le
# MÊME mapping présent dans les deux fichiers (même source, même destination,
# mêmes exclusions effectives) — les deux passages font alors la même chose.

import json as _json
import os as _os


def read_mappings_document(path):
    """(mappings, exclusions globales) d'un fichier de mappings, ou None s'il
    est absent ou illisible. Les deux formats : liste simple, ou objet
    {exclusions, mappings}. Les exclusions sont rendues sous la forme
    {"names": [...], "patterns": [...]}."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = _json.load(f)
    except (OSError, ValueError):
        return None
    if isinstance(data, list):
        return [m for m in data if isinstance(m, dict)], {"names": [], "patterns": []}
    if isinstance(data, dict):
        maps = data.get("mappings") or []
        ex = data.get("exclusions") or {}
        if not isinstance(maps, list) or not isinstance(ex, dict):
            return None
        return ([m for m in maps if isinstance(m, dict)],
                {"names": list(ex.get("names") or []),
                 "patterns": list(ex.get("patterns") or [])})
    return None


def effective_exclusions(mapping, global_ex):
    """Exclusions qui s'appliquent réellement au mapping : celles du fichier
    plus les siennes, sous forme comparable (deux ensembles)."""
    own = mapping.get("exclusions") or {}
    g = global_ex or {}
    names = set(g.get("names") or []) | set(own.get("names") or [])
    patterns = set(g.get("patterns") or []) | set(own.get("patterns") or [])
    return frozenset(names), frozenset(patterns)


def _norm(path):
    parts = [p for p in (path or "").strip().split("/") if p and p != "."]
    return "/" + "/".join(parts)


def same_mapping(a, global_a, b, global_b):
    """Vrai si a et b font la même chose : même source, même destination, mêmes
    exclusions effectives. Deux copies du même mapping dans deux fichiers ne se
    nuisent pas ; des exclusions différentes, si — l'un enverrait ce que
    l'autre met à la corbeille. Même raison pour excluded_remote : « keep »
    d'un côté, la corbeille de l'autre jetterait ce que le premier garde."""
    return (_norm(a.get("source")) == _norm(b.get("source"))
            and _norm(a.get("dest_parent")) == _norm(b.get("dest_parent"))
            and effective_exclusions(a, global_a) == effective_exclusions(b, global_b)
            and (a.get("excluded_remote") == "keep") == (b.get("excluded_remote") == "keep"))


def cross_conflicts(mine, global_mine, theirs, global_theirs):
    """Conflits entre les mappings de deux fichiers. Retourne une liste de
    (i, j, kind, folder_i, folder_j) : i indice dans `mine`, j dans `theirs`,
    kind "same" (même dossier d'arrivée), "inside" (celui de i est dans celui
    de j) ou "contains" (celui de j est dans celui de i). Le même mapping
    présent dans les deux fichiers n'est pas un conflit."""
    out = []
    theirs_folders = [arrival_folder(t) if isinstance(t, dict) else None for t in theirs]
    for i, m in enumerate(mine):
        if not isinstance(m, dict):
            continue
        fi = arrival_folder(m)
        if fi is None:
            continue
        for j, t in enumerate(theirs):
            kind = conflict_kind(fi, theirs_folders[j])
            if kind is None:
                continue
            if kind == "same" and same_mapping(m, global_mine, t, global_theirs):
                continue
            out.append((i, j, {"same": "same", "a_in_b": "inside", "b_in_a": "contains"}[kind],
                        fi, theirs_folders[j]))
    return out


def other_running_files(path, running):
    """Fichiers en service (`running` : chemins, None ignoré) autres que `path`,
    sans doublon, comparés après résolution des liens."""
    here = _os.path.realpath(path) if path else None
    out, seen = [], set()
    for p in running:
        if not p:
            continue
        rp = _os.path.realpath(p)
        if rp == here or rp in seen:
            continue
        seen.add(rp)
        out.append(p)
    return out


# ── Mappings deux sens (clé "sync_mappings") ─────────────────────────────────
# Un mapping deux sens ne doit chevaucher AUCUN autre mapping (à sens unique ou
# deux sens), ni en local (même dossier, parent, sous-dossier), ni sur Proton
# (règle des dossiers d'arrivée ci-dessus). Les mappings à sens unique, eux,
# gardent leur comportement : ils peuvent se chevaucher entre eux.

SYNC_DIRECTIONS = ("twoway",)
SYNC_TYPES = ("folder", "file")


def _local_path(mapping):
    src = (mapping.get("source") or "").strip() if isinstance(mapping, dict) else ""
    if not src:
        return None
    return os.path.normpath(src)


def local_overlap(a, b):
    """Relation locale entre deux mappings : "same", "parent" (a contient b),
    "child" (a est dans b) ou None. Comparaison à frontière de segment."""
    pa, pb = _local_path(a), _local_path(b)
    if pa is None or pb is None:
        return None
    if pa == pb:
        return "same"
    if pb.startswith(pa.rstrip("/") + "/"):
        return "parent"
    if pa.startswith(pb.rstrip("/") + "/"):
        return "child"
    return None


def sync_overlap(mapping, other):
    """Relation entre un mapping et un autre : (raison, détail) avec la même
    raison que sync_mapping_conflicts ("local-same", "local-parent",
    "local-child", "arrival-same", "arrival-inside", "arrival-contains"), ou
    None. Le détail est le dossier d'arrivée de l'autre pour les trois
    dernières, "" sinon."""
    rel = local_overlap(mapping, other)
    if rel:
        return "local-" + rel, ""
    hit = first_arrival_conflict(mapping, [other])
    if hit:
        _other, rel, _mine, theirs = hit
        return "arrival-" + rel, theirs
    return None


def runnable_sync_mappings(sync_mappings):
    """Les entrées de "sync_mappings" que le moteur peut lancer : un objet de
    direction et de type connus, avec source et destination. Seules celles-là
    comptent pour retenir les suppressions d'un mapping à sens unique."""
    return [m for m in (sync_mappings or ())
            if isinstance(m, dict)
            and m.get("direction") in SYNC_DIRECTIONS
            and m.get("type") in SYNC_TYPES
            and m.get("source") and m.get("dest_parent")]


def upload_sync_holds(upload_mappings, sync_mappings):
    """Mappings à sens unique qui chevauchent un mapping deux sens (les six
    relations de sync_mapping_conflicts). Retourne {indice dans
    upload_mappings: (mapping deux sens, raison, détail)}, au premier trouvé.
    Leurs suppressions doivent être retenues : sinon ils mettraient à la
    corbeille ce que l'autre côté a ajouté sur Proton."""
    twoway = runnable_sync_mappings(sync_mappings)
    out = {}
    if not twoway:
        return out
    for i, m in enumerate(upload_mappings or ()):
        if not isinstance(m, dict):
            continue
        for t in twoway:
            hit = sync_overlap(t, m)
            if hit:
                out[i] = (t, hit[0], hit[1])
                break
    return out


def read_sync_mappings(path):
    """Les entrées (objets) de la clé "sync_mappings" d'un fichier de mappings ;
    liste vide si la clé manque, si le fichier est une liste, absent ou
    illisible (read_mappings_document dit déjà si le fichier se lit)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = _json.load(f)
    except (OSError, ValueError):
        return []
    found = data.get("sync_mappings") if isinstance(data, dict) else None
    if not isinstance(found, list):
        return []
    return [m for m in found if isinstance(m, dict)]


def sync_mapping_conflicts(sync_mappings, upload_mappings=()):
    """Mappings deux sens en conflit, à vérifier à chaque chargement du fichier.

    `sync_mappings` : la liste de la clé "sync_mappings" ; `upload_mappings` :
    celle de "mappings". Retourne {indice dans sync_mappings: (raison, autre
    mapping, détail)}, au premier conflit trouvé pour chaque mapping. La raison
    vaut "local-same", "local-parent" (ce mapping contient l'autre),
    "local-child" (il est dans l'autre), ou "arrival-same", "arrival-inside",
    "arrival-contains" (dossiers d'arrivée sur Proton) ; le détail est le
    dossier d'arrivée de l'autre pour ces trois dernières, "" sinon.
    Un élément qui n'est pas un dict est ignoré."""
    out = {}
    pool = [(("sync", i), m) for i, m in enumerate(sync_mappings or ())
            if isinstance(m, dict)]
    pool += [(("up", i), m) for i, m in enumerate(upload_mappings or ())
             if isinstance(m, dict)]
    for (kind, i), m in pool:
        if kind != "sync":
            continue
        others = [o for key, o in pool if key != (kind, i)]
        found = None
        for o in others:
            rel = local_overlap(m, o)
            if rel:
                found = ("local-" + rel, o, "")
                break
        if found is None:
            hit = first_arrival_conflict(m, others)
            if hit:
                other, rel, _mine, theirs = hit
                found = ("arrival-" + rel, other, theirs)
        if found:
            out[i] = found
    return out

#!/usr/bin/env python3
"""
Étape 1 (révisée) — Détection du type de source et vérification de sécurité
avant d'autoriser la propagation de suppressions vers Proton Drive.

Sert à DEUX usages :
  - le GUI, à l'édition d'un mapping : détecter automatiquement si une source est
    de type « nfs » (portée par un montage réseau) ou « local » (vrai disque),
    pour que l'utilisateur confirme avant d'enregistrer ;
  - le moteur, la nuit : vérifier qu'une source déclarée « nfs » est bien portée
    par un montage réseau VIVANT avant d'autoriser une suppression — protection
    contre une panne du NAS qui ferait apparaître la source vide.

Découverte importante sur l'environnement cible (via `findmnt`) : les dossiers
sous /home/<user>/ comme Documents, Images, etc. ne sont PAS des binds locaux —
ce sont des montages NFS directs (chacun avec sa propre SOURCE réseau). Donc dès
qu'un chemin dépend du NAS, il apparaît dans /proc/mounts avec un type nfs/nfs4.
Si le NAS tombe, ces montages disparaissent et le chemin retombe sur du local
(ext4) — ce qu'on détecte et qui doit bloquer les suppressions pour une source
déclarée « nfs ».
"""
import os


# Types de systèmes de fichiers considérés comme « réseau ».
NETWORK_FS_TYPES = {"nfs", "nfs4", "cifs", "smb3", "smbfs"}


def _read_mounts():
    """Lit /proc/mounts -> liste de (source, point, fstype).
    Décode les espaces encodés \\040 dans /proc/mounts."""
    mounts = []
    try:
        with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                src = parts[0].replace("\\040", " ")
                point = parts[1].replace("\\040", " ")
                fstype = parts[2]
                mounts.append((src, point, fstype))
    except OSError:
        pass
    return mounts


def _longest_mount_for(path, mounts):
    """Retourne le montage (src, point, fstype) dont le point est le plus long
    préfixe du chemin — le système de fichiers qui porte réellement ce chemin."""
    real = os.path.realpath(path)
    best = None
    best_len = -1
    for src, point, fstype in mounts:
        p = point.rstrip("/") or "/"
        if real == p or real.startswith(p + "/") or p == "/":
            if len(p) > best_len:
                best = (src, point, fstype)
                best_len = len(p)
    return best


def detect_source_kind(path):
    """Détecte le type d'une source et retourne un dict de diagnostic :
      {
        "kind":        "nfs" | "local" | "missing",
        "mount_point": point de montage porteur (ou None),
        "mount_src":   source du montage (ex. 192.168.1.10:/media/nas1) ou None,
        "fstype":      type du système de fichiers porteur (ou None),
        "readable":    bool — la source est-elle listable sans erreur,
        "detail":      message lisible
      }

    Utilisé par le GUI pour proposer une détection (que l'utilisateur confirme).
    """
    result = {
        "kind": "missing", "mount_point": None, "mount_src": None,
        "fstype": None, "readable": False, "detail": "",
    }

    if not os.path.exists(path):
        result["detail"] = f"Source introuvable : {path}"
        return result

    mounts = _read_mounts()
    mount = _longest_mount_for(path, mounts)
    if mount is not None:
        src, point, fstype = mount
        result["mount_point"] = point
        result["mount_src"] = src
        result["fstype"] = fstype
        result["kind"] = "nfs" if fstype in NETWORK_FS_TYPES else "local"

    # Test d'accès en lecture (un montage effondré lève ici).
    try:
        with os.scandir(path) as it:
            for _ in it:
                break
        result["readable"] = True
    except OSError as e:
        result["readable"] = False
        result["detail"] = f"Source illisible : {e}"
        return result

    if result["kind"] == "nfs":
        result["detail"] = (
            f"Détecté comme NFS (réseau) — porté par {result['mount_src']} "
            f"sur {result['mount_point']}."
        )
    elif result["kind"] == "local":
        result["detail"] = (
            f"Détecté comme LOCAL — porté par {result['mount_point']} "
            f"(type {result['fstype']})."
        )
    return result


def source_is_safe_for_delete(source_path, expected_kind, verbose=False):
    """Garde-fou du MOTEUR, appelé avant toute suppression dans un mapping.

    `expected_kind` est le type déclaré dans le mapping ("nfs" ou "local"),
    confirmé par l'utilisateur à l'édition.

    Règles :
      - source doit exister, être un dossier, et être lisible ;
      - si expected_kind == "nfs", la source DOIT actuellement être portée par un
        montage réseau (nfs/nfs4...). Si elle est retombée sur du local (NAS
        déconnecté), on refuse — c'est la protection clé ;
      - si expected_kind == "local", on exige juste existence + lisibilité.

    Retour : (ok: bool, raison: str). ok=False => AUCUNE suppression dans ce
    mapping (les uploads, eux, continuent).
    """
    if not os.path.exists(source_path):
        return False, f"source introuvable : {source_path}"
    if not os.path.isdir(source_path):
        return False, f"source n'est pas un dossier : {source_path}"

    info = detect_source_kind(source_path)

    if not info["readable"]:
        return False, f"source illisible (montage effondré ?) : {source_path}"

    if expected_kind == "nfs":
        if info["kind"] != "nfs":
            # Le cas dangereux : on attendait du réseau, mais la source est
            # actuellement portée par du local -> le NAS est probablement tombé
            # et le chemin est vide. On refuse catégoriquement de supprimer.
            return False, (
                f"source déclarée NFS mais actuellement portée par "
                f"{info['fstype']} sur {info['mount_point']} — NAS déconnecté ? "
                f"Suppressions bloquées par sécurité."
            )
        if verbose:
            print(f"    [delete-guard] OK NFS vivant : {info['mount_src']}")
        return True, "ok (nfs vivant)"

    if expected_kind == "local":
        if verbose:
            print(f"    [delete-guard] OK local : {source_path}")
        return True, "ok (local)"

    # expected_kind inconnu / absent -> sécurité : refus.
    return False, (
        f"type de source non déclaré pour {source_path} — "
        f"suppression refusée (déclare 'source_kind' dans le mapping)."
    )

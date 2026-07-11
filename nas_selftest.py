#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Self-test des correspondances de chemins NAS — CŒUR LOGIQUE (v1.5.0, en cours).

Ce module contient UNIQUEMENT la logique de décision (fonctions pures, sans I/O
et sans dépendance aux services). Il est volontairement isolé pour respecter
l'invariant du chantier : le self-test est PUREMENT ADDITIF et ne touche à aucun
chemin de code du moteur / consommateur / watcher en fonctionnement normal.

L'orchestration réelle (écriture du témoin côté desktop, observation inotify côté
watcher NAS, canal de retour desktop<->NAS, statuts colorés dans le GUI) sera
branchée SÉPARÉMENT et plus tard. Ici, on fige et on teste la logique en DEUX
TEMPS validée avec l'utilisateur :

  Temps 1 — présence PHYSIQUE du témoin (tranche la CORRESPONDANCE) :
    le desktop écrit .selftest-<id> dans son montage ; on regarde s'il apparaît
    dans le dossier NAS traduit. Présent => mêmes dossiers physiques. Absent =>
    correspondance fausse (ou dossier NAS absent).

  Temps 2 — inotify capte + marqueur écrit (tranche le FONCTIONNEMENT) :
    correspondance déjà confirmée bonne ; on vérifie que la chaîne watcher a bien
    capté l'événement et pu écrire le marqueur.

Trois couleurs : "green" | "yellow" | "red". La couleur donne la gravité, le
message donne l'action (principe « le retour à l'utilisateur compte »).
"""
__version__ = "1.0.0"   # version propre à CE fichier ; incrémentée quand il change (indépendant de GitHub)

# Codes de couleur (indépendants de tout toolkit graphique).
GREEN = "green"
YELLOW = "yellow"
RED = "red"


class SelfTestObservation:
    """Ensemble des faits observés pendant un self-test d'UNE correspondance.
    Rempli par l'orchestration réelle (desktop + watcher) ; ici on ne fait que
    RAISONNER dessus.

    Le test se fait en DEUX PHASES (idée « B puis A ») :

      Phase B — écriture LOCALE au NAS : le watcher écrit un témoin dans son
        propre volume (/volume1/X) et vérifie que SON inotify le capte. Isole la
        question « inotify fonctionne-t-il DU TOUT sur ce montage ? ». C'est le
        test de référence.

      Phase A — écriture DISTANTE par le desktop : le desktop écrit un témoin
        dans son montage (/media/nas1/X), qui arrive physiquement sur le NAS ; le
        watcher vérifie (1) que le témoin est PHYSIQUEMENT présent dans /volume1/X
        (Temps 1 : la correspondance) et (2) que son inotify capte cette écriture
        DISTANTE (Temps 2 : le vrai cas d'usage, la particularité NFS/SMB).

    La combinaison B/A donne un diagnostic fin : B OK + A muet = inotify marche en
    local mais pas pour les écritures distantes (le piège NFS) ; témoin distant
    absent = mauvaise correspondance ; tout OK = chaîne complète.

    Champs :
      local_prefix / nas_prefix   : la paire testée (pour les messages).
      # Phase B (locale, référence)
      local_witness_written       : le watcher a-t-il pu écrire son témoin local ?
      local_inotify_caught        : son inotify a-t-il capté cette écriture locale ?
      nas_dir_exists              : le dossier NAS traduit existe-t-il ?
      # Phase A (distante, vrai cas)
      remote_witness_written      : le desktop a-t-il pu écrire son témoin ?
      remote_witness_seen_on_nas  : ce témoin est-il physiquement dans /volume1/X ?
      remote_inotify_caught       : l'inotify du watcher a-t-il capté l'écriture
                                    DISTANTE ?
      marker_written              : le marqueur de test a-t-il pu être écrit ?
      timed_out                   : aucune réponse du watcher dans le délai.
    """

    def __init__(self, local_prefix="", nas_prefix="",
                 local_witness_written=False, local_inotify_caught=False,
                 nas_dir_exists=False,
                 remote_witness_written=False, remote_witness_seen_on_nas=False,
                 remote_inotify_caught=False, marker_written=False,
                 timed_out=False):
        self.local_prefix = local_prefix
        self.nas_prefix = nas_prefix
        # Phase B
        self.local_witness_written = local_witness_written
        self.local_inotify_caught = local_inotify_caught
        self.nas_dir_exists = nas_dir_exists
        # Phase A
        self.remote_witness_written = remote_witness_written
        self.remote_witness_seen_on_nas = remote_witness_seen_on_nas
        self.remote_inotify_caught = remote_inotify_caught
        self.marker_written = marker_written
        self.timed_out = timed_out


def verdict(obs, _=None):
    """Retourne (couleur, message) à partir des observations. Fonction PURE.

    Logique « B puis A » : la phase B (locale) sert de référence pour interpréter
    la phase A (distante). L'ordre des tests ci-dessous suit le diagnostic du plus
    fondamental au plus fin.

    `_` est un traducteur optionnel (gettext) ; par défaut identité, pour que le
    module soit testable sans i18n installée.
    """
    if _ is None:
        def _(s):
            return s

    # 0) Indéterminé : pas de réponse du watcher (service éteint, NAS injoignable).
    #    Jaune (pas rouge) : on ne PEUT PAS conclure. On invite à vérifier.
    if obs.timed_out:
        return (YELLOW, _("Not tested: no response from the NAS watcher — is the "
                          "service running and the NAS reachable?"))

    # 1) Le dossier NAS traduit existe-t-il seulement ? Sinon, la correspondance
    #    pointe vers un chemin inexistant -> rouge sans ambiguïté.
    if not obs.nas_dir_exists:
        return (RED, _("The NAS path {n} does not exist — check the correspondence "
                       "or the mount.").format(n=obs.nas_prefix))

    # 2) PHASE B (référence) : inotify fonctionne-t-il DU TOUT sur ce montage ?
    #    Le watcher a écrit un témoin LOCAL et regardé si son inotify le capte.
    if not obs.local_witness_written:
        # Le watcher n'a pas pu écrire dans son propre volume -> permissions NAS.
        return (YELLOW, _("The NAS watcher cannot write into {n} — check "
                          "permissions on the NAS side.").format(n=obs.nas_prefix))
    if not obs.local_inotify_caught:
        # inotify ne capte même pas une écriture locale : problème fondamental
        # (filesystem sans inotify, watch non posé…). Jaune : la correspondance
        # n'est pas en cause, mais le temps réel ne peut pas marcher ici.
        return (YELLOW, _("inotify does not report changes in {n} even for local "
                          "writes — this filesystem may not support it; real-time "
                          "cannot work here.").format(n=obs.nas_prefix))

    # À partir d'ici : inotify fonctionne en local (B OK). On interprète A.

    # 3) PHASE A — TEMPS 1 (la correspondance) : le témoin écrit par le DESKTOP
    #    est-il physiquement présent dans le dossier NAS traduit ?
    if not obs.remote_witness_written:
        # Le desktop n'a pas pu écrire son témoin -> permissions côté machine locale.
        return (YELLOW, _("Could not write the test file on this machine — check "
                          "permissions on {l}.").format(l=obs.local_prefix))
    if not obs.remote_witness_seen_on_nas:
        # Écrit côté desktop mais ABSENT de /volume1/X : les deux chemins ne
        # désignent PAS le même dossier physique -> correspondance fausse.
        return (RED, _("Mismatch: what this machine sees as {l} is not the same "
                       "folder the NAS sees as {n} — the correspondence is wrong.")
                .format(l=obs.local_prefix, n=obs.nas_prefix))

    # Le témoin distant est bien là : la correspondance est BONNE.
    # 4) PHASE A — TEMPS 2 (le fonctionnement distant) : l'inotify du watcher
    #    a-t-il capté l'écriture DISTANTE ? C'est le vrai cas d'usage.
    if not obs.remote_inotify_caught:
        # B OK mais A muet : inotify marche en LOCAL mais pas pour les écritures
        # DISTANTES (SMB/NFS). C'est le piège classique -> message ciblé.
        return (YELLOW, _("Paths match ({l} ↔ {n}), and inotify works locally, but "
                          "changes written from this machine are not detected — the "
                          "NAS may not deliver inotify events for remote (SMB/NFS) "
                          "writes.").format(l=obs.local_prefix, n=obs.nas_prefix))

    if not obs.marker_written:
        # Capté mais marqueur non écrit -> permissions sur la file.
        return (YELLOW, _("Paths match ({l} ↔ {n}), but the marker could not be "
                          "written — check permissions on the marker queue.")
                .format(l=obs.local_prefix, n=obs.nas_prefix))

    # Tout fonctionne de bout en bout.
    return (GREEN, _("OK: {l} ↔ {n} match and the real-time chain works end to end.")
            .format(l=obs.local_prefix, n=obs.nas_prefix))

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Orchestration du self-test de correspondance NAS — CÔTÉ WATCHER (v1.5.0).

Ce module vit ENTIÈREMENT à côté du chemin de code normal du watcher : il
n'émet aucun marqueur de synchro, ne touche ni au moteur ni aux files de
marqueurs des utilisateurs. Il ne fait qu'observer et répondre. Invariant :
purement additif ; si quoi que ce soit échoue ici, le watcher continue de
fonctionner normalement.

Protocole 1 (desktop pilote, watcher réagit) :

  1. Le desktop dépose  SELFTEST_DIR/request-<id>.json  décrivant le test :
       {"id","local_prefix","nas_prefix","nas_test_dir","reply",
        "remote_witness"}
     - nas_test_dir    : dossier NAS (déjà traduit) où faire le test
     - remote_witness  : nom du fichier témoin que le DESKTOP va écrire (phase A)
     - reply           : chemin du fichier de réponse à écrire

  2. Le watcher, à réception :
       Phase B (locale, référence) — écrit lui-même un témoin dans nas_test_dir,
         attend brièvement, et regarde via le REGISTRE d'événements du handler si
         son inotify l'a capté. Isole « inotify marche-t-il du tout ici ? ».
       Phase A (distante, vrai cas) — attend que le témoin du DESKTOP apparaisse
         dans nas_test_dir (présence physique = Temps 1 = la correspondance), et
         regarde si l'inotify a capté cette écriture distante (Temps 2).
       Puis écrit reply-<id>.json avec toutes les observations, et nettoie.

Le REGISTRE d'événements est un simple dict {chemin_normalisé: timestamp} que le
handler du watcher alimente passivement (voir EventRegistry). Aucune influence
sur l'émission des marqueurs.
"""
__version__ = "1.0.0"   # version propre à CE fichier ; incrémentée quand il change (indépendant de GitHub)

import os
import json
import time
import threading


SELFTEST_SUBDIR = "selftest"   # sous NAS_BASE (montage partagé desktop<->NAS)


class EventRegistry:
    """Journal borné, thread-safe, des chemins récemment vus par inotify.
    Alimenté passivement par le handler du watcher. Sert au self-test à savoir
    si un témoin donné a été capté, sans interférer avec l'émission normale."""

    def __init__(self, ttl=30.0, cap=2000):
        self._lock = threading.Lock()
        self._seen = {}          # path_normalisé -> dernier timestamp vu
        self._ttl = float(ttl)
        self._cap = int(cap)

    def note(self, path):
        """Enregistre qu'un chemin vient d'être vu (appel passif du handler)."""
        now = time.time()
        with self._lock:
            self._seen[os.path.normpath(path)] = now
            # Purge paresseuse si trop gros.
            if len(self._seen) > self._cap:
                cutoff = now - self._ttl
                self._seen = {p: t for p, t in self._seen.items() if t >= cutoff}

    def seen_since(self, path, since_ts):
        """True si `path` a été vu à un instant >= since_ts."""
        p = os.path.normpath(path)
        with self._lock:
            t = self._seen.get(p)
            return t is not None and t >= since_ts


def selftest_dir(nas_base):
    return os.path.join(nas_base, SELFTEST_SUBDIR)


def _write_reply(reply_path, obs_dict, log=None):
    """Écrit la réponse (dict d'observations) de façon atomique."""
    try:
        os.makedirs(os.path.dirname(reply_path), exist_ok=True)
        tmp = reply_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obs_dict, f)
        os.replace(tmp, reply_path)
    except OSError as e:
        if log:
            log(f"  ⚠ self-test reply write failed: {e}")


def handle_request(req, registry, log=None, add_watch=None, rm_watch=None,
                   selftest_dir_nas=None,
                   local_settle=1.5, remote_wait=15.0, poll=0.3):
    """Exécute UN test (phases B puis A) à partir d'une demande `req` (dict).
    Consulte `registry` pour savoir si l'inotify du watcher a capté les témoins.
    Écrit la réponse. Ne lève jamais : en cas d'erreur, répond au mieux.

    `selftest_dir_nas` : le dossier selftest/ tel que le WATCHER le voit (côté
    NAS). Les fichiers de contrôle (ready/reply) sont désignés dans la demande par
    leur NOM SEUL (reply_name/ready_name) et résolus ici avec ce dossier — car le
    montage partagé a des chemins différents côté desktop et côté NAS.

    `add_watch(path)` / `rm_watch(token)` : callbacks optionnels pour poser un
    watch inotify TEMPORAIRE sur le dossier de test (permet de tester une
    correspondance SANS mapping existant — le découplage voulu).

    Retourne le dict d'observations (aussi écrit dans le fichier reply).
    """
    obs = {
        "id": req.get("id"),
        "local_prefix": req.get("local_prefix", ""),
        "nas_prefix": req.get("nas_prefix", ""),
        "nas_dir_exists": False,
        "local_witness_written": False,
        "local_inotify_caught": False,
        "remote_witness_written": False,       # rempli par le desktop, pas ici
        "remote_witness_seen_on_nas": False,
        "remote_inotify_caught": False,
        "marker_written": False,               # non testé ici (voir note plus bas)
        "timed_out": False,
    }
    nas_dir = req.get("nas_test_dir")
    # Résoudre reply/ready avec le dossier selftest/ CÔTÉ NAS (noms seuls dans la
    # demande). Repli sur d'éventuels chemins absolus hérités (rétrocompat).
    base = selftest_dir_nas or ""
    reply_name = req.get("reply_name")
    ready_name = req.get("ready_name")
    reply_path = (os.path.join(base, reply_name) if (base and reply_name)
                  else req.get("reply"))
    ready_path = (os.path.join(base, ready_name) if (base and ready_name)
                  else req.get("ready"))
    if not nas_dir or not reply_path:
        return obs   # demande incomplète : on ne peut rien faire

    # Le dossier NAS traduit existe-t-il ?
    obs["nas_dir_exists"] = os.path.isdir(nas_dir)
    if not obs["nas_dir_exists"]:
        _write_reply(reply_path, obs, log)
        return obs

    # Poser un watch TEMPORAIRE sur le dossier de test (découplage : on peut
    # tester une correspondance sans mapping existant). Retiré en fin de test.
    watch_token = None
    if add_watch is not None:
        try:
            watch_token = add_watch(nas_dir)
        except Exception:
            watch_token = None

    try:
        # ---- PHASE B : écriture LOCALE au NAS (référence inotify) ----
        b_name = f".proton-selftest-B-{obs['id']}"
        b_path = os.path.join(nas_dir, b_name)
        t0 = time.time()
        try:
            with open(b_path, "w", encoding="utf-8") as f:
                f.write("selftest-B")
            obs["local_witness_written"] = True
        except OSError:
            obs["local_witness_written"] = False
        if obs["local_witness_written"]:
            deadline = time.time() + local_settle
            while time.time() < deadline:
                if registry.seen_since(b_path, t0):
                    obs["local_inotify_caught"] = True
                    break
                time.sleep(poll)
            try:
                os.remove(b_path)
            except OSError:
                pass

        # ---- Signal READY : le watch est posé (phase B faite). On invite le
        #      desktop à écrire MAINTENANT son témoin distant, pour éviter la
        #      course « A écrit avant que le watch soit posé » (sinon inotify ne
        #      capterait pas l'écriture distante et on aurait un faux jaune).
        remote_from = time.time()   # on ne compte les captures d'A qu'à partir d'ici
        if ready_path:
            try:
                with open(ready_path + ".tmp", "w", encoding="utf-8") as f:
                    f.write("ready")
                os.replace(ready_path + ".tmp", ready_path)
            except OSError:
                pass

        # ---- PHASE A : témoin DISTANT écrit par le DESKTOP (après READY) ----
        remote_name = req.get("remote_witness")
        if remote_name:
            remote_path = os.path.join(nas_dir, remote_name)
            deadline = time.time() + remote_wait
            while time.time() < deadline:
                # Sur NFS, os.path.exists() peut renvoyer un résultat CACHÉ et ne
                # pas voir tout de suite un fichier créé par le client distant. On
                # force le rafraîchissement du cache du dossier en le relistant
                # (os.listdir invalide le cache d'attributs du répertoire), puis on
                # teste la présence par le nom.
                try:
                    present = remote_name in os.listdir(nas_dir)
                except OSError:
                    present = os.path.exists(remote_path)
                # inotify a-t-il capté cette écriture distante ? (test SÉPARÉ)
                if registry.seen_since(remote_path, remote_from):
                    obs["remote_inotify_caught"] = True
                if present:
                    # La présence physique tranche la CORRESPONDANCE. On peut
                    # sortir dès qu'on l'a — l'inotify est déjà évalué ci-dessus,
                    # on lui laisse juste un court instant de grâce si pas encore vu.
                    obs["remote_witness_seen_on_nas"] = True
                    if obs["remote_inotify_caught"]:
                        break
                    # petit délai de grâce pour l'événement inotify, puis on sort
                    time.sleep(poll)
                    if registry.seen_since(remote_path, remote_from):
                        obs["remote_inotify_caught"] = True
                    break
                time.sleep(poll)
    finally:
        # Toujours retirer le watch temporaire.
        if watch_token is not None and rm_watch is not None:
            try:
                rm_watch(watch_token)
            except Exception:
                pass

    _write_reply(reply_path, obs, log)
    if log:
        log(f"  🔎 self-test done (id {obs['id']}): "
            f"B={'ok' if obs['local_inotify_caught'] else 'no'} "
            f"A_seen={'ok' if obs['remote_witness_seen_on_nas'] else 'no'} "
            f"A_inotify={'ok' if obs['remote_inotify_caught'] else 'no'}")
    return obs


def poll_requests(nas_base, registry, log=None, stop_event=None, interval=1.0,
                  add_watch=None, rm_watch=None):
    """Boucle (à lancer dans un thread) : surveille SELFTEST_DIR pour les
    demandes request-*.json, les traite, puis les supprime. S'arrête quand
    stop_event est posé. Ne lève jamais (le watcher ne doit pas tomber à cause
    du self-test). `add_watch`/`rm_watch` permettent le watch temporaire."""
    sdir = selftest_dir(nas_base)
    while stop_event is None or not stop_event.is_set():
        try:
            names = os.listdir(sdir)
        except OSError:
            names = []   # dossier pas encore créé : rien à faire
        for name in names:
            if not (name.startswith("request-") and name.endswith(".json")):
                continue
            rpath = os.path.join(sdir, name)
            try:
                with open(rpath, "r", encoding="utf-8") as f:
                    req = json.load(f)
            except (OSError, ValueError):
                try:
                    os.remove(rpath)
                except OSError:
                    pass
                continue
            try:
                handle_request(req, registry, log=log,
                               add_watch=add_watch, rm_watch=rm_watch,
                               selftest_dir_nas=sdir)
            except Exception as e:
                if log:
                    log(f"  ⚠ self-test handler error: {e}")
            try:
                os.remove(rpath)
            except OSError:
                pass
        if stop_event is not None:
            stop_event.wait(interval)
        else:
            time.sleep(interval)

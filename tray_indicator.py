#!/usr/bin/env python3
"""
tray_indicator.py — Icône d'état dans la barre des tâches (zone de
notification) : double flèche circulaire aux couleurs de Proton.

Quatre états, décidés à partir du battement de cœur (status.json) écrit par le
consommateur temps réel à chaque cycle :
  • VIOLET            : démons actifs + session Proton considérée valide ;
  • VIOLET + « ! »    : démons actifs, mais des scripts NAS sont en attente de
                        déploiement (écart poste↔NAS) — ouvrir l'éditeur et lancer
                        Installer/Mettre à jour. Avertissement (la synchro tourne) ;
  • GRIS + X ROUGE    : démons actifs mais session expirée/trousseau verrouillé
                        (constaté par le consommateur en tentant de traiter) ;
  • GRIS              : démons arrêtés (battement absent ou trop vieux).

Technologie : XApp.StatusIcon (libxapp, projet Linux Mint) — natif sous
Cinnamon, MATE et Xfce, disponible sur toutes les grandes distributions
(paquet Debian/Ubuntu/Mint : gir1.2-xapp-1.0 + python3-gi). AUCUN identifiant
ni appel Proton ici : l'applet ne fait que LIRE un fichier d'état local.

L'applet se termine de lui-même si le réglage « tray_enabled » passe à False
(décoché dans ⚙ Configuration…) — pas besoin de le tuer.

Clic gauche : ouvre l'éditeur de mappings. Clic droit : menu (Ouvrir / Quitter).
"""
__version__ = "1.1.0"   # version propre à CE fichier ; incrémentée quand il change (indépendant de GitHub)

import json
import os
import subprocess
import sys
import time

APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)

# i18n (import guardé : sans i18n.py, messages en anglais).
try:
    from i18n import _
except ImportError:
    def _(s):
        return s

# Réglages (import tolérant, comme partout dans le projet).
try:
    import config as appconfig
    _HAS_CONFIG = True
except ImportError:
    _HAS_CONFIG = False

STATUS_FILE = (appconfig.STATUS_FILE if _HAS_CONFIG
               else os.path.expanduser("~/.proton-drive-sync/status.json"))
EDITOR = os.path.join(APP_DIR, "proton_mapping_editor.py")

ICONS = {
    "ok":            os.path.join(APP_DIR, "tray_connected.png"),
    "scripts_stale": os.path.join(APP_DIR, "tray_scripts.png"),
    "expired":       os.path.join(APP_DIR, "tray_expired.png"),
    "stopped":       os.path.join(APP_DIR, "tray_stopped.png"),
}

REFRESH_SECONDS = 5     # cadence de lecture du battement de cœur

# Marge de fraîcheur : le battement est réécrit chaque cycle (par défaut 30 s) ;
# on tolère 3 cycles manqués (ou 90 s minimum) avant de déclarer « arrêté »,
# pour ne pas clignoter sur un cycle un peu long (gros dossier en traitement).
_MIN_STALE_SECONDS = 90


def decide_state(status, now):
    """État de l'icône à partir du contenu de status.json (dict ou None).
    Fonction PURE (testable sans GTK).
    Retourne 'ok' | 'scripts_stale' | 'expired' | 'stopped'."""
    if not isinstance(status, dict):
        return "stopped"
    ts = status.get("ts")
    if not isinstance(ts, (int, float)):
        return "stopped"
    cycle = status.get("cycle_seconds")
    cycle = cycle if isinstance(cycle, (int, float)) and cycle > 0 else 30
    stale_after = max(3 * cycle, _MIN_STALE_SECONDS)
    if now - ts > stale_after:
        return "stopped"
    # Priorité : stopped > expired > scripts_stale > ok. « scripts_stale » est un
    # AVERTISSEMENT (la synchro tourne, mais des scripts NAS sont à déployer),
    # donc SOUS les états critiques (démons arrêtés, session expirée).
    if not status.get("auth_ok", True):
        return "expired"
    if status.get("nas_scripts_stale"):
        return "scripts_stale"
    return "ok"


def read_status():
    try:
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def build_editor_cmd(status):
    """Commande d'ouverture de l'éditeur. Si le battement de cœur indique le
    fichier de mappings ACTIF (celui des démons) et qu'il existe toujours,
    l'éditeur s'ouvre directement DESSUS — sinon, ouverture simple. Fonction
    pure (testable sans GTK)."""
    cmd = [sys.executable, EDITOR]
    if isinstance(status, dict):
        mp = status.get("mappings_path")
        if isinstance(mp, str) and mp and os.path.isfile(mp):
            cmd.append(mp)
    return cmd


TOOLTIPS = {
    "ok":            lambda: _("Proton Drive sync — active, session OK"),
    "scripts_stale": lambda: _("Proton Drive sync — NAS scripts out of date: "
                               "open the editor and run Install / Update"),
    "expired":       lambda: _("Proton Drive sync — session expired or keyring locked"),
    "stopped":       lambda: _("Proton Drive sync — daemons stopped"),
}


def main():
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("XApp", "1.0")
        from gi.repository import Gtk, XApp, GLib
    except (ImportError, ValueError) as e:
        print(_("Tray indicator unavailable: {e}\n"
                "Install the packages: python3-gi gir1.2-xapp-1.0").format(e=e))
        sys.exit(1)

    icon = XApp.StatusIcon()
    icon.set_name("proton-drive-sync")

    state_holder = {"state": None}

    def apply_state(state):
        if state == state_holder["state"]:
            return
        state_holder["state"] = state
        icon.set_icon_name(ICONS[state])
        icon.set_tooltip_text(TOOLTIPS[state]())

    def open_editor(*_a):
        subprocess.Popen(build_editor_cmd(read_status()),
                         cwd=APP_DIR, start_new_session=True)

    def quit_app(*_a):
        Gtk.main_quit()

    # Clic gauche -> ouvrir l'éditeur.
    icon.connect("activate", open_editor)

    # Clic droit -> menu.
    menu = Gtk.Menu()
    mi_open = Gtk.MenuItem(label=_("Open the mappings editor"))
    mi_open.connect("activate", open_editor)
    menu.append(mi_open)
    menu.append(Gtk.SeparatorMenuItem())
    mi_quit = Gtk.MenuItem(label=_("Quit the indicator"))
    mi_quit.connect("activate", quit_app)
    menu.append(mi_quit)
    menu.show_all()
    icon.set_secondary_menu(menu)

    def refresh():
        # L'applet s'éteint de lui-même si le réglage est décoché dans le GUI.
        if _HAS_CONFIG and not appconfig.tray_enabled():
            Gtk.main_quit()
            return False
        apply_state(decide_state(read_status(), time.time()))
        return True   # re-planifier

    apply_state(decide_state(read_status(), time.time()))
    GLib.timeout_add_seconds(REFRESH_SECONDS, refresh)
    Gtk.main()


if __name__ == "__main__":
    main()

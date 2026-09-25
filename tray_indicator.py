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
__version__ = "1.3.0"   # version propre à CE fichier ; incrémentée quand il change (indépendant de GitHub)

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
    # « degraded » réutilise volontairement l'icône de session expirée : elle
    # attire l'œil, et ajouter un cinquième dessin aurait dilué le signal sans
    # rien apprendre de plus. La distinction se fait dans l'infobulle, qui
    # énumère TOUTES les causes actives (voir build_tooltip) — sinon, avec deux
    # causes possibles derrière la même icône, la chaîne de priorité en cacherait
    # une.
    "degraded":      os.path.join(APP_DIR, "tray_expired.png"),
    "expired":       os.path.join(APP_DIR, "tray_expired.png"),
    "stopped":       os.path.join(APP_DIR, "tray_stopped.png"),
}

REFRESH_SECONDS = 5     # cadence de lecture du battement de cœur

# Marge de fraîcheur : le battement est réécrit chaque cycle (par défaut 30 s) ;
# on tolère 3 cycles manqués (ou 90 s minimum) avant de déclarer « arrêté »,
# pour ne pas clignoter sur un cycle un peu long (gros dossier en traitement).
_MIN_STALE_SECONDS = 90


def _alert_lists(status):
    """Les deux listes d'alerte de status.json, nettoyées.

    Tolérant : clés absentes (battement écrit par une version antérieure) ou
    contenu inattendu -> listes vides. L'indicateur ne doit jamais tomber à
    cause d'un fichier d'observation."""
    if not isinstance(status, dict):
        return [], []
    def _liste(cle):
        v = status.get(cle)
        return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []
    return _liste("unreadable"), _liste("cold_roots")


def decide_state(status, now):
    """État de l'icône à partir du contenu de status.json (dict ou None).
    Fonction PURE (testable sans GTK).
    Retourne 'ok' | 'scripts_stale' | 'degraded' | 'expired' | 'stopped'."""
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
    # Priorité : stopped > expired > degraded > scripts_stale > ok.
    #
    # « degraded » passe DEVANT « scripts_stale » : un dossier illisible veut
    # dire que de la sauvegarde ne se fait pas — des scripts NAS à déployer, non.
    # Il reste DERRIÈRE « expired », qui empêche toute synchro.
    if not status.get("auth_ok", True):
        return "expired"
    illisibles, racines = _alert_lists(status)
    if illisibles or racines:
        return "degraded"
    if status.get("nas_scripts_stale"):
        return "scripts_stale"
    return "ok"


def read_status():
    try:
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def scheduled_mappings_path():
    """Chemin du fichier de mappings du service PLANIFIÉ, ou None. Sert de repli
    quand aucun battement de cœur n'est disponible : le heartbeat n'est écrit que
    par le consommateur TEMPS RÉEL ; si l'utilisateur n'a installé QUE la
    planification, l'éditeur s'ouvrait vide. Tolérant : le systray ne doit jamais
    tomber à cause de cette recherche."""
    try:
        import schedule_manager
        return schedule_manager.read_service_mappings_path()
    except Exception:
        return None


def build_editor_cmd(status, fallback_path=None):
    """Commande d'ouverture de l'éditeur. Si le battement de cœur indique le
    fichier de mappings ACTIF (celui des démons) et qu'il existe toujours,
    l'éditeur s'ouvre directement DESSUS. Sinon, on retombe sur `fallback_path`
    (typiquement le fichier du service planifié) s'il existe — sinon, ouverture
    simple. Fonction pure (testable sans GTK) : la résolution du repli est faite
    par l'appelant, pas ici."""
    cmd = [sys.executable, EDITOR]
    if isinstance(status, dict):
        mp = status.get("mappings_path")
        if isinstance(mp, str) and mp and os.path.isfile(mp):
            cmd.append(mp)
            return cmd
    if isinstance(fallback_path, str) and fallback_path and os.path.isfile(fallback_path):
        cmd.append(fallback_path)
    return cmd


TOOLTIPS = {
    "ok":            lambda: _("Proton Drive sync — active, session OK"),
    "scripts_stale": lambda: _("Proton Drive sync — NAS scripts out of date: "
                               "open the editor and run Install / Update"),
    "degraded":      lambda: _("Proton Drive sync — some folders are not being "
                               "backed up"),
    "expired":       lambda: _("Proton Drive sync — session expired or keyring locked"),
    "stopped":       lambda: _("Proton Drive sync — daemons stopped"),
}

# Nombre de chemins détaillés dans l'infobulle avant de résumer. Une infobulle
# n'est pas un journal : au-delà, on dit combien il en reste et on renvoie à
# l'éditeur, qui a la place de tout afficher.
_MAX_PATHS_IN_TOOLTIP = 3


def build_tooltip(status, state):
    """Texte de l'infobulle : l'état principal, PUIS toutes les causes actives.

    Fonction PURE (testable sans GTK).

    POURQUOI énumérer au lieu de se contenter de l'état. « degraded » et
    « expired » partagent la même icône, et la chaîne de priorité n'en retient
    qu'un : si la session expire pendant qu'un dossier est illisible, l'icône ne
    changerait pas et le second problème resterait invisible. L'infobulle dit
    donc tout ce qui est vrai en ce moment, pas seulement ce qui a gagné.

    On NOMME les chemins : une alerte qui ne dit pas sur quoi agir ne sert à
    rien. C'est aussi pourquoi rien n'est publié dont on ne sache dire la
    cause."""
    lignes = [TOOLTIPS.get(state, TOOLTIPS["ok"])()]
    illisibles, racines = _alert_lists(status)
    if illisibles:
        lignes.append("")
        lignes.append(_("Cannot be read — their contents are NOT backed up:"))
        for p in illisibles[:_MAX_PATHS_IN_TOOLTIP]:
            lignes.append("  • " + p)
        reste = len(illisibles) - _MAX_PATHS_IN_TOOLTIP
        if reste > 0:
            lignes.append(_("  … and {n} more").format(n=reste))
    if racines:
        lignes.append("")
        lignes.append(_("Never fully analysed — real-time does not cover them "
                        "yet; run “Prime the cache”:"))
        for p in racines[:_MAX_PATHS_IN_TOOLTIP]:
            lignes.append("  • " + p)
        reste = len(racines) - _MAX_PATHS_IN_TOOLTIP
        if reste > 0:
            lignes.append(_("  … and {n} more").format(n=reste))
    return "\n".join(lignes)


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

    shown = {"state": None, "tooltip": None}

    def apply_state(state, tooltip):
        # On compare le COUPLE (état, infobulle), pas l'état seul. Sinon, tant
        # que l'état reste le même — « degraded » avec un dossier illisible de
        # plus, ou « expired » pendant qu'un dossier devient illisible —
        # l'infobulle resterait figée sur son premier texte.
        if state == shown["state"] and tooltip == shown["tooltip"]:
            return
        shown["state"] = state
        shown["tooltip"] = tooltip
        icon.set_icon_name(ICONS[state])
        icon.set_tooltip_text(tooltip)

    def open_editor(*_a):
        subprocess.Popen(build_editor_cmd(read_status(), scheduled_mappings_path()),
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

    def _refresh_once():
        status = read_status()
        state = decide_state(status, time.time())
        apply_state(state, build_tooltip(status, state))

    def refresh():
        # L'applet s'éteint de lui-même si le réglage est décoché dans le GUI.
        if _HAS_CONFIG and not appconfig.tray_enabled():
            Gtk.main_quit()
            return False
        _refresh_once()
        return True   # re-planifier

    _refresh_once()
    GLib.timeout_add_seconds(REFRESH_SECONDS, refresh)
    Gtk.main()


if __name__ == "__main__":
    main()

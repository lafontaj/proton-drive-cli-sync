#!/usr/bin/env python3
"""
Éditeur de mappings + lanceur de synchro pour Proton Drive (Jean / Maryse).

Deux rôles :
  1. Éditer la liste des paires source (local) / destination (Proton Drive)
     dans un fichier JSON, lu ensuite par le moteur (proton_sync.py).
  2. Lancer la synchro directement depuis l'interface (ou copier la commande
     équivalente pour la coller dans un terminal), avec les options voulues.

Usage :
    python3 proton_mapping_editor.py                # ouvre un sélecteur de fichier
    python3 proton_mapping_editor.py mappings-user1.json
"""
import json
import os
import queue
import sys
import threading
import time
import subprocess
import datetime
import shlex
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog, scrolledtext

# --- i18n (socle gettext du projet) ---
# Import guardé : l'absence de i18n.py ne casse rien, on affiche alors les
# chaînes source (anglais). Même pattern que mount_check dans local_watcher.
try:
    import i18n
    from i18n import _
    _HAS_I18N = True
except ImportError:
    def _(s):
        return s
    _HAS_I18N = False

# Détection du type de source (nfs/local) — module de l'étape 1.
# Importé de façon tolérante : si le fichier n'est pas là, le GUI fonctionne
# quand même, mais sans la détection automatique (on prévient l'utilisateur).
try:
    import mount_check
    _HAS_MOUNT_CHECK = True
except ImportError:
    _HAS_MOUNT_CHECK = False

try:
    import schedule_manager
    _HAS_SCHEDULE = True
except ImportError:
    _HAS_SCHEDULE = False

try:
    import realtime_manager
    _HAS_REALTIME = True
except ImportError:
    _HAS_REALTIME = False

# Réglages d'installation (chemins, présence NAS, extensions...) : une SEULE
# source de vérité partagée par le moteur, le GUI et les démons. Import
# tolérant : si absent, le GUI retombe sur les défauts historiques.
try:
    import config as appconfig
    _HAS_CONFIG = True
except ImportError:
    _HAS_CONFIG = False

# Moteur importé comme MODULE pour réutiliser get_remote_listing (navigateur de
# destinations Proton) — aucune logique de parsing parallèle. Import tolérant :
# sans lui, le bouton « Parcourir Proton… » affiche simplement une erreur.
try:
    import proton_sync as _ENGINE
    _HAS_ENGINE = True
except Exception:
    _ENGINE = None
    _HAS_ENGINE = False

# Classe Exclusions du moteur, réutilisée pour calculer l'empreinte d'exclusions
# effective courante (indicateur d'état « prêt » (b)) — même logique que le cache,
# aucune duplication. Import tolérant : si indisponible, l'indicateur reste prudent.
try:
    from proton_sync import Exclusions as _PS_EXCL
except Exception:
    _PS_EXCL = None

APP_TITLE = _("Mappings editor — Proton Drive sync")

# ============================================================
#  Palette et dialogues sur mesure (style Proton)
# ============================================================
# Couleurs inspirées de l'identité Proton, pour des boîtes de dialogue propres
# et cohérentes, en remplacement des messagebox natifs (police trop grasse,
# mise en page rigide).
PROTON_PURPLE = "#6d4aff"
DLG_BG = "#ffffff"
DLG_TEXT = "#1f1b2e"
DLG_MUTED = "#555577"
# Accent par type de message (la bande supérieure + le pictogramme).
DLG_KINDS = {
    "info":     {"accent": "#6d4aff", "glyph": "i",  "title": "Information"},
    "question": {"accent": "#6d4aff", "glyph": "?",  "title": "Confirmation"},
    "warning":  {"accent": "#e8a200", "glyph": "!",  "title": "Avertissement"},
    "error":    {"accent": "#d2294b", "glyph": "✕",  "title": "Erreur"},
    "success":  {"accent": "#1a9e57", "glyph": "✓",  "title": "Succès"},
}


class StyledDialog(tk.Toplevel):
    """Boîte de dialogue sur mesure, cohérente avec l'identité visuelle.

    Bande de couleur supérieure (accent selon le type) + pictogramme, titre,
    message en police propre (sans gras systématique), et un ou deux boutons.
    Retourne True/False (pour les confirmations) ou True (simple acquittement)
    via l'attribut .result après fermeture.
    """

    def __init__(self, parent, message, kind="info", title=None,
                 ok_text="OK", cancel_text=None,
                 checkbox_text=None, checkbox_default=False):
        super().__init__(parent)
        # CACHÉE pendant la construction : un Toplevel devient visible dès sa
        # création, à la position par défaut du gestionnaire de fenêtres, PUIS
        # le centrage le déplace — d'où le « saut » visible à chaque ouverture
        # (défaut présent depuis l'origine). On construit fenêtre retirée, on
        # calcule la position, et on la révèle DÉJÀ en place.
        self.withdraw()
        spec = DLG_KINDS.get(kind, DLG_KINDS["info"])
        self.result = False
        # Case à cocher optionnelle (ex. « vider aussi le Drive »). Sa valeur au
        # moment du OK est exposée via self.checkbox_value.
        self.checkbox_value = bool(checkbox_default)
        self._cb_var = tk.BooleanVar(value=bool(checkbox_default)) if checkbox_text else None
        self.configure(bg=DLG_BG)
        self.title(title or spec["title"])
        self.resizable(False, False)
        self.transient(parent)

        # Bande d'accent supérieure avec pictogramme + titre.
        band = tk.Frame(self, bg=spec["accent"], height=54)
        band.pack(side="top", fill="x")
        band.pack_propagate(False)
        tk.Label(band, text=spec["glyph"], bg=spec["accent"], fg="white",
                 font=("DejaVu Sans", 20, "bold")).pack(side="left", padx=(16, 8))
        tk.Label(band, text=title or spec["title"], bg=spec["accent"], fg="white",
                 font=("DejaVu Sans", 13)).pack(side="left")

        def on_ok():
            self.result = True
            if self._cb_var is not None:
                self.checkbox_value = bool(self._cb_var.get())
            self.destroy()

        def on_cancel():
            self.result = False
            self.destroy()

        # IMPORTANT : on réserve la zone des BOUTONS EN PREMIER, ancrée en BAS
        # (side="bottom"), AVANT de placer le corps. Sinon, avec un message long,
        # le corps (expand=True) pousse les boutons hors de la fenêtre non
        # redimensionnable et le bouton de lancement devient invisible (bug connu
        # de la confirmation « Propager suppressions »). En réservant le bas
        # d'abord, les boutons sont toujours visibles quelle que soit la longueur
        # du message.
        btns = tk.Frame(self, bg=DLG_BG, padx=22, pady=12)
        btns.pack(side="bottom", fill="x")

        # Bouton principal (accent), à droite.
        ok_btn = tk.Button(btns, text=ok_text, command=on_ok,
                           bg=spec["accent"], fg="white",
                           activebackground=spec["accent"], activeforeground="white",
                           font=("DejaVu Sans", 10), relief="flat",
                           padx=18, pady=6, cursor="hand2", bd=0)
        ok_btn.pack(side="right")

        if cancel_text:
            cancel_btn = tk.Button(btns, text=cancel_text, command=on_cancel,
                                   bg="#e8e8ef", fg=DLG_TEXT,
                                   activebackground="#dadae5", activeforeground=DLG_TEXT,
                                   font=("DejaVu Sans", 10), relief="flat",
                                   padx=18, pady=6, cursor="hand2", bd=0)
            cancel_btn.pack(side="right", padx=(0, 8))

        # Corps : le message. Placé APRÈS les boutons, il occupe l'espace restant
        # entre la bande et la zone de boutons (déjà réservée en bas).
        body = tk.Frame(self, bg=DLG_BG, padx=22, pady=18)
        body.pack(side="top", fill="both", expand=True)
        tk.Label(body, text=message, bg=DLG_BG, fg=DLG_TEXT,
                 font=("DejaVu Sans", 10), justify="left", wraplength=420,
                 anchor="w").pack(anchor="w")

        if checkbox_text:
            tk.Checkbutton(body, text=checkbox_text, variable=self._cb_var,
                           bg=DLG_BG, fg=DLG_TEXT, activebackground=DLG_BG,
                           font=("DejaVu Sans", 10), anchor="w",
                           selectcolor=DLG_BG, cursor="hand2").pack(
                               anchor="w", pady=(12, 0))

        # Raccourcis clavier : Entrée = OK, Échap = Annuler/Fermer.
        self.bind("<Return>", lambda e: on_ok())
        self.bind("<Escape>", lambda e: on_cancel())
        ok_btn.focus_set()

        # Centrer sur le parent PENDANT que la fenêtre est retirée, puis la
        # révéler déjà en place. wait_visibility avant grab_set : un grab sur
        # une fenêtre pas encore affichée échoue (« window not viewable »).
        self.update_idletasks()
        self._center_on(parent)
        self.deiconify()
        try:
            self.wait_visibility()
        except tk.TclError:
            pass
        self.grab_set()
        self.wait_window()

    def _center_on(self, parent):
        try:
            pw, ph = parent.winfo_width(), parent.winfo_height()
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            # winfo_width d'une fenêtre RETIRÉE vaut 1 : on mesure la taille
            # DEMANDÉE (reqwidth/reqheight), fiable avant affichage.
            w = max(self.winfo_width(), self.winfo_reqwidth())
            h = max(self.winfo_height(), self.winfo_reqheight())
            x = px + (pw - w) // 2
            y = py + (ph - h) // 3
            self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        except Exception:
            pass


def dlg_info(parent, message, title=None):
    StyledDialog(parent, message, kind="info", title=title)


def dlg_error(parent, message, title=None):
    StyledDialog(parent, message, kind="error", title=title)


def dlg_warning(parent, message, title=None):
    StyledDialog(parent, message, kind="warning", title=title)


def dlg_success(parent, message, title=None):
    StyledDialog(parent, message, kind="success", title=title)


def dlg_confirm(parent, message, title=None, kind="question",
                ok_text=_("Yes"), cancel_text=_("Cancel")):
    """Confirmation Oui/Annuler. Retourne True si l'utilisateur confirme."""
    d = StyledDialog(parent, message, kind=kind, title=title,
                     ok_text=ok_text, cancel_text=cancel_text)
    return d.result


def dlg_confirm_checkbox(parent, message, checkbox_text, title=None,
                         kind="question", ok_text=_("Yes"), cancel_text=_("Cancel"),
                         checkbox_default=False):
    """Confirmation Oui/Annuler AVEC une case à cocher (ex. option de vidage).
    Retourne (confirmé: bool, case_cochée: bool)."""
    d = StyledDialog(parent, message, kind=kind, title=title,
                     ok_text=ok_text, cancel_text=cancel_text,
                     checkbox_text=checkbox_text, checkbox_default=checkbox_default)
    return d.result, d.checkbox_value



# Icône de la fenêtre (barre des tâches, Alt+Tab).
# Doit être un PNG nommé « icone.png », placé dans le MÊME dossier que ce script
# (c.-à-d. ~/Logiciels/Proton-drive/icone.png). Chaque utilisateur (Jean, Maryse)
# y dépose l'image de son choix sous ce nom — aucune modification du code requise.
# Si le fichier est absent, l'application démarre quand même, sans icône perso.
WINDOW_ICON_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "icone.png"
)

# Dossier où vit l'application — point de départ par défaut des sélecteurs de
# fichiers (c'est là que sont rangés les mappings-*.json), ET base de résolution
# des autres emplacements ci-dessous (dérivés de __file__, jamais recopiés en
# dur — installer le dossier entier ailleurs fonctionne sans rien reconfigurer).
APP_DIR = os.path.dirname(os.path.abspath(__file__))

# Emplacements par défaut. Le binaire CLI suit la résolution PARTAGÉE de
# config.py (variable d'environnement > réglage persistant > défaut intégré) ;
# les deux autres se déduisent simplement d'APP_DIR (pas un réglage distinct —
# c'est toujours « à côté de ce fichier »).
DEFAULT_CLI = appconfig.resolve_proton_cli() if _HAS_CONFIG else os.path.join(APP_DIR, "proton-drive")
DEFAULT_ENGINE = os.path.join(APP_DIR, "proton_sync.py")
DEFAULT_LOG_DIR = os.path.join(APP_DIR, "logs")


# ============================================================
#  Sélecteurs de fichiers (zenity natif GTK -> repli Tk)
# ============================================================
# Le sélecteur intégré de Tk est en colonnes horizontales avec ascenseur
# horizontal, jugé désagréable. On privilégie donc zenity (vrai sélecteur GTK
# vertical, avec favoris et navigation NFS), avec repli sur filedialog Tk si
# zenity est absent (autre machine). L'API reste la même quel que soit le moteur.
import shutil

_ZENITY = shutil.which("zenity")


def _zenity_run(args):
    """Lance zenity avec les arguments donnés. Retourne le chemin choisi (str)
    ou None si annulé/erreur. L'environnement de locale est ajusté pour que
    l'interface PROPRE de zenity (calendrier, jours, mois, boutons) suive la
    langue choisie dans le GUI et non celle du système."""
    env = i18n.subprocess_env() if _HAS_I18N else None
    try:
        r = subprocess.run([_ZENITY] + args, capture_output=True, text=True,
                           env=env)
    except Exception:
        return None
    if r.returncode != 0:
        return None  # annulé par l'utilisateur
    out = r.stdout.strip()
    return out or None


def pick_open_file(parent=None, title=_("Open a mappings file"),
                   initialdir=None, json_only=True):
    """Sélecteur d'ouverture de fichier. zenity si dispo, sinon Tk."""
    initialdir = initialdir or APP_DIR
    if _ZENITY:
        args = ["--file-selection", "--title", title,
                # --filename définit le dossier de départ (slash final = dossier)
                "--filename", initialdir.rstrip("/") + "/"]
        if json_only:
            args += ["--file-filter", "JSON | *.json",
                     "--file-filter", "Tous les fichiers | *"]
        return _zenity_run(args)
    # Repli Tk
    filetypes = [("JSON", "*.json"), ("Tous les fichiers", "*.*")] if json_only \
        else [("Tous les fichiers", "*.*")]
    return filedialog.askopenfilename(title=title, filetypes=filetypes,
                                      initialdir=initialdir) or None


def pick_save_file(parent=None, title=_("Save the mappings file"),
                   initialdir=None, initialfile="mappings.json"):
    """Sélecteur d'enregistrement. zenity si dispo, sinon Tk."""
    initialdir = initialdir or APP_DIR
    if _ZENITY:
        start = os.path.join(initialdir, initialfile)
        args = ["--file-selection", "--save", "--confirm-overwrite",
                "--title", title, "--filename", start,
                "--file-filter", "JSON | *.json",
                "--file-filter", "Tous les fichiers | *"]
        path = _zenity_run(args)
        # S'assurer de l'extension .json si l'utilisateur ne l'a pas mise
        if path and not path.lower().endswith(".json"):
            path += ".json"
        return path
    return filedialog.asksaveasfilename(
        title=title, defaultextension=".json",
        filetypes=[("JSON", "*.json")], initialdir=initialdir,
        initialfile=initialfile) or None


def pick_move_target(parent=None, title=None, initialdir=None):
    """Sélecteur pour la DESTINATION d'un transfert de mapping : le fichier peut
    être EXISTANT (on y ajoute le mapping) ou NOUVEAU (on le crée). On n'utilise
    donc PAS --confirm-overwrite : choisir un fichier existant ne l'« écrase »
    pas, on lui ajoute une entrée — le message « le fichier existe, remplacer ? »
    du sélecteur d'enregistrement serait trompeur ici. zenity si dispo, sinon Tk."""
    title = title or _("Move to which mappings file?")
    initialdir = initialdir or APP_DIR
    if _ZENITY:
        start = os.path.join(initialdir, "mappings.json")
        args = ["--file-selection", "--save",   # --save autorise un nom nouveau…
                "--title", title, "--filename", start,          # …SANS confirm-overwrite
                "--file-filter", "JSON | *.json",
                "--file-filter", "Tous les fichiers | *"]
        path = _zenity_run(args)
        if path and not path.lower().endswith(".json"):
            path += ".json"
        return path
    # Tk : asksaveasfilename avec confirmoverwrite=False (permet existant ou neuf,
    # sans dialogue d'écrasement).
    return filedialog.asksaveasfilename(
        title=title, defaultextension=".json",
        filetypes=[("JSON", "*.json")], initialdir=initialdir,
        initialfile="mappings.json", confirmoverwrite=False) or None


def pick_directory(parent=None, title=_("Choose a source folder"), initialdir=None):
    """Sélecteur de dossier. zenity si dispo, sinon Tk."""
    initialdir = initialdir or "/media"
    if _ZENITY:
        args = ["--file-selection", "--directory", "--title", title,
                "--filename", initialdir.rstrip("/") + "/"]
        return _zenity_run(args)
    return filedialog.askdirectory(title=title, initialdir=initialdir) or None


# Explications des options, affichées via le bouton « ? » à côté de chaque case.
OPTION_HELP = {
    "dry-run": _(
        "Test mode (--dry-run)\n\n"
        "Shows what would be done — which files would be sent — WITHOUT "
        "transferring anything and WITHOUT touching the cache.\n\n"
        "Ideal to check what the sync would do before running it for real."
    ),
    "verify-hash": _(
        "Content verification (--verify-hash)\n\n"
        "In addition to comparing file sizes, computes a SHA1 fingerprint of "
        "the content to detect modified files whose size did not change "
        "(e.g. a rewritten music tag).\n\n"
        "SLOWER: reads every file in full. Also ignores the cache.\n"
        "Reserve it for an occasional check (e.g. once a month), not for "
        "daily use."
    ),
    "verbose": _(
        "Detailed mode (-v)\n\n"
        "Shows the RAW output of the engine: every file examined (unchanged, "
        "uploaded, renamed…), cache skips (“cache valid”), and transfer "
        "summaries — instead of the condensed view (one line per folder as "
        "it is scanned, plus status/error lines only).\n\n"
        "Useful to see exactly what happens inside a folder (e.g. to check "
        "why a file failed), but the output is much longer.\n\n"
        "TIP: combine with “❗ Errors only” to see just the problems within "
        "a detailed run, without scrolling through everything."
    ),
    "delete": _(
        "Propagate deletions (--delete)\n\n"
        "This box only affects the MANUAL “▶ Run sync” button. Priming and "
        "Reset ignore it: they always follow each mapping's own configuration "
        "(its trash field).\n\n"
        "MASTER SWITCH for a manual run. Without this box, a manual sync is "
        "purely additive: it sends, but never erases anything on Proton — even "
        "for mappings set to mirror. With this box, every mapping that has "
        "“Allow deletion” (set in Edit) propagates local deletions: what is on "
        "Proton but was deleted locally gets deleted.\n\n"
        "The deletion MODE (trash recoverable for 30 days, or permanent) is "
        "the one defined in each mapping. This box only enables the "
        "propagation; the mapping decides trash or permanent.\n\n"
        "A safety check first verifies that the source is healthy (NAS "
        "mount alive); when in doubt, deletions for that mapping are "
        "disabled and only uploads happen.\n\n"
        "TIP: run “Test (dry-run)” first to see what would be deleted, "
        "without erasing anything."
    ),
}

# Textes d'aide (« ? ») pour le dialogue Configuration — niveau non-programmeur,
# même mécanisme que OPTION_HELP (un bouton « ? » par réglage).
CONFIG_HELP = {
    "nas-enabled": _(
        "Use a NAS\n\n"
        "Turn this ON if your files come from a NAS (a network storage box) "
        "mounted on this computer, in addition to — or instead of — purely "
        "local folders.\n\n"
        "Turn it OFF if this computer only backs up its own local folders, "
        "with no NAS at all. When off, the app never tries to reach a NAS — "
        "no more waiting, no more messages about a missing network drive.\n\n"
        "Changing this takes effect the next time you restart this window "
        "and the background services."
    ),
    "nas-mount-path": _(
        "NAS mount point\n\n"
        "The folder on THIS computer where the NAS's shared configuration "
        "and marker queue appear — not your NAS document folders themselves, "
        "just the small technical folder used to talk to the NAS watcher.\n\n"
        "Leave the default unless you know your NAS is mounted at a "
        "different location. Only used when “Use a NAS” is turned on."
    ),
    "rename-ext-enabled": _(
        "Fix uppercase extensions\n\n"
        "Proton Drive can fail to show a thumbnail, preview or correct icon "
        "for a file whose extension is written in UPPERCASE (e.g. "
        "“PHOTO.JPG” instead of “photo.jpg”) — even though the file itself "
        "is perfectly fine.\n\n"
        "When this is on, the app automatically renames such files on your "
        "computer (only the extension, never the name) so Proton Drive "
        "recognizes them correctly. Every rename is written to a log file, "
        "and the app never overwrites another file by mistake (see the "
        "suffix setting below).\n\n"
        "Turn this off only if you have a specific reason to keep uppercase "
        "extensions exactly as they are."
    ),
    "rename-ext-suffix": _(
        "Suffix used to avoid overwriting\n\n"
        "If fixing an extension would create a file that already exists "
        "(e.g. renaming “DOC.PDF” to “doc.pdf” when a different “doc.pdf” is "
        "already there), the app adds this short piece of text to the new "
        "file's name instead of overwriting anything (e.g. "
        "“doc_ProtonEditExt.pdf”).\n\n"
        "You can change this text if you prefer something else, but it "
        "cannot contain / \\ \" ' (characters that would break a file name)."
    ),
    "proton-cli-path": _(
        "Path to the Proton Drive CLI\n\n"
        "This app does not include Proton's CLI — you download it separately "
        "from Proton (see the README) and it does the actual talking to your "
        "Drive.\n\n"
        "Leave this empty if you placed the “proton-drive” binary in the same "
        "folder as the app (the simplest setup). Otherwise, put the full path "
        "to the binary here, e.g. /home/me/tools/proton-drive.\n\n"
        "The background services pick up this path too (they are reinstalled "
        "on the next Install/Update). An advanced alternative is the "
        "PROTON_DRIVE_CLI environment variable, which takes priority over this "
        "setting."
    ),
    "nas-identity": _(
        "NAS identity (account name)\n\n"
        "The stable name used on the NAS for YOUR configuration copy and "
        "marker queue (config/mappings-<name>.json and queue/<name>). It "
        "depends neither on your mappings file's name nor on your Proton "
        "account: you can rename files or switch accounts, the NAS side "
        "stays the same.\n\n"
        "It is set automatically the first time: from your existing services "
        "on an upgrade, or a neutral unique name (user1, user2, …) claimed on "
        "the NAS at first use on a fresh install.\n\n"
        "If you change it and the old name exists on the NAS, the app offers "
        "to MIGRATE it (a simple rename — pending markers preserved). "
        "Afterwards, restart the background services so they follow the new "
        "name. Leave the field empty to return to automatic mode."
    ),
    "tray-icon": _(
        "Status icon in the system tray\n\n"
        "Shows a small circular double-arrow icon near the clock:\n"
        "  • purple — the background service is running and the Proton "
        "session is fine;\n"
        "  • grey with a red X — the service is running but the Proton "
        "session has expired (sign in again from this window);\n"
        "  • grey — the background service is stopped.\n\n"
        "Left-click opens this editor. The icon starts automatically with "
        "your session. It only reads a small local status file — it never "
        "contacts Proton itself.\n\n"
        "Requires the system packages “python3-gi” and “gir1.2-xapp-1.0” "
        "(already installed on Linux Mint)."
    ),
}


class RemoteFolderPicker(tk.Toplevel):
    """Navigateur des dossiers EXISTANTS sur Proton Drive, pour choisir une
    destination de mapping sans la retranscrire à la main (une faute de frappe
    créerait une NOUVELLE arborescence au lieu de rejoindre l'existante).

    - Arbre paresseux depuis la racine « / » : montre TOUTES les racines que le
      CLI expose — dont /my-files et /shared-with-me (les destinations
      partagées en écriture deviennent visibles, donc utilisables).
    - Chaque dossier est listé À L'OUVERTURE de son nœud (un appel CLI par
      dossier, en thread — l'UI ne gèle jamais), avec cache : replier/déplier
      ne re-liste pas.
    - Les appels CLI sont SÉRIALISÉS par le même verrou que les sondes d'auth
      (app._auth_lock) : pas de contention de trousseau avec le reste du GUI.
    - Réutilise get_remote_listing du MOTEUR (import proton_sync) — aucune
      logique de parsing parallèle."""

    _DUMMY = "\x01loading"

    def __init__(self, parent, app, dest_var):
        super().__init__(parent)
        self.withdraw()                      # anti-saut (même principe que StyledDialog)
        self.app = app
        self.dest_var = dest_var
        self._cache = {}                     # chemin -> [noms de sous-dossiers]
        self.title(_("Choose a destination folder on Proton Drive"))
        self.transient(parent)

        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="both", expand=True)

        self.tree = ttk.Treeview(frm, show="tree", selectmode="browse", height=18)
        ysb = ttk.Scrollbar(frm, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ysb.pack(side="left", fill="y")

        bottom = ttk.Frame(self, padding=(10, 0, 10, 10))
        bottom.pack(fill="x")
        self.status_var = tk.StringVar(value=_("Loading…"))
        ttk.Label(bottom, textvariable=self.status_var, foreground="#555577",
                  wraplength=430, justify="left").pack(side="left", fill="x", expand=True)
        ttk.Button(bottom, text=_("Cancel"), command=self.destroy).pack(side="right", padx=(4, 0))
        self.choose_btn = ttk.Button(bottom, text=_("Choose this folder"),
                                     command=self._choose, state="disabled")
        self.choose_btn.pack(side="right")

        self.tree.bind("<<TreeviewOpen>>", self._on_open)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", lambda e: self._choose()
                       if str(self.choose_btn.cget("state")) == "normal" else None)

        self.minsize(460, 420)
        self.update_idletasks()
        try:
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            self.geometry(f"+{px + 60}+{py + 40}")
        except Exception:
            pass
        self.deiconify()
        try:
            self.wait_visibility()
        except tk.TclError:
            pass
        self.grab_set()

        self._populate("", "/")              # racine, en thread

    # ---- arbre paresseux ----
    def _node_path(self, item):
        return item                          # iid == chemin distant complet

    def _insert_folder(self, parent_item, path, name):
        iid = path
        if self.tree.exists(iid):
            return
        self.tree.insert(parent_item, "end", iid=iid, text=" 📁 " + name)
        # Enfant factice -> le nœud est dépliable ; remplacé au premier dépliage.
        self.tree.insert(iid, "end", iid=iid + self._DUMMY, text=_("Loading…"))

    # Racines PROPOSÉES comme destination (liste blanche, décision Jean) :
    # /my-files (l'espace du compte) et /shared-with-me (dossiers partagés en
    # écriture — l'intérêt même du sélecteur). Les autres contextes sont
    # écartés : /trash et /photos-trash (aucun sens comme destination),
    # /photos, /albums et leurs variantes (mécanique Proton Photos, pas le
    # système de fichiers — notre moteur n'y écrit pas), /devices (sauvegardes
    # des clients officiels), /shared-by-me (simple VUE de ses propres
    # partages : les éléments vivent déjà dans /my-files).
    _ALLOWED_ROOTS = ("/my-files", "/shared-with-me")

    def _list_roots(self):
        """Cas particulier de la racine « / » : le CLI y renvoie une forme
        DIFFÉRENTE — des éléments {"path": "/my-files"} SANS champ name ni type
        (constaté en production). On lit donc « path » directement, et on ne
        retient que la liste blanche. Retourne [(chemin, nom_affiché), ...]."""
        data, _err = _ENGINE.cli_json(["filesystem", "list", "/"])
        roots = []
        for item in data or []:
            p = item.get("path") if isinstance(item, dict) else None
            try:
                p = _ENGINE._unwrap(p)
            except Exception:
                pass
            if isinstance(p, str) and p in self._ALLOWED_ROOTS:
                roots.append((p, p.lstrip("/")))
        return roots

    def _populate(self, item, path):
        """Liste `path` en THREAD (CLI sérialisé par app._auth_lock) puis remplit
        le nœud `item` avec ses sous-dossiers, triés."""
        if path in self._cache:
            self._fill(item, self._cache[path])
            return
        def work():
            try:
                if _ENGINE is None:
                    raise RuntimeError("proton_sync.py missing next to the editor")
                with self.app._auth_lock:
                    if path == "/":
                        entries = self._list_roots()
                    else:
                        listing = _ENGINE.get_remote_listing(path)
                        base = path.rstrip("/")
                        entries = sorted(
                            ((base + "/" + n, n) for n, i in listing.items()
                             if (i.get("type") or "") == "folder"),
                            key=lambda t: str.casefold(t[1]))
                err = None
            except Exception as e:
                entries, err = [], str(e)
            def apply():
                if err is not None or (path == "/" and not entries):
                    self.status_var.set(_("Could not list Proton Drive — check the "
                                          "session (⚙ Configuration…) and try again."))
                    return
                self._cache[path] = entries
                self._fill(item, entries)
            try:
                self.after(0, apply)
            except tk.TclError:
                pass                          # fenêtre fermée entre-temps
        threading.Thread(target=work, daemon=True).start()

    def _fill(self, item, entries):
        for child in self.tree.get_children(item):
            if child.endswith(self._DUMMY):
                self.tree.delete(child)
        for full_path, name in entries:
            self._insert_folder(item, full_path, name)
        if item and not entries:
            self.tree.insert(item, "end", iid=item + "\x01empty",
                             text=_("(no subfolder)"))
        if not item:
            self.status_var.set(_("Double-click to open a folder; select the "
                                  "destination, then “Choose this folder”."))

    def _on_open(self, _e):
        item = self.tree.focus()
        if not item or item.endswith(self._DUMMY) or "\x01" in item:
            return
        children = self.tree.get_children(item)
        if len(children) == 1 and children[0].endswith(self._DUMMY):
            self._populate(item, item)

    def _on_select(self, _e):
        item = self.tree.focus()
        ok = bool(item) and "\x00" not in item
        self.choose_btn.config(state="normal" if ok else "disabled")
        if ok:
            self.status_var.set(item)

    def _choose(self):
        item = self.tree.focus()
        if item and "\x00" not in item:
            self.dest_var.set(item)
            self.destroy()


class MappingEditor(tk.Tk):
    def __init__(self, config_path=None):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1060x700")
        self.minsize(880, 580)

        # Icône de la fenêtre (barre des tâches / Alt+Tab). On garde une
        # référence sur self pour que l'image ne soit pas détruite par le
        # ramasse-miettes (sinon l'icône disparaît).
        self._set_window_icon()

        self.config_path = config_path
        self.mappings = []  # liste de dicts {type, source, dest_parent, [exclusions]}
        # Sonde d'auth : verrou de sérialisation (voir _check_auth_settled) et
        # marqueur « un message d'erreur d'auth est affiché en barre d'état »
        # (pour l'EFFACER dès que la session redevient bonne — sinon témoin vert
        # et message rouge coexistent, contradiction constatée en production).
        self._auth_lock = threading.Lock()
        self._auth_error_in_status = False
        self._account_email = None   # adresse du VRAI compte connecté (cache)
        # AMORÇAGE INTELLIGENT de l'identité NAS stable : si le réglage
        # « account_name » est vide et que des unités systemd existent déjà,
        # on le sème avec l'identité ACTUELLE (dérivée du chemin des unités) —
        # les installations existantes (user1, user2) sont préservées telles
        # quelles, sans rien taper. Installation neuve (pas d'unités) : le
        # réglage sera semé avec l'adresse Proton complète à la première
        # connexion réussie (voir _check_auth_settled).
        if _HAS_CONFIG and _HAS_REALTIME and not appconfig.account_name():
            try:
                old_units = realtime_manager.read_units_mappings_path()
                if old_units:
                    appconfig.set_account_name(
                        realtime_manager.user_from_mappings_path(old_units))
            except Exception:
                pass
        self.global_exclusions = {"names": [], "patterns": []}  # exclusions globales
        self.dirty = False
        self.sync_process = None  # processus de synchro en cours, le cas échéant

        # Options de lancement (cases à cocher)
        self.opt_dry_run = tk.BooleanVar(value=False)
        self.opt_verify_hash = tk.BooleanVar(value=False)
        self.opt_ignore_cache = tk.BooleanVar(value=False)
        self.opt_verbose = tk.BooleanVar(value=False)
        self.opt_delete = tk.BooleanVar(value=False)
        # Filtre d'AFFICHAGE seulement (n'affecte pas le moteur ni le log disque) :
        # quand coché, la zone de sortie ne montre que les lignes d'erreur/avertis-
        # sement (❌ ⚠ ⛔ / « échec »/« failed »). Utile pour vérifier vite ce qui a
        # coincé sans dérouler tout le journal. Re-décochable à tout moment.
        self.opt_errors_only = tk.BooleanVar(value=False)

        self._build_ui()

        if self.config_path and os.path.exists(self.config_path):
            self._load(self.config_path)
        else:
            self._update_title()
            self._update_excl_summary()

        # Détection automatique de l'état d'authentification Proton, en arrière-plan
        # (ne bloque pas le démarrage). Met à jour l'étiquette du bouton, et si la
        # session est indisponible (token expiré ou trousseau verrouillé), le
        # signale discrètement dans la barre d'état pour guider vers le bouton.
        if _HAS_REALTIME:
            self.after(300, self._detect_auth_at_startup)
            # Re-vérifier quand la fenêtre reprend le focus (ex. après s'être
            # reconnecté ailleurs) : l'affichage ne reste jamais périmé.
            self._auth_focus_after = None
            self.bind("<FocusIn>", self._on_focus_auth)

        # Rafraîchissement périodique (lent) de la colonne d'état : filet pour les
        # changements survenus HORS du GUI (planification/temps réel qui consolide
        # un mapping pendant que la fenêtre est ouverte). Les changements FAITS dans
        # le GUI (exclusions, mappings) rafraîchissent, eux, immédiatement.
        self.after(4000, self._state_slow_tick)

        # Pompe de sortie : un unique consommateur, côté thread PRINCIPAL, vide une
        # file thread-safe par LOTS. Remplace les after() inter-threads déposés
        # ligne par ligne depuis le thread moteur (qui, sous rafale — ex. milliers
        # de « 🗑 corbeille » d'un amorçage --delete — saturaient l'UI et figeaient
        # la sortie). Ici : une seule insertion + un seul autoscroll par lot.
        self._out_queue = queue.Queue()
        self.after(80, self._pump_output)

    def _state_slow_tick(self):
        try:
            self._refresh_state_column()
        except Exception:
            pass
        self.after(30000, self._state_slow_tick)   # toutes les 30 s

    def _on_focus_auth(self, _event=None):
        # Anti-rebond : <FocusIn> se déclenche souvent ; on ne sonde qu'après une
        # courte pause de stabilité, et pas en rafale.
        if getattr(self, "_auth_focus_after", None):
            try:
                self.after_cancel(self._auth_focus_after)
            except Exception:
                pass
        self._auth_focus_after = self.after(600, self._detect_auth_at_startup)

    def _sync_in_progress(self):
        """True si un passage moteur tourne (synchro OU amorçage/réinitialisation).
        Pendant un passage, une sonde d'auth concurrente est peu fiable (le moteur
        tient le trousseau) — et de toute façon un passage qui tourne PROUVE que la
        session est valide."""
        proc = getattr(self, "sync_process", None)
        if proc is not None and proc.poll() is None:
            return True
        return bool(getattr(self, "_prime_running", False))

    def _check_auth_settled(self):
        """Sonde d'auth FIABILISÉE — point unique pour TOUS les indicateurs
        (démarrage, focus, dialogue Configuration, amorçage).

        - SÉRIALISÉE (verrou) : plusieurs sondes peuvent se déclencher quasi
          simultanément (celle du <FocusIn> + celle du dialogue Configuration,
          par exemple) et se disputer le trousseau — c'était la cause des faux
          « session expirée » à l'ouverture de Configuration alors que le
          témoin venait d'être peint en vert. Ici, elles passent une à la fois.
        - TOLÉRANTE : un premier « non » est revérifié après 2,5 s (faux
          négatif transitoire connu — même principe que la sonde d'amorçage).
          Un VRAI échec, lui, persiste sur les deux essais.

        Retourne True/False, ou None si la sonde n'a pas pu conclure — dans ce
        cas l'appelant NE repeint PAS (on ne remplace pas un état affiché par
        une incertitude)."""
        with self._auth_lock:
            try:
                ok = realtime_manager.check_auth()
                if not ok:
                    time.sleep(2.5)
                    ok = bool(realtime_manager.check_auth())
            except Exception:
                return None
            # Adresse du VRAI compte connecté (pas un nom dérivé du fichier de
            # mappings) : récupérée UNE fois au premier succès, sous le même
            # verrou (aucun appel CLI concurrent), mise en cache. Invalidée dès
            # qu'une sonde échoue — une reconnexion peut être un AUTRE compte.
            try:
                if ok and self._account_email is None and _HAS_ENGINE:
                    # Adresse pour le TÉMOIN seulement. L'identité NAS, elle,
                    # n'est PAS semée ici : c'est un nom neutre user{n} attribué
                    # au premier usage réel (ensure_nas_identity) — indépendant
                    # du compte Proton, qui peut changer.
                    self._account_email = _ENGINE.get_account_email()
                elif not ok:
                    self._account_email = None
            except Exception:
                pass
            return ok

    def _detect_auth_at_startup(self):
        def work():
            # Sonde peu fiable pendant qu'un passage tient le trousseau : un
            # check_auth concurrent peut renvoyer un faux « verrouillé » et peindre
            # un faux « session expirée ». On ne touche donc PAS à l'indicateur
            # pendant un passage (le succès du passage le corrigera à la fin).
            if self._sync_in_progress():
                return
            ok = self._check_auth_settled()
            if ok is None:
                return
            def apply():
                self._set_auth_state(ok)
                if not ok and hasattr(self, "status"):
                    self.status.set(_("Proton session unavailable (expired token "
                        "or locked keyring) — open “⚙ Configuration…” to sign in."))
                    self._auth_error_in_status = True
            self.after(0, apply)
        threading.Thread(target=work, daemon=True).start()

    # ---------- UI ----------
    def _set_window_icon(self):
        """Définit l'icône de la fenêtre (barre des tâches, Alt+Tab) à partir
        du PNG « icone.png » situé dans le dossier de l'application. Silencieux
        si le fichier est absent ou illisible — l'app démarre quand même."""
        if not os.path.exists(WINDOW_ICON_PATH):
            return
        try:
            # On garde la référence sur self pour éviter que l'image soit
            # libérée par le ramasse-miettes (sinon l'icône disparaît aussitôt).
            self._icon_image = tk.PhotoImage(file=WINDOW_ICON_PATH)
            self.iconphoto(True, self._icon_image)
        except Exception:
            pass  # format non lisible par Tk (ex. un .ico renommé) -> on ignore

    def _build_ui(self):
        # Barre d'outils sur deux rangées pour éviter tout débordement
        # horizontal, quelle que soit la largeur de la fenêtre.
        # Rangée 1 : actions sur le FICHIER de mappings.
        toolbar1 = ttk.Frame(self, padding=(8, 8, 8, 2))
        toolbar1.pack(side="top", fill="x")
        ttk.Button(toolbar1, text=_("📂 Open…"), command=self.on_open).pack(side="left", padx=2)
        ttk.Button(toolbar1, text=_("💾 Save"), command=self.on_save).pack(side="left", padx=2)
        ttk.Button(toolbar1, text=_("💾 Save as…"), command=self.on_save_as).pack(side="left", padx=2)
        ttk.Separator(toolbar1, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(toolbar1, text=_("🌐 Global exclusions…"), command=self.on_edit_global_exclusions).pack(side="left", padx=2)
        ttk.Separator(toolbar1, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(toolbar1, text=_("⏰ Schedule…"), command=self.on_edit_schedule).pack(side="left", padx=2)
        ttk.Button(toolbar1, text=_("⚡ Real-time…"), command=self.on_edit_realtime).pack(side="left", padx=2)
        ttk.Separator(toolbar1, orient="vertical").pack(side="left", fill="y", padx=8)
        # Un SEUL bouton pour tout ce qui relève des préférences : compte Proton
        # (connexion), langue et réglages d'installation — la barre d'outils était
        # surchargée. L'état de connexion, lui, vit en bas à droite (témoin).
        ttk.Button(toolbar1, text=_("⚙ Configuration…"),
                   command=self.on_configuration).pack(side="left", padx=2)

        # Rangée 2 : actions sur le MAPPING sélectionné.
        toolbar2 = ttk.Frame(self, padding=(8, 2, 8, 6))
        toolbar2.pack(side="top", fill="x")
        ttk.Button(toolbar2, text=_("➕ Folder…"), command=self.on_add_folder).pack(side="left", padx=2)
        ttk.Button(toolbar2, text=_("➕ File…"), command=self.on_add_file).pack(side="left", padx=2)
        ttk.Button(toolbar2, text=_("✏ Edit"), command=self.on_edit_mapping).pack(side="left", padx=2)
        ttk.Button(toolbar2, text=_("🚫 Mapping exclusions"), command=self.on_edit_mapping_exclusions).pack(side="left", padx=2)
        ttk.Button(toolbar2, text=_("🗑 Delete"), command=self.on_remove).pack(side="left", padx=2)
        ttk.Button(toolbar2, text=_("↪ Move to file…"), command=self.on_move_mapping).pack(side="left", padx=2)

        # Ligne de résumé des exclusions globales (juste sous la barre d'outils)
        self.excl_summary = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.excl_summary, anchor="w",
                  padding=(10, 2), foreground="#555577").pack(side="top", fill="x")

        # Barre d'état (placée AVANT le paned pour rester collée en bas) :
        # message de statut à gauche, TÉMOIN de connexion Proton à droite
        # (remplace l'ancien bouton d'auth de la barre d'outils — l'action de
        # connexion vit désormais dans « ⚙ Configuration… »).
        statusbar = ttk.Frame(self)
        statusbar.pack(side="bottom", fill="x")
        self.status = tk.StringVar(value=_("Ready."))
        ttk.Label(statusbar, textvariable=self.status, anchor="w",
                  padding=(8, 4)).pack(side="left", fill="x", expand=True)
        self.auth_status = tk.StringVar(value="")
        self.auth_label = ttk.Label(statusbar, textvariable=self.auth_status,
                                    anchor="e", padding=(8, 4))
        self.auth_label.pack(side="right")

        # --- Zone de lancement de la synchro (au-dessus de la barre d'état) ---
        run_frame = ttk.LabelFrame(self, text=_("Run sync"), padding=8)
        run_frame.pack(side="bottom", fill="x", padx=8, pady=(4, 4))

        # --- Options du passage (ligne principale) ---
        # « Ignorer cache » (Bloc 4) a été retiré : « ♻ Réinitialiser le mapping »
        # couvre le même besoin (reconstruction) AVEC la gestion des services.
        # opt_ignore_cache subsiste (toujours False) pour ne pas toucher à la
        # construction de commande, mais n'est plus exposée.
        # Zone options en GRILLE pour aligner verticalement les « ? » de la ligne
        # principale et de la ligne avancée, et RÉSERVER la place de la ligne
        # cachée (le GUI ne change pas de hauteur quand on déplie les avancées :
        # on masque le CONTENU, pas la rangée).
        optbar = ttk.Frame(run_frame)
        optbar.pack(side="top", fill="x")

        # Colonne gauche : options du passage, sur 2 rangées (principale + avancée).
        opts = ttk.Frame(optbar)
        opts.grid(row=0, column=0, sticky="nw")
        # Rangée 0 : options courantes, chacune dans sa cellule (case + « ? »).
        self._add_option_grid(opts, 0, 0, _("Test (dry-run)"), self.opt_dry_run, "dry-run")
        self._add_option_grid(opts, 0, 2, _("Propagate deletions"), self.opt_delete, "delete")
        self._adv_visible = False
        self._adv_toggle = ttk.Button(opts, text=_("Advanced options ▾"),
                                      command=self._toggle_advanced, width=20)
        self._adv_toggle.grid(row=0, column=4, padx=(8, 0), sticky="w")
        # Rangée 1 RÉSERVÉE : les widgets avancés (SHA1) existent toujours et
        # occupent la rangée, mais restent invisibles tant que non dépliés — la
        # hauteur de la zone est donc constante. Alignés sous la colonne 0.
        self._adv_widgets = self._add_option_grid(
            opts, 1, 0, _("SHA1 check"), self.opt_verify_hash, "verify-hash",
            return_widgets=True)
        for w in self._adv_widgets:
            w.grid_remove()   # réserve la géométrie sans afficher

        # Colonne droite : encadré « Affichage » (réglages de VUE de la fenêtre
        # d'exécution), à côté du bouton Options avancées.
        optbar.columnconfigure(0, weight=1)   # pousse l'encadré à droite
        display = ttk.LabelFrame(optbar, text=_("Display"), padding=6)
        display.grid(row=0, column=1, rowspan=2, sticky="ne", padx=(12, 0))
        self._add_option(display, _("Verbose"), self.opt_verbose, "verbose")
        ttk.Checkbutton(display, text=_("❗ Errors only"),
                        variable=self.opt_errors_only,
                        command=self._reapply_output_filter).pack(side="left", padx=(0, 16))
        ttk.Button(display, text=_("🧹 Clear output"),
                   command=self.on_clear_output).pack(side="left", padx=2)

        actions = ttk.Frame(run_frame)
        actions.pack(side="top", fill="x", pady=(8, 0))
        self.prime_button = ttk.Button(actions, text=_("🌱 Prime cache"),
                                       command=self.on_prime_cache)
        self.prime_button.pack(side="left", padx=2)
        self.reset_button = ttk.Button(actions, text=_("♻ Reset mapping"),
                                       command=self.on_reset_mapping)
        self.reset_button.pack(side="left", padx=2)
        self.run_button = ttk.Button(actions, text=_("▶ Run sync"), command=self.on_run_sync)
        self.run_button.pack(side="left", padx=2)
        self.stop_button = ttk.Button(actions, text=_("⏹ Stop"), command=self.on_stop_sync, state="disabled")
        self.stop_button.pack(side="left", padx=2)
        ttk.Button(actions, text=_("📋 Copy command"), command=self.on_copy_command).pack(side="left", padx=2)

        # --- Zone centrale partagée : tableau (haut) + sortie (bas) ---
        # Un PanedWindow vertical laisse l'utilisateur ajuster la répartition
        # en glissant la poignée, et garantit que les deux zones restent visibles.
        paned = ttk.PanedWindow(self, orient="vertical")
        paned.pack(side="top", fill="both", expand=True, padx=8, pady=(0, 4))

        # Tableau des mappings (panneau du haut)
        tree_frame = ttk.Frame(paned)
        columns = ("state", "type", "del", "source", "dest_parent", "exclusions")
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="extended")
        self.tree.heading("state", text=_("Ready"))
        self.tree.heading("type", text=_("Type"))
        self.tree.heading("del", text="🗑")
        self.tree.heading("source", text=_("Source (local)"))
        self.tree.heading("dest_parent", text=_("Destination (parent folder on Proton Drive)"))
        self.tree.heading("exclusions", text=_("Mapping exclusions"))
        self.tree.column("state", width=48, anchor="center")
        self.tree.column("type", width=55, anchor="center")
        self.tree.column("del", width=34, anchor="center")
        self.tree.column("source", width=330)
        self.tree.column("dest_parent", width=310)
        self.tree.column("exclusions", width=150)
        # Largeur des colonnes étroites ajustée au CONTENU réel (en-tête + valeurs)
        # mesuré dans la langue active, et figée (stretch=False) : elles ne s'étirent
        # plus à l'agrandissement — seules Source/Destination absorbent la place —
        # mais restent redimensionnables à la main.
        self._autosize_fixed_columns()
        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda e: self.on_edit_mapping())
        self.tree.bind("<Control-a>", self._select_all_rows)
        self.tree.bind("<Control-A>", self._select_all_rows)
        paned.add(tree_frame, weight=2)

        # Zone de sortie en direct (panneau du bas)
        out_frame = ttk.LabelFrame(paned, text=_("Sync output"), padding=4)
        self.output = scrolledtext.ScrolledText(out_frame, height=10, wrap="none",
                                                font=("monospace", 9), state="disabled")
        self.output.pack(side="top", fill="both", expand=True)
        paned.add(out_frame, weight=3)

        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _autosize_fixed_columns(self):
        """Ajuste Prêt / Type / Corbeille / Exclusions à la largeur de LEUR contenu
        (en-tête traduit + valeurs possibles), mesurée avec les vraies polices, et
        les fige (stretch=False) pour qu'elles ne s'étirent plus à l'agrandissement.
        Le redimensionnement manuel reste possible. Recalculé dans la langue active
        au démarrage (un changement de langue impose de toute façon un redémarrage),
        donc jamais tronqué ni gonflé quelle que soit la langue. Source/Destination
        restent élastiques et absorbent l'agrandissement."""
        import tkinter.font as tkfont
        style = ttk.Style()

        def _font(spec, fallback):
            try:
                return tkfont.Font(font=spec) if spec else tkfont.nametofont(fallback)
            except Exception:
                return tkfont.nametofont("TkDefaultFont")

        hfont = _font(style.lookup("Treeview.Heading", "font"), "TkHeadingFont")
        cfont = _font(style.lookup("Treeview", "font"), "TkDefaultFont")
        PAD = 30   # bordures + indicateur de tri + respiration (espace libre)

        def fit(col, samples):
            w = hfont.measure(self.tree.heading(col)["text"])
            for s in samples:
                w = max(w, cfont.measure(s))
            self.tree.column(col, width=w + PAD, stretch=False)

        fit("state", ["✅", "⏳", "—"])
        fit("type",  [_("Folder"), _("File")])
        fit("del",   ["⛔", "🗑", ""])
        fit("exclusions", ["—", "99 nom(s), 99 motif(s)"])
        # Colonnes de chemins : élastiques (absorbent l'agrandissement).
        self.tree.column("source", stretch=True)
        self.tree.column("dest_parent", stretch=True)

    def _add_option_grid(self, parent, row, col, label, var, help_key,
                         return_widgets=False):
        """Variante de _add_option en GRILLE : place la case à `row,col` et son
        bouton « ? » à `row,col+1`, avec un petit espace à droite. Permet
        d'aligner verticalement les « ? » de plusieurs rangées. Si
        return_widgets, renvoie (case, aide) pour pouvoir les masquer/réafficher
        (grid_remove/grid) tout en réservant leur place dans la grille."""
        cb = ttk.Checkbutton(parent, text=label, variable=var)
        cb.grid(row=row, column=col, sticky="w", pady=1)
        btn = ttk.Button(parent, text="?", width=2,
                         command=lambda k=help_key: self._show_help(k))
        btn.grid(row=row, column=col + 1, sticky="w", padx=(2, 16))
        if return_widgets:
            return (cb, btn)

    def _toggle_advanced(self):
        """Affiche/masque les options avancées (SHA1). La rangée est TOUJOURS
        réservée dans la grille (place occupée en permanence) : on ne fait
        qu'afficher (grid) ou masquer (grid_remove) les widgets, sans changer la
        hauteur de la fenêtre. Le libellé du bouton suit l'état (▾/▴)."""
        self._adv_visible = not self._adv_visible
        if self._adv_visible:
            for w in self._adv_widgets:
                w.grid()
            self._adv_toggle.config(text=_("Advanced options ▴"))
        else:
            for w in self._adv_widgets:
                w.grid_remove()
            self._adv_toggle.config(text=_("Advanced options ▾"))

    def _add_option(self, parent, label, var, help_key):
        """Ajoute une case à cocher suivie d'un petit bouton « ? » d'aide."""
        cell = ttk.Frame(parent)
        cell.pack(side="left", padx=(0, 16))
        ttk.Checkbutton(cell, text=label, variable=var).pack(side="left")
        ttk.Button(cell, text="?", width=2,
                   command=lambda k=help_key: self._show_help(k)).pack(side="left", padx=(2, 0))

    def _show_help(self, help_key, parent=None):
        """Affiche l'aide « ? ». `parent` : fenêtre au-dessus de laquelle la boîte
        doit apparaître — INDISPENSABLE quand l'appel vient d'un dialogue modal
        (grab_set), sinon la boîte, transient de la fenêtre principale, s'empile
        SOUS le dialogue (bug constaté dans Configuration)."""
        text = OPTION_HELP.get(help_key) or CONFIG_HELP.get(help_key) or _("(no help)")
        dlg_info(parent or self, text, title=_("Help — ") + help_key)

    def _update_title(self):
        name = os.path.basename(self.config_path) if self.config_path else _("(new file)")
        star = " *" if self.dirty else ""
        self.title(f"{APP_TITLE} — {name}{star}")

    def _refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        for i, m in enumerate(self.mappings):
            label = _("Folder") if m["type"] == "folder" else _("File")
            excl = m.get("exclusions") or {}
            n_names = len(excl.get("names", []) or [])
            n_pat = len(excl.get("patterns", []) or [])
            if n_names or n_pat:
                excl_txt = f"{n_names} nom(s), {n_pat} motif(s)"
            else:
                excl_txt = "—"
            # Symbole de suppression discret :
            #   (vide)  -> additif, pas de suppression
            #   🗑      -> suppression vers corbeille
            #   ⛔      -> suppression définitive
            if m.get("allow_delete"):
                del_sym = "⛔" if m.get("delete_mode") == "permanent" else "🗑"
            else:
                del_sym = ""
            self.tree.insert("", "end", iid=str(i),
                             values=(self._mapping_state_symbol(m),
                                     label, del_sym, m["source"], m["dest_parent"], excl_txt))

    # ---------- Indicateur d'état « prêt pour le temps réel » (✅/⏳) ----------
    def _select_all_rows(self, _event=None):
        self.tree.selection_set(self.tree.get_children())
        return "break"

    def _read_cache_data(self):
        """Contenu du cache du fichier de mappings actif (dict), ou {} si absent.
        MÉMOÏSÉ par mtime : on ne re-parse le fichier QUE s'il a changé sur disque.
        Crucial pendant l'amorçage — sinon un json.load du gros cache à chaque tick
        (1,5 s) sur le thread principal fige l'UI (sortie qui ne défile plus). Le
        moteur écrit le cache ~toutes les 10 s (atomique), donc en pratique on ne
        parse plus qu'~1×/10 s au lieu de 2×/1,5 s."""
        if not self.config_path:
            return {}
        cache_dir = appconfig.CACHE_DIR if _HAS_CONFIG else os.path.expanduser("~/.proton_sync_cache")
        name = os.path.basename(self.config_path).replace(".json", "") + ".cache"
        path = os.path.join(cache_dir, name)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            self._cache_memo = None
            return {}
        memo = getattr(self, "_cache_memo", None)
        if memo is not None and memo[0] == path and memo[1] == mtime:
            return memo[2]
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            data = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            data = {}
        self._cache_memo = (path, mtime, data)
        return data

    def _effective_excl_fingerprint(self, mapping):
        """Empreinte d'exclusions EFFECTIVE COURANTE (globales + propres au mapping),
        calculée avec la MÊME logique que le moteur (classe Exclusions réutilisée,
        aucune duplication). None si aucune exclusion effective. Sert à l'option (b)
        de l'indicateur : détecter qu'un changement d'exclusions a périmé la
        consolidation, sans attendre qu'un passage réécrive le cache."""
        if _PS_EXCL is None:
            return None
        try:
            g = _PS_EXCL(self.global_exclusions.get("names"),
                         self.global_exclusions.get("patterns"))
            ex = mapping.get("exclusions") or {}
            local = _PS_EXCL(ex.get("names"), ex.get("patterns"))
            eff = g.merged_with(local)
            return eff.fingerprint() if eff else None
        except Exception:
            return None

    def _mapping_state_symbol(self, mapping, cache_data=None):
        """✅ si le mapping est PRÊT pour le temps réel (racine subtree_complete ET
        empreinte d'exclusions courante == celle stockée), sinon ⏳. Les mappings de
        type 'file' n'ont pas d'arbre à analyser -> pas d'indicateur (—)."""
        if mapping.get("type") != "folder":
            return "—"
        data = cache_data if cache_data is not None else self._read_cache_data()
        src = os.path.normpath(mapping.get("source", ""))
        entry = data.get(src)
        if not isinstance(entry, dict) or not entry.get("subtree_complete"):
            return "⏳"
        # (b) l'empreinte d'exclusions courante correspond-elle à celle consolidée ?
        sig = entry.get("sig")
        cached_fp = sig.get("excl") if isinstance(sig, dict) else None
        return "✅" if self._effective_excl_fingerprint(mapping) == cached_fp else "⏳"

    def _refresh_state_column(self):
        """Recalcule et met à jour la seule colonne d'état de chaque ligne (léger :
        lit le cache une fois). Appelé sur événements clés, pendant l'amorçage, et
        périodiquement (filet pour les changements externes)."""
        if not self.tree.get_children():
            return
        data = self._read_cache_data()
        for i, m in enumerate(self.mappings):
            iid = str(i)
            if self.tree.exists(iid):
                self.tree.set(iid, "state", self._mapping_state_symbol(m, data))

    def _excl_summary_text(self, ex):
        names = ex.get("names", []) or []
        pats = ex.get("patterns", []) or []
        parts = []
        if names:
            parts.append(_("names: ") + ", ".join(names))
        if pats:
            parts.append(_("patterns: ") + ", ".join(pats))
        return " | ".join(parts) if parts else _("(none)")

    def _update_excl_summary(self):
        txt = self._excl_summary_text(self.global_exclusions)
        self.excl_summary.set(_("🌐 Global exclusions — ") + txt)

    def _set_dirty(self, value=True):
        self.dirty = value
        self._update_title()

    # ---------- Actions fichier ----------
    def on_open(self):
        path = pick_open_file(self, title=_("Open a mappings file"),
                              initialdir=APP_DIR, json_only=True)
        if path:
            self._load(path)

    def _load(self, path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Deux formats acceptés :
            #  - ancien : liste simple de mappings
            #  - nouveau : objet { exclusions: {...}, mappings: [...] }
            if isinstance(data, list):
                mappings = data
                global_ex = {"names": [], "patterns": []}
            elif isinstance(data, dict):
                mappings = data.get("mappings", [])
                ex = data.get("exclusions", {}) or {}
                global_ex = {
                    "names": list(ex.get("names", []) or []),
                    "patterns": list(ex.get("patterns", []) or []),
                }
            else:
                raise ValueError("Le fichier doit contenir une liste ou un objet.")
            for entry in mappings:
                if "type" not in entry or "source" not in entry or "dest_parent" not in entry:
                    raise ValueError("Chaque entrée doit avoir : type, source, dest_parent.")
            self.mappings = mappings
            self.global_exclusions = global_ex
            self.config_path = path
            self._set_dirty(False)
            self._refresh_tree()
            self._update_excl_summary()
            self.status.set(_("Loaded: {p} ({n} entries)").format(p=path, n=len(self.mappings)))
        except Exception as e:
            dlg_error(self, str(e), title=_("Load error"))

    def on_save(self):
        if not self.config_path:
            return self.on_save_as()
        self._save(self.config_path)

    def on_save_as(self):
        path = pick_save_file(self, title=_("Save the mappings file"),
                             initialdir=APP_DIR, initialfile="mappings.json")
        if path:
            self._save(path)

    def _save(self, path):
        try:
            # Décider du format de sortie :
            #  - si aucune exclusion (ni globale, ni par mapping), on garde le
            #    format « liste simple » historique (compatibilité maximale).
            #  - sinon, format objet { exclusions, mappings }.
            has_global = bool(self.global_exclusions.get("names") or
                              self.global_exclusions.get("patterns"))
            has_mapping_excl = any(m.get("exclusions") for m in self.mappings)

            if has_global or has_mapping_excl:
                out = {
                    "exclusions": {
                        "names": self.global_exclusions.get("names", []),
                        "patterns": self.global_exclusions.get("patterns", []),
                    },
                    "mappings": self.mappings,
                }
            else:
                out = self.mappings  # format liste simple

            with open(path, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=2)
            self.config_path = path
            self._set_dirty(False)
            self.status.set(_("Saved: {p}").format(p=path))
            self._auto_push_mappings(path)
        except Exception as e:
            dlg_error(self, str(e), title=_("Save error"))

    def _auto_push_mappings(self, path):
        """Pousse automatiquement la copie des mappings vers le NAS après un
        enregistrement (best-effort, dans un thread, sans dialogue). Le watcher
        NAS lit cette copie. Silencieux si le temps réel n'est pas disponible ou
        si le nom ne suit pas la convention mappings-<user>.json."""
        if not _HAS_REALTIME:
            return
        base = os.path.basename(path)
        if not (base.startswith("mappings-") and base.endswith(".json")):
            return

        def work():
            try:
                ok, msg = realtime_manager.push_mappings_to_nas(path)
            except Exception as e:
                ok, msg = False, f"push NAS impossible : {e}"
            # Mise à jour de la barre d'état sur le thread Tk ; pas de modale pour
            # ne pas interrompre le flux d'enregistrement. La dérive (fenêtre
            # Temps réel) reflète l'état réel.
            self.after(0, lambda: self.status.set(
                f"Enregistré et poussé vers le NAS : {base}" if ok
                else f"Enregistré : {base}  —  {msg}"))
        threading.Thread(target=work, daemon=True).start()

    def on_close(self):
        if self.sync_process and self.sync_process.poll() is None:
            if not dlg_confirm(
                self,
                _("A sync is currently running. Quitting will interrupt it. Continue?"),
                title=_("Sync in progress"), kind="warning",
                ok_text=_("Quit"), cancel_text=_("Cancel")):
                return
            self.on_stop_sync()
        if self.dirty:
            if not dlg_confirm(
                self,
                _("Some changes have not been saved. Quit without saving?"),
                title=_("Unsaved changes"), kind="warning",
                ok_text=_("Quit without saving"), cancel_text=_("Cancel")):
                return
        self.destroy()

    # ---------- Actions mapping ----------
    def _mapping_dialog(self, kind, mapping=None):
        """Dialogue unifié d'ajout/modification d'un mapping.

        `kind` : "folder" ou "file" (ignoré si `mapping` est fourni — on garde
                 alors son type existant).
        `mapping` : si fourni, on édite ce mapping ; sinon, création.

        Retourne un dict mapping complet, ou None si annulé.
        """
        is_edit = mapping is not None
        m_type = mapping["type"] if is_edit else kind

        dlg = tk.Toplevel(self)
        dlg.title(_("Edit mapping") if is_edit else
                  (_("Add a folder") if m_type == "folder" else _("Add a file")))
        dlg.geometry("680x560")
        dlg.minsize(620, 520)
        dlg.transient(self)
        dlg.grab_set()

        result = {"value": None}

        # Boutons en bas, réservés en premier pour ne jamais être tronqués.
        btns = ttk.Frame(dlg, padding=8)
        btns.pack(side="bottom", fill="x")

        body = ttk.Frame(dlg, padding=10)
        body.pack(side="top", fill="both", expand=True)

        # --- Source ---
        ttk.Label(body, text=_("Source (local)"), font=("", 10, "bold")).pack(anchor="w")
        src_row = ttk.Frame(body)
        src_row.pack(fill="x", pady=(2, 8))
        src_var = tk.StringVar(value=mapping["source"] if is_edit else "")
        src_entry = ttk.Entry(src_row, textvariable=src_var)
        src_entry.pack(side="left", fill="x", expand=True)

        def browse():
            if m_type == "folder":
                p = pick_directory(dlg, title=_("Choose a source folder"),
                                   initialdir="/media")
            else:
                p = pick_open_file(dlg, title=_("Choose a source file"),
                                   initialdir="/media", json_only=False)
            if p:
                src_var.set(p)
                detect_and_show()

        ttk.Button(src_row, text=_("Browse…"), command=browse).pack(side="left", padx=(6, 0))

        # --- Destination ---
        ttk.Label(body, text=_("Destination (parent folder on Proton Drive)"),
                  font=("", 10, "bold")).pack(anchor="w")
        dest_var = tk.StringVar(value=mapping["dest_parent"] if is_edit else "/my-files")
        dest_row = ttk.Frame(body)
        dest_row.pack(fill="x", pady=(2, 10))
        ttk.Entry(dest_row, textvariable=dest_var).pack(side="left", fill="x", expand=True)
        # Sélecteur des destinations EXISTANTES sur Proton (y compris les
        # dossiers partagés en écriture sous /shared-with-me) : évite les
        # fautes de frappe d'une retranscription manuelle — une destination
        # mal tapée créerait une NOUVELLE arborescence au lieu de rejoindre
        # l'existante.
        ttk.Button(dest_row, text=_("🔍 Browse Proton…"),
                   command=lambda: RemoteFolderPicker(dlg, self, dest_var)
                   ).pack(side="left", padx=(6, 0))

        # --- Zone Suppression ---
        del_frame = ttk.LabelFrame(body, text=_("Deletion propagation"), padding=8)
        del_frame.pack(fill="x", pady=(0, 8))

        # État initial depuis le mapping existant
        allow_init = bool(mapping.get("allow_delete")) if is_edit else False
        mode_init = (mapping.get("delete_mode") or "trash") if is_edit else "trash"
        kind_init = (mapping.get("source_kind") or "") if is_edit else ""

        allow_var = tk.BooleanVar(value=allow_init)
        mode_var = tk.StringVar(value=mode_init)
        srckind_var = tk.StringVar(value=kind_init)

        allow_chk = ttk.Checkbutton(
            del_frame,
            text=_("Allow this mapping to delete on Proton what was deleted locally"),
            variable=allow_var, command=lambda: toggle_delete())
        allow_chk.pack(anchor="w")

        # Sous-zone activée seulement si allow coché
        sub = ttk.Frame(del_frame)
        sub.pack(fill="x", pady=(6, 0))

        mode_row = ttk.Frame(sub)
        mode_row.pack(anchor="w", pady=2)
        ttk.Label(mode_row, text=_("Mode: ")).pack(side="left")
        rb_trash = ttk.Radiobutton(mode_row, text=_("Proton trash (recoverable for 30 days)"),
                                   variable=mode_var, value="trash")
        rb_trash.pack(side="left", padx=(0, 10))
        rb_perm = ttk.Radiobutton(mode_row, text=_("Permanent deletion"),
                                  variable=mode_var, value="permanent")
        rb_perm.pack(side="left")

        # Détection du type de source
        detect_lbl = ttk.Label(sub, text="", wraplength=600, foreground="#444466",
                               justify="left")
        detect_lbl.pack(anchor="w", pady=(6, 2))

        kind_row = ttk.Frame(sub)
        kind_row.pack(anchor="w", pady=2)
        ttk.Label(kind_row, text=_("Confirmed source type: ")).pack(side="left")
        rb_nfs = ttk.Radiobutton(kind_row, text=_("NFS (network/NAS)"),
                                 variable=srckind_var, value="nfs")
        rb_nfs.pack(side="left", padx=(0, 10))
        rb_local = ttk.Radiobutton(kind_row, text=_("Local (internal disk)"),
                                   variable=srckind_var, value="local")
        rb_local.pack(side="left")

        warn_lbl = ttk.Label(sub, text="", wraplength=600, foreground="#b00020",
                             justify="left")
        warn_lbl.pack(anchor="w", pady=(4, 0))

        def detect_and_show():
            """Lance la détection sur la source courante et met à jour l'affichage."""
            path = src_var.get().strip()
            warn_lbl.config(text="")
            if not path:
                detect_lbl.config(text=_("(pick a source to detect its type)"))
                return
            if not _HAS_MOUNT_CHECK:
                detect_lbl.config(
                    text=_("⚠ Detection module missing (mount_check.py) — "
                         "pick the type manually."))
                return
            # Pour un fichier, on analyse son dossier parent.
            probe = path if os.path.isdir(path) else os.path.dirname(path)
            info = mount_check.detect_source_kind(probe)
            detected = info["kind"]
            detect_lbl.config(text="🔎 " + info.get("detail", ""))
            # Pré-sélectionner le type détecté si l'utilisateur n'a pas déjà choisi
            if detected in ("nfs", "local"):
                if not srckind_var.get():
                    srckind_var.set(detected)
                # Avertir si le choix actuel contredit la détection
                if srckind_var.get() != detected:
                    warn_lbl.config(
                        text=f"⚠ Tu as choisi « {srckind_var.get()} » mais la source "
                             f"est détectée « {detected} ». Vérifie avant d'enregistrer.")
            elif detected == "missing":
                warn_lbl.config(
                    text="⚠ Source actuellement introuvable ou inaccessible — "
                         "impossible de détecter le type. Si c'est un montage NAS "
                         "non monté, monte-le d'abord.")

        def toggle_delete():
            state = "normal" if allow_var.get() else "disabled"
            for w in (rb_trash, rb_perm, rb_nfs, rb_local):
                w.config(state=state)
            if allow_var.get():
                detect_and_show()
            else:
                detect_lbl.config(text="")
                warn_lbl.config(text="")

        # Re-détecter si l'utilisateur change manuellement le type
        srckind_var.trace_add("write", lambda *a: detect_and_show() if allow_var.get() else None)
        # Re-détecter si la source est tapée à la main
        src_var.trace_add("write", lambda *a: None)  # évite le spam ; détection via Parcourir / focus-out
        src_entry.bind("<FocusOut>", lambda e: detect_and_show() if allow_var.get() else None)

        toggle_delete()  # état initial des sous-contrôles

        # --- Validation et OK ---
        def on_ok():
            source = src_var.get().strip()
            dest = dest_var.get().strip()
            if not source:
                dlg_warning(dlg, "Indique une source.", title=_("Missing source"))
                return
            if not dest:
                dlg_warning(dlg, "Indique une destination.", title=_("Missing destination"))
                return
            new_m = {"type": m_type, "source": source, "dest_parent": dest}
            # Conserver les exclusions existantes si on édite
            if is_edit and mapping.get("exclusions"):
                new_m["exclusions"] = mapping["exclusions"]
            # Réglages de suppression
            if allow_var.get():
                chosen_kind = srckind_var.get()
                if chosen_kind not in ("nfs", "local"):
                    dlg_warning(
                        dlg,
                        "Tu as autorisé la suppression : confirme le type de source "
                        "(NFS ou local) avant d'enregistrer.",
                        title=_("Source type required"))
                    return
                # Avertissement fort pour le mode définitif
                if mode_var.get() == "permanent":
                    if not dlg_confirm(
                        dlg,
                        _("You chose PERMANENT deletion (no trash).\n\n"
                        "Files deleted locally will be erased from Proton with "
                        "no possibility of recovery.\n\nConfirm this choice?"),
                        title=_("Permanent deletion"), kind="warning",
                        ok_text=_("Confirm"), cancel_text=_("Cancel")):
                        return
                new_m["allow_delete"] = True
                new_m["delete_mode"] = mode_var.get()
                new_m["source_kind"] = chosen_kind
            result["value"] = new_m
            dlg.destroy()

        ttk.Button(btns, text=_("Cancel"), command=dlg.destroy).pack(side="right", padx=2)
        ttk.Button(btns, text="OK", command=on_ok).pack(side="right", padx=2)

        self.wait_window(dlg)
        return result["value"]

    def on_add_folder(self):
        m = self._mapping_dialog("folder")
        if m is None:
            return
        self.mappings.append(m)
        self._set_dirty(True)
        self._refresh_tree()
        self.status.set(_("Added (folder): {s}").format(s=m["source"]))

    def on_add_file(self):
        m = self._mapping_dialog("file")
        if m is None:
            return
        self.mappings.append(m)
        self._set_dirty(True)
        self._refresh_tree()
        self.status.set(_("Added (file): {s}").format(s=m["source"]))

    def _selected_index(self):
        """Index de l'UNIQUE mapping sélectionné, pour les opérations ciblées
        (Modifier / Exclusions du mapping / Supprimer). Le tableau autorise la
        multi-sélection (pour l'amorçage), mais ces opérations n'ont de sens que sur
        un seul mapping : si 0 ou plusieurs lignes sont sélectionnées, on avertit
        plutôt que d'agir en douce sur la première."""
        sel = self.tree.selection()
        if not sel:
            dlg_info(self, _("First select a row in the list."), title=_("No selection"))
            return None
        if len(sel) > 1:
            dlg_info(self, _("Several mappings are selected. This action applies to "
                     "a single mapping — select just one row."),
                     title=_("Single selection required"))
            return None
        return int(sel[0])

    def on_edit_mapping(self):
        """Ouvre le dialogue complet d'édition du mapping sélectionné, puis gère
        les changements de VOCATION (additif <-> miroir) sur un mapping déjà
        amorcé (Bloc 2) :
          - additif amorcé -> miroir : le cache ne peut pas rester « prêt » (il
            n'a jamais réconcilié les suppressions). On PRÉVIENT et, sur
            confirmation, on INVALIDE l'amorçage du mapping (il repasse ⏳).
            L'utilisateur ré-amorcera quand il voudra (il peut finir ses autres
            éditions d'abord).
          - miroir amorcé -> additif : aucun amorçage requis (on arrête juste de
            supprimer). Simple information, le cache reste valide.
        Un mapping jamais amorcé ne déclenche aucun avertissement (Q4)."""
        idx = self._selected_index()
        if idx is None:
            return
        old = self.mappings[idx]
        old_mirror = bool(old.get("allow_delete"))
        # Le mapping est-il DÉJÀ amorcé (prêt pour le temps réel) ?
        was_primed = (old.get("type") == "folder"
                      and self._mapping_state_symbol(old) == "✅")

        updated = self._mapping_dialog(old["type"], mapping=old)
        if updated is None:
            return
        new_mirror = bool(updated.get("allow_delete"))

        # --- Changement de vocation sur un mapping DÉJÀ amorcé ---
        if was_primed and (new_mirror != old_mirror):
            if new_mirror:
                # additif -> miroir : l'amorçage doit être refait.
                if not dlg_confirm(
                    self,
                    _("This mapping was primed as ADDITIVE and you are switching it "
                      "to MIRROR (deletion enabled).\n\n"
                      "Its priming can no longer be used as-is: a mirror mapping "
                      "must reconcile the destination before real-time can handle "
                      "its deletions. Saving will therefore RESET this mapping's "
                      "primed state — it becomes unavailable for real-time until you "
                      "prime it again (in mirror mode).\n\n"
                      "Tip: it is best to decide a mapping's vocation before it holds "
                      "a lot of data, so this re-priming stays quick.\n\n"
                      "Save and reset the primed state?"),
                    title=_("Change to mirror mode"), kind="warning",
                    ok_text=_("Save and reset"), cancel_text=_("Cancel")):
                    return
                self.mappings[idx] = updated
                self._invalidate_mapping_cache(updated)
                self._set_dirty(True)
                self._refresh_tree()
                self.status.set(_("Mapping switched to mirror — prime it again to "
                                  "enable real-time deletions: {s}").format(s=updated["source"]))
                return
            else:
                # miroir -> additif : aucun ré-amorçage requis.
                dlg_info(
                    self,
                    _("This mapping switches from MIRROR to ADDITIVE: it will no "
                      "longer delete anything on Proton. No re-priming is required — "
                      "its primed state stays valid.\n\n"
                      "Note: if you switch it back to mirror later, the next mirror "
                      "priming will take longer to reconcile the destination again."),
                    title=_("Change to additive mode"))

        self.mappings[idx] = updated
        self._set_dirty(True)
        self._refresh_tree()
        self.status.set(_("Mapping updated: {s}").format(s=updated["source"]))

    def _invalidate_mapping_cache(self, mapping):
        """Retire du cache l'état « amorcé » (subtree_complete) de la racine du
        mapping et de tous ses sous-dossiers, de sorte que le mapping repasse ⏳
        (non prêt pour le temps réel) et devra être ré-amorcé. N'efface pas le
        reste du cache (les autres mappings). Écriture atomique."""
        if not self.config_path or mapping.get("type") != "folder":
            return
        cache_dir = (appconfig.CACHE_DIR if _HAS_CONFIG
                     else os.path.expanduser("~/.proton_sync_cache"))
        name = os.path.basename(self.config_path).replace(".json", "") + ".cache"
        path = os.path.join(cache_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
        except (OSError, ValueError):
            return
        src = os.path.normpath(mapping.get("source", ""))
        prefix = src + os.sep
        changed = False
        for key in list(data.keys()):
            if key == "__meta__":
                continue
            if key == src or key.startswith(prefix):
                del data[key]
                changed = True
        if not changed:
            return
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)
            self._cache_memo = None   # forcer la relecture
        except OSError:
            pass

    # ── Transfert d'un mapping vers un AUTRE fichier de mappings ────────────
    @staticmethod
    def _cache_path_for(config_path):
        """Chemin du fichier cache associé à un fichier de mappings quelconque
        (même règle que le moteur : CACHE_DIR/<nom sans .json>.cache)."""
        cache_dir = (appconfig.CACHE_DIR if _HAS_CONFIG
                     else os.path.expanduser("~/.proton_sync_cache"))
        name = os.path.basename(config_path).replace(".json", "") + ".cache"
        return os.path.join(cache_dir, name)

    @staticmethod
    def _read_cache_file(path):
        """Lit un fichier cache (dict) ou renvoie None s'il n'existe pas / illisible.
        None signifie « pas de cache » (jamais amorcé) — distinct d'un cache vide."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    @staticmethod
    def _cache_account(cache_data):
        """Compte Proton estampillé dans un cache (clé __meta__.account), ou None."""
        if not isinstance(cache_data, dict):
            return None
        m = cache_data.get("__meta__")
        return m.get("account") if isinstance(m, dict) else None

    @staticmethod
    def _subtree_keys(cache_data, source):
        """Clés de cache appartenant au sous-arbre `source` (racine + descendants),
        par préfixe de chemin — même logique que Cache.purge_subtree du moteur."""
        base = os.path.normpath(source)
        prefix = base.rstrip("/") + "/"
        out = []
        for k in cache_data:
            if k == "__meta__":
                continue
            nk = os.path.normpath(k)
            if nk == base or (nk + "/").startswith(prefix):
                out.append(k)
        return out

    def _file_has_mappings(self, config_path):
        """True si le fichier de mappings destination contient déjà ≥1 mapping.
        Lit le JSON sans altérer l'état de l'éditeur. Gère les deux formats
        (liste simple, ou objet {exclusions, mappings})."""
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return False
        if isinstance(raw, list):
            return len(raw) > 0
        if isinstance(raw, dict):
            return len(raw.get("mappings", [])) > 0
        return False

    def on_move_mapping(self):
        """Déplace le mapping sélectionné vers un AUTRE fichier de mappings, avec
        son sous-arbre de cache (pour préserver l'amorçage). Vérification d'identité
        Proton STRICTE : on refuse dès qu'on ne peut pas garantir que la destination
        vise le même compte que la source (cf. cas ci-dessous). Un mapping à la fois."""
        idx = self._selected_index()
        if idx is None:
            return
        if not self.config_path:
            dlg_info(self, _("Save the current mappings file first."),
                     title=_("Move mapping"))
            return
        # Le transfert est ATOMIQUE : il enregistrera le fichier source (sans le
        # mapping) juste après avoir écrit la destination et déplacé le cache. Si
        # le fichier source a d'AUTRES modifications non enregistrées, elles seront
        # donc écrites aussi — on prévient et on demande confirmation.
        if self.dirty:
            if not dlg_confirm(self, _("This file has unsaved changes. Moving a "
                "mapping saves the whole file (to keep both files consistent). "
                "Continue and save?"), title=_("Move mapping"), kind="question",
                ok_text=_("Continue"), cancel_text=_("Cancel")):
                return
        mapping = self.mappings[idx]

        # Choisir le fichier destination (existant ou nouveau) — SANS message
        # d'écrasement (on ajoute au fichier, on ne le remplace pas).
        dest = pick_move_target(self, title=_("Move to which mappings file?"),
                                initialdir=APP_DIR)
        if not dest:
            return
        if os.path.normpath(dest) == os.path.normpath(self.config_path):
            dlg_info(self, _("Source and destination are the same file."),
                     title=_("Move mapping"))
            return

        # ── Vérification d'identité Proton (stricte) ───────────────────────
        src_cache = self._read_cache_file(self._cache_path_for(self.config_path))
        dst_cache_path = self._cache_path_for(dest)
        dst_cache = self._read_cache_file(dst_cache_path)
        src_account = self._cache_account(src_cache)
        dst_account = self._cache_account(dst_cache)
        dest_exists = os.path.exists(dest)

        if dst_cache is not None and dst_account:
            # B a un cache estampillé : must match A.
            if src_account and dst_account != src_account:
                dlg_error(self, _("The destination file is linked to another Proton "
                    "account:\n  destination: {b}\n  source: {a}\n\nTransfer refused "
                    "— the cache must not mix two accounts.").format(
                    a=src_account, b=dst_account), title=_("Different Proton account"))
                return
        elif dest_exists and self._file_has_mappings(dest) and dst_cache is None:
            # B a des mappings mais aucun cache -> identité indéterminable.
            dlg_error(self, _("The destination file already has mappings but no cache "
                "yet, so its Proton account cannot be verified.\n\nPrime the "
                "destination file at least once first, then move the mapping — this "
                "avoids mixing two accounts by mistake."),
                title=_("Destination identity unknown"))
            return
        # Autres cas (B vide/neuf, ou B au même compte, ou A sans cache) : autorisé.

        # ── Exclusions globales : elles font partie de l'empreinte qui rend un
        #    mapping « prêt ». Un mapping amorcé dans A ne reste ✅ dans B que si
        #    B applique les MÊMES exclusions globales. Logique :
        #      - src_excl == dst_excl (identiques, y compris toutes deux vides) :
        #        AUCUN conflit -> on ajoute simplement le mapping, pas de dialogue.
        #      - exclusions différentes, B vide/neuf : les exclusions de A suivent
        #        (message informatif), rien à écraser.
        #      - exclusions différentes, B a des mappings : on regarde si écraser
        #        invaliderait RÉELLEMENT des mappings prêts de B (empreinte testée
        #        un à un). Si OUI -> avertir + choix Annuler/Écraser. Si NON (aucun
        #        mapping prêt réellement impacté) -> écrasement silencieux inoffensif.
        #    `copy_excl` = True -> on écrira les exclusions de A dans B.
        src_excl = {"names": sorted(self.global_exclusions.get("names", []) or []),
                    "patterns": sorted(self.global_exclusions.get("patterns", []) or [])}
        dst_excl = self._read_global_exclusions(dest) if dest_exists else \
                   {"names": [], "patterns": []}
        dst_excl_norm = {"names": sorted(dst_excl.get("names", []) or []),
                         "patterns": sorted(dst_excl.get("patterns", []) or [])}
        dst_has_excl = bool(dst_excl_norm["names"] or dst_excl_norm["patterns"])
        dst_has_maps = dest_exists and self._file_has_mappings(dest)
        same_excl = (src_excl == dst_excl_norm)
        copy_excl = False

        if not same_excl:
            if not dst_has_excl and not dst_has_maps:
                # B neuf/vide : les exclusions de A suivent sans rien écraser.
                copy_excl = True
                if src_excl["names"] or src_excl["patterns"]:
                    dlg_info(self, _("The source file's global exclusions will be "
                        "copied to the destination — this is required so the mapping "
                        "keeps its primed state and syncs the same way."),
                        title=_("Global exclusions copied"))
            else:
                # B a des exclusions et/ou des mappings ET elles diffèrent. On ne
                # dérange l'utilisateur QUE si l'écrasement casserait réellement des
                # mappings prêts de B (test d'empreinte un à un). Un mapping déjà
                # doté des bons filtres (p. ex. après un aller-retour de transfert)
                # n'est PAS compté et ne déclenche pas d'alerte.
                impacted = self._mappings_invalidated_by_globals(
                    dest, src_excl, exclude_source=mapping.get("source", ""))
                if impacted:
                    names = "\n  • ".join(os.path.basename(s.rstrip("/"))
                                          for s in impacted)
                    choice = dlg_confirm(self, _("The destination file uses different "
                        "global exclusions, and applying this file's would make {n} "
                        "already-primed mapping(s) there no longer ready:\n  • {names}\n\n"
                        "They would fall back to ⏳ and need re-priming. Other mappings "
                        "are unaffected.\n\nApply this file's global exclusions to the "
                        "destination?\n(Cancel to check and harmonize them yourself "
                        "first.)").format(n=len(impacted), names=names),
                        title=_("Global exclusions differ"), kind="warning",
                        ok_text=_("Apply and move"), cancel_text=_("Cancel"))
                    if not choice:
                        self.status.set(_("Move cancelled."))
                        return
                # Impacté ou non, on aligne les exclusions de B sur A pour que le
                # mapping transféré reste cohérent (inoffensif si rien n'est prêt).
                copy_excl = True

        # Confirmation.
        if not dlg_confirm(self, _("Move this mapping to “{f}”?\n\n  • {s}\n\nIts cache "
            "(priming) is moved too, so it stays ready — no re-priming needed as long "
            "as the Proton destination is unchanged.\n\nAfterwards, reinstall/restart "
            "the background services of BOTH files (⚡ Real-time…, ⏰ Schedule…) so "
            "they follow the change.").format(
            f=os.path.basename(dest), s=mapping.get("source", "")),
            title=_("Move mapping"), kind="question",
            ok_text=_("Move"), cancel_text=_("Cancel")):
            return

        try:
            self._do_move_mapping(idx, dest, src_account,
                                  copy_excl=copy_excl, src_excl=src_excl)
        except Exception as e:
            dlg_error(self, _("Move failed: {e}").format(e=e), title=_("Move mapping"))
            return

        self.mappings.pop(idx)
        # Transfert ATOMIQUE : la destination et le cache sont déjà écrits ;
        # on persiste MAINTENANT le fichier source (sans le mapping) pour qu'il
        # n'y ait aucun état incohérent, même si l'utilisateur n'enregistre pas
        # ensuite (le mapping ne doit pas « réapparaître » à la réouverture).
        self._save(self.config_path)
        self._refresh_tree()
        self.status.set(_("Mapping moved to {f}: {s}").format(
            f=os.path.basename(dest), s=mapping.get("source", "")))
        dlg_info(self, _("Mapping moved.\n\nRemember to reinstall/restart the "
            "background services of both files (⚡ Real-time…, ⏰ Schedule…) so they "
            "follow the change."), title=_("Move mapping"))

    def _mapping_fp_with_globals(self, mapping, global_excl):
        """Empreinte d'exclusions effective d'un mapping calculée avec des
        exclusions GLOBALES données (pas forcément celles de l'éditeur courant).
        Sert à prédire si un mapping resterait « prêt » sous d'autres exclusions
        globales. None si aucune exclusion effective ou si le moteur d'exclusions
        n'est pas disponible."""
        if _PS_EXCL is None:
            return None
        try:
            g = _PS_EXCL(global_excl.get("names"), global_excl.get("patterns"))
            ex = mapping.get("exclusions") or {}
            local = _PS_EXCL(ex.get("names"), ex.get("patterns"))
            eff = g.merged_with(local)
            return eff.fingerprint() if eff else None
        except Exception:
            return None

    def _mappings_invalidated_by_globals(self, config_path, new_globals,
                                         exclude_source=None):
        """Renvoie la liste des sources de mappings de `config_path` qui seraient
        RÉELLEMENT invalidés si on remplaçait ses exclusions globales par
        `new_globals` : c.-à-d. les mappings actuellement « prêts » (racine
        subtree_complete + empreinte stockée) dont l'empreinte ne correspondrait
        PLUS sous les nouvelles globales. Un mapping qui a déjà les bons filtres
        (empreinte inchangée) n'est PAS listé — on ne l'invalidera pas. Ne modifie
        rien (simulation)."""
        cache = self._read_cache_file(self._cache_path_for(config_path))
        if not cache:
            return []
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return []
        maps = raw if isinstance(raw, list) else (raw.get("mappings", [])
               if isinstance(raw, dict) else [])
        excl_norm = os.path.normpath(exclude_source) if exclude_source else None
        hit = []
        for m in maps:
            if m.get("type") != "folder":
                continue
            src = os.path.normpath(m.get("source", ""))
            if src == excl_norm:
                continue
            entry = cache.get(src)
            if not (isinstance(entry, dict) and entry.get("subtree_complete")):
                continue   # pas prêt de toute façon -> rien à casser
            sig = entry.get("sig")
            stored_fp = sig.get("excl") if isinstance(sig, dict) else None
            new_fp = self._mapping_fp_with_globals(m, new_globals)
            if new_fp != stored_fp:
                hit.append(m.get("source", ""))
        return hit

    def _invalidate_other_mappings_in_file(self, config_path, exclude_source,
                                           new_globals):
        """Invalide SÉLECTIVEMENT (retire subtree_complete) les seuls mappings de
        `config_path` dont l'empreinte devient invalide sous `new_globals`. Les
        mappings qui gardent une empreinte valide (déjà les bons filtres, p. ex.
        après un aller-retour de transfert) sont LAISSÉS INTACTS. `exclude_source`
        (le mapping qu'on vient de transférer) est ignoré. Écriture atomique."""
        targets = set(os.path.normpath(s) for s in
                      self._mappings_invalidated_by_globals(
                          config_path, new_globals, exclude_source))
        if not targets:
            return
        cache_path = self._cache_path_for(config_path)
        cache = self._read_cache_file(cache_path)
        if not cache:
            return
        changed = False
        for tgt in targets:
            for k in self._subtree_keys(cache, tgt):
                entry = cache.get(k)
                if isinstance(entry, dict) and entry.get("subtree_complete"):
                    entry["subtree_complete"] = False
                    changed = True
        if not changed:
            return
        try:
            tmp = cache_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False)
            os.replace(tmp, cache_path)
        except OSError:
            pass

    def _read_global_exclusions(self, config_path):
        """Lit les exclusions globales d'un fichier de mappings quelconque, sans
        altérer l'état de l'éditeur. Gère les deux formats (liste simple -> pas
        d'exclusions ; objet {exclusions, mappings})."""
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return {"names": [], "patterns": []}
        if isinstance(raw, dict):
            ex = raw.get("exclusions") or {}
            return {"names": ex.get("names", []) or [],
                    "patterns": ex.get("patterns", []) or []}
        return {"names": [], "patterns": []}

    def _do_move_mapping(self, idx, dest, src_account, copy_excl=False, src_excl=None):
        """Effectue le déplacement : entrée du mapping (A->B dans le JSON) + son
        sous-arbre de cache (copie vers B.cache, purge de A.cache). Écritures
        atomiques. Ne retire PAS l'entrée de self.mappings (fait par l'appelant
        après succès).

        Si copy_excl, écrit `src_excl` comme exclusions globales de B (les mappings
        déjà présents dans B verront alors leur amorçage invalidé — c'est le prix,
        accepté explicitement par l'utilisateur au dialogue de conflit)."""
        mapping = self.mappings[idx]
        source = mapping.get("source", "")

        # 1) Charger le JSON destination (ou structure vide) et y ajouter le mapping.
        dst_map = {"exclusions": {"names": [], "patterns": []}, "mappings": []}
        if os.path.exists(dest):
            try:
                with open(dest, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, list):
                    dst_map = {"exclusions": {"names": [], "patterns": []},
                               "mappings": raw}
                elif isinstance(raw, dict):
                    dst_map = raw
                    dst_map.setdefault("mappings", [])
                    dst_map.setdefault("exclusions", {"names": [], "patterns": []})
            except (OSError, ValueError):
                pass
        if copy_excl and src_excl is not None:
            dst_map["exclusions"] = {"names": list(src_excl.get("names", [])),
                                     "patterns": list(src_excl.get("patterns", []))}
        dst_map["mappings"].append(mapping)
        tmp = dest + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(dst_map, f, ensure_ascii=False, indent=2)
        os.replace(tmp, dest)

        # 1b) Si on a écrasé les exclusions de B, l'amorçage des mappings DÉJÀ
        #     présents dans B dont l'empreinte devient invalide est retiré
        #     (sélectivement — ceux qui gardent les bons filtres restent prêts).
        if copy_excl and os.path.exists(dest) and src_excl is not None:
            self._invalidate_other_mappings_in_file(dest, exclude_source=source,
                                                    new_globals=src_excl)

        # 2) Déplacer le sous-arbre de cache (seulement si A a un cache et que le
        #    mapping est un dossier — un 'file' ou un mapping jamais amorcé n'a
        #    rien à transférer).
        if mapping.get("type") != "folder":
            return
        src_cache_path = self._cache_path_for(self.config_path)
        src_cache = self._read_cache_file(src_cache_path)
        if not src_cache:
            return
        keys = self._subtree_keys(src_cache, source)
        if not keys:
            return
        # Cache destination : charger l'existant ou créer, en estampillant le compte.
        dst_cache_path = self._cache_path_for(dest)
        dst_cache = self._read_cache_file(dst_cache_path)
        if dst_cache is None:
            dst_cache = {}
        if src_account and "__meta__" not in dst_cache:
            dst_cache["__meta__"] = {"account": src_account}
        for k in keys:
            dst_cache[k] = src_cache[k]
        tmp = dst_cache_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(dst_cache, f, ensure_ascii=False)
        os.replace(tmp, dst_cache_path)
        # Purger le sous-arbre du cache source.
        for k in keys:
            del src_cache[k]
        tmp = src_cache_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(src_cache, f, ensure_ascii=False)
        os.replace(tmp, src_cache_path)
        self._cache_memo = None

    def on_remove(self):
        idx = self._selected_index()
        if idx is None:
            return
        removed = self.mappings.pop(idx)
        self._set_dirty(True)
        self._refresh_tree()
        self.status.set(_("Removed: {s}").format(s=removed["source"]))

    # ---------- Exclusions ----------
    def _edit_exclusions_dialog(self, title, current, context_label):
        """Ouvre une fenêtre modale pour éditer noms exacts + motifs.
        `current` est un dict {names: [...], patterns: [...]}.
        Retourne le nouveau dict, ou None si annulé."""
        dlg = tk.Toplevel(self)
        dlg.title(title)
        dlg.geometry("580x520")
        dlg.minsize(520, 460)
        dlg.transient(self)
        dlg.grab_set()

        result = {"value": None}

        # Boutons empilés EN PREMIER (côté bas), pour qu'ils soient toujours
        # réservés et jamais tronqués, même si le reste déborde.
        btns = ttk.Frame(dlg, padding=8)
        btns.pack(side="bottom", fill="x")

        ttk.Label(dlg, text=context_label, padding=(10, 8), wraplength=560,
                  foreground="#333333").pack(side="top", fill="x")

        help_txt = (
            _("• Exact names: one per line (e.g. .caltrash, trash, .Trash-1000). "
            "Case-insensitive. Excludes any folder OR file with that name.\n"
            "• Patterns: one per line, shell style (e.g. *.tmp, .Trash-*, ~*). "
            "The * matches any sequence of characters.")
        )
        ttk.Label(dlg, text=help_txt, padding=(10, 4), wraplength=560,
                  foreground="#666666", justify="left").pack(side="top", fill="x")

        body = ttk.Frame(dlg, padding=8)
        body.pack(side="top", fill="both", expand=True)

        # Colonne noms
        names_frame = ttk.LabelFrame(body, text=_("Exact names (one per line)"), padding=4)
        names_frame.pack(side="left", fill="both", expand=True, padx=(0, 4))
        names_text = tk.Text(names_frame, width=24, height=12, font=("monospace", 10))
        names_text.pack(side="top", fill="both", expand=True)
        names_text.insert("1.0", "\n".join(current.get("names", []) or []))

        # Colonne motifs
        pat_frame = ttk.LabelFrame(body, text=_("Patterns (one per line)"), padding=4)
        pat_frame.pack(side="left", fill="both", expand=True, padx=(4, 0))
        pat_text = tk.Text(pat_frame, width=24, height=12, font=("monospace", 10))
        pat_text.pack(side="top", fill="both", expand=True)
        pat_text.insert("1.0", "\n".join(current.get("patterns", []) or []))

        def on_ok():
            names = [l.strip() for l in names_text.get("1.0", "end").splitlines() if l.strip()]
            pats = [l.strip() for l in pat_text.get("1.0", "end").splitlines() if l.strip()]
            result["value"] = {"names": names, "patterns": pats}
            dlg.destroy()

        def on_cancel():
            dlg.destroy()

        ttk.Button(btns, text=_("Cancel"), command=on_cancel).pack(side="right", padx=2)
        ttk.Button(btns, text="OK", command=on_ok).pack(side="right", padx=2)

        self.wait_window(dlg)
        return result["value"]

    def on_edit_global_exclusions(self):
        new_ex = self._edit_exclusions_dialog(
            _("Global exclusions"),
            self.global_exclusions,
            _("These exclusions apply to ALL mappings. "
            "A folder or file whose name matches will be ignored during the sync.")
        )
        if new_ex is None:
            return
        self.global_exclusions = new_ex
        self._set_dirty(True)
        self._update_excl_summary()
        self._refresh_state_column()   # les exclusions globales changent -> tous les états
        self.status.set(_("Global exclusions updated."))

    def on_edit_mapping_exclusions(self):
        idx = self._selected_index()
        if idx is None:
            return
        m = self.mappings[idx]
        current = m.get("exclusions") or {"names": [], "patterns": []}
        new_ex = self._edit_exclusions_dialog(
            _("Exclusions of this mapping"),
            current,
            _("These exclusions add to the global exclusions, ONLY for:\n{s}").format(s=m["source"])
        )
        if new_ex is None:
            return
        # Si les deux listes sont vides, on retire la clé pour garder le JSON propre.
        if not new_ex["names"] and not new_ex["patterns"]:
            m.pop("exclusions", None)
        else:
            m["exclusions"] = new_ex
        self._set_dirty(True)
        self._refresh_tree()
        self.status.set(_("Mapping exclusions updated."))

    # ---------- Planification (systemd) ----------
    def on_edit_schedule(self):
        if not _HAS_SCHEDULE:
            dlg_error(
                self,
                _("The schedule_manager.py module was not found.\n\n"
                "Place it next to proton_mapping_editor.py to manage the "
                "schedule from this interface."),
                title=_("Missing module"))
            return
        if not self.config_path:
            dlg_info(
                self,
                _("Open or save a mappings file first: the schedule will "
                "apply to that file."),
                title=_("No file"))
            return
        ScheduleDialog(self, self.config_path)


    # ---------- Temps réel (démons systemd --user) ----------
    def on_edit_realtime(self):
        if not _HAS_REALTIME:
            dlg_error(
                self,
                _("The realtime_manager.py module was not found.\n\n"
                "Place it next to proton_mapping_editor.py to manage "
                "real-time from this interface."),
                title=_("Missing module"))
            return
        if not self.config_path:
            dlg_info(
                self,
                _("Open or save a mappings file first: real-time will "
                "apply to that file."),
                title=_("No file"))
            return
        RealtimeDialog(self, self.config_path)

    def _set_auth_state(self, ok):
        """Peint le TÉMOIN de connexion (bas droite) : vert « connecté — <compte> »
        ou rouge « session expirée ». Point unique — tous les indicateurs d'auth
        (sonde de démarrage, résultat réel d'un passage, retour de connexion)
        passent par ici."""
        if not hasattr(self, "auth_status"):
            return
        user = self._account_email   # le VRAI compte, pas le nom du fichier
        if ok:
            txt = (_("🔑 Proton: connected — {u}").format(u=user) if user
                   else _("🔑 Proton: connected"))
            self.auth_label.config(foreground="#1a9e57")
            # Effacer un message d'erreur d'auth PÉRIMÉ en barre d'état : sans
            # ça, le témoin repasse au vert mais « session indisponible » reste
            # affiché — contradiction constatée en production.
            if getattr(self, "_auth_error_in_status", False) and hasattr(self, "status"):
                self.status.set(_("Connected to Proton."))
                self._auth_error_in_status = False
        else:
            txt = _("🔑 Proton: session expired")
            self.auth_label.config(foreground="#d2294b")
        self.auth_status.set(txt)

    def on_proton_login(self):
        """Ouvre le dialogue de connexion Proton (authentification par navigateur).
        Aucun identifiant ne transite par ce logiciel : le CLI ouvre le navigateur,
        l'utilisateur s'y authentifie, et on ne fait que diffuser la sortie du CLI
        (URL de secours + message de succès)."""
        if not _HAS_REALTIME:
            dlg_error(self, _("The realtime_manager.py module was not found."),
                      title=_("Missing module"))
            return
        ProtonLoginDialog(self, on_done=self._refresh_auth_indicator)

    def _mark_auth_disconnected(self):
        """Force l'indicateur d'auth à « non connecté », d'après le RÉSULTAT RÉEL
        d'un passage (tag [auth-failed]). Fait autorité sur la sonde de démarrage,
        qui peut donner un faux positif (token Proton en cache côté CLI)."""
        self._set_auth_state(False)
        if hasattr(self, "status"):
            self.status.set(_("Proton authentication failed during the run — "
                "open “⚙ Configuration…” to renew the session."))
            self._auth_error_in_status = True

    def _mark_auth_connected(self):
        """Force l'indicateur à « connecté », d'après le SUCCÈS RÉEL d'un passage :
        si le moteur a synchronisé, la session Proton est forcément valide. Corrige
        un éventuel faux « session expirée » peint par une sonde concurrente pendant
        le passage."""
        self._set_auth_state(True)

    def _refresh_auth_indicator(self):
        """Met à jour le témoin d'auth ET la barre d'état selon l'état de la
        session (sonde FIABILISÉE : sérialisée + reprise). Appelée après une
        (dé)connexion : efface le message « session indisponible » dès que la
        connexion réussit."""
        def work():
            ok = self._check_auth_settled()
            if ok is None:
                return
            def apply():
                self._set_auth_state(ok)
                if hasattr(self, "status"):
                    if ok:
                        self.status.set(_("Connected to Proton."))
                        self._auth_error_in_status = False
                    else:
                        self.status.set(_("Proton session unavailable (expired token "
                            "or locked keyring) — open “⚙ Configuration…” to sign in."))
                        self._auth_error_in_status = True
            self.after(0, apply)
        threading.Thread(target=work, daemon=True).start()

    def _tray_running(self):
        """True si l'applet de barre des tâches tourne déjà pour CET utilisateur
        (évite une double icône au lancement)."""
        try:
            r = subprocess.run(["pgrep", "-u", str(os.getuid()), "-f", "tray_indicator.py"],
                               capture_output=True)
            return r.returncode == 0
        except Exception:
            return False

    def _apply_tray_setting(self, enabled):
        """Applique le réglage « icône de barre des tâches » : gère le fichier
        d'autostart (~/.config/autostart) et démarre l'applet immédiatement à
        l'activation. À la désactivation, RIEN à tuer : l'applet relit le
        réglage à chaque rafraîchissement et s'éteint d'elle-même."""
        desktop_path = os.path.expanduser(
            "~/.config/autostart/proton-drive-sync-tray.desktop")
        tray_script = os.path.join(APP_DIR, "tray_indicator.py")
        if enabled:
            try:
                os.makedirs(os.path.dirname(desktop_path), exist_ok=True)
                content = (
                    "[Desktop Entry]\n"
                    "Type=Application\n"
                    "Name=Proton Drive Sync — status icon\n"
                    "Name[fr]=Synchro Proton Drive — icône d'état\n"
                    f"Exec={sys.executable} {tray_script}\n"
                    f"Icon={os.path.join(APP_DIR, 'tray_connected.png')}\n"
                    "Comment=Status icon for the Proton Drive sync daemons\n"
                    "Comment[fr]=Icône d'état des démons de synchro Proton Drive\n"
                    "X-GNOME-Autostart-enabled=true\n")
                tmp = desktop_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(content)
                os.replace(tmp, desktop_path)
            except OSError:
                pass   # l'autostart est un confort ; le lancement direct suit
            if not self._tray_running():
                try:
                    subprocess.Popen([sys.executable, tray_script],
                                     cwd=APP_DIR, start_new_session=True)
                except Exception:
                    pass
        else:
            try:
                if os.path.exists(desktop_path):
                    os.remove(desktop_path)
            except OSError:
                pass

    def on_configuration(self):
        """Dialogue UNIFIÉ des préférences : compte Proton (connexion), langue et
        réglages d'installation (NAS, extensions) — un seul bouton dans la barre
        d'outils au lieu de trois. Langue et réglages sont persistés dans
        settings.json et s'appliquent au prochain lancement ; la connexion
        Proton, elle, agit immédiatement (le témoin en bas à droite se met à
        jour)."""
        dlg = tk.Toplevel(self)
        dlg.title(_("Configuration"))
        dlg.transient(self)
        dlg.grab_set()
        dlg.resizable(False, False)

        frm = ttk.Frame(dlg, padding=14)
        frm.pack(fill="both", expand=True)

        def help_btn(parent, key):
            # Aide parentée AU DIALOGUE (et pas à la fenêtre principale), sinon
            # elle s'empile SOUS ce dialogue modal (bug constaté). Le grab est
            # repris au retour (la boîte d'aide l'a consommé en se fermant).
            def show(k=key):
                self._show_help(k, parent=dlg)
                try:
                    dlg.grab_set()
                except tk.TclError:
                    pass
            ttk.Button(parent, text="?", width=2, command=show).pack(side="left", padx=(4, 0))

        # ---- Section Compte Proton ----
        if _HAS_REALTIME:
            acct = ttk.LabelFrame(frm, text=_("Proton account"), padding=10)
            acct.pack(fill="x", pady=(0, 10))
            acct_var = tk.StringVar(value=_("Checking…"))
            acct_label = ttk.Label(acct, textvariable=acct_var)
            acct_label.pack(anchor="w")

            def paint_acct():
                def work():
                    # Sonde FIABILISÉE (sérialisée + reprise) : l'ancienne sonde
                    # directe entrait en course avec celle du <FocusIn> à
                    # l'ouverture du dialogue -> faux « session expirée » alors
                    # que le témoin venait d'être peint en vert (bug constaté).
                    ok = self._check_auth_settled()
                    if ok is None:
                        return   # pas conclu -> on garde « Vérification… »
                    def apply_state():
                        user = self._account_email
                        if ok:
                            acct_var.set(_("🔑 Connected — {u}").format(u=user)
                                         if user else _("🔑 Connected"))
                            acct_label.config(foreground="#1a9e57")
                        else:
                            acct_var.set(_("🔑 Session expired or locked"))
                            acct_label.config(foreground="#d2294b")
                        self._set_auth_state(ok)   # témoin du bas, en même temps
                    try:
                        dlg.after(0, apply_state)
                    except tk.TclError:
                        pass   # dialogue fermé entre-temps
                threading.Thread(target=work, daemon=True).start()

            def sign_in():
                # Parenté au dialogue -> reste AU-DESSUS ; à la fermeture du
                # dialogue de connexion, on reprend le grab et on repeint l'état.
                def done():
                    try:
                        dlg.grab_set()
                    except tk.TclError:
                        pass
                    paint_acct()
                    self._refresh_auth_indicator()
                ProtonLoginDialog(dlg, on_done=done)

            ttk.Button(acct, text=_("🔑 Sign in to Proton"),
                       command=sign_in).pack(anchor="w", pady=(6, 0))
            paint_acct()

        # ---- Section Proton Drive CLI ----
        if _HAS_CONFIG:
            cli_frame = ttk.LabelFrame(frm, text=_("Proton Drive CLI"), padding=10)
            cli_frame.pack(fill="x", pady=(0, 10))
            crow = ttk.Frame(cli_frame); crow.pack(anchor="w", fill="x")
            ttk.Label(crow, text=_("Path to the proton-drive binary: ")).pack(side="left")
            cli_var = tk.StringVar(value=appconfig.proton_cli_path() or "")
            cli_entry = ttk.Entry(crow, textvariable=cli_var, width=34)
            cli_entry.pack(side="left")
            help_btn(crow, "proton-cli-path")

        # ---- Section Langue ----
        if _HAS_I18N:
            lang_frame = ttk.LabelFrame(frm, text=_("Interface language"), padding=10)
            lang_frame.pack(fill="x", pady=(0, 10))
            lang_var = tk.StringVar(value=i18n.read_language_setting())
            for value, label in (("auto", _("Auto (system language)")),
                                 ("en", "English"),
                                 ("fr", "Français"),
                                 ("de", "Deutsch"),
                                 ("es", "Español"),
                                 ("it", "Italiano"),
                                 ("pt", "Português")):
                ttk.Radiobutton(lang_frame, text=label, value=value,
                                variable=lang_var).pack(anchor="w", pady=1)

        # ---- Section NAS ----
        if _HAS_CONFIG:
            nas_frame = ttk.LabelFrame(frm, text=_("NAS"), padding=10)
            nas_frame.pack(fill="x", pady=(0, 10))
            nas_var = tk.BooleanVar(value=appconfig.nas_enabled())
            row1 = ttk.Frame(nas_frame); row1.pack(anchor="w", fill="x")
            ttk.Checkbutton(row1, text=_("Use a NAS"), variable=nas_var).pack(side="left")
            help_btn(row1, "nas-enabled")

            row2 = ttk.Frame(nas_frame); row2.pack(anchor="w", fill="x", pady=(8, 0))
            ttk.Label(row2, text=_("NAS mount point: ")).pack(side="left")
            mount_var = tk.StringVar(value=appconfig.nas_mount_path())
            mount_entry = ttk.Entry(row2, textvariable=mount_var, width=28)
            mount_entry.pack(side="left")
            help_btn(row2, "nas-mount-path")

            row2b = ttk.Frame(nas_frame); row2b.pack(anchor="w", fill="x", pady=(8, 0))
            ttk.Label(row2b, text=_("NAS identity (account name): ")).pack(side="left")
            ident_var = tk.StringVar(value=appconfig.account_name() or "")
            ident_entry = ttk.Entry(row2b, textvariable=ident_var, width=28)
            ident_entry.pack(side="left")
            help_btn(row2b, "nas-identity")

            def sync_mount_state(*_a):
                state = "normal" if nas_var.get() else "disabled"
                mount_entry.configure(state=state)
                ident_entry.configure(state=state)
            nas_var.trace_add("write", sync_mount_state)
            sync_mount_state()

            # ---- Section Extensions ----
            ext_frame = ttk.LabelFrame(frm, text=_("File extensions"), padding=10)
            ext_frame.pack(fill="x", pady=(0, 10))
            rename_var = tk.BooleanVar(value=appconfig.rename_ext_enabled())
            row3 = ttk.Frame(ext_frame); row3.pack(anchor="w", fill="x")
            ttk.Checkbutton(row3, text=_("Fix uppercase extensions"),
                            variable=rename_var).pack(side="left")
            help_btn(row3, "rename-ext-enabled")

            row4 = ttk.Frame(ext_frame); row4.pack(anchor="w", fill="x", pady=(8, 0))
            ttk.Label(row4, text=_("Suffix on name clash: ")).pack(side="left")
            suffix_var = tk.StringVar(value=appconfig.rename_ext_collision_suffix())
            suffix_entry = ttk.Entry(row4, textvariable=suffix_var, width=20)
            suffix_entry.pack(side="left")
            help_btn(row4, "rename-ext-suffix")

            def sync_suffix_state(*_a):
                suffix_entry.configure(state="normal" if rename_var.get() else "disabled")
            rename_var.trace_add("write", sync_suffix_state)
            sync_suffix_state()

            # ---- Section Barre des tâches ----
            tray_frame = ttk.LabelFrame(frm, text=_("System tray"), padding=10)
            tray_frame.pack(fill="x", pady=(0, 10))
            tray_var = tk.BooleanVar(value=appconfig.tray_enabled())
            row5 = ttk.Frame(tray_frame); row5.pack(anchor="w", fill="x")
            ttk.Checkbutton(row5, text=_("Show the status icon in the system tray"),
                            variable=tray_var).pack(side="left")
            help_btn(row5, "tray-icon")

        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(4, 0))

        def apply():
            if _HAS_CONFIG:
                ok, reason = appconfig.validate_collision_suffix(suffix_var.get())
                if not ok:
                    dlg_error(dlg, reason, title=_("Configuration"))
                    dlg.grab_set()
                    return
            if _HAS_I18N:
                i18n.write_language_setting(lang_var.get())
            if _HAS_CONFIG:
                appconfig.set_proton_cli_path((cli_var.get() or "").strip() or None)
            if _HAS_CONFIG:
                appconfig.set_nas_enabled(nas_var.get())
                appconfig.set_nas_mount_path(mount_var.get())
                # MIGRATION C : si l'identité change et que l'ancienne existe
                # sur le NAS, proposer le renommage (file + copie de mappings,
                # billets préservés). Refusée ou non applicable -> le réglage
                # change quand même ; l'ancien reste sur le NAS (le watcher le
                # signalera comme orphelin s'il contient des billets).
                old_ident = appconfig.account_name()
                new_ident = (ident_var.get() or "").strip()
                if (_HAS_REALTIME and old_ident and new_ident
                        and old_ident != new_ident
                        and nas_var.get()
                        and realtime_manager.nas_reachable()
                        and (os.path.isdir(os.path.join(
                                realtime_manager.NAS_QUEUE_DIR, old_ident))
                             or os.path.exists(os.path.join(
                                realtime_manager.NAS_CONFIG_DIR,
                                f"mappings-{old_ident}.json")))):
                    if dlg_confirm(dlg,
                            _("The identity “{a}” exists on the NAS (marker "
                              "queue and/or mappings copy).\n\nMigrate it to "
                              "“{b}”? Everything is simply renamed — pending "
                              "markers and continuity are preserved.\n\n"
                              "Afterwards, restart the background services "
                              "(⚡ Real-time… → Install / Update) so they "
                              "follow the new name.").format(
                              a=old_ident, b=new_ident),
                            title=_("NAS identity"),
                            ok_text=_("Migrate"),
                            cancel_text=_("Do not migrate")):
                        mok, mmsg = realtime_manager.migrate_nas_identity(
                            old_ident, new_ident)
                        (dlg_info if mok else dlg_error)(dlg, mmsg,
                                                         title=_("NAS identity"))
                        if not mok:
                            dlg.grab_set()
                            return       # réglage inchangé : re-choisir
                    dlg.grab_set()
                appconfig.set_account_name(ident_var.get())
                appconfig.set_rename_ext_enabled(rename_var.get())
                appconfig.set_rename_ext_collision_suffix(suffix_var.get())
                # Icône de barre des tâches : effet IMMÉDIAT (démarrage à
                # l'activation ; extinction d'elle-même à la désactivation).
                appconfig.set_tray_enabled(tray_var.get())
                self._apply_tray_setting(tray_var.get())
            dlg.destroy()
            dlg_info(
                self,
                _("Configuration saved.\n\n"
                  "It will apply to this window at its next launch, and to "
                  "the background daemons at their next restart."),
                title=_("Configuration"))

        ttk.Button(btns, text=_("Cancel"),
                   command=dlg.destroy).pack(side="right", padx=4)
        ttk.Button(btns, text=_("OK"), command=apply).pack(side="right")



    # ---------- Lancement de la synchro ----------
    def _current_selection_sources(self):
        """Sources des mappings actuellement sélectionnés dans l'arbre.
        Retourne None si aucune ligne n'est sélectionnée (=> passage sur TOUS les
        mappings), sinon la liste des 'source' sélectionnées (=> --only-source
        ciblé). Le tableau autorise la multi-sélection, exactement comme
        l'amorçage : « Lancer la synchro » et « Amorcer » suivent la même règle."""
        sel = self.tree.selection()
        if not sel:
            return None
        try:
            chosen = [self.mappings[int(iid)] for iid in sel]
        except (ValueError, IndexError):
            return None
        srcs = [m["source"] for m in chosen if m.get("source")]
        return srcs or None

    def _mappings_for_sources(self, only_sources):
        """Sous-ensemble de mappings correspondant à only_sources (comparaison de
        chemins normalisée, comme le moteur). Si only_sources est None, renvoie
        tous les mappings. Sert à cadrer l'avertissement de suppression sur la
        portée réelle du passage."""
        if not only_sources:
            return list(self.mappings)
        wanted = {os.path.normpath(s) for s in only_sources}
        return [m for m in self.mappings
                if os.path.normpath(m.get("source", "")) in wanted]

    def _build_engine_args(self, only_sources=None):
        """Construit la liste d'arguments du moteur selon les options cochées.
        Si only_sources est fourni (sélection dans l'arbre), restreint le passage
        à ces mappings via --only-source (répétable) — même mécanisme que
        l'amorçage. Sans sélection : tous les mappings sont traités."""
        args = [self.config_path]
        if self.opt_dry_run.get():
            args.append("--dry-run")
        if self.opt_verify_hash.get():
            args.append("--verify-hash")
        if self.opt_ignore_cache.get():
            args.append("--ignore-cache")
        if self.opt_verbose.get():
            args.append("-v")
        if self.opt_delete.get():
            args.append("--delete")
        for s in (only_sources or []):
            args += ["--only-source", s]
        return args

    def _log_path(self):
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M")
        return os.path.join(DEFAULT_LOG_DIR, f"sync-{ts}.log")

    def _build_shell_command(self, only_sources=None):
        """Reconstruit une commande shell équivalente, copiable dans un terminal.
        Reflète la sélection courante (only_sources) pour rester fidèle à ce que
        « Lancer la synchro » exécuterait."""
        engine_args = self._build_engine_args(only_sources)
        log_path = self._log_path()
        cmd = (
            f"PROTON_DRIVE_CLI={shlex.quote(DEFAULT_CLI)} "
            f"python3 {shlex.quote(DEFAULT_ENGINE)} "
            + " ".join(shlex.quote(a) for a in engine_args)
            + f" 2>&1 | tee {shlex.quote(log_path)}"
        )
        return cmd

    def on_copy_command(self):
        if not self.config_path:
            dlg_info(self, _("Open or save a mappings file first."), title=_("No file"))
            return
        cmd = self._build_shell_command(self._current_selection_sources())
        self.clipboard_clear()
        self.clipboard_append(cmd)
        self.status.set(_("Command copied to the clipboard."))
        self._append_output("[Commande copiée]\n" + cmd + "\n\n")

    def on_clear_output(self):
        self._drain_output_queue()
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.configure(state="disabled")

    def _drain_output_queue(self):
        """Vide la file de sortie sans l'afficher (avant un effacement / re-filtrage)."""
        q = getattr(self, "_out_queue", None)
        if q is None:
            return
        try:
            while True:
                q.get_nowait()
        except queue.Empty:
            pass

    def _append_output(self, text):
        """Producteur : file la ligne dans une queue thread-safe. Le rendu réel est
        fait par _pump_output (thread principal, par lots). Sûr depuis n'importe
        quel thread — aucun appel Tkinter inter-thread."""
        q = getattr(self, "_out_queue", None)
        if q is None:
            self._out_queue = q = queue.Queue()
        q.put(text)

    # Cap anti-gonflement du widget de sortie : au-delà de _OUTPUT_MAX_LINES, on
    # rogne les plus anciennes lignes (le log DISQUE reste la référence complète).
    # Garde le widget réactif sur les très gros passages.
    _OUTPUT_MAX_LINES = 8000
    _OUTPUT_TRIM_TO = 6000

    def _pump_output(self):
        """Consommateur (thread principal) : vide la file par LOTS — une seule
        insertion et un seul autoscroll pour tout ce qui s'est accumulé depuis le
        dernier passage. Se reprogramme toujours."""
        chunks = []
        try:
            while True:
                chunks.append(self._out_queue.get_nowait())
        except queue.Empty:
            pass
        if chunks:
            try:
                self.output.configure(state="normal")
                self.output.insert("end", "".join(chunks))
                total = int(self.output.index("end-1c").split(".")[0])
                if total > self._OUTPUT_MAX_LINES:
                    self.output.delete("1.0", f"{total - self._OUTPUT_TRIM_TO}.0")
                self.output.see("end")
                self.output.configure(state="disabled")
            except Exception:
                pass
        self.after(80, self._pump_output)

    # ---------- Affichage épuré (un chemin par dossier) vs brut (« Détaillé ») ----------
    _STATUS_PREFIXES = ("===", "▶", "⏸", "✓", "✗", "❌", "⚠", "🔑", "🌱", "⟳",
                        "♻", "🗑", "⛔", "📂", "==", "Terminé", "Done", "Erreur",
                        "Error", "Cache", "Global", "▶ Mapping", "  ↪")

    def _is_status_line(self, stripped):
        """Ligne d'orchestration / de résumé / d'erreur : toujours affichée, même en
        mode épuré (pour ne jamais perdre le fil ni masquer une erreur)."""
        if stripped.startswith(self._STATUS_PREFIXES):
            return True
        low = stripped.lower()
        return ("mapping " in low and "/" in stripped and "=>" in stripped) \
            or "error" in low or "erreur" in low or "❌" in stripped

    def _is_error_line(self, stripped):
        """Ligne d'erreur ou d'avertissement (pour le filtre « ❗ Erreurs seules »).
        Les glyphes ❌ ⛔ ⚠ marquent toujours une erreur, où qu'ils soient (ils ne
        figurent jamais dans un nom de fichier légitime). Les MOTS-CLÉS (error /
        erreur / échec / failed), eux, ne sont cherchés que dans le PRÉFIXE de la
        ligne — avant le premier « / » — pour NE PAS attraper un fichier ou dossier
        légitime dont le NOM contient ces mots (ex. « 📂 …/Rapport_erreur »)."""
        if not stripped:
            return False
        if any(g in stripped for g in ("❌", "⛔", "⚠")):
            return True
        head = stripped.split("/", 1)[0].lower()   # préfixe/label, avant le chemin
        return ("erreur" in head or "error" in head
                or "échec" in head or "echec" in head or "failed" in head)

    def _feed_output(self, line):
        """Affiche une ligne du moteur selon le filtre courant, et la bufferise via
        le log disque (le vrai flux complet). L'appelant écrit toujours la ligne
        entière dans le log disque, quel que soit le filtre d'affichage."""
        self._render_line(line)

    def _render_line(self, line):
        """Décide de l'affichage d'UNE ligne selon les filtres courants :
          1) « ❗ Erreurs seules » (prioritaire) -> uniquement les lignes d'erreur ;
          2) « Détaillé » -> brut ;
          3) sinon -> épuré : lignes de statut/progression (dont « 📂 <dossier> »
             émises par le moteur, un dossier par ligne dans l'ordre du balayage) +
             lignes d'erreur. Le détail (fichiers, JSON, uploads…) est masqué. Le
             GUI ne DEVINE plus le dossier — le moteur l'annonce."""
        if self.opt_errors_only.get():
            stripped = line.strip()
            if self._is_error_line(stripped):
                self._append_output(line if line.endswith("\n") else line + "\n")
            return
        if line.lstrip().startswith("📂"):
            # Compteur de dossiers balayés (sert au message « rien à synchroniser »
            # si zéro dossier n'a été vu). Compté quel que soit le mode d'affichage.
            self._folders_shown = getattr(self, "_folders_shown", 0) + 1
        if self.opt_verbose.get():
            self._append_output(line)
            return
        stripped = line.strip()
        if not stripped:
            return
        if self._is_status_line(stripped):
            self._append_output(line if line.endswith("\n") else line + "\n")
        # sinon : ligne de détail -> masquée en mode épuré

    def _reapply_output_filter(self):
        """Re-filtre l'affichage quand on (dé)coche « Erreurs seules » : on efface
        la zone et on rejoue le log disque du passage courant (source complète) à
        travers le filtre courant. Ainsi cocher la case révèle les erreurs déjà
        passées, pas seulement les lignes à venir. Sans log courant : no-op."""
        path = getattr(self, "_current_log_path", None)
        self._drain_output_queue()
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.configure(state="disabled")
        self._last_shown_folder = None
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    self._render_line(line)
        except OSError:
            pass

    def on_run_sync(self):
        if not self.config_path:
            dlg_info(self, _("Open or save a mappings file first."), title=_("No file"))
            return
        # Portée du passage = sélection VIVANTE, capturée AVANT tout enregistrement.
        # Un enregistrement rafraîchit l'arbre (tree.delete des enfants) et
        # effacerait la sélection : on la lit donc en premier, comme l'amorçage.
        # None => tous les mappings ; sinon => --only-source ciblé (D1).
        only_sources = self._current_selection_sources()
        if self.dirty:
            if dlg_confirm(self,
                           _("The mappings file has unsaved changes. "
                           "Save them before running?"),
                           title=_("Save first?"), kind="question",
                           ok_text=_("Save"), cancel_text=_("No")):
                self.on_save()
        if self.sync_process and self.sync_process.poll() is None:
            dlg_info(self, _("A sync is already running."), title=_("Already running"))
            return
        if not os.path.exists(DEFAULT_ENGINE):
            dlg_error(self, _("The engine was not found at:\n{p}").format(p=DEFAULT_ENGINE),
                      title=_("Engine not found"))
            return

        # Mappings réellement concernés par ce passage (portée) : la sélection
        # restreint aussi l'avertissement de suppression, pour qu'il ne parle que
        # des mappings qui seront effectivement traités.
        eff_mappings = self._mappings_for_sources(only_sources)
        # Réutilise les chaînes de portée déjà présentes au catalogue (amorçage).
        scope_txt = (_("{n} selected mapping(s)").format(n=len(only_sources))
                     if only_sources
                     else _("all {n} mapping(s)").format(n=len(self.mappings)))
        scope_line = _("Scope: {s}").format(s=scope_txt)
        if only_sources:
            scope_line += "\n  • " + "\n  • ".join(
                os.path.basename(s) for s in only_sources)

        # Confirmation de sécurité si la propagation des suppressions est active
        # en mode RÉEL (le dry-run ne supprime rien, donc pas de confirmation).
        if self.opt_delete.get() and not self.opt_dry_run.get():
            # Le mode (corbeille/définitif) vient de chaque mapping. On regarde
            # s'il existe au moins un mapping qui supprime ET en mode définitif,
            # pour renforcer l'avertissement le cas échéant — restreint à la portée.
            perm_mappings = [m for m in eff_mappings
                             if m.get("allow_delete") and m.get("delete_mode") == "permanent"]
            any_delete = any(m.get("allow_delete") for m in eff_mappings)
            if not any_delete:
                msg = (scope_line + "\n\n" +
                       _("“Propagate deletions” is checked, but no mapping in scope "
                       "allows deletion (set it via Edit). Nothing will be "
                       "deleted.\n\nRun anyway?"))
                kind = "info"
            elif perm_mappings:
                noms = "\n  • ".join(os.path.basename(m["source"]) for m in perm_mappings)
                msg = (scope_line + "\n\n" +
                       _("You are launching a sync with “Propagate deletions”.\n\n"
                       "⚠  {n} mapping(s) are in PERMANENT mode "
                       "(deletion without trash, IRREVERSIBLE):\n  • {names}"
                       "\n\nThe other mappings allowing deletion will go to the "
                       "trash (recoverable for 30 days).\n\n"
                       "Tip: a “Test (dry-run)” first shows what would be "
                       "deleted.\n\nRun anyway?").format(n=len(perm_mappings), names=noms))
                kind = "warning"
            else:
                msg = (scope_line + "\n\n" +
                       _("You are launching a sync with “Propagate deletions”.\n\n"
                       "For the mappings that allow deletion, what was deleted "
                       "locally will be sent to the Proton trash "
                       "(recoverable for 30 days).\n\n"
                       "Tip: a “Test (dry-run)” first shows what would be "
                       "deleted.\n\nRun?"))
                kind = "question"
            if not dlg_confirm(self, msg, title=_("Confirm deletion propagation"),
                               kind=kind, ok_text=_("Run"), cancel_text=_("Cancel")):
                self.status.set(_("Launch cancelled."))
                return

        os.makedirs(DEFAULT_LOG_DIR, exist_ok=True)
        log_path = self._log_path()
        self._current_log_path = log_path
        engine_args = self._build_engine_args(only_sources)
        cmd = ["python3", DEFAULT_ENGINE] + engine_args

        env = dict(os.environ)
        env["PROTON_DRIVE_CLI"] = DEFAULT_CLI

        # Bannière de portée (D2 = option a) : bien visible, sans modale pour un
        # passage additif. Sélection => sous-ensemble ; aucune => tous. Réutilise
        # scope_line (chaînes déjà au catalogue) pour éviter de nouveaux msgid.
        self._append_output("▶ " + scope_line + "\n")
        self._append_output(_("=== Launch: {c} ===").format(c=" ".join(shlex.quote(c) for c in cmd)) + "\n")
        self._append_output(_("=== Log: {p} ===").format(p=log_path) + "\n\n")
        self.status.set(_("Sync in progress…")
                        + ((" — " + scope_txt) if only_sources else ""))
        self.run_button.configure(state="disabled")
        self.stop_button.configure(state="normal")

        # Lancer dans un thread pour ne pas figer l'interface.
        t = threading.Thread(target=self._run_sync_thread, args=(cmd, env, log_path), daemon=True)
        t.start()

    def _run_sync_thread(self, cmd, env, log_path):
        try:
            with open(log_path, "w", encoding="utf-8") as logf:
                self.sync_process = subprocess.Popen(
                    cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1,  # line-buffered
                )
                self._last_shown_folder = None
                self._folders_shown = 0
                self._auth_failed_seen = False
                for line in self.sync_process.stdout:
                    if "[account-changed]" in line and "→" not in line:
                        self.after(0, lambda: self.status.set(
                            _("Proton account changed — prime the cache (or "
                              "reset the mappings) to rebuild on the new "
                              "account.")))
                    if "[auth-failed]" in line:
                        self._auth_failed_seen = True
                    self._feed_output(line)      # affichage (brut ou épuré)
                    logf.write(line)             # log disque : toujours complet
                    logf.flush()
                self.sync_process.wait()
            code = self.sync_process.returncode
            # Mode épuré : si aucun dossier n'a été affiché, l'écran serait vide ->
            # message explicite pour ne pas laisser croire à un plantage.
            if not self.opt_verbose.get() and getattr(self, "_folders_shown", 0) == 0 and code == 0:
                self._append_output(_("  ✓ Nothing to update — everything is already in sync.") + "\n")
            self._append_output("\n" + _("=== Finished (code {c}) — log: {p} ===").format(c=code, p=log_path) + "\n\n")
            self.after(0, lambda: self.status.set(_("Sync finished (code {c}).").format(c=code)))
            # Un vrai passage fait autorité sur l'état d'auth : s'il a rapporté un
            # échec d'authentification, l'indicateur doit le refléter (plus fiable
            # que la sonde de démarrage, qui peut donner un faux positif si le CLI
            # Proton a un token en cache).
            if getattr(self, "_auth_failed_seen", False):
                self.after(0, self._mark_auth_disconnected)
            else:
                self.after(0, self._mark_auth_connected)
        except Exception as e:
            self._append_output("\n" + _("=== Launch error: {e} ===").format(e=e) + "\n\n")
            self.after(0, lambda: self.status.set(_("Error: {e}").format(e=e)))
        finally:
            self.sync_process = None
            self.after(0, lambda: self.run_button.configure(state="normal"))
            self.after(0, lambda: self.stop_button.configure(state="disabled"))

    def on_stop_sync(self):
        # Casser une éventuelle attente de verrou (amorçage/reset patients).
        self._stop_requested = True
        if self.sync_process and self.sync_process.poll() is None:
            self.sync_process.terminate()
            self._append_output("\n=== Interruption demandée (terminate) ===\n")
            self.status.set(_("Sync interrupted."))

    def _lock_is_busy(self, env=None):
        """True si le verrou moteur (~/.proton_sync.lock) est actuellement tenu par
        un autre passage. Utilise --check-lock du moteur : une sonde qui teste
        EXACTEMENT le même flock puis le relâche aussitôt (non destructif, ne lance
        aucune synchro). Renvoie False en cas de doute (mieux vaut tenter le passage
        que rester bloqué à tort)."""
        try:
            e = env if env is not None else dict(os.environ)
            e.setdefault("PROTON_DRIVE_CLI", DEFAULT_CLI)
            r = subprocess.run(
                ["python3", DEFAULT_ENGINE, self.config_path, "--check-lock"],
                env=e, capture_output=True, text=True, timeout=15)
            # Convention moteur : code 0 = libre, code non nul = occupé.
            return r.returncode != 0
        except Exception:
            return False

    # ---------- Amorçage du cache (passage complet ciblé, --delete corbeille) ----------
    def _cache_complete_count(self):
        """Nombre de dossiers marqués subtree_complete dans le cache du fichier
        actif (métrique de progression fiable, lue au fil de l'amorçage). Réutilise
        _read_cache_data (mémoïsé par mtime) — donc AUCUN parse supplémentaire quand
        _prime_tick lit déjà le cache au même instant."""
        data = self._read_cache_data()
        return sum(1 for v in data.values()
                   if isinstance(v, dict) and v.get("subtree_complete"))

    def on_prime_cache(self):
        """Amorce le cache : passage COMPLET `--delete` (corbeille) sur les mappings
        sélectionnés (ou tous si aucune sélection), qui marque subtree_complete et
        rend ces mappings « prêts pour le temps réel ». Orchestre tout : arrêt des
        démons et du timer, vérif de la session Proton, amorçage avec progression,
        puis redémarrage des démons. Une seule instance peut tourner à la fois."""
        if not self.config_path:
            dlg_info(self, _("Open or save a mappings file first."), title=_("No file"))
            return
        if self.dirty:
            if dlg_confirm(self, _("The mappings file has unsaved changes. "
                           "Save them before priming?"), title=_("Save first?"),
                           kind="question", ok_text=_("Save"), cancel_text=_("No")):
                self.on_save()
        if self.sync_process and self.sync_process.poll() is None:
            dlg_info(self, _("A sync is already running."), title=_("Already running"))
            return
        if not os.path.exists(DEFAULT_ENGINE):
            dlg_error(self, _("The engine was not found at:\n{p}").format(p=DEFAULT_ENGINE),
                      title=_("Engine not found"))
            return

        # Sélection -> sources (Q5). Aucune sélection -> tous les mappings (Q8=a).
        sel = self.tree.selection()
        if sel:
            try:
                chosen = [self.mappings[int(iid)] for iid in sel]
            except (ValueError, IndexError):
                chosen = list(self.mappings)
        else:
            chosen = list(self.mappings)
        sources = [m["source"] for m in chosen if m.get("source")]
        if not sources:
            dlg_info(self, _("No mapping to prime."), title=_("Nothing to do"))
            return
        scope = (_("all {n} mapping(s)").format(n=len(sources)) if not sel
                 else _("{n} selected mapping(s)").format(n=len(sources)))

        # Amorçage piloté PAR LA CONFIG DE CHAQUE MAPPING — plus aucune option
        # dans ce dialogue. Le moteur reçoit --delete globalement, mais l'applique
        # mapping par mapping : `mapping_delete = delete and allow_delete` (voir
        # proton_sync). Donc chaque mapping suit SA vocation, en un seul passage :
        #   - corbeille vide  (allow_delete absent) -> additif : rien n'est supprimé
        #   - corbeille       (delete_mode=trash)   -> miroir corbeille (30 j)
        #   - suppr. immédiate(delete_mode=permanent)-> miroir définitif
        # Le delete_mode est lui aussi lu par mapping. Pas besoin de regrouper en
        # lots : un unique passage --delete réalise nativement le comportement
        # « chaque mapping avec ses options ».
        names = "\n  • ".join(os.path.basename(s) for s in sources)
        if not dlg_confirm(
            self,
            _("Prime {scope}:\n  • {names}\n\n"
              "Each mapping is primed with the options set in its own "
              "configuration (the trash field): additive mappings upload without "
              "ever deleting; mirror mappings (trash / permanent) reconcile the "
              "destination. Once done, the mappings become available for real-time "
              "processing.\n\n"
              "The real-time consumer and the scheduled timer are paused during "
              "priming and restored afterwards. This can take a while on large "
              "folders (it is interruptible and resumes where it left off).\n\n"
              "Start priming?").format(scope=scope, names=names),
            title=_("Prime cache"), kind="question",
            ok_text=_("Start priming"), cancel_text=_("Cancel")):
            self.status.set(_("Priming cancelled."))
            return

        self.prime_button.configure(state="disabled")
        self.reset_button.configure(state="disabled")
        self.run_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self._prime_current = ""
        self._progress_kind = "prime"
        self._prime_running = True
        t = threading.Thread(target=self._prime_thread, args=(sources,), daemon=True)
        t.start()
        self._prime_tick()

    def on_reset_mapping(self):
        """Réinitialise le(s) mapping(s) SÉLECTIONNÉ(s) : purge de leur cache (retour
        en ⏳) puis reconstruction via un passage --delete ciblé (identique à
        l'amorçage → cache armé subtree_complete + delete_synced). Option « vider
        aussi le dossier distant » (corbeille, sous garde-fou de montage).
        Idempotent : rejouable autant que voulu (⏳ → Réinitialiser ; ✅ → Lancer)."""
        if not self.config_path:
            dlg_info(self, _("Open or save a mappings file first."), title=_("No file"))
            return
        if self.dirty:
            if dlg_confirm(self, _("The mappings file has unsaved changes. "
                           "Save them before resetting?"), title=_("Save first?"),
                           kind="question", ok_text=_("Save"), cancel_text=_("No")):
                self.on_save()
        if self.sync_process and self.sync_process.poll() is None:
            dlg_info(self, _("A sync is already running."), title=_("Already running"))
            return
        if not os.path.exists(DEFAULT_ENGINE):
            dlg_error(self, _("The engine was not found at:\n{p}").format(p=DEFAULT_ENGINE),
                      title=_("Engine not found"))
            return

        # La réinitialisation EXIGE une sélection explicite (à la différence de
        # l'amorçage) : réinitialiser « tout » d'un coup serait rarement voulu et
        # potentiellement destructeur (avec vidage). Aucune sélection -> on informe.
        sel = self.tree.selection()
        if not sel:
            dlg_info(self, _("Select the mapping(s) to reset in the list first."),
                     title=_("No selection"))
            return
        try:
            chosen = [self.mappings[int(iid)] for iid in sel]
        except (ValueError, IndexError):
            dlg_info(self, _("Select the mapping(s) to reset in the list first."),
                     title=_("No selection"))
            return
        sources = [m["source"] for m in chosen if m.get("source")]
        if not sources:
            dlg_info(self, _("No mapping to reset."), title=_("Nothing to do"))
            return

        names = "\n  • ".join(os.path.basename(s.rstrip("/")) for s in sources)
        ok, wipe = dlg_confirm_checkbox(
            self,
            _("Reset {n} selected mapping(s):\n  • {names}\n\n"
              "This PURGES their local cache (they fall back to ⏳ “pending”) and "
              "rebuilds them with a targeted pass — like priming. Each mapping is "
              "rebuilt according to ITS OWN configuration (the trash field): "
              "additive mappings come back ready for real-time without deleting "
              "anything; mirror mappings come back fully armed for their deletions "
              "(trash or permanent, as configured).\n\n"
              "Optionally, tick below to also empty each mapping's REMOTE folder "
              "(sent to Proton TRASH, recoverable 30 days) before rebuilding — under "
              "the mount guard. You will purge the trash yourself once you have "
              "checked the re-upload succeeded.\n\n"
              "Real-time consumer and the scheduled timer are paused during the "
              "reset and restored afterwards. It is interruptible: to resume, just "
              "press Reset again (idempotent).\n\n"
              "Start reset?").format(n=len(sources), names=names),
            checkbox_text=_("Also empty the remote folder (→ Proton trash)"),
            title=_("Reset mapping"), kind="warning",
            ok_text=_("Start reset"), cancel_text=_("Cancel"),
            checkbox_default=False)
        if not ok:
            self.status.set(_("Reset cancelled."))
            return

        self.prime_button.configure(state="disabled")
        self.reset_button.configure(state="disabled")
        self.run_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self._prime_current = ""
        self._progress_kind = "reset"
        self._prime_running = True
        t = threading.Thread(target=self._reset_thread, args=(sources, wipe), daemon=True)
        t.start()
        self._prime_tick()

    def _prime_tick(self):
        """Rafraîchit la barre d'état pendant un passage orchestré (amorçage ou
        réinitialisation) : nombre de dossiers analysés (lu du cache) + dossier en
        cours (extrait du flux). Le vocabulaire suit self._progress_kind."""
        if not getattr(self, "_prime_running", False):
            return
        n = self._cache_complete_count()
        cur = getattr(self, "_prime_current", "")
        cur_txt = (" · " + cur) if cur else ""
        if getattr(self, "_progress_kind", "prime") == "reset":
            self.status.set(_("♻ Resetting — {n} folder(s) analyzed{c}").format(n=n, c=cur_txt))
        else:
            self.status.set(_("🌱 Priming — {n} folder(s) analyzed{c}").format(n=n, c=cur_txt))
        # Mettre à jour la colonne d'état en direct : les mappings passent à ✅ un à
        # un à mesure qu'ils sont consolidés (indicateur d'avancement mapping/mapping).
        try:
            self._refresh_state_column()
        except Exception:
            pass
        self.after(1500, self._prime_tick)

    def _prime_thread(self, sources):
        """Enveloppe : construit la commande d'amorçage puis délègue à
        l'orchestration commune.

        --delete est TOUJOURS passé : le moteur l'applique mapping par mapping
        (`mapping_delete = delete and allow_delete`), donc les mappings additifs
        ne suppriment rien tandis que les mappings miroir réconcilient selon leur
        propre delete_mode. Un seul passage réalise « chaque mapping avec ses
        options »."""
        # --accept-account-change : ces deux actions (Amorcer/Réinitialiser)
        # sont PRÉCISÉMENT la voie de sortie prévue après un changement de
        # compte Proton — le moteur écarte alors l'ancien cache et reconstruit
        # pour le nouveau compte. Sans changement de compte, le drapeau est
        # sans effet.
        cmd = ["python3", DEFAULT_ENGINE, self.config_path, "--delete", "-v",
               "--accept-account-change"]
        for s in sources:
            cmd += ["--only-source", s]
        self._orchestrated_run(cmd, "prime", len(sources))

    def _reset_thread(self, sources, wipe):
        """Enveloppe : construit la commande de réinitialisation (--reset-source,
        + --wipe-remote si demandé) puis délègue à l'orchestration commune. Le
        moteur force lui-même --delete pour armer le cache (delete_synced)."""
        cmd = ["python3", DEFAULT_ENGINE, self.config_path, "-v",
               "--accept-account-change"]
        for s in sources:
            cmd += ["--reset-source", s]
        if wipe:
            cmd.append("--wipe-remote")
        self._orchestrated_run(cmd, "reset", len(sources))

    def _orchestrated_run(self, cmd, kind, n_sources):
        """Cœur COMMUN à l'amorçage (kind='prime') et à la réinitialisation
        (kind='reset') : vérif de la session Proton, arrêt du SEUL consommateur
        (le watcher reste actif), pause du timer, exécution du moteur avec
        affichage épuré/brut + progression, puis redémarrage du consommateur et du
        timer, et bilan X/Y prêts pour le temps réel. `cmd` est déjà construite ;
        les deux appelants ne diffèrent que par leurs drapeaux. On ne duplique pas
        cette orchestration — les deux passages la partagent."""
        is_reset = (kind == "reset")
        if is_reset:
            header       = _("Resetting the mapping(s)")
            abort_status = _("Reset aborted: sign in to Proton first.")
            started_txt  = _("▶ Reset started ({n} mapping(s))…").format(n=n_sources)
        else:
            header       = _("Priming the cache")
            abort_status = _("Priming aborted: sign in to Proton first.")
            started_txt  = _("▶ Priming started ({n} mapping(s))…").format(n=n_sources)

        if not _HAS_REALTIME:
            self._append_output(_("[realtime_manager.py missing — cannot orchestrate daemons]") + "\n")
        try:
            # 1) Vérifier la session Proton AVANT tout (inutile de lancer si le
            #    token va lâcher).
            self._append_output("\n=== " + header + " ===\n")
            if _HAS_REALTIME:
                self._append_output(_("🔑 Checking Proton session…") + "\n")
                # Sonde FIABILISÉE partagée (_check_auth_settled) : sérialisée
                # avec les autres sondes (focus, dialogue Configuration) et
                # tolérante au faux négatif transitoire (reprise après 2,5 s).
                # Un VRAI échec, lui, persiste sur les deux essais.
                ok = self._check_auth_settled()
                if not ok:   # False OU None (pas conclu) -> on ne lance pas
                    self._append_output(_("❌ Proton session unavailable — sign in "
                        "first (button “Sign in to Proton”), then prime again.") + "\n")
                    self.after(0, lambda: self.status.set(abort_status))
                    return
                self._append_output(_("   ✓ session OK") + "\n")
                # 2) Arrêter SEULEMENT le consommateur (éviter la collision de
                #    verrou). Le WATCHER reste actif : il continue de capter les
                #    vrais changements locaux sur les mappings déjà prêts (marqueurs
                #    conservés, traités dès le retour du consommateur).
                self._append_output(_("⏸ Stopping the consumer (watcher kept running)…") + "\n")
                realtime_manager.stop_consumer()
                self._append_output(_("⏸ Pausing the scheduled timer…") + "\n")
                if _HAS_SCHEDULE:
                    try:
                        schedule_manager.pause_timer()
                    except Exception:
                        pass

            # 3) Exécuter le moteur (commande déjà construite par l'appelant).
            os.makedirs(DEFAULT_LOG_DIR, exist_ok=True)
            log_path = self._log_path()
            self._current_log_path = log_path
            env = dict(os.environ)
            env["PROTON_DRIVE_CLI"] = DEFAULT_CLI

            # 3a) ATTENTE PATIENTE DU VERROU (au lieu d'échouer en code 1). Le
            #     consommateur vient d'être arrêté, mais le watcher NAS ou une passe
            #     planifiée peut encore tenir le flock ~/.proton_sync.lock ; et le
            #     consommateur peut mettre un instant à le relâcher. Plutôt que de
            #     laisser le moteur sortir immédiatement (« Une autre instance… »,
            #     code 1), on sonde le verrou (--check-lock, non destructif) et on
            #     réessaie toutes les ~3 s. Le bouton Arrêter pose _stop_requested
            #     et casse la boucle proprement. Le verrou lui-même n'est pas touché.
            self._stop_requested = False
            waited = False
            while self._lock_is_busy(env):
                if getattr(self, "_stop_requested", False):
                    self._append_output(_("⏹ Cancelled while waiting for the lock.") + "\n")
                    self.after(0, lambda: self.status.set(_("Priming cancelled.")))
                    return
                if not waited:
                    self._append_output(_("⏳ Waiting for the lock to be released "
                        "(another pass is running)… (Stop to cancel)") + "\n")
                    waited = True
                time.sleep(3)
            if waited:
                self._append_output(_("🔓 Lock released — starting.") + "\n")

            self._append_output(started_txt + "\n\n")
            self._last_shown_folder = None
            self._folders_shown = 0
            self._auth_failed_seen = False

            with open(log_path, "w", encoding="utf-8") as logf:
                self.sync_process = subprocess.Popen(
                    cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1)
                for line in self.sync_process.stdout:
                    if "[account-changed]" in line and "→" not in line:
                        self.after(0, lambda: self.status.set(
                            _("Proton account changed — prime the cache (or "
                              "reset the mappings) to rebuild on the new "
                              "account.")))
                    if "[auth-failed]" in line:
                        self._auth_failed_seen = True
                    self._feed_output(line)          # affichage (brut ou épuré)
                    logf.write(line); logf.flush()   # log disque : toujours complet
                    # Extraire un chemin pour la barre de progression (dossier courant).
                    self._prime_current = self._extract_path(line) or self._prime_current
                self.sync_process.wait()
            code = self.sync_process.returncode
            if is_reset:
                self._append_output("\n=== " + _("Reset finished (code {c})").format(c=code) + " ===\n")
            else:
                self._append_output("\n=== " + _("Priming finished (code {c})").format(c=code) + " ===\n")
            if getattr(self, "_auth_failed_seen", False):
                self.after(0, self._mark_auth_disconnected)
            else:
                self.after(0, self._mark_auth_connected)

            # 4) Redémarrer le consommateur et le timer.
            if _HAS_REALTIME:
                self._append_output(_("▶ Restarting the consumer…") + "\n")
                realtime_manager.start_consumer()
                if _HAS_SCHEDULE:
                    try:
                        schedule_manager.resume_timer()
                        self._append_output(_("▶ Scheduled timer restored.") + "\n")
                    except Exception:
                        pass
                ready, total = realtime_manager.mappings_ready_count(self.config_path)
                self._append_output(_("✓ {r}/{t} mapping(s) now ready for real-time.").format(r=ready, t=total) + "\n\n")
                if is_reset:
                    self.after(0, lambda: self.status.set(
                        _("Reset done — {r}/{t} mapping(s) ready for real-time.").format(r=ready, t=total)))
                else:
                    self.after(0, lambda: self.status.set(
                        _("Priming done — {r}/{t} mapping(s) ready for real-time.").format(r=ready, t=total)))
            else:
                if is_reset:
                    self.after(0, lambda: self.status.set(_("Reset finished (code {c}).").format(c=code)))
                else:
                    self.after(0, lambda: self.status.set(_("Priming finished (code {c}).").format(c=code)))
        except Exception as e:
            if is_reset:
                self._append_output("\n=== " + _("Reset error: {e}").format(e=e) + " ===\n")
                self.after(0, lambda: self.status.set(_("Reset error: {e}").format(e=e)))
            else:
                self._append_output("\n=== " + _("Priming error: {e}").format(e=e) + " ===\n")
                self.after(0, lambda: self.status.set(_("Priming error: {e}").format(e=e)))
        finally:
            self.sync_process = None
            self._prime_running = False
            self.after(0, lambda: self.prime_button.configure(state="normal"))
            self.after(0, lambda: self.reset_button.configure(state="normal"))
            self.after(0, lambda: self.run_button.configure(state="normal"))
            self.after(0, lambda: self.stop_button.configure(state="disabled"))
            # Rafraîchir la colonne « Prêt » IMMÉDIATEMENT à la fin du passage.
            # Sans ça, _prime_tick (1,5 s) vient de s'arrêter et seul le tick lent
            # (30 s) finirait par mettre à jour l'affichage -> le crochet ✅
            # apparaissait avec un délai « aléatoire ». On invalide d'abord le
            # cache mémoïsé (par mtime) pour lire la version fraîche que le moteur
            # vient d'écrire, puis on redessine la colonne.
            def _final_state_refresh():
                self._cache_memo = None
                self._refresh_state_column()
            self.after(0, _final_state_refresh)
            # Filet : la toute dernière écriture atomique du cache par le moteur
            # peut arriver un instant après ce finally. Un second rafraîchissement
            # ~1,2 s plus tard rattrape ce décalage sans attendre le tick lent.
            self.after(1200, _final_state_refresh)

    @staticmethod
    def _extract_path(line):
        """Meilleur effort : renvoie le nom du dossier mentionné dans une ligne de
        sortie (pour l'afficher comme « dossier en cours »). Extraction par POSITION
        (après le séparateur « : » ou « => »), car un chemin peut contenir des
        ESPACES (ex. « Personne - Alex ») — un découpage par espaces le
        tronquerait. None si aucun chemin absolu."""
        s = line.rstrip("\n")
        # Prendre ce qui suit le dernier « => » ou le premier « : », puis isoler le
        # segment commençant par « / ».
        for sep in (" => ", "=> "):
            if sep in s:
                s = s.split(sep, 1)[1]
                break
        else:
            if ": " in s:
                s = s.split(": ", 1)[1]
        s = s.strip()
        idx = s.find("/")
        if idx == -1:
            return None
        path = s[idx:].strip()
        # Si la ligne mentionne une action après le chemin (rare), on garde tel
        # quel : l'immense majorité des lignes finissent par le chemin.
        if "." in os.path.basename(path):   # ressemble à un fichier -> dossier parent
            path = os.path.dirname(path)
        return os.path.basename(path) if path else None


# ============================================================
#  Fenêtre de gestion de la planification systemd
# ============================================================
class ScheduleDialog(tk.Toplevel):
    """Fenêtre de gestion complète de la planification (timer systemd --user).

    Affiche l'état courant et permet : installer/mettre à jour, changer l'heure,
    basculer Option A/B (--delete), activer/désactiver le timer, lancer un
    passage immédiat. Le linger (qui exige sudo) est affiché mais non modifié.
    """

    # Fréquences proposées + jours de la semaine (libellé FR -> jeton systemd).
    FREQ_LABELS = [_("Daily"), _("Weekly"), _("Hourly")]
    DOW = [(_("Monday"), "Mon"), (_("Tuesday"), "Tue"), (_("Wednesday"), "Wed"),
           (_("Thursday"), "Thu"), (_("Friday"), "Fri"), (_("Saturday"), "Sat"),
           (_("Sunday"), "Sun")]

    def __init__(self, parent, mappings_path):
        super().__init__(parent)
        self.parent = parent
        self.mappings_path = mappings_path
        self.title(_("Sync schedule"))
        self.geometry("660x680")
        self.minsize(620, 640)
        self.transient(parent)
        self.grab_set()

        self._build()
        self._refresh()

    def _build(self):
        # Bouton Fermer réservé EN PREMIER (côté bas) pour qu'il ne soit jamais
        # tronqué, même si le contenu au-dessus déborde.
        ttk.Button(self, text=_("Close"), command=self.destroy).pack(side="bottom", pady=8)

        # Zone d'état (haut)
        state_frame = ttk.LabelFrame(self, text=_("Current state"), padding=10)
        state_frame.pack(side="top", fill="x", padx=10, pady=(10, 6))
        self.state_text = tk.StringVar(value=_("Reading…"))
        ttk.Label(state_frame, textvariable=self.state_text, justify="left",
                  font=("monospace", 9)).pack(anchor="w")

        # Zone réglages
        cfg = ttk.LabelFrame(self, text=_("Settings"), padding=10)
        cfg.pack(side="top", fill="x", padx=10, pady=6)

        # Fichier de mappings visé (info)
        ttk.Label(cfg, text=_("Scheduled mappings file:"),
                  font=("", 9, "bold")).pack(anchor="w")
        ttk.Label(cfg, text=self.mappings_path, foreground="#445",
                  font=("monospace", 9), wraplength=580).pack(anchor="w", pady=(0, 8))

        # Fréquence + jour (si hebdo) + heure
        frow = ttk.Frame(cfg)
        frow.pack(anchor="w", fill="x", pady=2)
        ttk.Label(frow, text=_("Frequency: ")).pack(side="left")
        self.freq_var = tk.StringVar(value=_("Daily"))
        self.freq_combo = ttk.Combobox(frow, textvariable=self.freq_var,
                                       values=self.FREQ_LABELS,
                                       state="readonly", width=16)
        self.freq_combo.pack(side="left")
        self.freq_combo.bind("<<ComboboxSelected>>", lambda e: self._update_freq_state())

        ttk.Label(frow, text=_("   Day: ")).pack(side="left")
        self.day_var = tk.StringVar(value="Dimanche")
        self.day_combo = ttk.Combobox(frow, textvariable=self.day_var,
                                       values=[lab for lab, _tok in self.DOW],
                                       state="disabled", width=11)
        self.day_combo.pack(side="left")

        ttk.Label(frow, text=_("   Time: ")).pack(side="left")
        self.hour_var = tk.StringVar(value="03:00")
        self.hour_combo = ttk.Combobox(frow, textvariable=self.hour_var,
                                       values=[f"{h:02d}:00" for h in range(24)],
                                       state="readonly", width=7)
        self.hour_combo.pack(side="left")

        # Option suppression (A/B)
        self.delete_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            cfg,
            text=_("Option B — propagate deletions in the schedule (--delete)"),
            variable=self.delete_var).pack(anchor="w", pady=(8, 0))
        ttk.Label(
            cfg,
            text=_("Unchecked = Option A (additive, recommended). Checked = mirror: "
                 "local deletions propagate automatically at night."),
            foreground="#666", wraplength=580, justify="left").pack(anchor="w", pady=(0, 4))

        # Boutons d'action
        actions = ttk.LabelFrame(self, text=_("Actions"), padding=10)
        actions.pack(side="top", fill="x", padx=10, pady=6)
        row1 = ttk.Frame(actions); row1.pack(anchor="w", fill="x", pady=2)
        ttk.Button(row1, text=_("💾 Install / Update"),
                   command=self.on_apply).pack(side="left", padx=2)
        self.enable_btn = ttk.Button(row1, text=_("▶ Enable the timer"),
                                     command=self.on_enable)
        self.enable_btn.pack(side="left", padx=2)
        self.disable_btn = ttk.Button(row1, text=_("⏸ Disable the timer"),
                                      command=self.on_disable)
        self.disable_btn.pack(side="left", padx=2)
        row2 = ttk.Frame(actions); row2.pack(anchor="w", fill="x", pady=2)
        ttk.Button(row2, text=_("🧪 Run a pass now"),
                   command=self.on_run_now).pack(side="left", padx=2)
        ttk.Button(row2, text=_("🔄 Refresh state"),
                   command=self._refresh).pack(side="left", padx=2)
        ttk.Button(row2, text=_("📜 Run history…"),
                   command=self.on_show_journal).pack(side="left", padx=2)

        # Zone linger (info + rappel commande copiable)
        self.linger_frame = ttk.LabelFrame(self, text=_("Linger (persistence)"), padding=10)
        self.linger_frame.pack(side="top", fill="x", padx=10, pady=6)
        self.linger_text = tk.StringVar(value="")
        ttk.Label(self.linger_frame, textvariable=self.linger_text, justify="left",
                  wraplength=580).pack(anchor="w")

        # Ligne commande copiable (visible seulement si linger inactif).
        self.linger_cmd_row = ttk.Frame(self.linger_frame)
        # (pack/forget géré dynamiquement dans _refresh)
        self.linger_cmd_var = tk.StringVar(value="")
        # Entry en lecture seule mais sélectionnable (donc copiable à la souris).
        self.linger_cmd_entry = ttk.Entry(self.linger_cmd_row,
                                          textvariable=self.linger_cmd_var,
                                          font=("monospace", 9), state="readonly")
        self.linger_cmd_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(self.linger_cmd_row, text=_("📋 Copy"),
                   command=self._copy_linger_cmd).pack(side="left", padx=(6, 0))

    def _copy_linger_cmd(self):
        cmd = self.linger_cmd_var.get()
        if cmd:
            self.clipboard_clear()
            self.clipboard_append(cmd)

    def _refresh(self):
        st = schedule_manager.status()
        lines = []
        lines.append(_("Service installed : {v}").format(v=_("yes") if st['service_exists'] else _("no")))
        lines.append(_("Timer installed   : {v}").format(v=_("yes") if st['timer_exists'] else _("no")))
        lines.append(_("Timer active      : {v}").format(v=_("yes") if st['timer_active'] else _("no")))
        if st["calendar"]:
            lines.append(_("Scheduled         : {v}").format(v=self._humanize_calendar(st['calendar'])))
        if st["mappings_path"]:
            lines.append(_("Scheduled mappings: {v}").format(v=st['mappings_path']))
        opt = _("B (deletions active)") if st["delete"] else _("A (additive)")
        lines.append(_("Mode              : Option {v}").format(v=opt))
        if st["next_run"]:
            lines.append(_("Next run          : {v}").format(v=st['next_run']))
        self.state_text.set("\n".join(lines))

        # Synchroniser les contrôles avec l'état lu
        self.delete_var.set(bool(st["delete"]))
        if st["calendar"]:
            self._set_controls_from_calendar(st["calendar"])
        else:
            self._update_freq_state()

        # Boutons activer/désactiver selon l'état
        if st["timer_active"]:
            self.enable_btn.config(state="disabled")
            self.disable_btn.config(state="normal")
        else:
            self.enable_btn.config(state="normal" if st["timer_exists"] else "disabled")
            self.disable_btn.config(state="disabled")

        # Linger
        if st["linger"]:
            self.linger_text.set(_("✅ Linger active — the schedule survives "
                                 "logout and reboots."))
            # Cacher la ligne commande (inutile quand le linger est actif).
            self.linger_cmd_row.pack_forget()
            self.linger_cmd_var.set("")
        else:
            self.linger_text.set(
                _("⚠ Linger inactive. Without it, the schedule only runs while "
                "a session is open. To enable it (admin rights required, once), "
                "copy and run this command in a terminal:"))
            # Afficher la commande dans un champ copiable + bouton Copier.
            self.linger_cmd_var.set(schedule_manager.linger_command())
            self.linger_cmd_row.pack(side="top", fill="x", pady=(6, 0))

    # ---------- Fréquence : construction / lecture du OnCalendar ----------
    def _update_freq_state(self):
        """Active le champ Jour uniquement en hebdomadaire ; grise l'Heure quand
        « Toutes les heures » (sans objet)."""
        freq = self.freq_var.get()
        self.day_combo.config(state="readonly" if freq == _("Weekly") else "disabled")
        self.hour_combo.config(state="disabled" if freq == _("Hourly") else "readonly")

    def _dow_token(self, label):
        for lab, tok in self.DOW:
            if lab == label:
                return tok
        return "Sun"

    def _dow_label(self, token):
        for lab, tok in self.DOW:
            if tok == token:
                return lab
        return "Dimanche"

    def _build_calendar(self):
        """Construit la valeur OnCalendar à partir des trois contrôles."""
        freq = self.freq_var.get()
        if freq == _("Hourly"):
            return "*-*-* *:00:00"
        hh = self.hour_var.get().split(":")[0]
        if freq == _("Weekly"):
            return f"{self._dow_token(self.day_var.get())} *-*-* {hh}:00:00"
        return f"*-*-* {hh}:00:00"  # Quotidien

    def _set_controls_from_calendar(self, cal):
        """Positionne fréquence/jour/heure d'après un OnCalendar lu."""
        import re
        if re.fullmatch(r"\*-\*-\* \*:00:00", cal):
            self.freq_var.set(_("Hourly"))
        else:
            m = re.fullmatch(r"([A-Za-z]{3}) \*-\*-\* (\d{2}):00:00", cal)
            if m:
                self.freq_var.set(_("Weekly"))
                self.day_var.set(self._dow_label(m.group(1)))
                self.hour_var.set(f"{m.group(2)}:00")
            else:
                m = re.fullmatch(r"\*-\*-\* (\d{2}):00:00", cal)
                if m:
                    self.freq_var.set(_("Daily"))
                    self.hour_var.set(f"{m.group(1)}:00")
                # sinon : format non reconnu — on laisse les contrôles tels quels
        self._update_freq_state()

    def _humanize_calendar(self, cal):
        """Rend un OnCalendar lisible pour la zone d'état."""
        import re
        if not cal:
            return None
        if re.fullmatch(r"\*-\*-\* \*:00:00", cal):
            return _("Hourly")
        m = re.fullmatch(r"([A-Za-z]{3}) \*-\*-\* (\d{2}):00:00", cal)
        if m:
            return _("Weekly — {d} at {h}:00").format(d=self._dow_label(m.group(1)), h=m.group(2))
        m = re.fullmatch(r"\*-\*-\* (\d{2}):00:00", cal)
        if m:
            return _("Daily at {h}:00").format(h=m.group(1))
        return cal

    def on_apply(self):
        cal = self._build_calendar()
        delete = self.delete_var.get()
        # Confirmation renforcée si on active l'Option B
        if delete:
            if not dlg_confirm(
                self,
                _("You are enabling Option B: the scheduled sync will "
                "AUTOMATICALLY propagate local deletions to Proton (according "
                "to each mapping's settings), without intervention.\n\n"
                "Safety nets: a several-hour window before execution, and the "
                "Proton trash for 30 days (mappings in trash mode).\n\n"
                "Make sure you have tested --delete manually first. "
                "Continue?"),
                title=_("Enable automatic deletions?"), kind="warning",
                ok_text=_("Enable Option B"), cancel_text=_("Cancel")):
                return
        ok, msg = schedule_manager.install_or_update(
            self.mappings_path, on_calendar=cal, delete=delete, enable=True)
        if ok:
            dlg_info(self, msg, title=_("Schedule"))
        else:
            dlg_error(self, msg, title=_("Schedule"))
        self._refresh()

    def on_enable(self):
        ok, msg = schedule_manager.enable_timer()
        (dlg_success if ok else dlg_error)(self, msg, title="Timer")
        self._refresh()

    def on_disable(self):
        ok, msg = schedule_manager.disable_timer()
        (dlg_success if ok else dlg_error)(self, msg, title="Timer")
        self._refresh()

    def on_run_now(self):
        ok, msg = schedule_manager.run_now()
        (dlg_info if ok else dlg_error)(self, msg, title=_("Manual run"))
        self._refresh()

    def on_show_journal(self):
        # Sous-fenêtre modale par-dessus la Planification : on attend sa fermeture
        # puis on rétablit le grab de ScheduleDialog (qui l'avait pris à l'ouverture).
        dlg = PlanificationJournalDialog(self)
        self.wait_window(dlg)
        try:
            self.grab_set()
        except Exception:
            pass


# ============================================================
#  Fenêtre du journal de la planification (passages nocturnes)
# ============================================================
class PlanificationJournalDialog(tk.Toplevel):
    """Consultation du journal des passages planifiés.

    Par défaut : la DERNIÈRE exécution, isolée par son InvocationID (pas un nombre
    de lignes arbitraire). Un sélecteur de date permet de remonter à un jour
    précis. Une ligne de résumé en tête donne le résultat du dernier passage
    (succès / échec + code), ce qui rend visible d'un coup d'œil une collision de
    verrou ou un échec qui, sinon, ne se voit que dans journalctl.

    Aucun fichier n'est créé : on lit le journal systemd (borné et auto-purgé)."""

    def __init__(self, parent):
        super().__init__(parent)
        self.parent = parent
        self.title(_("Schedule run history"))
        self.geometry("900x620")
        self.minsize(700, 480)
        self.transient(parent)
        self.grab_set()
        self._build()
        self._load_last_run()  # vue par défaut : dernière exécution

    def _build(self):
        ttk.Button(self, text=_("Close"), command=self.destroy).pack(side="bottom", pady=8)

        # Résumé du dernier passage (haut)
        top = ttk.LabelFrame(self, text=_("Last run"), padding=10)
        top.pack(side="top", fill="x", padx=10, pady=(10, 6))
        self.summary_var = tk.StringVar(value=_("Reading…"))
        ttk.Label(top, textvariable=self.summary_var, justify="left",
                  font=("monospace", 9)).pack(anchor="w")

        # Barre d'actions
        bar = ttk.Frame(self)
        bar.pack(side="top", fill="x", padx=10, pady=(0, 6))
        ttk.Button(bar, text=_("⟲ Last run"),
                   command=self._load_last_run).pack(side="left", padx=2)
        ttk.Button(bar, text=_("📅 Pick a date…"),
                   command=self._pick_date).pack(side="left", padx=2)
        self.view_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.view_var,
                  foreground="#445").pack(side="left", padx=8)

        # Zone journal : Text + scrollbars verticale ET horizontale (wrap="none"
        # pour garder une ligne par entrée et lire les longs chemins).
        body = ttk.LabelFrame(self, text=_("Log"), padding=6)
        body.pack(side="top", fill="both", expand=True, padx=10, pady=(0, 6))
        inner = ttk.Frame(body)
        inner.pack(fill="both", expand=True)
        self.log = tk.Text(inner, wrap="none", font=("monospace", 9),
                           state="disabled")
        vsb = ttk.Scrollbar(inner, orient="vertical", command=self.log.yview)
        hsb = ttk.Scrollbar(inner, orient="horizontal", command=self.log.xview)
        self.log.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        inner.rowconfigure(0, weight=1)
        inner.columnconfigure(0, weight=1)

    def _set_log(self, text):
        if not self.winfo_exists():
            return
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.insert("end", text)
        self.log.see("1.0")
        self.log.configure(state="disabled")

    def _refresh_summary(self):
        s = schedule_manager.last_run_summary()
        if not s:
            self.summary_var.set(
                _("(state unavailable — the scheduled service may never have run)"))
            return
        when = s.get("when") or "—"
        self.summary_var.set(_("{when}\nResult: {label}").format(when=when, label=s.get("label", "—")))

    def _load_last_run(self):
        self._refresh_summary()
        self.view_var.set(_("View: last run"))
        self._set_log(_("Reading the journal…"))

        def work():
            _ok, text = schedule_manager.journal_last_run()
            self.after(0, lambda: self._set_log(text))

        threading.Thread(target=work, daemon=True).start()

    def _pick_date(self):
        date_str = None
        if _ZENITY:
            date_str = _zenity_run(["--calendar", "--date-format=%Y-%m-%d",
                                    "--title=" + _("Pick a date"),
                                    "--text=" + _("Log of the scheduled run for that day:")])
        else:
            date_str = simpledialog.askstring(
                _("Pick a date"), _("Date (YYYY-MM-DD):"), parent=self)
        if not date_str:
            return
        self.view_var.set(_("View: {d}").format(d=date_str))
        self._set_log(_("Reading the journal…"))

        def work():
            _ok, text = schedule_manager.journal_for_date(date_str)
            self.after(0, lambda: self._set_log(text))

        threading.Thread(target=work, daemon=True).start()


# ============================================================
#  Connexion à Proton (authentification par navigateur)
# ============================================================
class ProtonLoginDialog(tk.Toplevel):
    """Dialogue de connexion Proton. Lance `proton-drive auth login` (auth par
    NAVIGATEUR) et diffuse sa sortie. Aucun identifiant ne transite par ce
    logiciel : mot de passe et 2FA se saisissent dans le navigateur, côté Proton.
    On n'affiche que l'URL de secours (si le navigateur ne s'ouvre pas) et le
    message de succès."""

    def __init__(self, parent, on_done=None):
        super().__init__(parent)
        self.parent = parent
        self.on_done = on_done
        self._proc = None
        self._alive = True
        self._url = None
        self.title(_("Sign in to Proton"))
        self.geometry("640x360")
        self.minsize(520, 300)
        self.transient(parent)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._build()
        self._start_login()

    def _build(self):
        top = ttk.Frame(self, padding=10)
        top.pack(side="top", fill="x")
        ttk.Label(top, justify="left", wraplength=600, text=_(
            "A browser window will open for you to sign in to Proton "
            "(password and 2FA stay in the browser — never handled here).\n"
            "Keep this window open until it confirms success.")).pack(anchor="w")

        # URL de secours (copiable) si le navigateur ne s'ouvre pas seul.
        urow = ttk.Frame(self, padding=(10, 0))
        urow.pack(side="top", fill="x")
        self.url_var = tk.StringVar(value="")
        self.url_entry = ttk.Entry(urow, textvariable=self.url_var, state="readonly")
        self.url_entry.pack(side="left", fill="x", expand=True)
        self.copy_btn = ttk.Button(urow, text=_("📋 Copy URL"),
                                   command=self._copy_url, state="disabled")
        self.copy_btn.pack(side="left", padx=(6, 0))

        # Sortie du CLI.
        body = ttk.LabelFrame(self, text=_("Sign-in progress"), padding=6)
        body.pack(side="top", fill="both", expand=True, padx=10, pady=6)
        self.out = tk.Text(body, wrap="word", height=8, font=("monospace", 9),
                           state="disabled")
        self.out.pack(fill="both", expand=True)

        self.status_var = tk.StringVar(value=_("Starting sign-in…"))
        ttk.Label(self, textvariable=self.status_var,
                  font=("", 10, "bold")).pack(side="top", anchor="w", padx=10)

        self.close_btn = ttk.Button(self, text=_("Close"), command=self._close)
        self.close_btn.pack(side="bottom", pady=8)

    def _copy_url(self):
        if self._url:
            self.clipboard_clear()
            self.clipboard_append(self._url)

    def _append(self, text):
        if not self.winfo_exists():
            return
        self.out.configure(state="normal")
        self.out.insert("end", text)
        self.out.see("end")
        self.out.configure(state="disabled")
        # Repérer l'URL de secours pour la rendre copiable.
        if "http" in text and self._url is None:
            for tok in text.split():
                if tok.startswith("http"):
                    self._url = tok
                    self.url_var.set(tok)
                    self.copy_btn.config(state="normal")
                    break

    def _set_status(self, text):
        if self.winfo_exists():
            self.status_var.set(text)

    def _start_login(self):
        try:
            self._proc = subprocess.Popen(
                realtime_manager.auth_login_command(),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1)
        except FileNotFoundError:
            self._append(_("[proton-drive binary not found]\n"))
            self._set_status(_("❌ proton-drive not found — check its path."))
            return
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        proc = self._proc
        if proc is None:
            return
        try:
            for line in proc.stdout:
                if not self._alive:
                    break
                self.after(0, lambda l=line: self._append(l))
        except Exception:
            pass
        code = proc.wait()
        self.after(0, lambda c=code: self._finished(c))

    def _finished(self, code):
        if not self.winfo_exists():
            return
        if code == 0:
            self._set_status(_("✓ Connected to Proton."))
        else:
            self._set_status(_("❌ Sign-in failed (code {c}).").format(c=code))
        if self.on_done:
            try:
                self.on_done()
            except Exception:
                pass

    def _close(self):
        self._alive = False
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:
                pass
        if self.on_done:
            try:
                self.on_done()
            except Exception:
                pass
        self.destroy()


# ============================================================
#  Fenêtre de gestion du temps réel (démons systemd --user)
# ============================================================
class RealtimeDialog(tk.Toplevel):
    """Fenêtre de pilotage du temps réel pour le fichier de mappings actif.

    Cinq sections : (1) démons de la machine locale (installer/démarrer/arrêter),
    (2) délai de debounce/cycle, (3) push des mappings vers le NAS + indicateur de
    dérive, (4) observation du watcher NAS (lecture seule, pas de SSH), (5) nettoyage
    des files de marqueurs. Le watcher NAS est géré SUR le NAS (son propre systemd) ;
    ici on ne fait que l'observer via la file NFS.
    """

    # Couleurs reprises de la palette des dialogues stylisés.
    C_OK = "#1a9e57"
    C_WARN = "#e8a200"
    C_ERR = "#d2294b"
    C_MUTED = "#555577"

    def __init__(self, parent, mappings_path):
        super().__init__(parent)
        self.parent = parent
        self.mappings_path = mappings_path
        # Titre basé sur le fichier RÉELLEMENT surveillé par les services (celui
        # des unités installées), pas sur le fichier ouvert dans l'éditeur — la
        # fenêtre parle de la surveillance en cours. Nom de base seul (sans le
        # chemin). Fallback sur le fichier courant si aucun service n'est installé.
        watched = realtime_manager.read_units_mappings_path() or mappings_path
        self.title(_("Real-time — watching {f}").format(
            f=os.path.basename(watched)))
        # Taille relative à l'écran, centrée — s'adapte aux deux postes (Jean en
        # pleine résolution, Maryse en résolution réduite / texte agrandi) sans
        # qu'une taille fixe convienne mal à l'un ou à l'autre. Plafonnée pour ne
        # pas devenir démesurée sur un très grand moniteur.
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        w = min(int(sw * 0.86), 1500)
        h = min(int(sh * 0.92), 1100)
        x = max((sw - w) // 2, 0)
        y = max((sh - h) // 2 - 10, 0)
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.minsize(900, 540)
        self.transient(parent)
        self.grab_set()

        self._alive = True
        self._auto_after = None
        self._delays_seen = False  # spinboxes renseignées une seule fois (pas d'écrasement)
        self._tail_proc = None     # processus journalctl -f
        self._tail_thread = None
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build()
        self._refresh()           # premier affichage immédiat
        self._schedule_tick()     # rafraîchissement périodique non bloquant
        self._start_tail()        # suivi en direct du journal des démons

    # ---------- Construction ----------
    def _build(self):
        # Bouton Fermer réservé EN PREMIER (bas) pour ne jamais être tronqué.
        ttk.Button(self, text=_("Close"), command=self._on_close).pack(side="bottom", pady=8)

        # Deux colonnes ajustables (poignée déplaçable) :
        #   gauche  = formulaire (défilant verticalement, pour ne jamais déborder
        #             hors écran, utile quand le texte est agrandi),
        #   droite  = journal des événements, sur toute la hauteur, toujours visible.
        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(side="top", fill="both", expand=True, padx=8, pady=(8, 0))

        # --- Colonne gauche : formulaire défilant ---
        left = ttk.Frame(paned)
        # Largeur calée pour loger les boutons de la section 1 sans les comprimer,
        # mais sans excès : tout l'espace au-delà va au journal (élastique) grâce
        # au poids du PanedWindow. Position exacte du séparateur -> _init_sash.
        self._form_canvas = tk.Canvas(left, highlightthickness=0, width=520, height=560)
        yscroll = ttk.Scrollbar(left, orient="vertical", command=self._form_canvas.yview)
        self._form_canvas.configure(yscrollcommand=yscroll.set)
        yscroll.pack(side="right", fill="y")
        self._form_canvas.pack(side="left", fill="both", expand=True)
        form = ttk.Frame(self._form_canvas)
        self._form = form
        self._form_win = self._form_canvas.create_window((0, 0), window=form, anchor="nw")
        # La zone défilable suit la hauteur du formulaire ; la largeur interne est
        # calée sur celle du canvas pour que les sections remplissent la colonne.
        form.bind("<Configure>", lambda e: self._form_canvas.configure(
            scrollregion=self._form_canvas.bbox("all")))
        self._form_canvas.bind("<Configure>", lambda e: self._form_canvas.itemconfigure(
            self._form_win, width=e.width))

        WL = 440  # longueur d'enroulement des textes d'aide (colonne ~520 px)

        # État global.
        state_frame = ttk.LabelFrame(form, text=_("Current state"), padding=10)
        state_frame.pack(side="top", fill="x", padx=10, pady=(10, 6))
        self.state_text = tk.StringVar(value=_("Reading…"))
        ttk.Label(state_frame, textvariable=self.state_text, justify="left",
                  font=("monospace", 9)).pack(anchor="w")

        # Section 1 — Démons de la machine locale.
        d = ttk.LabelFrame(form, text=_("1) Local machine daemons (watcher + consumer)"),
                           padding=10)
        d.pack(side="top", fill="x", padx=10, pady=6)
        self.daemon_note = tk.StringVar(value="")
        self._daemon_note_label = ttk.Label(d, textvariable=self.daemon_note,
                                            foreground=self.C_WARN, wraplength=WL,
                                            justify="left")
        # Packé dynamiquement seulement s'il y a un avertissement (voir
        # _apply_status), pour ne pas réserver une ligne vide le reste du temps.
        drow1 = ttk.Frame(d); drow1.pack(anchor="w", fill="x", pady=2)
        self._daemon_first_row = drow1
        ttk.Button(drow1, text=_("💾 Install / Update"),
                   command=self.on_install).pack(side="left", padx=2)
        self.start_btn = ttk.Button(drow1, text=_("▶ Start"), command=self.on_start)
        self.start_btn.pack(side="left", padx=2)
        self.stop_btn = ttk.Button(drow1, text=_("⏹ Stop"), command=self.on_stop)
        self.stop_btn.pack(side="left", padx=2)
        drow2 = ttk.Frame(d); drow2.pack(anchor="w", fill="x", pady=2)
        ttk.Button(drow2, text=_("🔄 Restart"), command=self.on_restart).pack(side="left", padx=2)
        ttk.Button(drow2, text=_("🚫 Disable autostart"),
                   command=self.on_disable).pack(side="left", padx=2)
        ttk.Button(drow2, text=_("🔃 Refresh state"), command=self._refresh).pack(side="left", padx=2)

        # Section 2 — Délai (debounce / cycle).
        c = ttk.LabelFrame(form, text=_("2) Consumer delays"), padding=10)
        c.pack(side="top", fill="x", padx=10, pady=6)
        crow = ttk.Frame(c); crow.pack(anchor="w", fill="x", pady=2)
        ttk.Label(crow, text=_("Debounce: ")).pack(side="left")
        self.debounce_var = tk.IntVar(value=30)
        ttk.Spinbox(crow, from_=1, to=300, width=5,
                    textvariable=self.debounce_var).pack(side="left")
        ttk.Label(crow, text=_(" s     Cycle: ")).pack(side="left")
        self.cycle_var = tk.IntVar(value=30)
        ttk.Spinbox(crow, from_=1, to=120, width=5,
                    textvariable=self.cycle_var).pack(side="left")
        ttk.Label(crow, text=" s   ").pack(side="left")
        ttk.Button(crow, text=_("💾 Apply"), command=self.on_apply_delays).pack(side="left", padx=6)
        ttk.Label(c, text=_("The debounce groups bursts of writes before starting the engine; "
                          "the cycle is the polling rhythm of the queues. "
                          "Applied live, at the next cycle."),
                  foreground=self.C_MUTED, wraplength=WL, justify="left").pack(anchor="w", pady=(4, 0))

        # Mode local seul (réglage persistant, voir Configuration…) : les sections
        # NAS (3 et 4) n'ont pas leur place — les masquer plutôt que les griser,
        # avec une ligne d'explication à la place.
        nas_on = (appconfig.nas_enabled() if _HAS_CONFIG else True)

        # Section 3 — Mappings -> NAS + dérive.
        if nas_on:
            v = ttk.LabelFrame(form, text=_("3) Mappings push to the NAS"), padding=10)
            v.pack(side="top", fill="x", padx=10, pady=6)
            self.drift_var = tk.StringVar(value="")
            self.drift_label = ttk.Label(v, textvariable=self.drift_var, font=("", 10, "bold"))
            self.drift_label.pack(anchor="w", pady=(0, 4))
            self.pushed_var = tk.StringVar(value="")
            ttk.Label(v, textvariable=self.pushed_var, foreground=self.C_MUTED).pack(anchor="w")
            ttk.Button(v, text=_("⬆ Push mappings to the NAS"),
                       command=self.on_push).pack(anchor="w", pady=(6, 0))
            ttk.Label(v, text=_("The NAS watcher reads this copy to watch the NAS sources; "
                              "the push also happens automatically on every save."),
                      foreground=self.C_MUTED, wraplength=WL, justify="left").pack(anchor="w", pady=(4, 0))
        else:
            self.drift_var = tk.StringVar(value="")
            self.pushed_var = tk.StringVar(value="")
            # Widget réel mais JAMAIS affiché (pas de .pack) : _apply_status()
            # configure sa couleur sans condition — le créer ici évite un crash
            # en mode local seul plutôt que d'éparpiller des gardes dans
            # _apply_status pour un widget qui, de toute façon, ne sert à rien
            # tant que le NAS est désactivé.
            self.drift_label = ttk.Label(form, textvariable=self.drift_var)

        # Section 4 — Watcher NAS (observation).
        if nas_on:
            n = ttk.LabelFrame(form, text=_("4) NAS watcher (observation)"), padding=10)
            n.pack(side="top", fill="x", padx=10, pady=6)
            self.nas_var = tk.StringVar(value="")
            self.nas_label = ttk.Label(n, textvariable=self.nas_var, font=("", 10, "bold"))
            self.nas_label.pack(anchor="w")
            ttk.Label(n, text=_("The NAS watcher runs on the NAS (its own systemd) and stays "
                              "active on its own. The GUI does not control it: it observes its "
                              "activity through the NFS queue."),
                      foreground=self.C_MUTED, wraplength=WL, justify="left").pack(anchor="w", pady=(4, 0))
        else:
            self.nas_var = tk.StringVar(value="")
            # Même besoin que drift_label : _apply_status() configure sa couleur
            # sans condition -> widget réel mais jamais affiché.
            self.nas_label = ttk.Label(form, textvariable=self.nas_var)
            no_nas = ttk.LabelFrame(form, text=_("3-4) NAS features"), padding=10)
            no_nas.pack(side="top", fill="x", padx=10, pady=6)
            ttk.Label(no_nas, text=_("Local-only mode is active (see Configuration…): "
                              "no NAS mapping push, no NAS watcher to observe."),
                      foreground=self.C_MUTED, wraplength=WL, justify="left").pack(anchor="w")

        # Section 5 — Files de marqueurs.
        q = ttk.LabelFrame(form, text=_("5) Marker queues"), padding=10)
        q.pack(side="top", fill="x", padx=10, pady=6)
        self.queue_var = tk.StringVar(value="")
        ttk.Label(q, textvariable=self.queue_var, font=("monospace", 9)).pack(anchor="w")
        ttk.Button(q, text=_("🧹 Clear the queues"),
                   command=self.on_clean).pack(anchor="w", pady=(6, 0))
        ttk.Label(q, text=_("Clears the pending markers (local + NAS). Useful for a fresh "
                          "start; changes not yet processed will be forgotten."),
                  foreground=self.C_MUTED, wraplength=WL, justify="left").pack(anchor="w", pady=(4, 0))

        # Linger (lecture seule + rappel commande).
        lf = ttk.LabelFrame(form, text=_("Linger (persistence outside the session)"), padding=10)
        lf.pack(side="top", fill="x", padx=10, pady=6)
        self.linger_text = tk.StringVar(value="")
        ttk.Label(lf, textvariable=self.linger_text, justify="left",
                  wraplength=WL).pack(anchor="w")
        self.linger_cmd_row = ttk.Frame(lf)
        self.linger_cmd_var = tk.StringVar(value="")
        self.linger_cmd_entry = ttk.Entry(self.linger_cmd_row,
                                          textvariable=self.linger_cmd_var,
                                          font=("monospace", 9), state="readonly")
        self.linger_cmd_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(self.linger_cmd_row, text=_("📋 Copy"),
                   command=self._copy_linger_cmd).pack(side="left", padx=(6, 0))

        paned.add(left, weight=0)   # le formulaire garde sa largeur (ne s'étire pas)

        # --- Colonne droite : journal des événements, pleine hauteur ---
        ev = ttk.LabelFrame(paned, text=_("Real-time events (daemon logs)"), padding=6)
        ev_bar = ttk.Frame(ev)
        ev_bar.pack(side="top", fill="x", pady=(0, 4))
        self.autoscroll_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(ev_bar, text=_("Auto-scroll"),
                        variable=self.autoscroll_var).pack(side="left")
        ttk.Button(ev_bar, text=_("🧹 Clear"), command=self._clear_events).pack(side="right")
        # Text + scrollbars verticale ET horizontale : wrap="none" garde une ligne
        # par événement (alignement des horodatages), et la scrollbar horizontale
        # permet de lire les très longs chemins sans troncature.
        ev_body = ttk.Frame(ev)
        ev_body.pack(side="top", fill="both", expand=True)
        self.events = tk.Text(ev_body, width=46, wrap="none",
                              font=("monospace", 9), state="disabled")
        ev_vsb = ttk.Scrollbar(ev_body, orient="vertical", command=self.events.yview)
        ev_hsb = ttk.Scrollbar(ev_body, orient="horizontal", command=self.events.xview)
        self.events.configure(yscrollcommand=ev_vsb.set, xscrollcommand=ev_hsb.set)
        self.events.grid(row=0, column=0, sticky="nsew")
        ev_vsb.grid(row=0, column=1, sticky="ns")
        ev_hsb.grid(row=1, column=0, sticky="ew")
        ev_body.rowconfigure(0, weight=1)
        ev_body.columnconfigure(0, weight=1)
        paned.add(ev, weight=1)     # le journal absorbe tout l'espace disponible

        # Position initiale du séparateur : formulaire à largeur raisonnable
        # (≤ 400 px et ≤ 42 % de la fenêtre), le reste au journal.
        self._paned = paned
        self.after(80, self._init_sash)

        # Molette de souris pour le formulaire de gauche (Linux : Button-4/5).
        self._bind_wheel_to_form()

    def _init_sash(self):
        """Cale la position initiale du séparateur : formulaire à ~520 px (assez
        pour les boutons), le reste au journal, en garantissant au journal au
        moins 360 px. Sur un petit écran (Maryse) la fenêtre est plus étroite,
        donc le formulaire se réduit un peu mais le journal reste utilisable. On
        réessaie tant que le PanedWindow n'a pas encore sa taille réelle."""
        try:
            total = self._paned.winfo_width()
            if total <= 1:
                self.after(80, self._init_sash)
                return
            form_w = min(520, total - 360)
            self._paned.sashpos(0, form_w)
        except Exception:
            pass

    def _bind_wheel_to_form(self):
        """Active la molette sur toute la colonne gauche (canvas + descendants),
        sans gêner le défilement propre de la zone d'événements (à droite)."""
        def _wheel(event):
            if getattr(event, "num", 0) == 4:
                self._form_canvas.yview_scroll(-1, "units")
            elif getattr(event, "num", 0) == 5:
                self._form_canvas.yview_scroll(1, "units")
            elif getattr(event, "delta", 0):
                self._form_canvas.yview_scroll(int(-event.delta / 120), "units")
            return "break"

        def _bind(w):
            w.bind("<Button-4>", _wheel)
            w.bind("<Button-5>", _wheel)
            w.bind("<MouseWheel>", _wheel)
            for child in w.winfo_children():
                _bind(child)

        _bind(self._form_canvas)
        _bind(self._form)

    def _copy_linger_cmd(self):
        cmd = self.linger_cmd_var.get()
        if cmd:
            self.clipboard_clear()
            self.clipboard_append(cmd)

    # ---------- Événements temps réel (suivi du journal) ----------
    def _start_tail(self):
        """Lance « journalctl --user -f » sur les deux démons et déverse les
        lignes dans la zone d'événements, via un thread (sans figer l'UI)."""
        try:
            self._tail_proc = subprocess.Popen(
                realtime_manager.journal_follow_command(),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1)
        except FileNotFoundError:
            self._append_event("[journalctl introuvable — suivi indisponible]\n")
            self._tail_proc = None
            return
        self._tail_thread = threading.Thread(target=self._tail_reader, daemon=True)
        self._tail_thread.start()

    def _tail_reader(self):
        proc = self._tail_proc
        if proc is None:
            return
        try:
            for line in proc.stdout:
                if not self._alive:
                    break
                self.after(0, lambda l=line: self._append_event(l))
        except Exception:
            pass

    def _append_event(self, text):
        if not self._alive or not self.winfo_exists():
            return
        self.events.configure(state="normal")
        self.events.insert("end", text)
        if self.autoscroll_var.get():
            self.events.see("end")
        self.events.configure(state="disabled")

    def _clear_events(self):
        self.events.configure(state="normal")
        self.events.delete("1.0", "end")
        self.events.configure(state="disabled")

    # ---------- Rafraîchissement ----------
    def _refresh(self):
        """Relevé synchrone immédiat (utilisé après une action de l'utilisateur)."""
        self._apply_status(realtime_manager.status(self.mappings_path))

    # ---------- Rafraîchissement automatique (non bloquant) ----------
    REFRESH_MS = 3000  # cadence du rafraîchissement périodique

    def _schedule_tick(self):
        if not self._alive:
            return
        self._auto_after = self.after(self.REFRESH_MS, self._tick)

    def _tick(self):
        if not self._alive:
            return
        # Le relevé (systemctl + NFS) est fait dans un thread de fond pour ne
        # jamais figer l'interface, puis appliqué sur le thread Tk via after(0).
        def work():
            try:
                st = realtime_manager.status(self.mappings_path)
            except Exception:
                st = None
            if self._alive:
                self.after(0, lambda: self._after_tick(st))
        threading.Thread(target=work, daemon=True).start()

    def _after_tick(self, st):
        if not self._alive or not self.winfo_exists():
            return
        if st is not None:
            self._apply_status(st)
        self._schedule_tick()  # reprogrammer le prochain cycle (pas d'empilement)

    def _on_close(self):
        # Stoppe le minuteur AVANT de détruire la fenêtre, pour ne pas exécuter
        # un rafraîchissement sur des widgets détruits.
        self._alive = False
        if self._auto_after is not None:
            try:
                self.after_cancel(self._auto_after)
            except Exception:
                pass
            self._auto_after = None
        # Couper le suivi du journal (le thread lecteur sortira sur EOF).
        if self._tail_proc is not None:
            try:
                self._tail_proc.terminate()
            except Exception:
                pass
            self._tail_proc = None
        self.destroy()

    def _apply_status(self, st):
        # Bloc d'état.
        lines = []
        if not st["scripts_present"]:
            lines.append(_("⚠ Daemon scripts not found in ~/Logiciels/Proton-drive/"))
        lines.append(_("Services installed : {v}").format(v=_("yes") if st['units_exist'] else _("no")))
        # Fichier de mappings GRAVÉ dans les unités (celui que les démons
        # suivent réellement) — affiché en permanence, comme en planification.
        ump = st["units_mappings_path"]
        lines.append(_("Services' file     : {v}").format(
            v=os.path.basename(ump) if ump else "—"))
        lines.append(_("Local watcher      : {v}").format(v=_("active") if st['watch_active'] else _("stopped")))
        lines.append(_("Consumer           : {v}").format(v=_("active") if st['consume_active'] else _("stopped")))
        lines.append(_("Debounce / cycle   : {d} s / {c} s").format(d=st['debounce_seconds'], c=st['cycle_seconds']))
        opt = _("active") if st["linger"] else _("inactive")
        lines.append(_("Linger             : {v}").format(v=opt))
        self.state_text.set("\n".join(lines))

        # Note si les services visent un AUTRE fichier de mappings que l'actif.
        # Ton INFORMATIF (pas alarmant) : c'est une situation normale quand on
        # édite un second fichier (ex. après un transfert de mapping) — les
        # services continuent de fonctionner sur leur fichier ; il suffit de
        # cliquer « Installer / Mettre à jour » pour les faire basculer.
        if st["units_exist"] and ump and os.path.normpath(ump) != os.path.normpath(self.mappings_path):
            self.daemon_note.set(
                _("ℹ The background services currently watch this file: {f} "
                "(not the one open in the editor).").format(f=os.path.basename(ump)))
            self._daemon_note_label.pack(anchor="w", pady=(0, 6),
                                         before=self._daemon_first_row)
        else:
            self.daemon_note.set("")
            self._daemon_note_label.pack_forget()

        # Boutons démarrer/arrêter selon l'état.
        running = st["watch_active"] or st["consume_active"]
        self.start_btn.config(state="normal" if (st["units_exist"] and not running) else "disabled")
        self.stop_btn.config(state="normal" if running else "disabled")

        # Délais : renseigner les spinboxes UNE SEULE FOIS (au premier affichage).
        # Sinon un rafraîchissement automatique remettrait la valeur du disque
        # pendant que l'utilisateur modifie le champ.
        if not self._delays_seen:
            self.debounce_var.set(int(st["debounce_seconds"]))
            self.cycle_var.set(int(st["cycle_seconds"]))
            self._delays_seen = True

        # Dérive (section 3).
        drift = st["drift"]
        mapping = {
            "synced":          (self.C_OK,   _("🟢 Up to date on the NAS")),
            "local_newer":     (self.C_WARN, _("🟠 Local modified — push to the NAS")),
            "nas_missing":     (self.C_WARN, _("🟠 Never pushed to the NAS")),
            "nas_unreachable": (self.C_ERR,  _("🔴 NAS unreachable")),
        }
        color, label = mapping.get(drift["state"], (self.C_MUTED, drift["state"]))
        self.drift_var.set(label)
        self.drift_label.config(foreground=color)
        if drift.get("pushed_at"):
            self.pushed_var.set(_("Last push: {t}").format(t=drift["pushed_at"]))
        else:
            self.pushed_var.set("")

        # Observation NAS (section 4).
        nas = st["nas"]
        if not nas["reachable"]:
            self.nas_var.set(_("🔴 NAS unreachable (NFS mount missing)"))
            self.nas_label.config(foreground=self.C_ERR)
        else:
            if nas["last_activity"]:
                self.nas_var.set(_("🟢 NAS reachable — last activity {t} ago").format(t=nas["last_activity"]))
            else:
                self.nas_var.set(_("🟢 NAS reachable — no pending marker"))
            self.nas_label.config(foreground=self.C_OK)

        # Files (section 5).
        q = st["queues"]
        nas_part = f"{q['nas']}" if q["nas_reachable"] else _("— (NAS unreachable)")
        self.queue_var.set(_("Local: {n} marker(s)    NAS ({u}): {p}")
                           .format(n=q["local"], u=q["user"], p=nas_part))

        # Linger.
        if st["linger"]:
            self.linger_text.set(_("✅ Linger active — the daemons survive "
                                 "logout and reboots."))
            self.linger_cmd_row.pack_forget()
            self.linger_cmd_var.set("")
        else:
            self.linger_text.set(
                _("⚠ Linger inactive. Without it, the daemons only run while a "
                "session is open (they stop at logout). To keep them running "
                "continuously (admin rights, once), copy and run:"))
            self.linger_cmd_var.set(realtime_manager.linger_command())
            self.linger_cmd_row.pack(side="top", fill="x", pady=(6, 0))

    # ---------- Actions ----------
    def _warn_if_cold_cache(self):
        """Décision 3-B : avertit AVANT d'activer le temps réel SEULEMENT si aucun
        mapping n'est prêt (aucune racine subtree_complete). Un mapping n'est
        utilisable en temps réel que si tout son arbre a été analysé par un passage
        complet. Retourne True si on peut continuer, False si l'utilisateur préfère
        lancer un passage complet d'abord."""
        try:
            ready, total = realtime_manager.mappings_ready_count(self.mappings_path)
        except Exception:
            return True   # en cas de doute, ne bloque pas
        if total == 0 or ready > 0:
            return True   # au moins un mapping prêt (ou rien à juger) -> on continue
        return dlg_confirm(
            self,
            _("No mapping is ready for real-time yet: no full pass has analyzed the "
              "tree.\n\n"
              "Real-time only synchronizes folders whose entire tree has been "
              "analyzed; it never builds that analysis itself (it would freeze for "
              "hours on large folders). Until a full pass runs, real-time will hand "
              "every change over to the scheduled pass.\n\n"
              "Recommended: run a full pass first (⏰ Schedule, or the “Run sync” "
              "button) and let it finish, then enable real-time.\n\n"
              "Enable real-time anyway?"),
            title=_("No mapping ready for real-time"), kind="warning",
            ok_text=_("Enable anyway"), cancel_text=_("Cancel"))

    def on_install(self):
        if not self._warn_if_cold_cache():
            return
        # GARDE-FOU changement de FICHIER ACTIF : l'identité NAS est stable
        # (une seule queue), mais changer le fichier actif peut retirer des
        # dossiers de la surveillance — les billets en attente qui les visent
        # seront écartés (« ⊘ » au journal), rattrapables SEULEMENT en
        # rechargeant l'ancien fichier pour un passage manuel ou planifié.
        # TOUT changement demande donc confirmation, chiffres à l'appui
        # (des billets peuvent arriver à tout instant : pas d'exemption
        # « zéro billet »).
        old = realtime_manager.read_units_mappings_path()
        new = self.mappings_path
        if (old and new
                and os.path.realpath(old) != os.path.realpath(new)):
            try:
                total, uncovered = realtime_manager.pending_markers_report(new)
            except Exception:
                total, uncovered = 0, 0
            if total:
                counts = _("Pending markers: {n} — {m} of them target folders "
                           "NOT covered by the new file. Those will be "
                           "DISCARDED (“⊘” lines in the journal); they can "
                           "only be recovered by reloading the old file and "
                           "running a manual or scheduled pass."
                           ).format(n=total, m=uncovered)
            else:
                counts = _("Pending markers: none right now (new ones may "
                           "still arrive at any moment).")
            msg = _("The services currently use:\n  {old}\n"
                    "You are installing:\n  {new}\n\n{counts}").format(
                    old=os.path.basename(old), new=os.path.basename(new),
                    counts=counts)
            if not dlg_confirm(self, msg, title=_("Active mappings file change"),
                               ok_text=_("Confirm the change"),
                               cancel_text=_("Cancel (keep the current file)")):
                return
        ok, msg = realtime_manager.install_or_update_units(self.mappings_path, enable=True)
        (dlg_success if ok else dlg_error)(self, msg, title=_("Daemons"))
        self._refresh()

    def on_start(self):
        if not self._warn_if_cold_cache():
            return
        ok, msg = realtime_manager.start_daemons()
        (dlg_success if ok else dlg_error)(self, msg, title=_("Daemons"))
        self._refresh()

    def on_stop(self):
        ok, msg = realtime_manager.stop_daemons()
        (dlg_info if ok else dlg_error)(self, msg, title=_("Daemons"))
        self._refresh()

    def on_restart(self):
        ok, msg = realtime_manager.restart_daemons()
        (dlg_info if ok else dlg_error)(self, msg, title=_("Daemons"))
        self._refresh()

    def on_disable(self):
        if not dlg_confirm(
                self,
                _("Stop the daemons and disable their autostart?\n\n"
                "Real-time will be off until re-enabled. The scheduled nightly "
                "sync is not affected."),
                title=_("Disable real-time?"), kind="warning",
                ok_text=_("Disable"), cancel_text=_("Cancel")):
            return
        ok, msg = realtime_manager.disable_daemons()
        (dlg_info if ok else dlg_error)(self, msg, title=_("Daemons"))
        self._refresh()

    def on_apply_delays(self):
        ok, msg = realtime_manager.write_config(self.debounce_var.get(),
                                                self.cycle_var.get())
        (dlg_success if ok else dlg_error)(self, msg, title=_("Delays"))
        self._refresh()

    def on_push(self):
        # On pousse vers le NAS le fichier que les services surveillent RÉELLEMENT
        # (pas le fichier ouvert dans l'éditeur). Le watcher NAS lit cette copie ;
        # y pousser un autre fichier changerait ce qu'il surveille.
        units_path = realtime_manager.read_units_mappings_path()
        push_path = units_path or self.mappings_path
        # Garde-fou : si le fichier édité diffère du fichier des services, prévenir
        # clairement lequel sera poussé (évite de pousser le mauvais fichier par
        # mégarde).
        if (units_path and os.path.realpath(units_path)
                != os.path.realpath(self.mappings_path)):
            if not dlg_confirm(self, _("The background services watch {units} — that "
                "is the file that will be pushed to the NAS, not the one open in the "
                "editor ({open}). Push it?").format(
                units=os.path.basename(units_path),
                open=os.path.basename(self.mappings_path)),
                title=_("Push to the NAS"), kind="question",
                ok_text=_("Push"), cancel_text=_("Cancel")):
                return
        ok, msg = realtime_manager.push_mappings_to_nas(push_path)
        (dlg_success if ok else dlg_error)(self, msg, title=_("Push to the NAS"))
        self._refresh()

    def on_clean(self):
        q = realtime_manager.count_queues(self.mappings_path)
        nas_part = f"{q['nas']}" if q["nas_reachable"] else "NAS injoignable"
        if not dlg_confirm(
                self,
                _("Clear the pending markers?\n\n"
                "Local: {n}    NAS ({u}): {p}\n\n"
                "Changes not yet processed will be forgotten (the nightly "
                "sync remains the safety net).").format(n=q["local"], u=q["user"], p=nas_part),
                title=_("Clear the queues?"), kind="warning",
                ok_text=_("Clear"), cancel_text=_("Cancel")):
            return
        ok, msg = realtime_manager.clean_queues(self.mappings_path)
        (dlg_info if ok else dlg_error)(self, msg, title=_("Queues"))
        self._refresh()


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else None
    app = MappingEditor(config_path)
    app.mainloop()


if __name__ == "__main__":
    main()

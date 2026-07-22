#!/usr/bin/env python3
"""
Éditeur de mappings + lanceur de synchro pour Proton Drive (multi-comptes).

Deux rôles :
  1. Éditer la liste des paires source (local) / destination (Proton Drive)
     dans un fichier JSON, lu ensuite par le moteur (proton_sync.py).
  2. Lancer la synchro directement depuis l'interface (ou copier la commande
     équivalente pour la coller dans un terminal), avec les options voulues.

Usage :
    python3 proton_mapping_editor.py                # ouvre un sélecteur de fichier
    python3 proton_mapping_editor.py mappings-user1.json
"""
__version__ = "1.17.1"   # version propre à CE fichier ; incrémentée quand il change (indépendant de GitHub)

import json
import os
import queue
import sys
import socket
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
    "info":     {"accent": "#6d4aff", "glyph": "i",  "title": _("Information")},
    "question": {"accent": "#6d4aff", "glyph": "?",  "title": _("Confirmation")},
    "warning":  {"accent": "#e8a200", "glyph": "!",  "title": _("Warning")},
    "error":    {"accent": "#d2294b", "glyph": "✕",  "title": _("Error")},
    "success":  {"accent": "#1a9e57", "glyph": "✓",  "title": _("Success")},
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
                 checkbox_text=None, checkbox_default=False,
                 command=None):
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

        # Bloc COMMANDE COPIABLE (optionnel) : champ sélectionnable en lecture
        # seule (police monospace) + bouton « Copier » qui met la commande dans
        # le presse-papier. Évite à l'utilisateur de retaper une commande shell.
        if command:
            cmd_frame = tk.Frame(body, bg=DLG_BG)
            cmd_frame.pack(anchor="w", fill="x", pady=(12, 0))
            cmd_entry = tk.Entry(cmd_frame, font=("DejaVu Sans Mono", 9),
                                 relief="solid", bd=1, readonlybackground="#f4f4f8",
                                 fg=DLG_TEXT)
            cmd_entry.insert(0, command)
            cmd_entry.configure(state="readonly")
            cmd_entry.pack(side="left", fill="x", expand=True, ipady=3)

            def copy_cmd(_c=command, _e=cmd_entry):
                try:
                    self.clipboard_clear()
                    self.clipboard_append(_c)
                    copy_btn.configure(text=_("Copied ✓"))
                    # Le rétablissement différé peut arriver APRÈS la fermeture du
                    # dialogue (copie puis OK en < 1,5 s) : un after n'est pas
                    # annulé à la destruction du widget -> on garde contre TclError.
                    def _reset_copy():
                        try:
                            copy_btn.configure(text=_("Copy"))
                        except tk.TclError:
                            pass
                    self.after(1500, _reset_copy)
                except Exception:
                    pass
            copy_btn = tk.Button(cmd_frame, text=_("Copy"), command=copy_cmd,
                                 bg="#e8e8ef", fg=DLG_TEXT,
                                 activebackground="#dadae5", activeforeground=DLG_TEXT,
                                 font=("DejaVu Sans", 9), relief="flat",
                                 padx=12, pady=3, cursor="hand2", bd=0)
            copy_btn.pack(side="left", padx=(8, 0))

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
        # Largeur minimale : la bande d'accent (pictogramme + titre) est plus
        # large qu'un message court. Sans plancher, la fenêtre se dimensionne sur
        # le message et le titre déborde/se chevauche (constaté avec « Pas encore
        # testé. »). On garantit au moins de quoi afficher la bande entière.
        try:
            # Plancher plus large si une commande copiable est affichée (les
            # commandes shell sont longues et ne doivent pas être tronquées).
            floor = 560 if command else 380
            need_w = max(self.winfo_reqwidth(), floor)
            need_h = self.winfo_reqheight()
            self.minsize(need_w, need_h)
            if self.winfo_reqwidth() < floor:
                self.geometry(f"{need_w}x{need_h}")
        except Exception:
            pass
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


def dlg_info(parent, message, title=None, command=None):
    StyledDialog(parent, message, kind="info", title=title, command=command)


def dlg_error(parent, message, title=None, command=None):
    StyledDialog(parent, message, kind="error", title=title, command=command)


def dlg_warning(parent, message, title=None, command=None):
    StyledDialog(parent, message, kind="warning", title=title, command=command)


def dlg_success(parent, message, title=None, command=None):
    StyledDialog(parent, message, kind="success", title=title, command=command)


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
# (c.-à-d. ~/Logiciels/Proton-drive/icone.png). Chaque utilisateur
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
                     "--file-filter", _("All files") + " | *"]
        return _zenity_run(args)
    # Repli Tk
    filetypes = [("JSON", "*.json"), (_("All files"), "*.*")] if json_only \
        else [(_("All files"), "*.*")]
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
                "--file-filter", _("All files") + " | *"]
        path = _zenity_run(args)
        # S'assurer de l'extension .json si l'utilisateur ne l'a pas mise. B6 :
        # zenity a bien --confirm-overwrite, mais il confirme le nom SAISI. Si on
        # ajoute « .json » APRÈS coup, un « X.json » déjà présent pouvait être
        # écrasé sans confirmation quand l'utilisateur tapait « X ». On reconfirme
        # donc explicitement quand cet ajout d'extension vise un fichier existant.
        if path and not path.lower().endswith(".json"):
            path += ".json"
            if os.path.exists(path):
                if not dlg_confirm(
                        parent,
                        _("The file “{f}” already exists. Replace it?").format(
                            f=os.path.basename(path)),
                        title=_("Confirm"), kind="warning",
                        ok_text=_("Replace"), cancel_text=_("Cancel")):
                    return None
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
                "--file-filter", _("All files") + " | *"]
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
        "Detailed mode\n\n"
        "Shows the RAW output of the engine: every file examined (unchanged, "
        "uploaded, renamed…), cache skips (“cache valid”), and transfer "
        "summaries — instead of the condensed view (one line per folder as "
        "it is scanned, plus status/error lines only).\n\n"
        "This is a VIEW control: it applies to everything shown here — manual "
        "runs, priming and reset alike.\n\n"
        "Useful to see exactly what happens inside a folder (e.g. to check "
        "why a file failed), but the output is much longer. Changing it "
        "affects what is displayed from then on, not the lines already "
        "shown.\n\n"
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
    "nas-path-map": _(
        "NAS data-path correspondence\n\n"
        "Only needed if the NAS sees your DATA folders under different paths "
        "than this computer does — typically on a Synology/QNAP, where the NAS "
        "uses /volume1/… internally while this machine mounts the same folders "
        "under /media/nas1/… (or similar).\n\n"
        "Your mappings always use THIS computer's paths (left column). The "
        "watcher, running on the NAS, uses the right column to find those same "
        "folders on the NAS side. Add one row per volume.\n\n"
        "This is NOT the technical mount point above — it is the correspondence "
        "for your actual data folders. Leave the table EMPTY if the NAS and this "
        "computer use identical paths (the usual case for a Linux NAS mounted "
        "the same way). The table is pushed to the NAS with your configuration."
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
    "rename-ext-whitelist": _(
        "Which extensions to fix\n\n"
        "Fixing an extension only helps for files Proton can show a preview "
        "of — photos, videos, music, documents. Renaming a router backup or a "
        "phone settings file changes one of your files without repairing "
        "anything.\n\n"
        "It can even cause a pile-up: if another program (a phone backup app, "
        "for example) recreates the original name every night, a new renamed "
        "copy is added every night, on your disk and on your Drive.\n\n"
        "Separate the extensions with commas. Leave the field empty to fix "
        "every uppercase extension, as older versions did."
    ),
    "cli-stall-minutes": _(
        "Stop a frozen upload\n\n"
        "Proton's CLI can occasionally freeze at the very end of a large "
        "upload, waiting for an answer that never arrives. It would wait "
        "forever, and everything else stays queued behind it.\n\n"
        "After this many minutes without any activity at all, the app stops "
        "it and tries again later. Nothing is lost: the folder is simply "
        "picked up again on the next pass.\n\n"
        "A healthy upload has a quiet final stretch of about a minute and a "
        "half, so five minutes leaves a comfortable margin. Set 0 to never "
        "stop an upload."
    ),
    "cli-stall-max-kills": _(
        "Repeated freezes\n\n"
        "If the same folder freezes over and over, each attempt restarts the "
        "transfer from the beginning, which can waste a lot of bandwidth.\n\n"
        "This setting stops trying after that many freezes in a row and skips "
        "one pass before starting over. The folder is never abandoned — a "
        "backup that quietly stops backing up would be worse than the waste "
        "it avoids.\n\n"
        "Leave 0 to keep retrying every time."
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
    "launcher-desktop": _(
        "Desktop shortcut\n\n"
        "Creates a launcher icon on your desktop, in addition to (or instead "
        "of) the applications-menu entry.\n\n"
        "First launch from the desktop: some desktop environments "
        "(e.g. Cinnamon/Mint) mark a new desktop launcher as “untrusted” and "
        "ask you to allow it the first time — right-click the icon and choose "
        "“Allow Launching”. The app tries to mark it trusted for you, but if "
        "the prompt still appears, this one-time step clears it.\n\n"
        "The launcher opens the mappings editor (empty, or directly on the "
        "current mappings file if you chose that option)."
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

    # Racines PROPOSÉES comme destination (liste blanche, décision de conception) :
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
        def attempt():
            """Un essai de listage. Retourne (entries, err, ok).

            `ok` distingue « ce dossier est vide » de « je n'ai pas réussi à le
            lire » — les deux donnaient jusqu'ici une liste vide, et l'échec
            s'affichait donc comme « (aucun sous-dossier) », ce qui peut faire
            choisir une mauvaise destination.
            """
            if _ENGINE is None:
                raise RuntimeError("proton_sync.py missing next to the editor")
            with self.app._auth_lock:
                if path == "/":
                    entries = self._list_roots()
                    return entries, None, bool(entries)
                listing = _ENGINE.get_remote_listing(path)
                # Compat : un moteur antérieur renvoie un dict nu, sans « ok ».
                ok = getattr(listing, "ok", True)
                base = path.rstrip("/")
                entries = sorted(
                    ((base + "/" + n, n) for n, i in listing.items()
                     if (i.get("type") or "") == "folder"),
                    key=lambda t: str.casefold(t[1]))
                return entries, None, ok

        def work():
            # Un premier refus est souvent passager (session qui se réveille,
            # réponse réseau perdue). On retente UNE fois avant d'alarmer —
            # même principe que la sonde d'authentification.
            entries, err, ok = [], None, False
            for tentative in (1, 2):
                try:
                    entries, err, ok = attempt()
                except Exception as e:
                    entries, err, ok = [], str(e), False
                if ok and err is None:
                    break
                if tentative == 1:
                    time.sleep(1.5)
            def apply():
                if err is not None or not ok:
                    self.status_var.set(_("Could not list this folder — check the "
                                          "session (⚙ Configuration…) and try again. "
                                          "Expand it again to retry."))
                    return          # surtout : ne PAS mémoriser un échec
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
        ok = bool(item) and "\x01" not in item
        self.choose_btn.config(state="normal" if ok else "disabled")
        if ok:
            self.status_var.set(item)

    def _choose(self):
        item = self.tree.focus()
        if item and "\x01" not in item:
            self.dest_var.set(item)
            self.destroy()


def mapping_remote_path(dest_parent, source):
    """Chemin distant RÉELLEMENT écrit par un mapping : `dest_parent/<nom de la
    source>`, exactement comme le calcule le moteur (proton_sync.sync_mapping).

    Cette fonction existe pour les AVERTISSEMENTS. Le champ « Destination » de
    l'interface est un dossier PARENT : parler de « ce dossier de destination »
    dans un avertissement de suppression désigne donc le dossier de quelqu'un
    d'autre tout entier, alors que seul le sous-dossier créé par le mapping est
    concerné. Surestimer la portée finit par rendre l'avertissement inaudible.

    Retourne None si l'un des deux champs est encore vide (saisie en cours) :
    l'appelant affiche alors une formulation générique.
    """
    dest = (dest_parent or "").strip().rstrip("/")
    src = (source or "").strip().rstrip("/")
    if not dest or not src:
        return None
    return dest + "/" + os.path.basename(src)


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

        # Capacité du CLI (sait-il supprimer dans « Partagé avec moi » ?) et
        # contrôle de version : sondés en ARRIÈRE-PLAN. Sonder coûte plusieurs
        # secondes au tout premier appel (le binaire initialise le SDK avant de
        # répondre) ; le faire ici, synchroniquement, retardait d'autant
        # l'affichage de la fenêtre. None = pas encore connu.
        self._cli_shared_delete = None
        self._cli_probe_q = queue.Queue()
        self._start_cli_version_probe()

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

        # (Le contrôle de version du CLI et l'avis sur la normalisation des
        # extensions sont déclenchés par la sonde d'arrière-plan ci-dessus, une
        # fois le résultat disponible — la fenêtre est alors déjà affichée.)

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

    def _start_cli_version_probe(self):
        """Lance la sonde de version du CLI dans un FIL SÉPARÉ, puis scrute le
        résultat depuis le fil principal. Tk n'étant pas thread-safe (et `after()`
        n'étant appelable que depuis le fil principal), le fil dépose son résultat
        dans une file que le scrutateur relève — même motif que l'écouteur
        d'instance unique. La fenêtre s'affiche donc immédiatement ; l'avis de
        version arrive ensuite, sur une fenêtre déjà visible."""
        def work():
            try:
                status, version = _ENGINE.cli_version_status()
                shared = bool(_ENGINE.cli_supports_shared_delete())
            except Exception:
                # shared=None = INDÉTERMINÉ (et non False) : on ne mémorise jamais
                # un échec de sonde (règle du projet). Le poll ne figera donc pas
                # False ; la prochaine consultation re-sondera.
                status, version, shared = "unknown", None, None
            self._cli_probe_q.put((status, version, shared))
        if not _HAS_ENGINE:
            self._cli_shared_delete = False
            return
        threading.Thread(target=work, daemon=True).start()
        self.after(150, self._poll_cli_version_probe)

    def _poll_cli_version_probe(self):
        """Relève le résultat de la sonde (fil principal) et enchaîne les avis."""
        try:
            status, version, shared = self._cli_probe_q.get_nowait()
        except queue.Empty:
            self.after(150, self._poll_cli_version_probe)
            return
        # Ne mémoriser QUE si la sonde a abouti : None = échec transitoire
        # (trousseau momentanément verrouillé, réseau) -> laisser inconnu pour
        # re-sonder plus tard, jamais figer False pour toute la session.
        if shared is not None:
            self._cli_shared_delete = shared
        self._announce_cli_version(status, version)

    def _shared_delete_capability(self):
        """Le CLI sait-il supprimer dans « Partagé avec moi » ? Valeur connue si
        la sonde d'arrière-plan a abouti ; sinon sondée MAINTENANT (cas rare :
        ouvrir un dialogue de mapping dans la seconde qui suit le démarrage). Le
        coût est alors faible, le moteur mettant le résultat en cache sur disque.
        Repli conservateur (False = verrou maintenu) si indéterminable."""
        if self._cli_shared_delete is None:
            try:
                self._cli_shared_delete = bool(
                    _HAS_ENGINE and _ENGINE.cli_supports_shared_delete())
            except Exception:
                # Échec de sonde : NE PAS mémoriser (on reste None pour re-sonder
                # au prochain appel). On renvoie False pour CE geste seulement —
                # repli conservateur (verrou « ajout seul » maintenu) — sans figer
                # l'état de la session.
                return False
        return self._cli_shared_delete

    def _announce_cli_version(self, status, version):
        """Compare la version du CLI installé à celle sur laquelle le projet a été
        testé, et propose de QUITTER pour corriger la situation. Les comportements
        du CLI diffèrent d'une version à l'autre : seule la version documentée a
        été validée. On propose, on n'impose pas — « Continuer » reste possible.
        (Le MOTEUR, lui, tourne sans écran : il se contente d'une ligne
        d'avertissement et ne bloque jamais — voir proton_sync.)"""
        if not _HAS_ENGINE:
            self._maybe_disable_rename_ext()
            return
        try:
            tested = _ENGINE.CLI_TESTED_VERSION
        except Exception:
            self._maybe_disable_rename_ext()
            return
        if status == "ok":
            self._maybe_disable_rename_ext()
            return
        if status == "older":
            msg = _("The installed Proton CLI is {v}, but this application was "
                    "tested with {t}.\n\nAn older CLI lacks behaviours the "
                    "application relies on, so some features stay restricted.\n\n"
                    "You can quit to install {t}, or continue at your own risk."
                    ).format(v=version, t=tested)
        elif status == "newer":
            msg = _("The installed Proton CLI is {v}, which is newer than {t} — "
                    "the version this application was tested with.\n\nNewer "
                    "releases can change behaviour in ways that have not been "
                    "validated here.\n\nYou can quit to reinstall {t}, or "
                    "continue at your own risk.").format(v=version, t=tested)
        else:
            msg = _("The Proton CLI version could not be determined. This "
                    "application was tested with {t}.\n\nYou can quit to check "
                    "your installation, or continue at your own risk."
                    ).format(t=tested)
        if dlg_confirm(self, msg, title=_("Proton CLI version"), kind="warning",
                       ok_text=_("Quit"), cancel_text=_("Continue anyway")):
            self.destroy()
            return
        self._maybe_disable_rename_ext()

    def _maybe_disable_rename_ext(self):
        """Désactive UNE SEULE FOIS la normalisation des extensions, avec avis.

        À partir du CLI 0.5.0 le type de média est correct même avec une extension
        en majuscules : le contournement (qui RENOMME des fichiers locaux) n'est
        plus nécessaire. On le décoche donc d'office, on explique, et on mémorise
        que c'est fait. Si l'utilisateur le réactive — pour normaliser par
        discipline, ou pour réparer d'anciens téléversements mal typés — on n'y
        touche PLUS JAMAIS."""
        if not (_HAS_CONFIG and _HAS_ENGINE):
            return
        try:
            if appconfig.rename_ext_auto_disabled():
                return                      # déjà fait : le choix de l'utilisateur prime
            if not appconfig.rename_ext_enabled():
                appconfig.set_rename_ext_auto_disabled(True)   # déjà décoché
                return
            if not _ENGINE.cli_supports_shared_delete():
                return                      # CLI ancien : le contournement sert encore
            appconfig.set_rename_ext_enabled(False)
            appconfig.set_rename_ext_auto_disabled(True)
        except Exception:
            return
        dlg_info(self, _(
            "Automatic lowercasing of file extensions has been turned off.\n\n"
            "The Proton CLI now detects the media type correctly even when the "
            "extension is uppercase, so renaming your local files is no longer "
            "needed.\n\nYou can turn it back on in Configuration if you prefer "
            "your extensions normalised anyway, or to repair older uploads that "
            "were sent with an uppercase extension. Your choice will be kept."),
            title=_("File extensions"))

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
        run_frame = ttk.LabelFrame(self, text=_("Manual sync"), padding=8)
        run_frame.pack(side="bottom", fill="x", padx=8, pady=(4, 4))

        # Deux colonnes : ACTIONS à gauche (empilées), OPTIONS à droite (en
        # colonne, « ? » alignés en grille). Les contrôles de VUE de la sortie
        # (Erreurs seules, Effacer) NE SONT PLUS ici : ils pilotent la sortie
        # PARTAGÉE (synchro manuelle ET cache) et vivent désormais dans la zone
        # « Sortie de la synchro ». « Ignorer cache » reste retiré (couvert par
        # « Réinitialiser le mapping ») ; opt_ignore_cache subsiste (toujours False).
        run_frame.columnconfigure(1, weight=1)   # pousse la colonne d'options à droite

        # Colonne gauche : actions empilées, largeur uniforme.
        actions = ttk.Frame(run_frame)
        actions.grid(row=0, column=0, sticky="nw")
        self.run_button = ttk.Button(actions, text=_("▶ Run sync"),
                                     command=self.on_run_sync, width=22)
        self.run_button.pack(side="top", anchor="w", pady=1)
        self.stop_button = ttk.Button(actions, text=_("⏹ Stop"),
                                      command=self.on_stop_sync, state="disabled", width=22)
        self.stop_button.pack(side="top", anchor="w", pady=1)
        ttk.Button(actions, text=_("📋 Copy command"),
                   command=self.on_copy_command, width=22).pack(side="top", anchor="w", pady=1)

        # Colonne droite : options en COLONNE, « ? » alignés (grille). Chaque
        # option occupe une rangée : case en colonne 0, « ? » en colonne 1 → tous
        # les « ? » s'alignent. La rangée avancée (SHA1) est réservée en permanence
        # (masquée tant que non dépliée) pour que la hauteur ne bouge pas.
        opts = ttk.Frame(run_frame)
        opts.grid(row=0, column=1, sticky="ne")
        self._add_option_grid(opts, 0, 0, _("Test (dry-run)"), self.opt_dry_run, "dry-run")
        self._add_option_grid(opts, 1, 0, _("Propagate deletions"), self.opt_delete, "delete")
        # « Détaillée » n'est PAS ici : c'est un contrôle de VUE, qui s'applique
        # aussi bien à la synchro manuelle qu'à l'amorçage et à la
        # réinitialisation. Sa place est dans la barre de la zone de sortie, à
        # côté de « Erreurs seulement ». La laisser parmi les options de synchro
        # manuelle laissait croire qu'elle ne concernait que ce bouton.
        self._adv_visible = False
        self._adv_toggle = ttk.Button(opts, text=_("Advanced options ▾"),
                                      command=self._toggle_advanced, width=20)
        # columnspan=2 : le bouton (large) ne force PAS la largeur de la colonne 0,
        # pour que les « ? » restent alignés juste après les cases.
        self._adv_toggle.grid(row=2, column=0, columnspan=2, sticky="w", pady=(2, 0))
        self._adv_widgets = self._add_option_grid(
            opts, 4, 0, _("SHA1 check"), self.opt_verify_hash, "verify-hash",
            return_widgets=True)
        for w in self._adv_widgets:
            w.grid_remove()   # réserve la géométrie sans afficher

        # Encadré Cache — SÉPARÉ de la synchronisation manuelle. Ces passages sont
        # TOUJOURS réels et pilotés par la config de chaque mapping ; les options
        # de la synchro manuelle (dry-run, suppressions) ne s'y appliquent PAS —
        # d'où l'encadré distinct et la légende explicite. Packé après run_frame
        # (side=bottom) : apparaît JUSTE AU-DESSUS de « Synchronisation manuelle ».
        cache_frame = ttk.LabelFrame(self, text=_("Cache"), padding=8)
        cache_frame.pack(side="bottom", fill="x", padx=8, pady=(0, 4))
        ttk.Label(cache_frame, wraplength=900, justify="left",
                  text=_("Real pass, driven by each mapping's own settings — "
                         "the Test (dry-run) option does not apply here.")
                  ).pack(side="top", anchor="w", pady=(0, 4))
        cache_actions = ttk.Frame(cache_frame)
        cache_actions.pack(side="top", fill="x")
        self.prime_button = ttk.Button(cache_actions, text=_("🌱 Prime cache"),
                                       command=self.on_prime_cache)
        self.prime_button.pack(side="left", padx=2)
        self.reset_button = ttk.Button(cache_actions, text=_("♻ Reset mapping"),
                                       command=self.on_reset_mapping)
        self.reset_button.pack(side="left", padx=2)
        # Arrêter PROPRE à la boîte Cache : actif seulement pendant un amorçage /
        # une réinitialisation. Appelle le même on_stop_sync (le process est
        # partagé : self.sync_process). Placé ICI pour que l'utilisateur trouve
        # l'arrêt là où il a lancé l'action, malgré la séparation en deux boîtes.
        self.cache_stop_button = ttk.Button(cache_actions, text=_("⏹ Stop"),
                                            command=self.on_stop_sync, state="disabled")
        self.cache_stop_button.pack(side="left", padx=2)

        # --- Zone centrale partagée : tableau (haut) + sortie (bas) ---
        # Un PanedWindow vertical laisse l'utilisateur ajuster la répartition
        # en glissant la poignée, et garantit que les deux zones restent visibles.
        paned = tk.PanedWindow(self, orient="vertical", sashwidth=6,
                               sashrelief="raised", opaqueresize=True,
                               borderwidth=0)
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
        paned.add(tree_frame, minsize=96, stretch="always")

        # Zone de sortie en direct (panneau du bas)
        out_frame = ttk.LabelFrame(paned, text=_("Sync output"), padding=4)
        # Barre de contrôles de VUE de la sortie PARTAGÉE (s'applique aux DEUX
        # groupes : synchro manuelle ET cache). D'où sa place ICI, avec la sortie,
        # plutôt que sous « Synchronisation manuelle ».
        out_toolbar = ttk.Frame(out_frame)
        out_toolbar.pack(side="top", fill="x", pady=(0, 3))
        # Ordre voulu : du plus large au plus restrictif — « Détaillée » ouvre le
        # flux, « Erreurs seulement » le referme sur les problèmes.
        ttk.Checkbutton(out_toolbar, text=_("Verbose"),
                        variable=self.opt_verbose).pack(side="left", padx=(2, 4))
        ttk.Button(out_toolbar, text="?", width=2,
                   command=lambda: self._show_help("verbose")).pack(side="left")
        ttk.Checkbutton(out_toolbar, text=_("❗ Errors only"),
                        variable=self.opt_errors_only,
                        command=self._reapply_output_filter).pack(side="left", padx=(12, 16))
        ttk.Button(out_toolbar, text=_("🧹 Clear output"),
                   command=self.on_clear_output).pack(side="left", padx=2)
        self.output = scrolledtext.ScrolledText(out_frame, height=10, wrap="none",
                                                font=("monospace", 9), state="disabled")
        self.output.pack(side="top", fill="both", expand=True)

        # Barre de progression (Temps 1) : zone SÉPARÉE sous le journal. Affiche
        # l'envoi en cours (nb de fichiers + taille) sans se noyer dans le flux
        # de lignes. Masquée au repos. Alimentée par les lignes @@PROGRESS
        # émises par le moteur (interceptées, jamais écrites au journal).
        self.progress_frame = ttk.Frame(out_frame)
        # (pack fait/défait dynamiquement : caché au repos)
        # Indicateur d'envoi DISCRET : texte seul, pas de barre animée
        # « balayante » (jugée agressante visuellement). Affiche « ⬆ Envoi en
        # cours — N fichiers, X Go » pendant un envoi, masqué au repos.
        self.progress_label = ttk.Label(self.progress_frame, text="", font=("", 9),
                                         anchor="w")
        self.progress_label.pack(side="left", fill="x", expand=True, padx=(2, 0))
        self._progress_active = False

        # minsize 140 : la sortie porte AUSSI le titre de l'encadré et la barre
        # « Erreurs seules / Effacer » ; 76 px ne laissaient qu'~1-2 lignes de texte
        # (zone écrasée quand on agrandit le tableau). 140 px ≈ 5 lignes utiles.
        # Avec le tableau (96), le minimum total reste compatible basse résolution.
        paned.add(out_frame, minsize=140, stretch="always")

        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _autosize_fixed_columns(self):
        """Ajuste Prêt / Type / Corbeille / Exclusions à la largeur de LEUR contenu
        (en-tête traduit + valeurs possibles), mesurée avec les vraies polices, et
        les fige (stretch=False) pour qu'elles ne s'étirent plus à l'agrandissement.
        Le redimensionnement manuel reste possible. Recalculé dans la langue active
        au démarrage (un changement de langue impose de toute façon un redémarrage),
        donc jamais tronqué ni gonflé quelle que soit la langue. Source/Destination
        restent élastiques et absorbent l'agrandissement.

        Robustesse Tk 8.6 (segfault Tk_FreeFont) : cette méthode ne crée JAMAIS
        de police jetable et ne mesure JAMAIS d'emoji.
          * Polices : uniquement via nametofont() -> objets à delete_font=False,
            dont le __del__ n'appelle pas « font delete ». On évite ainsi le
            chemin tkfont.Font(font=spec) (delete_font=True), dont la libération
            au ramasse-miettes peut planter dans Tk_FreeFont sur certaines libs
            Tk 8.6 (Ubuntu 22.04 notamment).
          * Emoji : mesurer un glyphe couleur (✅ ⏳ ⛔ 🗑) force le chargement
            d'une police emoji couleur via fontconfig ; sa libération est le
            déclencheur observé du segfault. Les colonnes à glyphe (Prêt,
            Corbeille) ne mesurent donc aucun emoji — ni en valeur, ni en
            en-tête — et dérivent leur largeur d'un proxy neutre « MM »,
            relatif au DPI/scaling, assez large pour un glyphe unique.
        Compromis assumé : on n'interroge plus la police exacte surchargée par
        le thème pour le Treeview ; on prend les polices nommées standard. En
        pratique, avec le PAD de respiration, aucune troncature en 6 langues."""
        import tkinter.font as tkfont

        # Uniquement des références nommées (delete_font=False -> pas de
        # « font delete » au GC, donc aucun Tk_FreeFont déclenché par cette
        # méthode). Repli tolérant sur TkDefaultFont si un nom manque.
        def _named(name):
            try:
                return tkfont.nametofont(name)
            except Exception:
                return tkfont.nametofont("TkDefaultFont")

        hfont = _named("TkHeadingFont")   # police des en-têtes (souvent grasse)
        cfont = _named("TkDefaultFont")   # police des cellules
        PAD = 30   # bordures + indicateur de tri + respiration (espace libre)

        # Largeur d'un glyphe unique SANS mesurer d'emoji : proxy neutre « MM »
        # (deux capitales), toujours >= la largeur rendue d'un seul glyphe.
        GLYPH = cfont.measure("MM")

        def fit(col, samples, header_is_glyph=False, values_are_glyph=False):
            """Largeur = max(en-tête, échantillons TEXTE, réserve glyphe).
            `samples` ne doit contenir que du texte garanti sans emoji.
            header_is_glyph / values_are_glyph : réservent GLYPH au lieu de
            mesurer l'emoji correspondant (en-tête et/ou valeurs)."""
            w = 0
            if header_is_glyph:
                w = max(w, GLYPH)   # en-tête emoji : jamais mesuré
            else:
                w = max(w, hfont.measure(self.tree.heading(col)["text"]))
            if values_are_glyph:
                w = max(w, GLYPH)   # valeurs emoji : jamais mesurées
            for s in samples:
                if s:
                    w = max(w, cfont.measure(s))
            self.tree.column(col, width=w + PAD, stretch=False)

        # Prêt : en-tête texte traduit (mesuré), valeurs = glyphes (proxy).
        fit("state", [], values_are_glyph=True)
        # Type : en-tête + valeurs = texte traduit, mesuré normalement.
        fit("type",  [_("Folder"), _("File")])
        # Corbeille : en-tête ET valeurs = glyphes (aucune mesure d'emoji).
        fit("del",   [], header_is_glyph=True, values_are_glyph=True)
        # Exclusions : en-tête + valeurs = texte (le « — » est un tiret, pas un
        # emoji : mesuré sans risque).
        fit("exclusions", ["—", _("{n} name(s), {m} pattern(s)").format(n=99, m=99)])
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
                excl_txt = _("{n} name(s), {m} pattern(s)").format(n=n_names, m=n_pat)
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
        # B3 : ouvrir un autre fichier remplace le contenu en mémoire — ne pas
        # écraser en silence des modifications non enregistrées (comme le font
        # déjà on_close et on_move_mapping).
        if self.dirty:
            if not dlg_confirm(
                    self,
                    _("Some changes have not been saved. Open another file "
                      "without saving?"),
                    title=_("Unsaved changes"), kind="warning",
                    ok_text=_("Continue"), cancel_text=_("Cancel")):
                return
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
                raise ValueError(_("The file must contain a list or an object."))
            for entry in mappings:
                if "type" not in entry or "source" not in entry or "dest_parent" not in entry:
                    raise ValueError(_("Each entry must have: type, source, dest_parent."))
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

            # Écriture ATOMIQUE (tmp + os.replace), comme partout ailleurs dans
            # le projet (cache, déplacement de mapping, .desktop, push NAS). Le
            # fichier de mappings est lu en concurrence par le moteur (timer
            # planifié), le consommateur temps réel (chaque cycle) et le watcher
            # local (ré-scan) : une écriture directe « w » exposait un JSON
            # tronqué en cas de crash/disque plein — et perdait l'original, déjà
            # vidé par l'ouverture en « w ». os.replace est atomique : les lecteurs
            # voient soit l'ancien fichier, soit le nouveau, jamais un état partiel.
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
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
                ok, msg = False, _("NAS push failed: {e}").format(e=e)
            # Mise à jour de la barre d'état sur le thread Tk ; pas de modale pour
            # ne pas interrompre le flux d'enregistrement. La dérive (fenêtre
            # Temps réel) reflète l'état réel.
            self.after(0, lambda: self.status.set(
                _("Saved and pushed to the NAS: {base}").format(base=base) if ok
                else _("Saved: {base}  —  {msg}").format(base=base, msg=msg)))
        threading.Thread(target=work, daemon=True).start()

    def on_close(self):
        # Recueillir TOUTES les confirmations AVANT d'agir : sinon, se raviser à
        # la 2e question (modifs non enregistrées) laissait la fenêtre ouverte
        # mais la synchro DÉJÀ tuée pour rien. On tue donc la synchro seulement
        # une fois toutes les confirmations obtenues.
        sync_running = bool(self.sync_process and self.sync_process.poll() is None)
        if sync_running:
            if not dlg_confirm(
                self,
                _("A sync is currently running. Quitting will interrupt it. Continue?"),
                title=_("Sync in progress"), kind="warning",
                ok_text=_("Quit"), cancel_text=_("Cancel")):
                return
        if self.dirty:
            if not dlg_confirm(
                self,
                _("Some changes have not been saved. Quit without saving?"),
                title=_("Unsaved changes"), kind="warning",
                ok_text=_("Quit without saving"), cancel_text=_("Cancel")):
                return
        if sync_running:
            self.on_stop_sync()
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

        # Note affichée quand la destination est sous « Partagé avec moi » : la
        # suppression y est impossible (limitation CLI) -> mapping en ajout seul.
        shared_note = ttk.Label(del_frame, text="", wraplength=600,
                                foreground="#7a5c00", justify="left")
        shared_note.pack(anchor="w", pady=(2, 0))

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
                        text=_("⚠ You chose “{a}” but the source is detected as "
                               "“{b}”. Check before saving.").format(
                                   a=srckind_var.get(), b=detected))
            elif detected == "missing":
                warn_lbl.config(
                    text=_("⚠ Source currently missing or unreachable — could not "
                           "detect the type. If it is an unmounted NAS share, "
                           "mount it first."))

        def toggle_delete(probe=True):
            state = "normal" if allow_var.get() else "disabled"
            for w in (rb_trash, rb_perm, rb_nfs, rb_local):
                w.config(state=state)
            if allow_var.get():
                # probe=False : appel venu d'une TRACE de frappe
                # (apply_shared_lock suit dest_var/src_var à chaque caractère).
                # detect_and_show() sonde le montage (mount_check ->
                # os.path.exists) sur le fil principal : la déclencher à chaque
                # frappe recréait le spam désamorcé plus haut, et pouvait GELER
                # l'interface sur un montage NFS effondré. La détection reste
                # déclenchée par les gestes EXPLICITES : case cochée, Parcourir,
                # FocusOut du champ Source, changement manuel du type.
                if probe:
                    detect_and_show()
            else:
                detect_lbl.config(text="")
                warn_lbl.config(text="")

        # Re-détecter si l'utilisateur change manuellement le type
        srckind_var.trace_add("write", lambda *a: detect_and_show() if allow_var.get() else None)
        # Re-détecter si la source est tapée à la main
        src_var.trace_add("write", lambda *a: None)  # évite le spam ; détection via Parcourir / focus-out
        src_entry.bind("<FocusOut>", lambda e: detect_and_show() if allow_var.get() else None)

        def apply_shared_lock(*_a):
            """Destination sous « Partagé avec moi » : comportement CONDITIONNÉ à
            la version du CLI.
              • CLI < 0.5.0 : la mise à la corbeille y est impossible. On force
                l'AJOUT SEUL (case décochée ET désactivée). Ce n'est pas de la
                prudence : sans ce verrou, les tentatives échouent une à une en
                descendant dans l'arbre et allongent inutilement la synchro.
              • CLI ≥ 0.5.0 : la suppression fonctionne (elle atterrit dans la
                corbeille DU PROPRIÉTAIRE). On rend donc la case disponible, mais
                on avertit en permanence de ce que cela implique — voir aussi la
                confirmation renforcée au passage en mode miroir.
            Ré-évalué en direct quand la destination change."""
            is_shared = dest_var.get().strip().rstrip("/").startswith("/shared-with-me")
            if is_shared and not self._shared_delete_capability():
                allow_var.set(False)
                allow_chk.config(state="disabled")
                shared_note.config(text=_(
                    "Deletions can't be propagated to a “Shared with me” "
                    "destination with this Proton CLI version: this mapping is "
                    "upload-only."))
            elif is_shared:
                allow_chk.config(state="normal")
                # Aperçu du chemin distant SEULEMENT si la source est un chemin
                # absolu : basename() d'une saisie relative/fantaisiste renverrait
                # la chaîne entière, et l'aperçu afficherait du charabia avec
                # l'autorité d'un vrai chemin. isabs est un test purement textuel
                # (aucune sonde disque), sûr à la frappe. Source non absolue ->
                # message générique existant (branche « if target » ci-dessous).
                src_now = src_var.get().strip()
                target = (mapping_remote_path(dest_var.get(), src_now)
                          if os.path.isabs(src_now) else "")
                # Le chemin est ISOLÉ sur sa propre ligne : noyé dans le
                # paragraphe, il se coupait en fin de ligne et devenait illisible,
                # alors que c'est justement l'information qui borne la portée de
                # l'avertissement.
                if target:
                    shared_note.config(text=_(
                        "This folder belongs to someone else.\n\n{p}\n\n"
                        "If you enable deletion, anything inside that subfolder "
                        "that is missing locally is deleted and sent to the "
                        "OWNER'S trash. The rest of their shared folder is not "
                        "touched.").format(p=target))
                else:
                    shared_note.config(text=_(
                        "This folder belongs to someone else.\n\n"
                        "Once you pick a source, deletion will only affect the "
                        "subfolder named after it, inside this shared folder.\n\n"
                        "Anything missing locally is deleted from there and sent "
                        "to the OWNER'S trash. The rest of their shared folder is "
                        "not touched."))
            else:
                allow_chk.config(state="normal")
                shared_note.config(text="")
            toggle_delete(probe=False)   # (re)synchronise l'état des sous-contrôles, SANS sonde de montage

        dest_var.trace_add("write", apply_shared_lock)
        # La note nomme désormais le sous-dossier réel (destination + nom de la
        # source) : elle doit donc suivre les DEUX champs, pas seulement la
        # destination.
        src_var.trace_add("write", apply_shared_lock)
        apply_shared_lock()  # état initial (verrou + sous-contrôles)

        # --- Validation et OK ---
        def on_ok():
            source = src_var.get().strip()
            dest = dest_var.get().strip()
            if not source:
                dlg_warning(dlg, _("Enter a source."), title=_("Missing source"))
                return
            # Une source non ABSOLUE n'est jamais légitime : le moteur compare des
            # chemins normalisés absolus, les watchers posent leurs watches dessus,
            # et l'aperçu/avertissements en dérivent le chemin distant. Accepter
            # « jjk... » créait un mapping fantaisiste (constaté en prod). On ne
            # teste PAS l'existence : une source NAS non montée au moment de
            # l'édition reste un cas légitime (le moteur la saute proprement).
            if not os.path.isabs(source):
                dlg_warning(
                    dlg,
                    _("The source must be an ABSOLUTE path (starting with “/”).\n"
                      "Use “Browse…” or fix the path you typed."),
                    title=_("Invalid source"))
                return
            if not dest:
                dlg_warning(dlg, _("Enter a destination."), title=_("Missing destination"))
                return
            # La destination doit vivre sous une racine INSCRIPTIBLE de Proton
            # Drive : « /my-files » ou « /shared-with-me ». Les autres racines de
            # premier niveau (Photos, Devices) sont des espaces spéciaux non
            # visés par ce projet, et toute autre saisie (chemin relatif,
            # charabia) produirait un mapping fantaisiste — même famille que la
            # validation de source ci-dessus. Comparaison à FRONTIÈRE de segment
            # (« /my-filesX » ne passe pas). Test purement textuel, la validité
            # réelle du dossier reste tranchée par « Parcourir Proton… » et par
            # le moteur.
            _d = dest.rstrip("/") or "/"
            if not any(_d == r or _d.startswith(r + "/")
                       for r in ("/my-files", "/shared-with-me")):
                dlg_warning(
                    dlg,
                    _("The destination must be a Proton Drive folder under\n"
                      "“/my-files” or “/shared-with-me”.\n"
                      "Use “Browse Proton…” to pick it."),
                    title=_("Invalid destination"))
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
                        _("You enabled deletion: confirm the source type "
                          "(NFS or local) before saving."),
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
                # AVERTISSEMENT FORT : miroir vers un dossier appartenant à AUTRUI.
                # Depuis le CLI 0.5.0 la suppression y fonctionne — et atterrit dans
                # la corbeille DU PROPRIÉTAIRE. Sur un espace collaboratif, un
                # miroir efface donc le travail des autres (tout ce qui n'est pas
                # dans la source locale). La limitation levée était accidentellement
                # protectrice : cet avertissement la remplace.
                if dest.rstrip("/").startswith("/shared-with-me"):
                    if not dlg_confirm(
                        dlg,
                        _("This destination is a folder shared with you — it "
                          "belongs to someone else.\n\n"
                          "With deletion enabled, every file inside\n{p}\n"
                          "and its subfolders that is missing from your local "
                          "source will be deleted and sent to the OWNER'S trash — "
                          "including files other people put there. Only that "
                          "subfolder is affected: neither the rest of the shared "
                          "folder nor the rest of the Drive.\n\n"
                          "Only enable deletion on a folder you are the sole "
                          "contributor to, as one-way delivery to its owner. Any "
                          "other use is potentially destructive.\n\n"
                          "Enable deletion on this shared folder?"
                          ).format(p=mapping_remote_path(dest, source) or dest),
                        title=_("Deletion on a shared folder"), kind="warning",
                        ok_text=_("Enable"), cancel_text=_("Cancel")):
                        return
                new_m["allow_delete"] = True
                new_m["delete_mode"] = mode_var.get()
                new_m["source_kind"] = chosen_kind

            # AVERTISSEMENT FORT (chantier v1.5.0) : si ce mapping est NFS et que
            # sa source dépend d'une correspondance de chemin déclarée, vérifier le
            # verdict du dernier self-test de CETTE paire. Rouge / jamais testée /
            # invalidée -> on avertit et on demande confirmation. Vert -> on laisse
            # passer sans bruit. On NE bloque PAS en dur (pas d'impasse) ; on
            # renvoie l'utilisateur vers Configuration où les points colorés vivent.
            if _HAS_CONFIG and new_m.get("source_kind") == "nfs":
                try:
                    pair = appconfig.pair_covering(new_m["source"])
                except Exception:
                    pair = None
                if pair:
                    try:
                        verdict = appconfig.selftest_verdict(pair["local"], pair["nas"])
                    except Exception:
                        verdict = None
                    if verdict != "green":
                        if verdict == "red":
                            detail = _("its last test FAILED (paths do not match)")
                        elif verdict == "yellow":
                            detail = _("its last test reported a problem")
                        else:
                            detail = _("it has not been tested yet")
                        if not dlg_confirm(
                            dlg,
                            _("This folder is on the NAS and depends on the path "
                              "correspondence {l} ↔ {n}, but {d}.\n\n"
                              "If the correspondence is wrong, real-time sync may not "
                              "work for this folder. You can check it in "
                              "Configuration (coloured dots next to each "
                              "correspondence).\n\nAdd this mapping anyway?").format(
                                  l=pair["local"], n=pair["nas"], d=detail),
                            title=_("Correspondence not validated"), kind="warning",
                            ok_text=_("Add anyway"), cancel_text=_("Cancel")):
                            return

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
        # Re-résoudre la POSITION du mapping par identité d'objet : pendant que le
        # dialogue tournait (wait_window), une boîte imbriquée a pu consommer le
        # grab et rendre la fenêtre principale cliquable — l'utilisateur a pu y
        # supprimer/déplacer une ligne, périmant `idx`. Écrire self.mappings[idx]
        # sur un index périmé écraserait le mauvais mapping (ou lèverait
        # IndexError). Si l'objet `old` n'est plus dans la liste (supprimé/déplacé),
        # on abandonne l'édition sans rien toucher : la liste reflète déjà le geste
        # de l'utilisateur.
        try:
            idx = next(i for i, _m in enumerate(self.mappings) if _m is old)
        except StopIteration:
            return
        new_mirror = bool(updated.get("allow_delete"))

        # --- Changement de vocation sur un mapping DÉJÀ amorcé ---
        if was_primed and (new_mirror != old_mirror):
            if new_mirror:
                # M15 : cette bascule invalide le cache d'amorçage sur disque —
                # refuser si un passage tourne (il réécrirait le cache et
                # ressusciterait l'amorçage). Contrôle AVANT la confirmation.
                if self._pass_running_warn():
                    return
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
        # M15 : le déplacement mute le cache sur disque (il déplace le sous-arbre
        # d'amorçage) — refuser si un passage tourne, sinon le moteur en cours
        # réécrirait le cache et le déplacement serait perdu en silence.
        if self._pass_running_warn():
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
        path = self.config_path or schedule_manager.read_service_mappings_path()
        if not path:
            dlg_info(
                self,
                _("No mappings file is open and no schedule is installed. "
                "Open or save a mappings file to configure one."),
                title=_("No file"))
            return
        ScheduleDialog(self, path)


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
        path = self.config_path or realtime_manager.read_units_mappings_path()
        if not path:
            dlg_info(
                self,
                _("No mappings file is open and no real-time service is "
                "installed. Open or save a mappings file to configure one."),
                title=_("No file"))
            return
        RealtimeDialog(self, path)

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

    _LAUNCHER_NAME = "proton-drive-sync.desktop"

    def _launcher_menu_path(self):
        return os.path.expanduser(
            "~/.local/share/applications/" + self._LAUNCHER_NAME)

    def _desktop_dir(self):
        """Dossier Bureau de l'utilisateur (xdg-user-dir, repli ~/Desktop)."""
        try:
            r = subprocess.run(["xdg-user-dir", "DESKTOP"],
                               capture_output=True, text=True, timeout=5)
            d = (r.stdout or "").strip()
            if d and os.path.isdir(d):
                return d
        except (OSError, subprocess.SubprocessError):
            pass
        return os.path.expanduser("~/Desktop")

    def _launcher_desktop_path(self):
        return os.path.join(self._desktop_dir(), self._LAUNCHER_NAME)

    @staticmethod
    def _desktop_exec_quote(arg):
        """Quote un argument pour la ligne Exec d'un .desktop selon la spec
        Desktop Entry : guillemets doubles si l'argument contient un caractère
        réservé, avec échappement de " ` $ \\ . Sans quoi un chemin de mappings
        contenant une espace produisait un lanceur au 3e argument tronqué
        (« /home/x/Mes » au lieu de « /home/x/Mes documents/m.json »), et faussait
        la détection de cible de _launcher_existing_target."""
        if arg and all(c not in ' \t\n"\'\\`$<>~|&;()*?#' for c in arg):
            return arg
        esc = (arg.replace('\\', '\\\\').replace('"', '\\"')
                  .replace('`', '\\`').replace('$', '\\$'))
        return '"' + esc + '"'

    def _launcher_content(self, target_path=None):
        """Contenu du .desktop. `target_path` : fichier de mappings à ouvrir
        (Exec avec argument), ou None (éditeur vide). Catégorie Network
        (menu « Internet »). Grâce à l'instance unique, cliquer le lanceur
        remonte la fenêtre existante plutôt que d'en ouvrir une seconde."""
        editor = os.path.join(APP_DIR, "proton_mapping_editor.py")
        icon = os.path.join(APP_DIR, "icone.png")
        q = self._desktop_exec_quote
        exec_line = f"{q(sys.executable)} {q(editor)}"
        if target_path:
            exec_line += f" {q(target_path)}"
        return (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Version=1.0\n"
            "Name=Proton Drive Sync\n"
            "Name[fr]=Synchro Proton Drive\n"
            "Name[de]=Proton Drive Synchronisierung\n"
            "Name[es]=Sincronización de Proton Drive\n"
            "Name[it]=Sincronizzazione Proton Drive\n"
            "Name[pt]=Sincronização do Proton Drive\n"
            "Comment=Folder sync to Proton Drive\n"
            "Comment[fr]=Synchronisation de dossiers vers Proton Drive\n"
            "Comment[de]=Ordner-Synchronisierung mit Proton Drive\n"
            "Comment[es]=Sincronización de carpetas con Proton Drive\n"
            "Comment[it]=Sincronizzazione di cartelle con Proton Drive\n"
            "Comment[pt]=Sincronização de pastas com o Proton Drive\n"
            f"Exec={exec_line}\n"
            f"Icon={icon}\n"
            "Terminal=false\n"
            "Categories=Network;\n")

    def _write_desktop_file(self, path, content, trusted=False):
        """Écrit un .desktop (atomique, +x). Si trusted, tente de le marquer
        « fiable » (gio) pour éviter l'avertissement du bureau sur le Bureau."""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
        os.chmod(path, 0o755)                     # certains bureaux exigent +x
        if trusted:
            try:
                subprocess.run(["gio", "set", path, "metadata::trusted", "true"],
                               capture_output=True, timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass

    def _launcher_existing_target(self):
        """True si un lanceur existant (menu ou bureau) a un ARGUMENT après le
        script (donc une cible « mapping courant ») — sert à pré-régler la case
        cible pour que ré-enregistrer soit idempotent."""
        for p in (self._launcher_menu_path(), self._launcher_desktop_path()):
            try:
                with open(p, encoding="utf-8") as f:
                    for line in f:
                        if line.startswith("Exec="):
                            # shlex.split gère les guillemets doubles (cf.
                            # _desktop_exec_quote) : un chemin à espaces reste UN
                            # seul token. python + éditeur = 2 tokens ; un 3e =
                            # cible « mapping courant ». split() naïf recomptait
                            # faux dès qu'un chemin contenait une espace.
                            try:
                                return len(shlex.split(line[5:].strip())) >= 3
                            except ValueError:
                                return len(line[5:].split()) >= 3
            except OSError:
                continue
        return False

    def _apply_launcher_setting(self, menu_enabled, desktop_enabled, target_path):
        """Crée/retire le lanceur dans le MENU (~/.local/share/applications) et/ou
        sur le BUREAU. `target_path` : mappings à ouvrir, ou None (éditeur vide).
        L'existence des fichiers EST l'état (aucun réglage persistant). Tolérant
        (confort : silencieux en cas d'échec)."""
        content = self._launcher_content(target_path)
        menu_path = self._launcher_menu_path()
        desktop_path = self._launcher_desktop_path()
        try:
            if menu_enabled:
                self._write_desktop_file(menu_path, content)
            elif os.path.exists(menu_path):
                os.remove(menu_path)
        except OSError:
            pass
        try:
            if desktop_enabled:
                self._write_desktop_file(desktop_path, content, trusted=True)
            elif os.path.exists(desktop_path):
                os.remove(desktop_path)
        except OSError:
            pass
        # Rafraîchir le cache du menu si l'outil est présent (sinon le bureau
        # rattrape à son prochain scan). Silencieux.
        try:
            subprocess.run(["update-desktop-database",
                            os.path.dirname(menu_path)],
                           capture_output=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
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
        # Construite CACHÉE : la taille définitive dépend du contenu et n'est
        # calculable qu'une fois tout créé (dimensionnement résolution-conscient,
        # plus bas). Sans ce withdraw, la fenêtre s'affichait à sa taille
        # « naturelle » puis était redimensionnée -> saut visible à l'ouverture.
        # Le grab est posé APRÈS le deiconify : une fenêtre non affichée ne peut
        # pas recevoir de grab (« grab failed: window not viewable »).
        dlg.withdraw()
        dlg.resizable(True, True)

        # Barre de boutons FIXE en bas (hors zone défilante), packée en PREMIER
        # (side=bottom) : toujours réservée et visible, quelle que soit la hauteur
        # du contenu ou la résolution de l'écran (cas basse résolution où le bas
        # débordait auparavant).
        btns = ttk.Frame(dlg, padding=(14, 8))
        btns.pack(side="bottom", fill="x")

        # Contenu DÉFILANT : Canvas + Scrollbar verticale ; `frm` (inchangé pour
        # toutes les sections ci-dessous) est ancré dans le canvas.
        _outer = ttk.Frame(dlg)
        _outer.pack(side="top", fill="both", expand=True)
        _canvas = tk.Canvas(_outer, highlightthickness=0)
        _vsb = ttk.Scrollbar(_outer, orient="vertical", command=_canvas.yview)
        _canvas.configure(yscrollcommand=_vsb.set)
        _vsb.pack(side="right", fill="y")
        _canvas.pack(side="left", fill="both", expand=True)
        frm = ttk.Frame(_canvas, padding=14)
        _win = _canvas.create_window((0, 0), window=frm, anchor="nw")
        frm.bind("<Configure>",
                 lambda e: _canvas.configure(scrollregion=_canvas.bbox("all")))
        _canvas.bind("<Configure>",
                     lambda e: _canvas.itemconfigure(_win, width=e.width))
        # Molette (X11 : Button-4/5 ; sinon MouseWheel). bind_all le temps du
        # dialogue, retiré à sa fermeture pour ne pas affecter les autres fenêtres.
        def _wheel(e):
            n = getattr(e, "num", None)
            d = -1 if n == 4 else (1 if n == 5 else
                                   (int(-e.delta / 120) if getattr(e, "delta", 0) else 0))
            _canvas.yview_scroll(d, "units")
        for _seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            _canvas.bind_all(_seq, _wheel)

        def _unbind_wheel(e):
            if e.widget is dlg:
                for s in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                    try:
                        _canvas.unbind_all(s)
                    except tk.TclError:
                        pass
        dlg.bind("<Destroy>", _unbind_wheel)

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

            # Disjoncteur d'envoi : le CLI peut rester bloqué indéfiniment en
            # fin de transfert (aucun temporisateur réseau armé). Sans ce
            # garde-fou, le verrou du moteur reste tenu et toute la
            # synchronisation s'arrête derrière, jusqu'à intervention manuelle.
            srow = ttk.Frame(cli_frame); srow.pack(anchor="w", fill="x", pady=(8, 0))
            ttk.Label(srow, text=_("Stop a frozen upload after (minutes): ")).pack(side="left")
            stall_var = tk.StringVar(value=str(appconfig.cli_stall_minutes()))
            ttk.Entry(srow, textvariable=stall_var, width=6).pack(side="left")
            ttk.Label(srow, text=_("  (0 = never)")).pack(side="left")
            help_btn(srow, "cli-stall-minutes")

            krow = ttk.Frame(cli_frame); krow.pack(anchor="w", fill="x", pady=(8, 0))
            ttk.Label(krow, text=_("Consecutive freezes before skipping a pass: ")).pack(side="left")
            kills_var = tk.StringVar(value=str(appconfig.cli_stall_max_kills()))
            ttk.Entry(krow, textvariable=kills_var, width=6).pack(side="left")
            ttk.Label(krow, text=_("  (0 = unlimited)")).pack(side="left")
            help_btn(krow, "cli-stall-max-kills")

        # ---- Section Langue ----
        if _HAS_I18N:
            lang_frame = ttk.LabelFrame(frm, text=_("Interface language"), padding=10)
            lang_frame.pack(fill="x", pady=(0, 10))
            lang_var = tk.StringVar(value=i18n.read_language_setting())
            # Liste DÉROULANTE (au lieu de 7 boutons radio) : une seule rangée au
            # lieu de sept -> fenêtre Configuration nettement plus courte (utile en
            # basse résolution). `lang_var` continue de porter le CODE (auto/en/fr/…,
            # lu tel quel à l'enregistrement) ; le Combobox affiche des LIBELLÉS,
            # d'où la table de correspondance code <-> libellé.
            lang_choices = (("auto", _("Auto (system language)")),
                            ("en", "English"),
                            ("fr", "Français"),
                            ("de", "Deutsch"),
                            ("es", "Español"),
                            ("it", "Italiano"),
                            ("pt", "Português"))
            code_to_label = {c: lbl for c, lbl in lang_choices}
            label_to_code = {lbl: c for c, lbl in lang_choices}
            if lang_var.get() not in code_to_label:      # réglage inconnu -> auto
                lang_var.set("auto")
            lang_display = tk.StringVar(value=code_to_label[lang_var.get()])
            lang_combo = ttk.Combobox(
                lang_frame, textvariable=lang_display, state="readonly",
                values=[lbl for _c, lbl in lang_choices], width=26)
            lang_combo.pack(anchor="w")
            lang_combo.bind(
                "<<ComboboxSelected>>",
                lambda e: lang_var.set(label_to_code[lang_display.get()]))

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

            # ---- Tableau : correspondance des chemins de DONNÉES desktop<->NAS ----
            pm_head = ttk.Frame(nas_frame); pm_head.pack(anchor="w", fill="x", pady=(12, 0))
            ttk.Label(pm_head, text=_("NAS data-path correspondence:")).pack(side="left")
            help_btn(pm_head, "nas-path-map")

            # En-têtes de colonnes.
            pm_cols = ttk.Frame(nas_frame); pm_cols.pack(anchor="w", fill="x", pady=(2, 0))
            ttk.Label(pm_cols, text=_("Seen on this machine (desktop)"),
                      font=("", 8)).grid(row=0, column=0, sticky="w", padx=(0, 6))
            ttk.Label(pm_cols, text=_("Seen on the NAS"),
                      font=("", 8)).grid(row=0, column=1, sticky="w", padx=(0, 6))

            pm_rows_frame = ttk.Frame(nas_frame)
            pm_rows_frame.pack(anchor="w", fill="x")
            pm_rows = []   # liste de dicts {frame, local_var, nas_var}

            def pm_add_row(local="", nas=""):
                rf = ttk.Frame(pm_rows_frame); rf.pack(anchor="w", fill="x", pady=2)
                lv = tk.StringVar(value=local)
                nv = tk.StringVar(value=nas)
                le = ttk.Entry(rf, textvariable=lv, width=26)
                le.grid(row=0, column=0, padx=(0, 4))

                def browse_local(_v=lv):
                    d = pick_directory(dlg, title=_("Choose a data folder on this machine"))
                    if d:
                        _v.set(d)
                ttk.Button(rf, text="📁", width=3, command=browse_local).grid(row=0, column=1, padx=(0, 6))
                ne = ttk.Entry(rf, textvariable=nv, width=24)
                ne.grid(row=0, column=2, padx=(0, 4))

                # Point de statut coloré (gris = non testé, puis vert/jaune/rouge).
                # Pastille de statut dessinée (Canvas) : indépendante de la
                # police, la couleur est garantie (le glyphe ● n'était pas fiable
                # dans tous les environnements Tk). Gris = non testé.
                status = tk.Canvas(rf, width=16, height=16, highlightthickness=0, bd=0)
                _dot_id = status.create_oval(3, 3, 13, 13, fill="#999999", outline="")
                status.grid(row=0, column=3, padx=(4, 2))
                status._dot_id = _dot_id

                def set_dot(color, _c=status):
                    """Peindre la pastille de statut (color = code hex)."""
                    try:
                        _c.itemconfigure(_c._dot_id, fill=color)
                    except Exception:
                        pass
                status._set_dot = set_dot

                last_msg = {"text": _("Not tested yet.")}

                # Infobulle : afficher le message du dernier test au survol/clic.
                def show_status_msg(_e=None):
                    dlg_info(dlg, last_msg["text"], title=_("Correspondence test"))
                status.bind("<Button-1>", show_status_msg)

                entry = {"frame": rf, "local_var": lv, "nas_var": nv,
                         "status": status, "last_msg": last_msg, "color": None}

                def run_test(_e=entry):
                    loc = _e["local_var"].get().strip()
                    na = _e["nas_var"].get().strip()
                    if not loc or not na:
                        dlg_info(dlg, _("Fill both the desktop path and the NAS path "
                                        "before testing."), title=_("Correspondence test"))
                        return
                    _e["status"]._set_dot("#3b82f6")  # bleu = en cours

                    def worker():
                        try:
                            color, msg = realtime_manager.run_selftest(loc, na)
                        except Exception as ex:
                            color, msg = "yellow", _("Test error: {e}").format(e=ex)
                        def apply():
                            palette = {"green": "#22c55e", "yellow": "#eab308",
                                       "red": "#ef4444"}
                            _e["status"]._set_dot(palette.get(color, "#999999"))
                            _e["last_msg"]["text"] = msg
                            _e["color"] = color
                            # Mémoriser le verdict pour la paire EXACTE testée
                            # (consulté à l'ajout d'un mapping).
                            if _HAS_CONFIG:
                                try:
                                    appconfig.set_selftest_verdict(loc, na, color)
                                except Exception:
                                    pass
                        try:
                            dlg.after(0, apply)
                        except Exception:
                            pass
                    import threading as _th
                    _th.Thread(target=worker, daemon=True).start()

                # Éditer un champ INVALIDE le verdict : point -> gris, verdict
                # mémorisé effacé (il ne correspondrait plus à la paire affichée).
                # On ne relance PAS le test (évite les tests en rafale à la frappe).
                def invalidate(*_a, _e=entry):
                    if _e["color"] is not None:
                        # Effacer l'ancien verdict (ancienne paire) si connu.
                        pass
                    _e["status"]._set_dot("#999999")
                    _e["last_msg"]["text"] = _("Not tested yet.")
                    _e["color"] = None
                lv.trace_add("write", invalidate)
                nv.trace_add("write", invalidate)

                ttk.Button(rf, text=_("Test"), width=6,
                           command=run_test).grid(row=0, column=4, padx=(2, 4))

                def remove_this(_e=entry):
                    _e["frame"].destroy()
                    pm_rows.remove(_e)
                ttk.Button(rf, text="−", width=3, command=remove_this).grid(row=0, column=5)
                pm_rows.append(entry)
                return entry

            # Pré-remplir avec la table existante.
            for pair in appconfig.nas_path_map():
                e = pm_add_row(pair.get("local", ""), pair.get("nas", ""))
                # Restaurer le point coloré du dernier test mémorisé pour cette
                # paire EXACTE (le trace_add l'a mis au gris pendant le remplissage).
                if _HAS_CONFIG:
                    try:
                        c = appconfig.selftest_verdict(pair.get("local", ""),
                                                       pair.get("nas", ""))
                    except Exception:
                        c = None
                    if c:
                        palette = {"green": "#22c55e", "yellow": "#eab308",
                                   "red": "#ef4444"}
                        e["status"]._set_dot(palette.get(c, "#999999"))
                        e["color"] = c
                        # Restaurer aussi un message cohérent avec la couleur
                        # (seule la couleur est mémorisée ; on reconstruit un
                        # libellé d'état. Re-tester donne le détail complet frais).
                        restored = {
                            "green": _("Last test: OK (paths match and the "
                                       "real-time chain works). Re-test for a "
                                       "fresh check."),
                            "yellow": _("Last test: a problem was reported. "
                                        "Re-test for details."),
                            "red": _("Last test: paths did not match. "
                                     "Re-test for details."),
                        }.get(c)
                        if restored:
                            e["last_msg"]["text"] = restored

            pm_btns = ttk.Frame(nas_frame); pm_btns.pack(anchor="w", fill="x", pady=(2, 0))
            ttk.Button(pm_btns, text=_("+ Add a correspondence"),
                       command=lambda: pm_add_row()).pack(side="left")

            def test_all():
                for e in pm_rows:
                    # Déclenche le test de chaque ligne remplie (chacun dans son thread).
                    if e["local_var"].get().strip() and e["nas_var"].get().strip():
                        for w in e["frame"].winfo_children():
                            if isinstance(w, ttk.Button) and w.cget("text") == _("Test"):
                                w.invoke()
                                break
            ttk.Button(pm_btns, text=_("Test all"),
                       command=test_all).pack(side="left", padx=(6, 0))

            # Légende des couleurs (cliquer un point donne le détail).
            ttk.Label(nas_frame, font=("", 8), foreground="#666666",
                      text=_("Test: ● green = works · ● yellow = issue · "
                             "● red = paths don't match · click a dot for details")
                      ).pack(anchor="w", pady=(2, 0))

            def _set_children_state(container, state):
                """Active/désactive récursivement les contrôles d'un conteneur.
                Récursif (et non une liste tenue à la main) pour que les lignes
                AJOUTÉES APRÈS COUP suivent automatiquement l'état. Les Canvas
                (pastilles de statut) sont laissés intacts : purement informatifs,
                leur rendu ne doit pas changer."""
                for child in container.winfo_children():
                    if not isinstance(child, tk.Canvas):
                        try:
                            child.configure(state=state)
                        except tk.TclError:
                            pass          # conteneur sans option « state »
                    _set_children_state(child, state)

            def sync_mount_state(*_a):
                state = "normal" if nas_var.get() else "disabled"
                mount_entry.configure(state=state)
                ident_entry.configure(state=state)
                # Le tableau de correspondance suit le même sort que le reste des
                # réglages NAS : sans NAS il n'a aucun sens, or il restait actif
                # (incohérence). Le bouton d'aide « ? » de l'en-tête reste, lui,
                # accessible pour pouvoir lire l'explication.
                for container in (pm_cols, pm_rows_frame, pm_btns):
                    _set_children_state(container, state)
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

            row5 = ttk.Frame(ext_frame); row5.pack(anchor="w", fill="x", pady=(8, 0))
            ttk.Label(row5, text=_("Only these extensions: ")).pack(side="left")
            wl_var = tk.StringVar(value=appconfig.format_ext_list())
            wl_entry = ttk.Entry(row5, textvariable=wl_var, width=40)
            wl_entry.pack(side="left", fill="x", expand=True)
            help_btn(row5, "rename-ext-whitelist")

            def sync_suffix_state(*_a):
                state = "normal" if rename_var.get() else "disabled"
                suffix_entry.configure(state=state)
                wl_entry.configure(state=state)
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

            # ---- Section Lanceur d'application ----
            launcher_frame = ttk.LabelFrame(frm, text=_("Application launcher"), padding=10)
            launcher_frame.pack(fill="x", pady=(0, 10))
            launcher_menu_var = tk.BooleanVar(
                value=os.path.exists(self._launcher_menu_path()))
            launcher_desk_var = tk.BooleanVar(
                value=os.path.exists(self._launcher_desktop_path()))
            row6 = ttk.Frame(launcher_frame); row6.pack(anchor="w", fill="x")
            ttk.Checkbutton(row6, text=_("Show in the applications menu"),
                            variable=launcher_menu_var).pack(side="left")
            # Où le retrouver : le .desktop déclare « Categories=Network », que la
            # plupart des bureaux (Cinnamon, MATE, XFCE, KDE) rangent dans la
            # section « Internet ». Formulé avec « généralement » à dessein : le
            # nom exact dépend du bureau et de sa langue, et certains (GNOME
            # moderne) n'ont pas de sous-menus par catégorie du tout.
            ttk.Label(launcher_frame, font=("", 8), foreground="#666666",
                      text=_("Usually appears in the “Internet” section of the menu.")
                      ).pack(anchor="w", padx=(20, 0))
            row6b = ttk.Frame(launcher_frame); row6b.pack(anchor="w", fill="x")
            ttk.Checkbutton(row6b, text=_("Create a desktop shortcut"),
                            variable=launcher_desk_var).pack(side="left")
            help_btn(row6b, "launcher-desktop")
            # Cible : aucun fichier / mapping courant (courant seulement si un
            # fichier est ouvert). Pré-réglé selon un lanceur existant, pour que
            # ré-enregistrer soit idempotent.
            row6c = ttk.Frame(launcher_frame); row6c.pack(anchor="w", fill="x", pady=(4, 0))
            ttk.Label(row6c, text=_("The launcher opens:")).pack(side="left")
            launcher_target_var = tk.StringVar(
                value=("current" if (self._launcher_existing_target()
                                     and self.config_path) else "none"))
            ttk.Radiobutton(row6c, text=_("no file"), value="none",
                            variable=launcher_target_var).pack(side="left", padx=(6, 0))
            _cur_rb = ttk.Radiobutton(row6c, text=_("the current mapping"),
                                      value="current", variable=launcher_target_var)
            _cur_rb.pack(side="left", padx=(6, 0))
            if not self.config_path:
                _cur_rb.configure(state="disabled")
                launcher_target_var.set("none")

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
                # Table de correspondance des chemins de données (lignes non vides).
                pairs = [{"local": e["local_var"].get().strip(),
                          "nas": e["nas_var"].get().strip()}
                         for e in pm_rows
                         if e["local_var"].get().strip() and e["nas_var"].get().strip()]
                appconfig.set_nas_path_map(pairs)
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
                              "markers and continuity are preserved.").format(
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
                # Liste blanche : une saisie vide est LÉGITIME (= ne rien
                # restreindre), elle doit donc être enregistrée telle quelle.
                appconfig.set_rename_ext_whitelist(wl_var.get())
                # Disjoncteur : une saisie illisible est ignorée en silence
                # plutôt que d'écraser un réglage valide par une valeur fausse.
                appconfig.set_cli_stall_minutes(stall_var.get())
                appconfig.set_cli_stall_max_kills(kills_var.get())
                # Icône de barre des tâches : effet IMMÉDIAT (démarrage à
                # l'activation ; extinction d'elle-même à la désactivation).
                appconfig.set_tray_enabled(tray_var.get())
                self._apply_tray_setting(tray_var.get())
                self._apply_launcher_setting(
                    launcher_menu_var.get(), launcher_desk_var.get(),
                    self.config_path if launcher_target_var.get() == "current"
                    else None)

            # Redémarrage AUTOMATIQUE des démons SEULEMENT si un réglage FIGÉ a
            # changé. Analyse du code : le seul réglage lu au DÉMARRAGE du consumer
            # (hors boucle) est l'identité (account_name → _user_from_config). Tous
            # les autres réglages pertinents sont relus dynamiquement à chaque
            # cycle (nas_enabled, nas_mount_path) ou à chaque passage du moteur
            # (rename_ext_*), donc ils s'appliquent seuls — aucun redémarrage requis.
            # On redémarre donc silencieusement uniquement quand l'identité change.
            identity_changed = bool(
                _HAS_CONFIG and _HAS_REALTIME
                # account_name() peut valoir None (identité jamais définie) : le
                # normaliser en "" pour ne pas déclencher un redémarrage parasite
                # des démons quand l'identité était None ET le champ vide
                # (None != "" était vrai à tort).
                and (old_ident or "") != (ident_var.get() or "").strip())
            # restart_daemons() renvoie (ok, message) et NE LÈVE PAS en cas
            # d'échec (systemctl en erreur -> ok=False). L'ancien code posait
            # restarted=True sans regarder ok : le GUI annonçait « démons
            # redémarrés » même en échec, alors que le consommateur continuait avec
            # l'ANCIENNE identité NAS (billets déposés sous l'ancien nom). On teste
            # donc ok, et en cas d'échec on affiche le message localisé renvoyé par
            # realtime_manager (aucune chaîne neuve à traduire).
            restarted = False
            restart_msg = ""
            if identity_changed:
                try:
                    _ok_r, restart_msg = realtime_manager.restart_daemons()
                    restarted = bool(_ok_r)
                except Exception as _e:
                    restarted = False
                    restart_msg = str(_e)

            dlg.destroy()
            if restarted:
                dlg_info(self, _("Configuration saved.\n\nThe background daemons "
                        "were restarted to apply the new NAS identity."),
                        title=_("Configuration"))
            elif identity_changed:
                # Identité changée mais redémarrage ÉCHOUÉ : ne pas prétendre au
                # succès. Le message localisé de realtime_manager dit ce qui a
                # échoué ; les démons tournent encore avec l'ancienne identité.
                dlg_error(self, restart_msg or _("Configuration"),
                          title=_("Configuration"))
            else:
                dlg_info(self, _("Configuration saved.\n\nChanges apply "
                        "automatically (the window uses them at its next launch)."),
                        title=_("Configuration"))

        ttk.Button(btns, text=_("Cancel"),
                   command=dlg.destroy).pack(side="right", padx=4)
        ttk.Button(btns, text=_("OK"), command=apply).pack(side="right")

        # Dimensionnement résolution-conscient : largeur = contenu + scrollbar ;
        # hauteur plafonnée à ~90 % de l'écran (au-delà, le contenu défile), pour
        # que les boutons restent visibles même en basse résolution.
        dlg.update_idletasks()
        _w = frm.winfo_reqwidth() + _vsb.winfo_reqwidth() + 6
        _h = min(frm.winfo_reqheight() + btns.winfo_reqheight() + 6,
                 int(dlg.winfo_screenheight() * 0.9))
        # Position posée EN MÊME TEMPS que la taille (centrée horizontalement sur
        # la fenêtre parente, un peu au-dessus du centre vertical) : sinon le
        # gestionnaire de fenêtres place la fenêtre lui-même, et le
        # redimensionnement la fait bouger. Bornée à l'écran pour ne pas déborder.
        _x = self.winfo_rootx() + max(0, (self.winfo_width() - _w) // 2)
        _y = self.winfo_rooty() + max(0, (self.winfo_height() - _h) // 3)
        _x = max(0, min(_x, dlg.winfo_screenwidth() - _w))
        _y = max(0, min(_y, dlg.winfo_screenheight() - _h))
        dlg.geometry(f"{_w}x{_h}+{_x}+{_y}")
        # Affichage UNIQUE, déjà à la bonne taille et au bon endroit.
        dlg.deiconify()
        dlg.grab_set()



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
        self._append_output(_("[Command copied]") + "\n" + cmd + "\n\n")

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
        entière dans le log disque, quel que soit le filtre d'affichage.

        Les lignes de progression (préfixe @@PROGRESS) sont INTERCEPTÉES ici :
        elles alimentent la barre de progression et ne sont JAMAIS écrites au
        journal (sinon elles pollueraient le flux)."""
        if line.lstrip().startswith("@@PROGRESS"):
            self._handle_progress_line(line.strip())
            return
        self._render_line(line)

    def _handle_progress_line(self, line):
        """Parse une ligne @@PROGRESS et met à jour l'indicateur d'envoi (dans le
        thread GUI via after). Format : @@PROGRESS k=v k=v … . Parsing DÉFENSIF :
        un champ manquant ou un format inattendu ne casse rien (ligne ignorée).

        Seuls state=start (afficher « Envoi en cours — N fichiers, X Go ») et
        state=done (masquer) sont traités."""
        fields = {}
        try:
            for tok in line.split()[1:]:            # sauter « @@PROGRESS »
                if "=" in tok:
                    k, v = tok.split("=", 1)
                    fields[k] = v
        except Exception:
            return
        state = fields.get("state")
        if state == "start":
            files = fields.get("files", "?")
            try:
                gb = int(fields.get("bytes", "0")) / (1024 ** 3)
                size_txt = _("{gb:.2f} GB").format(gb=gb) if gb >= 0.01 else _("< 0.01 GB")
            except Exception:
                size_txt = "?"
            txt = _("Uploading — {n} file(s), {size}").format(n=files, size=size_txt)
            self.after(0, lambda: self._show_progress(txt))
        elif state == "done":
            self.after(0, self._hide_progress)

    def _show_progress(self, text):
        """Affiche l'indicateur d'envoi discret (texte seul, pas de barre animée)."""
        try:
            self.progress_label.configure(text="⬆ " + text)
            if not self._progress_active:
                self.progress_frame.pack(side="top", fill="x", pady=(4, 0))
                self._progress_active = True
        except Exception:
            pass

    def _hide_progress(self):
        """Masque l'indicateur d'envoi (fin d'un envoi)."""
        try:
            if self._progress_active:
                self.progress_frame.pack_forget()
                self._progress_active = False
        except Exception:
            pass

    def _render_line(self, line):
        """Décide de l'affichage d'UNE ligne selon les filtres courants :
          1) « ❗ Erreurs seules » (prioritaire) -> uniquement les lignes d'erreur ;
          2) « Détaillé » -> brut ;
          3) sinon -> épuré : lignes de statut/progression (dont « 📂 <dossier> »
             émises par le moteur, un dossier par ligne dans l'ordre du balayage) +
             lignes d'erreur. Le détail (fichiers, JSON, uploads…) est masqué. Le
             GUI ne DEVINE plus le dossier — le moteur l'annonce."""
        # Compteur de dossiers balayés (alimente « Rien à mettre à jour » si zéro
        # dossier n'a été vu). Compté EN PREMIER, avant tout return anticipé —
        # sinon le mode « Erreurs seules » (qui sort tout de suite) laissait le
        # compteur à 0 et affichait « Rien à mettre à jour » alors que des dossiers
        # avaient bel et bien été envoyés.
        if line.lstrip().startswith("📂"):
            self._folders_shown = getattr(self, "_folders_shown", 0) + 1
        if self.opt_errors_only.get():
            stripped = line.strip()
            if self._is_error_line(stripped):
                self._append_output(line if line.endswith("\n") else line + "\n")
            return
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
                    # @@PROGRESS = protocole interne (barre de progression), traité
                    # à part via _handle_progress_line : jamais au log (sinon
                    # rejoué à l'écran au re-filtrage, cf. docstring _feed_output).
                    if not line.startswith("@@PROGRESS"):
                        logf.write(line)         # log disque : sortie complète
                        logf.flush()
                self.sync_process.wait()
            code = self.sync_process.returncode
            # Mode épuré : si aucun dossier n'a été affiché, l'écran serait vide ->
            # message explicite pour ne pas laisser croire à un plantage.
            if not self.opt_verbose.get() and getattr(self, "_folders_shown", 0) == 0 and code == 0:
                self._append_output(_("  ✓ Nothing to update — everything is already in sync.") + "\n")
            self._append_output("\n" + _("=== Finished (code {c}) — log: {p} ===").format(c=code, p=log_path) + "\n\n")
            self.after(0, self._hide_progress)   # filet : masquer la barre si un « done » a été manqué
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
        # Capturer la référence AVANT de la tester : le finally d'un thread de
        # synchro peut poser self.sync_process = None entre le test et le
        # terminate() -> AttributeError. On travaille sur la copie locale.
        proc = self.sync_process
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
            self._append_output("\n" + _("=== Interruption requested (terminate) ===") + "\n")
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

    def _pass_running_warn(self):
        """M15 : True (et prévient) si un passage moteur tient le verrou. Les
        opérations qui MUTENT directement le cache sur disque (invalidation de
        l'amorçage lors d'un passage additif→miroir, déplacement d'un mapping)
        ne doivent pas s'exécuter pendant un passage : le moteur garde son cache
        EN MÉMOIRE et le réécrit intégralement à ses checkpoints, ce qui
        écraserait silencieusement la mutation du GUI (le sous-arbre « invalidé »
        ressusciterait). On prévient et on refuse ; l'utilisateur relance une fois
        le passage terminé. Réutilise la sonde non destructive --check-lock."""
        if self._lock_is_busy():
            dlg_warning(self, _("A sync is already running."),
                        title=_("Sync in progress"))
            return True
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
            _("This is a REAL pass, not a test — the Test (dry-run) option "
              "does not apply to priming.") + "\n\n" +
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
        self.cache_stop_button.configure(state="normal")
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

        # Vidage distant demandé ET destination appartenant à AUTRUI : second
        # verrou. Depuis le CLI 0.5.0, « vider le dossier distant » vide RÉELLEMENT
        # un dossier partagé — donc le travail de son propriétaire, vers SA
        # corbeille. Le vidage étant optionnel, on n'avertit que s'il est coché.
        if wipe:
            shared = [m for m in chosen
                      if str(m.get("dest_parent", "")).rstrip("/").startswith("/shared-with-me")]
            if shared:
                # On liste le sous-dossier RÉELLEMENT vidé, pas le dossier
                # partagé qui le contient : c'est lui, et lui seul, qui part à
                # la corbeille du propriétaire.
                names_shared = "\n  • ".join(
                    mapping_remote_path(m["dest_parent"], m.get("source", ""))
                    or m["dest_parent"] for m in shared)
                if not dlg_confirm(
                    self,
                    _("You asked to empty the remote folder, and {n} of the "
                      "selected mapping(s) write inside a folder shared with "
                      "you:\n  • {names}\n\n"
                      "Those subfolders sit in folders belonging to someone "
                      "else. Emptying them sends their content — including files "
                      "other people put there — to the OWNER'S trash. The rest of "
                      "their shared folders is not touched.\n\n"
                      "Empty these subfolders anyway?"
                      ).format(n=len(shared), names=names_shared),
                    title=_("Emptying inside a shared folder"), kind="warning",
                    ok_text=_("Empty anyway"), cancel_text=_("Cancel")):
                    self.status.set(_("Reset cancelled."))
                    return

        self.prime_button.configure(state="disabled")
        self.reset_button.configure(state="disabled")
        self.run_button.configure(state="disabled")
        self.cache_stop_button.configure(state="normal")
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
        # Drapeau du filet de sécurité (voir le finally) : True tant que le
        # consommateur/timer sont arrêtés SANS avoir été redémarrés. Défini
        # AVANT le try pour être toujours lisible dans le finally.
        daemons_stopped = False
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
                daemons_stopped = True
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
                    # B10 : différencier le message selon l'opération. Ce chemin
                    # est partagé par l'amorçage ET la réinitialisation ; afficher
                    # « Priming cancelled » après l'annulation d'un RESET était
                    # trompeur. Les deux msgid existent déjà au catalogue.
                    cancel_msg = _("Reset cancelled.") if is_reset else _("Priming cancelled.")
                    self.after(0, lambda m=cancel_msg: self.status.set(m))
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
                    # @@PROGRESS non écrit au log (protocole interne, cf. B9/site 1).
                    if not line.startswith("@@PROGRESS"):
                        logf.write(line); logf.flush()   # log disque : sortie complète
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
                daemons_stopped = False   # redémarrage normal effectué
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
            # FILET DE SÉCURITÉ : si le consommateur/timer ont été arrêtés mais
            # que le redémarrage normal (étape 4) n'a pas eu lieu — annulation
            # pendant l'attente du verrou (return), ou exception en cours de
            # route —, on les redémarre ICI. Sans ce filet, un simple clic sur
            # Arrêter pendant « Waiting for the lock… » laissait le temps réel
            # ET le passage nocturne arrêtés indéfiniment, en silence — pour un
            # logiciel de sauvegarde, c'est pire que l'erreur affichée.
            if _HAS_REALTIME and daemons_stopped:
                try:
                    self._append_output(_("▶ Restarting the consumer…") + "\n")
                    realtime_manager.start_consumer()
                    if _HAS_SCHEDULE:
                        try:
                            schedule_manager.resume_timer()
                            self._append_output(_("▶ Scheduled timer restored.") + "\n")
                        except Exception:
                            pass
                except Exception:
                    pass
            self.sync_process = None
            self._prime_running = False
            self.after(0, lambda: self.prime_button.configure(state="normal"))
            self.after(0, lambda: self.reset_button.configure(state="normal"))
            self.after(0, lambda: self.run_button.configure(state="normal"))
            self.after(0, lambda: self.cache_stop_button.configure(state="disabled"))
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
        # Dimensionnement résolution-conscient : la taille voulue (660x680)
        # dépasse un écran 1024x600 (l'autre poste). On plafonne à ~90 % de la
        # hauteur / ~95 % de la largeur de l'écran, et on abaisse la minsize en
        # conséquence pour que la fenêtre ne puisse JAMAIS exiger plus grand que
        # l'écran. Le bouton Fermer est packé côté bas EN PREMIER (voir _build) :
        # il reste ancré et atteignable même si le contenu est comprimé.
        _sw, _sh = self.winfo_screenwidth(), self.winfo_screenheight()
        _w, _h = min(660, int(_sw * 0.95)), min(680, int(_sh * 0.90))
        self.geometry(f"{_w}x{_h}")
        self.minsize(min(620, _w), min(640, _h))
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
        self.day_var = tk.StringVar(value=_("Sunday"))
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
        return _("Sunday")

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
        # Rappel de divergence : si le service planifié installé vise un AUTRE
        # fichier que celui qu'on s'apprête à appliquer (le fichier en cours
        # d'édition), prévenir — cela va re-pointer la planification.
        installed = schedule_manager.read_service_mappings_path()
        if (installed and os.path.realpath(installed)
                != os.path.realpath(self.mappings_path)):
            if not dlg_confirm(
                self,
                _("The scheduled service currently uses:\n  {old}\n"
                "You are installing:\n  {new}\n\n"
                "This will switch the schedule to the file you are editing. "
                "Continue?").format(old=os.path.basename(installed),
                                    new=os.path.basename(self.mappings_path)),
                title=_("Active mappings file change"), kind="warning",
                ok_text=_("Confirm the change"),
                cancel_text=_("Cancel (keep the current file)")):
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
        # Plafonnement résolution-conscient (cf. ScheduleDialog) : 900x620
        # débordait en 1024x600. Bouton Fermer packé côté bas en premier -> reste
        # visible même si la hauteur est plafonnée.
        _sw, _sh = self.winfo_screenwidth(), self.winfo_screenheight()
        _w, _h = min(900, int(_sw * 0.95)), min(620, int(_sh * 0.90))
        self.geometry(f"{_w}x{_h}")
        self.minsize(min(700, _w), min(480, _h))
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
        # Taille relative à l'écran, centrée — s'adapte aux deux postes (l'un en
        # pleine résolution, l'autre en résolution réduite / texte agrandi) sans
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
        # Toutes les lignes brutes reçues (pour pouvoir re-filtrer quand on
        # (dé)coche « Détaillée » sans relire journalctl). Cap élevé : on garde
        # beaucoup d'historique pour remonter loin, avec purge du plus ancien.
        self._event_lines = []
        self._EVENT_MAX = 5000
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
            # Écart de scripts NAS (déploiement en attente) — même champ que le
            # systray (nas_scripts_stale). Packé dynamiquement par _apply_status :
            # visible seulement quand un push de scripts est requis.
            self.nas_scripts_var = tk.StringVar(value="")
            self.nas_scripts_label = ttk.Label(n, textvariable=self.nas_scripts_var,
                                                font=("", 10, "bold"),
                                                foreground=self.C_WARN,
                                                wraplength=WL, justify="left")
        else:
            self.nas_var = tk.StringVar(value="")
            # Même besoin que drift_label : _apply_status() configure sa couleur
            # sans condition -> widget réel mais jamais affiché.
            self.nas_label = ttk.Label(form, textvariable=self.nas_var)
            # Idem pour l'alerte de scripts NAS : widget réel jamais affiché en
            # mode local (nas_scripts_stale y est toujours False).
            self.nas_scripts_var = tk.StringVar(value="")
            self.nas_scripts_label = ttk.Label(form, textvariable=self.nas_scripts_var)
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
        # « Détaillée » : filtre d'AFFICHAGE (ne change pas ce que les démons
        # journalisent). Décochée (défaut) = vue épurée centrée sur ce qui se
        # synchronise (lignes « → synchro : <dossier> », leur résultat, et les
        # événements notables) ; le bruit fin des watchers (ADD/DEL de marqueurs)
        # est masqué. Cochée = tout le détail. Re-filtre l'historique déjà affiché.
        self.detailed_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(ev_bar, text=_("Detailed"),
                        variable=self.detailed_var,
                        command=self._reapply_event_filter).pack(side="left", padx=(12, 0))
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
        moins 360 px. Sur un petit écran la fenêtre est plus étroite,
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
        # Mémoriser la ligne brute (pour re-filtrage), avec purge du plus ancien.
        self._event_lines.append(text)
        if len(self._event_lines) > self._EVENT_MAX:
            del self._event_lines[:len(self._event_lines) - self._EVENT_MAX]
        # N'afficher que si elle passe le filtre courant (Détaillée ou épuré).
        if not self._event_visible(text):
            return
        self.events.configure(state="normal")
        self.events.insert("end", text)
        if self.autoscroll_var.get():
            self.events.see("end")
        self.events.configure(state="disabled")

    def _event_visible(self, line):
        """Décide si une ligne de journal s'affiche dans le mode courant.
        Détaillée cochée -> tout. Décochée (épuré) -> on garde ce qui renseigne
        sur CE QUI SE SYNCHRONISE (« → synchro : <dossier> », résultats, erreurs)
        et les événements notables ; on masque le bruit fin des watchers (ADD/DEL
        de marqueurs) et les lignes systemd techniques."""
        if self.detailed_var.get():
            return True
        s = line.strip()
        if not s:
            return False
        # Bruit masqué en mode épuré : marqueurs ADD/DEL des watchers, et lignes
        # techniques de systemd (Started/Stopped/Consumed…).
        if ("-> marqueur sur" in s or "-> marker on" in s
                or s.startswith("ADD ") or s.startswith("DEL ")):
            return False
        if ("proton-consume.service" in s or "proton-watch.service" in s
                or "proton-nas-watch.service" in s):
            return False
        # Gardé en mode épuré : ce qui montre l'activité de synchro utile.
        keep_markers = ("→ synchro", "→ sync", "✓ ok", "✗", "❌", "⛔", "⚠",
                        "🚫", "⏳", "🔓", "⊘",
                        "[account-changed]", "[auth-failed]",
                        # messages d'état notables du démon
                        "démarré", "daemon started", "Surveillance", "watching",
                        "rattrapage", "catch", "session", "verrou", "lock",
                        "compte", "account", "prêt", "ready",
                        # fin de passage (dédoublement du message de reprise) :
                        # « ✓ Passage terminé » / « ✓ Pass finished » — n'est pas
                        # capté par « ✓ ok », d'où l'ajout explicite.
                        "Passage terminé", "Pass finished", "Durchlauf beendet",
                        "Pasada finalizada", "Passaggio completato",
                        "Passagem concluída")
        return any(m in s for m in keep_markers)

    def _reapply_event_filter(self):
        """Reconstruit l'affichage depuis le buffer selon le mode courant
        (appelé quand on (dé)coche « Détaillée »)."""
        if not self._alive or not self.winfo_exists():
            return
        self.events.configure(state="normal")
        self.events.delete("1.0", "end")
        visible = [l for l in self._event_lines if self._event_visible(l)]
        if visible:
            self.events.insert("end", "".join(visible))
        if self.autoscroll_var.get():
            self.events.see("end")
        self.events.configure(state="disabled")

    def _clear_events(self):
        self._event_lines = []
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

        # Observation NAS (section 4) — trois états distincts :
        #   A. pas de NAS voulu (nas_enabled=False) : neutre, informatif ;
        #   B. NAS voulu et joignable : vert ;
        #   C. NAS voulu mais absent : rouge, orienté action.
        nas = st["nas"]
        nas_wanted = (appconfig.nas_enabled() if _HAS_CONFIG else True)
        if not nas_wanted:
            self.nas_var.set(_("⚪ Local mode — no NAS configured."))
            self.nas_label.config(foreground=self.C_MUTED)
        elif not nas["reachable"]:
            self.nas_var.set(_("🔴 NAS configured but unreachable — please check."))
            self.nas_label.config(foreground=self.C_ERR)
        else:
            if nas["last_activity"]:
                self.nas_var.set(_("🟢 NAS reachable — last activity {t} ago").format(t=nas["last_activity"]))
            else:
                self.nas_var.set(_("🟢 NAS reachable — no pending marker"))
            self.nas_label.config(foreground=self.C_OK)

        # Écart de scripts NAS (déploiement en attente) : MÊME champ que le systray
        # (nas_scripts_stale, écrit par le consumer). Affiché seulement si présent,
        # et pointe vers l'action à faire. pack/pack_forget idempotents.
        if st.get("nas_scripts_stale"):
            self.nas_scripts_var.set(_("⚠ NAS scripts out of date — run "
                                       "Install / Update to push them, then restart "
                                       "the NAS watcher."))
            self.nas_scripts_label.config(foreground=self.C_WARN)
            self.nas_scripts_label.pack(anchor="w", pady=(4, 0))
        else:
            self.nas_scripts_var.set("")
            self.nas_scripts_label.pack_forget()

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
        ok, msg, cmd = realtime_manager.install_or_update_units(self.mappings_path, enable=True)
        (dlg_success if ok else dlg_error)(self, msg, title=_("Daemons"), command=cmd)
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
        nas_part = f"{q['nas']}" if q["nas_reachable"] else _("NAS unreachable")
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


# Nom de socket Unix ABSTRAIT (Linux) — préfixe \0, propre à l'utilisateur.
# Un socket abstrait n'a pas d'entrée sur le système de fichiers : le noyau
# libère le nom à la mort du process, donc jamais de nom périmé à nettoyer
# (contrairement à un socket-fichier ou un PID-file).
_SINGLETON_ADDR = "\0proton_mapping_editor_%d" % os.getuid()


def _try_become_primary():
    """Tente de devenir l'instance PRIMAIRE. Renvoie le socket serveur si on est
    la première instance (à garder ouvert pendant toute la vie du process),
    sinon None (une autre instance tient déjà le nom)."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(_SINGLETON_ADDR)                # échoue si déjà lié
    except OSError:
        srv.close()
        return None
    srv.listen(5)
    return srv


def _signal_existing():
    """Demande à l'instance primaire de remonter sa fenêtre. Tolérant : renvoie
    False si la connexion échoue (instance en train de mourir)."""
    try:
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(2.0)
        c.connect(_SINGLETON_ADDR)
        c.sendall(b"raise")
        c.close()
        return True
    except OSError:
        return False


def _start_singleton_listener(app, srv):
    """Écoute les connexions d'éventuelles 2e instances (voir main()) et remonte
    la fenêtre `app` au premier plan. Le thread écouteur ne touche JAMAIS Tk : il
    dépose un jeton dans une queue ; un scrutateur périodique (planifié sur le
    thread PRINCIPAL via app.after) la draine et remonte la fenêtre. C'est le
    seul marshaling sûr — Tk n'est pas thread-safe et after() n'est appelable que
    depuis le thread principal."""
    app._raise_q = queue.Queue()

    def loop():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return                           # socket fermé : arrêt de l'app
            try:
                conn.recv(64)                    # contenu ignoré (Option X :
                                                 # on remonte, pas d'autre fichier)
            except OSError:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass
            app._raise_q.put(1)

    threading.Thread(target=loop, daemon=True).start()
    _poll_raise_requests(app)                    # démarre le scrutateur (thread principal)


def _poll_raise_requests(app):
    """Scrutateur sur le thread Tk : draine la queue et remonte la fenêtre si une
    2e instance l'a demandé. Se replanifie toutes les 200 ms."""
    drained = False
    try:
        while True:
            app._raise_q.get_nowait()
            drained = True
    except queue.Empty:
        pass
    if drained:
        _raise_to_front(app)
    try:
        app.after(200, lambda: _poll_raise_requests(app))
    except Exception:
        pass                                     # interpréteur Tk détruit


def _raise_to_front(app):
    """Remonte la fenêtre `app` au premier plan (appelé sur le thread Tk)."""
    try:
        app.deiconify()                          # au cas où minimisée
        app.lift()
        app.attributes("-topmost", True)         # force au-dessus, puis relâche
        app.after(250, lambda: app.attributes("-topmost", False))
        app.focus_force()
    except Exception:
        pass


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else None
    # Instance unique : si une fenêtre est déjà ouverte, on lui demande de
    # remonter au premier plan et on sort (Option X : pas de 2e fenêtre ; le
    # fichier demandé est ignoré). Couvre TOUTES les voies de lancement
    # (systray icône + menu « Ouvrir », CLI, lanceur .desktop).
    srv = _try_become_primary()
    if srv is None:
        # Une instance primaire tient le nom : lui demander de remonter, puis sortir.
        if _signal_existing():
            return
        # Échec de la connexion : l'instance primaire est probablement en train de
        # mourir (elle tient encore le nom mais ne répond plus). On retente de
        # devenir primaire quelques fois avant d'abandonner, pour qu'un clic sur le
        # lanceur juste après une fermeture ne produise PAS « rien du tout ».
        for _ in range(10):
            time.sleep(0.2)
            srv = _try_become_primary()
            if srv is not None:
                break
        if srv is None:
            return   # une autre instance a bel et bien repris le nom -> on sort
    app = MappingEditor(config_path)
    app._singleton_srv = srv                     # garder la référence vivante
    _start_singleton_listener(app, srv)
    app.mainloop()


if __name__ == "__main__":
    main()

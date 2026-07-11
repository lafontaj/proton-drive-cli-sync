#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test visuel des glyphes/symboles utilisés par le projet Proton Drive.

But : voir EXACTEMENT ce que rend TA police système (via Tkinter, le même moteur
que le GUI), afin de repérer les symboles qui s'affichent mal (carré, point
d'interrogation, « pattes de mouches », ou glyphe trompeur comme le ℹ rendu « i »).

Usage :
    python3 test_glyphes.py

Chaque symbole est affiché :
  - en grand (comme dans un bouton / titre),
  - en taille normale (comme dans le journal),
  - avec son nom Unicode et son code.

Repère ceux qui sont ABSENTS (carré □, croix, « ? ») ou TROMPEURS (méconnaissables),
et note leur code U+XXXX. Renvoie-moi la liste (ou une capture) : on remplacera
seulement ceux-là.
"""
__version__ = "1.0.0"   # version propre à CE fichier ; incrémentée quand il change (indépendant de GitHub)

import tkinter as tk
from tkinter import ttk
import unicodedata

# Inventaire complet (issu du balayage des sorties du projet), groupé par usage.
GROUPES = [
    ("Séparateurs / puces / flèches (attendus fiables)", [
        "─", "•", "→", "←", "↑", "↓",
    ]),
    ("Emojis confirmés OK (monochrome)", [
        "🗑", "⏳", "✅",
    ]),
    ("Emojis pictogrammes (même famille — à vérifier)", [
        "🔑", "💾", "🌐", "🌱", "📋", "🔓", "📂", "🌍", "📅", "⏰",
        "🧹", "🔄", "🔃",
    ]),
    ("Coches / croix / signes", [
        "✓", "✔", "✗", "✕", "❌", "➕", "➖", "⬆", "⛔", "🚫",
    ]),
    ("Contrôles média (ex-FE0F, maintenant nettoyés)", [
        "▶", "⏸", "⏹", "⏭", "⚡", "⚠",
    ]),
    ("Flèches circulaires (rendu souvent incertain)", [
        "↺", "⟲", "⟳", "🔄", "🔃",
    ]),
    ("Symboles techniques / maths (à vérifier)", [
        "⤫", "⊘", "⊗", "∈", "≤", "≥", "↪", "⟳", "ℹ",
    ]),
]


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Test des glyphes — Proton Drive")
        self.geometry("900x700")

        head = ttk.Frame(self, padding=10)
        head.pack(side="top", fill="x")
        ttk.Label(head, font=("", 12, "bold"),
                  text="Repère les symboles ABSENTS (carré, ?, croix) "
                       "ou MÉCONNAISSABLES.").pack(anchor="w")
        ttk.Label(head, foreground="#555",
                  text="Note leur code U+XXXX (colonne de droite) et renvoie la "
                       "liste. Compare la grande taille et la petite.").pack(anchor="w")

        # Zone défilante
        canvas = tk.Canvas(self, highlightthickness=0)
        vsb = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        for titre, glyphes in GROUPES:
            grp = ttk.LabelFrame(inner, text=titre, padding=8)
            grp.pack(side="top", fill="x", padx=10, pady=6)
            for ch in glyphes:
                row = ttk.Frame(grp)
                row.pack(side="top", fill="x", pady=1)
                # Grand (titre/bouton)
                tk.Label(row, text=ch, font=("", 20), width=3,
                         anchor="center").pack(side="left")
                # Normal (journal, monospace)
                tk.Label(row, text=ch, font=("monospace", 11), width=3,
                         anchor="center").pack(side="left")
                # Dans un bouton (comme la barre d'outils)
                ttk.Button(row, text=f"{ch} Bouton").pack(side="left", padx=6)
                # Nom + code
                try:
                    name = unicodedata.name(ch)
                except ValueError:
                    name = "(inconnu)"
                code = f"U+{ord(ch):04X}"
                tk.Label(row, text=f"{code}   {name}", font=("monospace", 9),
                         anchor="w").pack(side="left", padx=8)

        ttk.Button(self, text="Fermer", command=self.destroy).pack(pady=8)


if __name__ == "__main__":
    App().mainloop()

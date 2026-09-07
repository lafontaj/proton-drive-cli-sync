#!/usr/bin/env python3
"""Génère un PDF imprimable depuis un document Markdown du projet.
Réglages validés : corps 13 pt, interligne 1.5, DejaVu Sans ; emoji remplacés
par des équivalents imprimables (pastilles colorées, glyphes couverts).
Usage : python3 build_pdf.py SOURCE.md SORTIE.pdf [TITRE]"""
__version__ = "1.6.0"   # version propre à CE fichier ; incrémentée quand il change (indépendant de GitHub)
#
# 1.6.0 — les tableaux redeviennent coupables par un saut de page.
#
# Même défaut que celui traité en 1.5.0 pour les blocs de code, mais sur un
# tableau — et sans que la taille soit en cause : un tableau de CINQ lignes a
# vidé une page, sous deux titres restés seuls en haut. « page-break-inside:
# avoid » est absolu : dès que l'élément ne tient pas dans la place restante,
# il bascule entier, quelle que soit sa hauteur. Constaté le 7 septembre 2026,
# page 15 du guide de l'hôte, sous « Incus / Choisir la branche ».
#
# POURQUOI PAS DE SEUIL ICI, contrairement à 1.5.0. Pour un bloc de code la
# mesure est exacte — chasse fixe, largeur connue, LARGEUR_MAX déjà calibré —
# et le calcul est vérifiable. Pour un tableau, rien de tel : la largeur des
# colonnes dépend du contenu, la police est proportionnelle, l'enroulement des
# cellules est imprévisible. Le seuil serait fondé sur une estimation, et ce
# fichier a déjà montré ce que coûte une décision prise sur une mesure
# approximative. Un comportement simple et prévisible vaut mieux qu'un
# mécanisme subtil bâti sur une approximation.
#
# CE QU'ON PERD, et c'est assumé : les PETITS tableaux redeviennent coupables.
# Un tableau de trois lignes coupé après la deuxième est plus disgracieux qu'un
# tableau de dix coupé au milieu, qui reste lisible. C'est la contrepartie du
# choix, écrite ici pour qui voudrait remettre la règle — la remettre
# ramènerait les pages blanches.
#
# LA CAUSE DU TITRE ORPHELIN N'EST TOUJOURS PAS CORRIGÉE :
# « page-break-after: avoid » est mal appliqué par le moteur WebKit de
# wkhtmltopdf. Retirer « avoid » des tableaux MASQUE le symptôme, en faisant
# remonter du contenu derrière les titres ; cela ne le répare pas.
#
# 1.5.0 — un bloc de code plus haut qu'une demi-page ne repousse plus la page.
#
# 1.4.0 (c) avait posé « pre { page-break-inside: avoid } » pour qu'un bloc de
# commandes ne soit jamais coupé en deux. La règle est juste pour la quasi-
# totalité des blocs, mais elle est absolue : un bloc trop grand pour la place
# restante bascule ENTIER à la page suivante, laissant derrière lui une page
# aux trois quarts blanche, souvent précédée du titre resté seul en haut.
#
# Constaté le 7 septembre 2026 dans le guide de l'hôte : le bloc des huit
# partages Samba a vidé la page 8, sous le titre « Les partages ».
#
# Le CSS ne sait pas compter des lignes : les blocs sont donc mesurés à la
# génération et ceux qui dépassent HAUTEUR_MAX_BLOC reçoivent une classe qui
# les rend de nouveau coupables. Au-delà de cette taille, un bloc n'est plus
# une commande qu'on copie d'un trait mais un fichier de configuration — une
# coupure y est moins gênante qu'une page blanche.
#
# ⚠ La hauteur se mesure APRÈS ENROULEMENT, pas en nombre de lignes source.
# `pre` étant en pre-wrap, une ligne de 112 caractères en occupe deux à
# l'écran : compter les lignes du source sous-estime la hauteur réelle, et
# c'est justement sur les documents riches en commandes longues — ceux qui
# produisent le défaut — que l'écart est le plus grand. D'où le croisement
# avec LARGEUR_MAX, déjà présent pour le contrôle de largeur : les deux
# constantes décrivent la même boîte, l'une en largeur, l'autre en hauteur.
#
# Ce réglage ne corrige PAS la cause du titre orphelin :
# « page-break-after: avoid » est mal appliqué par le moteur WebKit de
# wkhtmltopdf. Il la rend sans conséquence, le début du bloc suivant désormais
# son titre. Le même symptôme reviendrait sur un TABLEAU trop haut : ceux-ci
# portent encore « page-break-inside: avoid » sans exception de taille, faute
# d'un cas réel pour l'éprouver.
#
# 1.4.2 — commentaire, aucun changement de comportement. Deux compléments à
# 1.4.1, qui n'avait pas fini le travail :
#   1. L'affirmation fausse figurait à DEUX endroits — le journal en tête et le
#      commentaire en ligne, près du retrait du sélecteur. 1.4.1 n'avait rattrapé
#      que le premier. Leçon : corriger une formulation, c'est la chercher
#      partout, pas seulement là où on l'a remarquée.
#   2. Une question laissée ouverte est tranchée : l'ordre est-il nécessaire ?
#      Non. Les deux ordres ont été comparés sur un document réel et donnent un
#      résultat identique au caractère près, aucune clé de REPL ne portant de
#      sélecteur. Le « AVANT » est une convention et un invariant, pas une
#      exigence de correction — et il impose au contraire une contrainte sur la
#      table, notée en corollaire à l'endroit concerné.
#
# 1.4.1 — correction d'un COMMENTAIRE de 1.4.0, aucun changement de
# comportement. Le point (a) affirmait que les clés de REPL écrites sans
# sélecteur « ne reconnaissaient pas » la forme avec sélecteur. C'est faux :
# str.replace cherche une SOUS-CHAÎNE, donc « ⏸️ ».replace("⏸", "||") se
# déclenche bel et bien. Le seul défaut était le sélecteur laissé derrière.
# Vérifié caractère par caractère sur les cinq clés concernées.
# La note est conservée plutôt qu'effacée : une explication fausse dans un
# fichier lu comme référence coûte plus cher qu'un journal un peu long.
#
# 1.4.0 — quatre corrections, dont trois mécaniques et une de goût.
#
# a) Le sélecteur de variante U+FE0F est retiré AVANT la table REPL. C'est lui,
#    et non le pictogramme, qui faisait échouer le rendu : « ⚠️ » n'est pas un
#    caractère mais deux — U+26A0 suivi de U+FE0F — et U+26A0 EST couvert par
#    DejaVu Sans, tout comme U+2139 (« ℹ »). Le sélecteur envoyait le moteur
#    chercher une police emoji absente.
#    Effet de bord bénéfique : plusieurs clés de REPL sont écrites sans le
#    sélecteur (« ⏸ », « ▶ », « ⏭ », « 🗑 »). Elles se déclenchaient bien sur la
#    forme AVEC sélecteur — str.replace cherche une sous-chaîne, pas un
#    caractère entier — mais laissaient le U+FE0F derrière, collé au
#    remplacement : « ⚠️ ».replace("⚠", "[!]") rendait « [!]️ », un sélecteur
#    orphelin et invisible. Le retrait préalable supprime ce résidu.
#    (Formulation corrigée en 1.4.1 ; la version 1.4.0 disait à tort que ces
#     clés ne se déclenchaient pas du tout.)
#
# b) Texte barré. python-markdown ne rend PAS « ~~texte~~ » sans extension :
#    les tildes sortaient littéralement dans le PDF. Constaté sur les lignes
#    de modules retirés de strategie-sauvegarde.md, qui devaient être rayées.
#
# c) Sauts de page. Un tableau ou un bloc de commandes pouvait être coupé en
#    deux, et un titre rester orphelin en bas de page.
#
# d) ⚠ ✔ ✘ ℹ à la place de [!] [OK] [X]. Tous couverts par DejaVu une fois
#    (a) appliqué. CHOIX DE GOÛT, réversible : voir la table REPL.
#
# 1.3.0 — les blocs de code passent de 10,5 pt à 9,5 pt, et un contrôle de
# largeur avertit à la génération quand une ligne de code va être enroulée.
# Motif : `pre` est en white-space: pre-wrap, donc une ligne trop longue est
# repliée sans aucun signe visible — et le point de repli devient un vrai
# retour à la ligne quand on copie la commande depuis le PDF. Constaté le
# 5 septembre 2026 : « incus export ... $(date +%F).tar.gz » collé en deux
# morceaux, le « +%F » exécuté comme une commande à part. Le contrôle a
# ensuite trouvé 24 lignes dans le même cas, réparties sur cinq documents.

import sys, subprocess, markdown

src, out = sys.argv[1], sys.argv[2]
title = sys.argv[3] if len(sys.argv) > 3 else out.rsplit(".", 1)[0]

text = open(src, encoding="utf-8").read()

# 1.4.0 (a) — retirer le sélecteur de variante emoji U+FE0F.
# Les clés de REPL sont écrites sans sélecteur. Elles se déclenchent bien sur la
# forme « pictogramme + U+FE0F » — str.replace cherche une sous-chaîne — mais
# laissent le sélecteur collé au remplacement, orphelin et invisible.
# Placé en tête par CONVENTION, pas par nécessité : vérifié en comparant les deux
# ordres sur un document réel, le retrait après la table donne un résultat
# identique au caractère près. L'intérêt est l'invariant — passé cette ligne, le
# texte ne porte plus aucun sélecteur et tout ce qui suit travaille sur une forme
# unique, y compris le comptage de largeur des blocs de code.
# Corollaire : ne jamais ajouter à REPL une clé PORTANT un sélecteur ; le retrait
# préalable la rendrait inopérante.
text = text.replace("️", "")

# Emoji -> équivalents imprimables (DejaVu ne couvre pas les emoji couleur).
REPL = {
    "🟢": '<span style="color:#2e9e3f">●</span>',
    "🟠": '<span style="color:#e08a00">●</span>',
    "🔴": '<span style="color:#d23b3b">●</span>',
    "➕": "+", "➖": "−", "🔄": "↻", "⟳": "↻",
    "⚡": "", "⏰": "", "🔓": "", "🌍": "", "📜": "", "📅": "",
    # ⏳ NE DOIT PAS disparaître : il porte du sens (état « à amorcer ») et,
    # entouré de gras dans la source, sa disparition laissait un `****` vide
    # que Markdown ne sait pas apparier — le gras déraillait sur plusieurs
    # paragraphes après. Constaté dans le PDF du 28 août.
    "⏳": "[...]",
    # 🚫 rendait ⊘, que la ligne suivante retransformait en « x » : double
    # substitution, le symbole se confondait avec celui de « dossier disparu ».
    # ⊘ (U+2298) est couvert par DejaVu Sans — on le garde tel quel.
    "🚫": "⊘", "🗑": "[corbeille]",
    # 1.4.0 (d) — ces trois-là sont désormais rendus par de vrais glyphes.
    # ✔ U+2714, ✘ U+2718 et ⚠ U+26A0 sont tous couverts par DejaVu Sans une
    # fois le sélecteur de variante retiré. Pour revenir aux crochets, remettre
    # simplement :  "✅": "[OK]", "❌": "[X]", "⚠": "[!]",
    "✅": "✔", "❌": "✘",
    # « ⚠ » et « ℹ » ne sont plus substitués du tout : ils s'impriment tels quels.
    "🧪": "", "🧹": "", "💾": "", "📂": "", "🔃": "", "🔎": "",
    "▶": ">", "⏭": "»", "⏸": "||", "⏹": "[stop]", "↪": "->",
    # ✓ donnait « OK », d'où des « OK ok » illisibles quand la source cite la
    # sortie réelle du logiciel. √ (U+221A) est couvert et se lit comme une coche.
    "✓": "√", "✗": "×", "🌐": "", "•": "•",
}
for k, v in REPL.items():
    text = text.replace(k, v)

# Filet : si une substitution vide a malgré tout laissé une emphase creuse,
# on la retire AVANT que Markdown ne tente de l'apparier.
#
# 1.2.0 — les deux filets étaient trop larges et abîmaient du texte valide :
#
#   \*\*\s*\*\*  visait le « **** » laissé par un emoji supprimé, mais \s*
#   accepte aussi UN espace : « **a** **b** » (deux gras voisins) devenait
#   « **ab** », les deux mots collés en un seul gras.
#
#   (?<!\*)\*\s*\*(?!\*)  visait l'italique creux « * * », mais \s* accepte
#   ZÉRO espace : l'expression matchait donc « ** » tout court et effaçait
#   CHAQUE délimiteur de gras du document, silencieusement. Constaté en
#   production : 794 marqueurs dans chaque README, zéro <strong> en sortie.
#
# Correctif : le premier ne retire qu'une suite d'exactement quatre
# astérisques ; le second exige au moins une espace entre les deux.
import re as _re
text = _re.sub(r"\*{4}", "", text)
text = _re.sub(r"(?<!\*)\*[ \t]+\*(?!\*)", "", text)

# ---------------------------------------------------------------------------
# Contrôle de largeur des blocs de code.
#
# `pre` est en white-space: pre-wrap : une ligne trop longue est ENROULÉE, sans
# aucun signe visible. À la copie depuis le PDF, le point d'enroulement devient
# un vrai retour à la ligne — et une commande shell coupée en plein milieu est
# collée en deux morceaux. Constaté le 5 septembre 2026 sur
# « incus export ... $(date +%F).tar.gz » (96 caractères), dont le « +%F »
# s'est retrouvé exécuté comme une commande à part.
#
# À 9,5 pt en DejaVu Sans Mono, sur 180 mm de justification moins la marge
# interne du bloc, il entre environ 86 caractères. Le seuil est fixé un peu
# en dessous.
LARGEUR_MAX = 84
_dans_bloc = False
_trop_longues = []
for _no, _ligne in enumerate(text.splitlines(), 1):
    if _ligne.startswith("```"):
        _dans_bloc = not _dans_bloc
        continue
    if _dans_bloc and len(_ligne) > LARGEUR_MAX:
        _trop_longues.append((_no, len(_ligne), _ligne))

if _trop_longues:
    print(f"[!] {len(_trop_longues)} ligne(s) de code dépassent {LARGEUR_MAX} "
          f"caractères et seront enroulées dans le PDF —\n"
          f"    le copier-coller les cassera. À raccourcir :", file=sys.stderr)
    for _no, _n, _ligne in _trop_longues:
        print(f"    ligne {_no} ({_n} car.) : {_ligne[:70]}…", file=sys.stderr)

# 1.4.0 (b) — texte barré. python-markdown ne connaît pas « ~~texte~~ » en
# standard : les tildes ressortaient littéralement. L'extension est facultative
# pour ne pas transformer une dépendance de confort en dépendance dure ; son
# absence est signalée, elle n'interrompt pas la génération.
_EXT = ["tables", "fenced_code"]
try:
    import pymdownx.tilde  # noqa: F401  (paquet pip « pymdown-extensions »)
    _EXT.append("pymdownx.tilde")
except ImportError:
    print("[i] pymdown-extensions absent : le texte barré (~~...~~) sortira "
          "avec ses tildes.\n    Installer avec : pip install pymdown-extensions",
          file=sys.stderr)

body = markdown.markdown(text, extensions=_EXT)

# ---------------------------------------------------------------------------
# 1.5.0 — rendre coupables les blocs de code trop hauts.
#
# À 9,5 pt et interligne 1,35, une ligne occupe environ 4,5 mm ; il en entre une
# soixantaine sur une page A4 moins ses marges. Un bloc de 30 lignes fait donc
# près d'une demi-page : s'il bascule, la page perdue reste acceptable. Au-delà,
# elle devient visible.
#
# La hauteur est comptée APRÈS ENROULEMENT — une ligne plus large que
# LARGEUR_MAX en occupe plusieurs à l'écran. Compter les lignes du source
# sous-estimerait les blocs riches en commandes longues, c'est-à-dire
# précisément ceux qui produisent le défaut.
HAUTEUR_MAX_BLOC = 30

import html as _html
import math as _math


def _hauteur_affichee(contenu):
    """Nombre de lignes qu'occupera ce contenu une fois enroulé à LARGEUR_MAX."""
    return sum(max(1, _math.ceil(len(ligne) / LARGEUR_MAX))
               for ligne in contenu.splitlines())


_blocs_longs = []


def _marquer_blocs_longs(m):
    # Le <pre> contient un <code> : retirer les balises et dé-échapper les
    # entités, sans quoi « &lt; » compterait pour quatre caractères au lieu d'un.
    texte_brut = _html.unescape(_re.sub(r"<[^>]+>", "", m.group(1)))
    hauteur = _hauteur_affichee(texte_brut)
    if hauteur > HAUTEUR_MAX_BLOC:
        _blocs_longs.append(hauteur)
        return f'<pre class="long">{m.group(1)}</pre>'
    return m.group(0)


body = _re.sub(r"<pre>(.*?)</pre>", _marquer_blocs_longs, body, flags=_re.S)

if _blocs_longs:
    print(f"[i] {len(_blocs_longs)} bloc(s) de code dépassent "
          f"{HAUTEUR_MAX_BLOC} lignes une fois enroulés "
          f"({', '.join(str(n) for n in _blocs_longs)}) —\n"
          f"    ils pourront être coupés par un saut de page, plutôt que de "
          f"laisser une page blanche.", file=sys.stderr)

# Les images sont référencées RELATIVEMENT au document source (docs/images/…).
# Le HTML intermédiaire étant écrit dans /tmp, ces chemins n'y menaient nulle
# part : les quatre captures du README sortaient en cadres vides. Une balise
# <base> pointant sur le dossier du source rétablit la résolution.
import os as _os
_base = _os.path.dirname(_os.path.abspath(src)) + _os.sep

html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<base href="file://{_base}">
<style>
body {{ font-family: "DejaVu Sans", sans-serif; font-size: 13pt;
       line-height: 1.5; color: #1a1a1a; }}
h1 {{ font-size: 21pt; border-bottom: 2px solid #444; padding-bottom: 4px; }}
h2 {{ font-size: 17pt; border-bottom: 1px solid #999; padding-bottom: 3px;
      margin-top: 26px; }}
h3 {{ font-size: 14.5pt; margin-top: 20px; }}
/* 1.4.0 (c) — un titre ne reste pas seul en bas de page, un tableau ou un bloc
   de commandes n'est pas coupé en deux par un saut de page. */
h1, h2, h3, h4 {{ page-break-after: avoid; }}
code {{ font-family: "DejaVu Sans Mono", monospace; font-size: 11pt;
        background: #f2f2f2; padding: 1px 4px; border-radius: 3px; }}
pre {{ background: #f2f2f2; padding: 10px; border-radius: 4px;
       font-size: 9.5pt; line-height: 1.35; white-space: pre-wrap;
       page-break-inside: avoid; }}
/* 1.5.0 — au-delà de HAUTEUR_MAX_BLOC lignes affichées, un bloc redevient
   coupable : le garder entier le ferait basculer d'un seul tenant et
   laisserait une page blanche derrière lui. */
pre.long {{ page-break-inside: auto; }}
pre code {{ background: none; padding: 0; }}
/* 1.6.0 — pas de « page-break-inside: avoid » ici : il faisait basculer un
   tableau entier, même petit, dès qu'il ne tenait pas dans la place restante,
   et laissait une page blanche derrière lui. Contrepartie assumée : un petit
   tableau peut désormais être coupé. Voir le journal en tête. */
table {{ border-collapse: collapse; width: 100%; font-size: 11.5pt; }}
del {{ color: #777; }}
th, td {{ border: 1px solid #999; padding: 5px 8px; text-align: left; }}
th {{ background: #e8e8e8; }}
blockquote {{ border-left: 4px solid #bbb; margin-left: 0; padding-left: 12px;
              color: #444; }}
li {{ margin-bottom: 4px; }}
img {{ max-width: 100%; height: auto; border: 1px solid #ccc; }}
</style></head><body>{body}</body></html>"""

open("/tmp/doc.html", "w", encoding="utf-8").write(html)
r = subprocess.run(["wkhtmltopdf", "--encoding", "utf-8", "--enable-local-file-access",
                    "--margin-top", "16mm", "--margin-bottom", "16mm",
                    "--margin-left", "15mm", "--margin-right", "15mm",
                    "--quiet", "/tmp/doc.html", out])
sys.exit(r.returncode)

#!/usr/bin/env python3
"""Génère un PDF imprimable depuis un document Markdown du projet.
Réglages validés : corps 13 pt, interligne 1.5, DejaVu Sans.
Les symboles du document sont imprimés TELS QUELS ; l'outil se contente de
signaler ceux qu'aucune police disponible ne sait dessiner.
Usage : python3 build_pdf.py SOURCE.md SORTIE.pdf [TITRE]"""
__version__ = "1.8.0"   # version propre à CE fichier ; incrémentée quand il change (indépendant de GitHub)
#
# 1.8.0 — la table de substitution est SUPPRIMÉE, remplacée par un contrôle.
#
# Ce que la table faisait, mesuré le 25 septembre 2026 sur les 69 symboles que
# le logiciel emploie réellement : elle en altérait 32. Et pas seulement en
# apparence —
#
#   * elle FUSIONNAIT des signes que le logiciel distingue : « 🔄 », « ⟳ » et
#     « ↻ » devenaient tous « ↻ », et « 🚫 » devenait « ⊘ » alors que « ⊘ » est
#     déjà employé ailleurs par le moteur pour autre chose. La documentation
#     confondait ce que le logiciel sépare ;
#   * elle EFFAÇAIT onze pictogrammes de noms de boutons — « ⏰ Planification… »
#     s'imprimait « Planification… », avec le trou laissé par le caractère
#     disparu. Le lecteur cherchait à l'écran un bouton portant un autre nom.
#
# La justification d'origine était que « DejaVu ne couvre pas les emoji
# couleur ». Elle est fausse dans le cas général : wkhtmltopdf ne sait pas lire
# une police emoji EN COULEUR (des images, pas des dessins de lettres), mais
# fontconfig se rabat alors sur les polices monochromes du système — FreeSans,
# FreeSerif, Noto Sans Symbols2, Symbola — que le moteur rend sans difficulté.
# Vérifié par la mesure : 67 des 69 symboles s'impriment sans aucune table.
#
# À LA PLACE, un CONTRÔLE DE COUVERTURE. Avant la conversion, chaque caractère
# du document est confronté aux polices réellement installées. Celui qu'aucune
# ne couvre est SIGNALÉ — avec son code, son nom et sa ligne — et rien d'autre :
# il n'est ni remplacé ni effacé. Le PDF est fabriqué quand même.
#
# C'est plus sûr que la table pour trois raisons : le contrôle couvre TOUS les
# caractères et pas seulement ceux qu'on avait pensé à lister ; il ne peut pas
# faire converger deux signes distincts ; et il ne peut plus rien faire
# disparaître en silence — le défaut qui a lancé toute cette révision.
#
# LIMITE CONNUE, à ne pas oublier : une police emoji EN COULEUR couvre le
# caractère du point de vue de fontconfig, mais pas du point de vue de
# wkhtmltopdf. Les familles purement bitmap sont donc écartées du contrôle
# (POLICES_COULEUR ci-dessous), sans quoi il annoncerait « couvert » pour un
# caractère qui sortira vide. C'est exactement le cas de « 🧹 » et « 🧪 », les
# deux seuls symboles du logiciel que la chaîne ne sait pas rendre.
#
# LE HTML INTERMÉDIAIRE N'EST PLUS DANS /tmp. Il s'écrit à côté du PDF, sous le
# nom de la sortie suivi de « .html », et il est effacé dès que le PDF est fait.
# Il n'est CONSERVÉ qu'en cas d'échec, et le programme dit alors où le trouver.
# L'ancien chemin en dur « /tmp/doc.html » avait trois défauts : un nom fixe,
# donc deux générations simultanées s'écrasaient l'une l'autre sans un mot ; le
# fichier n'était jamais effacé ; et, appartenant au premier utilisateur qui
# l'avait créé, il rendait la génération impossible à tout autre
# (« Permission denied », constaté le 25 septembre 2026).
#
# 1.7.0 — la réparation des emphases mutilait les lignes de cron.
#
# Une ligne « 00 04 * * * root /chemin/script » dans un bloc de code sortait
# imprimée « 00 04 * root » : le filet anti-italique-creux, qui retire « * * »,
# ne savait pas qu'il traversait du code. Deux astérisques disparus, aucun
# message — et une ligne de cron recopiée depuis ce PDF aurait planifié la
# tâche à un tout autre moment. Constaté le 8 septembre 2026 dans le guide de
# sauvegarde, sur la tâche d'image de la clé d'amorçage.
#
# C'est la TROISIÈME régression de ce même filet (voir 1.2.0). Les deux
# premières venaient de motifs trop larges ; celle-ci vient du champ
# d'application. Corriger encore le motif n'aurait rien réglé : « * * » EST
# une emphase creuse en prose et NE L'EST PAS en code — aucune expression
# régulière ne peut trancher sans savoir où elle se trouve.
#
# Correctif : découper d'abord le document en prose et en code — blocs ``` et
# segments `entre accents graves` —, n'appliquer les filets qu'à la prose.
# Le code ressort octet pour octet.
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


import sys, os, subprocess, unicodedata, markdown
import re as _re
import html as _html
import math as _math

src, out = sys.argv[1], sys.argv[2]
title = sys.argv[3] if len(sys.argv) > 3 else out.rsplit(".", 1)[0]

text = open(src, encoding="utf-8").read()

# 1.4.0 (a) — retirer le sélecteur de variante emoji U+FE0F.
# « ⚠️ » n'est pas un caractère mais deux : U+26A0 suivi de U+FE0F. C'est le
# sélecteur, et non le pictogramme, qui envoyait le moteur chercher une police
# emoji absente. Passé cette ligne, le texte ne porte plus aucun sélecteur et
# tout ce qui suit travaille sur une forme unique — y compris le contrôle de
# couverture et le comptage de largeur des blocs de code.
text = text.replace("️", "")

# ---------------------------------------------------------------------------
# CONTRÔLE DE COUVERTURE DES CARACTÈRES (1.8.0)
#
# On demande à fontconfig — le même mécanisme que wkhtmltopdf interroge — quelle
# police couvre chaque symbole du document. Celui qu'aucune ne couvre sortira
# VIDE dans le PDF, sans le moindre signe. On le signale ici, nommément.
#
# Portée : seuls les caractères au-dessus de U+2000 sont contrôlés. En dessous,
# c'est l'alphabet latin, les accents et la ponctuation courante, que DejaVu
# Sans couvre intégralement ; les contrôler ferait 70 appels de plus pour rien.
#
# Les familles ci-dessous stockent des IMAGES en couleur plutôt que des dessins
# de lettres. fontconfig les compte comme couvrantes, wkhtmltopdf ne sait pas
# les lire : les inclure ferait dire « couvert » pour un caractère qui sortira
# vide. On les écarte donc du contrôle.
POLICES_COULEUR = ("Noto Color Emoji", "Apple Color Emoji", "Segoe UI Emoji",
                   "Twemoji Mozilla", "EmojiOne Color", "JoyPixels")

SEUIL_CONTROLE = 0x2000


def _polices_couvrant(caractere):
    """Familles non-couleur couvrant ce caractère, d'après fontconfig."""
    try:
        r = subprocess.run(["fc-list", ":charset=%04X" % ord(caractere), "family"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None          # fc-list indisponible : contrôle impossible
    familles = set()
    for ligne in r.stdout.splitlines():
        for nom in ligne.split(","):
            nom = nom.strip()
            if nom and nom not in POLICES_COULEUR:
                familles.add(nom)
    return familles


def _controler_couverture(contenu):
    """Signale les caractères qu'aucune police disponible ne sait dessiner.

    Ne remplace rien et n'efface rien : le PDF est fabriqué quand même, avec
    des trous à ces endroits-là. C'est à l'auteur de décider quoi en faire."""
    premieres_lignes = {}
    for no, ligne in enumerate(contenu.splitlines(), 1):
        for ch in ligne:
            if ord(ch) > SEUIL_CONTROLE and ch not in premieres_lignes:
                premieres_lignes[ch] = no
    if not premieres_lignes:
        return

    absents = []
    for ch in sorted(premieres_lignes, key=lambda c: premieres_lignes[c]):
        familles = _polices_couvrant(ch)
        if familles is None:
            print("[i] fc-list introuvable : impossible de vérifier que les "
                  "symboles du document seront rendus.", file=sys.stderr)
            return
        if not familles:
            absents.append((ch, premieres_lignes[ch]))

    if not absents:
        return
    print("[!] %d caractère(s) ne seront PAS dessinés dans le PDF : aucune "
          "police installée\n    ne les couvre. Ils y laisseront un blanc, sans "
          "autre signe. À remplacer\n    dans le document, ou à couvrir en "
          "installant une police adéquate :" % len(absents), file=sys.stderr)
    for ch, no in absents:
        try:
            nom = unicodedata.name(ch)
        except ValueError:
            nom = "(sans nom)"
        print("    ligne %-5d U+%05X  %s" % (no, ord(ch), nom), file=sys.stderr)


_controler_couverture(text)

# ---------------------------------------------------------------------------
# Filet : si une substitution a laissé une emphase creuse, on la retire AVANT
# que Markdown ne tente de l'apparier.
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
#
# 1.7.0 — et surtout : les deux filets NE TRAVERSENT PLUS LES BLOCS DE CODE.
#
# 1.8.0 — la table de substitution ayant disparu, ces filets ne rattrapent plus
# ses dégâts. Ils sont CONSERVÉS parce qu'un « **** » ou un « * * » peut venir
# du document lui-même, et parce qu'ils ne coûtent rien.
_MOTIFS_EMPHASE = (r"\*{4}", r"(?<!\*)\*[ \t]+\*(?!\*)")


def _reparer_emphases(fragment):
    for motif in _MOTIFS_EMPHASE:
        fragment = _re.sub(motif, "", fragment)
    return fragment


# Découpe le document en alternance « prose / code ». Le motif capture, dans
# l'ordre, les blocs délimités par ``` et les segments `entre accents graves`.
# re.split conserve les délimiteurs capturés, donc les morceaux d'indice impair
# sont exactement le code — qu'on laisse intact.
_SEPARATEUR = _re.compile(r"(^```[^\n]*\n.*?^```[^\n]*$|`[^`\n]+`)", _re.S | _re.M)
_morceaux = _SEPARATEUR.split(text)
text = "".join(m if i % 2 else _reparer_emphases(m) for i, m in enumerate(_morceaux))

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
# Une balise <base> pointant sur le dossier du source rétablit la résolution
# quel que soit l'endroit où le HTML intermédiaire est écrit.
_base = os.path.dirname(os.path.abspath(src)) + os.sep

html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>{title}</title>
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

# ---------------------------------------------------------------------------
# 1.8.0 — le HTML intermédiaire s'écrit à côté du PDF, sous le nom de la sortie
# suivi de « .html ». Deux sorties différentes ne peuvent donc plus s'écraser,
# et le fichier n'appartient jamais à quelqu'un d'autre. Il est effacé dès que
# le PDF est fait, et CONSERVÉ en cas d'échec — c'est là qu'on va regarder.
chemin_html = out + ".html"
open(chemin_html, "w", encoding="utf-8").write(html)

r = subprocess.run(["wkhtmltopdf", "--encoding", "utf-8", "--enable-local-file-access",
                    "--margin-top", "16mm", "--margin-bottom", "16mm",
                    "--margin-left", "15mm", "--margin-right", "15mm",
                    "--quiet", chemin_html, out])

if r.returncode == 0:
    try:
        os.remove(chemin_html)
    except OSError:
        pass
else:
    print("[!] wkhtmltopdf a échoué (code %d). Le HTML intermédiaire est "
          "conservé pour\n    examen : %s" % (r.returncode, chemin_html),
          file=sys.stderr)

sys.exit(r.returncode)

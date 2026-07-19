#!/bin/bash
# build_locales.sh — Development tool: (re)compile the translation catalogs.
#
# NEVER required in production (neither on the local machine nor on the NAS):
# the compiled .mo files are shipped alongside the .po sources. Run this only
# after editing a .po file (requires the 'gettext' package: sudo apt install gettext).
#
# Usage:  ./build_locales.sh          # normalize, then compile all catalogs
#         ./build_locales.sh --pot    # also refresh the extraction template
#
# Note: the template is extracted WITHOUT source locations ("#: file:line"), so
# that catalog diffs stay small and readable. When merging the template into the
# translations, keep that convention:
#     msgmerge --update --no-location --backup=none <po> locale/proton-sync.pot
set -e
cd "$(dirname "$0")"

# Every module that calls _(). Keep in sync with the source tree — the coverage
# check at the end of this script exists precisely because forgetting a file
# here fails SILENTLY: xgettext cannot complain about a file it was never given.
# That is how the NAS self-test verdicts stayed untranslated for several
# releases, in every language, with nothing to signal it.
TRANSLATED_FILES="i18n.py config.py proton_sync.py realtime_consumer.py \
local_watcher.py nas_watcher.py schedule_manager.py realtime_manager.py \
proton_mapping_editor.py mount_check.py tray_indicator.py nas_selftest.py"

if [ "$1" = "--pot" ]; then
    echo "Extraction du modèle locale/proton-sync.pot..."
    # --no-location : pas de commentaires « #: fichier:ligne ». Ces références
    # se décalent dès qu'on ajoute ou retire une ligne de code, ce qui produisait
    # d'énormes diffs .po (des milliers de lignes) sans qu'aucune traduction ne
    # change. Sans elles, les diffs ne montrent plus que les vrais changements.
    xgettext --language=Python --keyword=_ --from-code=UTF-8 \
        --no-location \
        --package-name="proton-sync" \
        -o locale/proton-sync.pot \
        $TRANSLATED_FILES 2>/dev/null
    # xgettext laisse « charset=CHARSET » dans l'en-tête ; on le fixe ici pour
    # que le modèle soit directement valide (évitait une retouche manuelle).
    sed -i 's/charset=CHARSET/charset=UTF-8/' locale/proton-sync.pot
fi

# ── Normalisation de la mise en forme ───────────────────────────────────────
# msgcat replie les chaînes à 78 colonnes, comme msgmerge. Sans ce passage, un
# catalogue produit par un autre outil (script maison, éditeur de traduction)
# peut être fonctionnellement correct tout en écrivant une entrée par ligne :
# CHAQUE entrée apparaît alors modifiée et le diff enfle de plusieurs milliers
# de lignes pour quelques chaînes ajoutées. L'opération est idempotente — un
# fichier déjà canonique ressort identique — mais elle réécrit les .po en place,
# donc `git status` peut signaler des fichiers modifiés après un simple build.
# --no-location : même convention que l'extraction ci-dessus.
echo "Normalisation de la mise en forme des catalogues..."
for f in locale/proton-sync.pot locale/*/LC_MESSAGES/*.po; do
    [ -f "$f" ] || continue
    msgcat --no-location "$f" -o "$f.tmp"
    mv "$f.tmp" "$f"
done

# ── Compilation ─────────────────────────────────────────────────────────────
# --check refuse un catalogue mal formé (séquence d'échappement invalide,
# placeholder incohérent). À ne jamais retirer : un contrôle équivalent côté
# Python est trompeur, l'analyseur de gettext en Python tolère des séquences
# que msgfmt rejette — un .po cassé peut donc passer un test Python et
# empêcher toute régénération des .mo sans qu'on le remarque.
for po in locale/*/LC_MESSAGES/*.po; do
    [ -f "$po" ] || continue
    mo="${po%.po}.mo"
    msgfmt --check -o "$mo" "$po"
    echo "✓ $mo"
done
echo "Terminé."

# ── Garde-fou : un module traduit absent de la liste ────────────────────────
# Détection par AST plutôt que par grep : seuls les VRAIS appels _("…") comptent,
# donc pas de faux positif sur un fichier qui contiendrait la suite « _( » dans
# un autre contexte. Avertit sans bloquer, mais APRÈS le « Terminé. » pour que
# le message ne se noie pas dans la liste des ✓.
MISSING=$(python3 - "$TRANSLATED_FILES" <<'PY'
import ast, os, sys
listed = set(sys.argv[1].split())
missing = []
for fn in sorted(f for f in os.listdir(".") if f.endswith(".py")):
    if fn in listed:
        continue
    try:
        tree = ast.parse(open(fn, encoding="utf-8").read())
    except (OSError, SyntaxError):
        continue
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_" and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            missing.append(fn)
            break
print(" ".join(missing))
PY
)
if [ -n "$MISSING" ]; then
    echo
    echo "⚠  ATTENTION — module(s) traduit(s) absent(s) de TRANSLATED_FILES :"
    for f in $MISSING; do echo "     • $f"; done
    echo "   Leurs chaînes ne sont PAS extraites : elles resteront en anglais"
    echo "   dans toutes les langues, sans aucun autre signe."
    echo "   Ajoutez-les à TRANSLATED_FILES en haut de ce script, puis relancez"
    echo "   ./build_locales.sh --pot"
fi

#!/bin/bash
# build_locales.sh — Development tool: (re)compile the translation catalogs.
#
# NEVER required in production (neither on the local machine nor on the NAS):
# the compiled .mo files are shipped alongside the .po sources. Run this only
# after editing a .po file (requires the 'gettext' package: sudo apt install gettext).
#
# Usage:  ./build_locales.sh          # compile all locale/*/LC_MESSAGES/*.po
#         ./build_locales.sh --pot    # also refresh the extraction template
#
# Note: the template is extracted WITHOUT source locations ("#: file:line"), so
# that catalog diffs stay small and readable. When merging the template into the
# translations, keep that convention:
#     msgmerge --update --no-location --backup=none <po> locale/proton-sync.pot
set -e
cd "$(dirname "$0")"

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
        i18n.py config.py proton_sync.py realtime_consumer.py local_watcher.py \
        nas_watcher.py schedule_manager.py realtime_manager.py \
        proton_mapping_editor.py mount_check.py tray_indicator.py 2>/dev/null
    # xgettext laisse « charset=CHARSET » dans l'en-tête ; on le fixe ici pour
    # que le modèle soit directement valide (évitait une retouche manuelle).
    sed -i 's/charset=CHARSET/charset=UTF-8/' locale/proton-sync.pot
fi

for po in locale/*/LC_MESSAGES/*.po; do
    [ -f "$po" ] || continue
    mo="${po%.po}.mo"
    msgfmt --check -o "$mo" "$po"
    echo "✓ $mo"
done
echo "Terminé."

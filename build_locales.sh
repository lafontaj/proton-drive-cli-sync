#!/bin/bash
# build_locales.sh — Development tool: (re)compile the translation catalogs.
#
# NEVER required in production (neither on the local machine nor on the NAS):
# the compiled .mo files are shipped alongside the .po sources. Run this only
# after editing a .po file (requires the 'gettext' package: sudo apt install gettext).
#
# Usage:  ./build_locales.sh          # compile all locale/*/LC_MESSAGES/*.po
#         ./build_locales.sh --pot    # also refresh the extraction template
set -e
cd "$(dirname "$0")"

if [ "$1" = "--pot" ]; then
    echo "Extraction du modèle locale/proton-sync.pot..."
    xgettext --language=Python --keyword=_ --from-code=UTF-8 \
        --package-name="proton-sync" \
        -o locale/proton-sync.pot \
        i18n.py config.py proton_sync.py realtime_consumer.py local_watcher.py \
        nas_watcher.py schedule_manager.py realtime_manager.py \
        proton_mapping_editor.py mount_check.py tray_indicator.py 2>/dev/null
fi

for po in locale/*/LC_MESSAGES/*.po; do
    [ -f "$po" ] || continue
    mo="${po%.po}.mo"
    msgfmt --check -o "$mo" "$po"
    echo "✓ $mo"
done
echo "Terminé."

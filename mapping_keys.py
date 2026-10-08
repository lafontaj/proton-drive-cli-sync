"""Pure helpers for mapping dictionaries (no GUI imports, testable without a display)."""
__version__ = "1.1.0"   # version propre à CE fichier ; incrémentée quand il change (indépendant de GitHub)

# Keys the mapping dialog builds itself on every save. A key in this set that the
# dialog leaves out was deliberately switched off by the user and must NOT come back.
# excluded_remote est édité par la case du dialogue : décochée, la clé ne revient pas.
# max_delete_* aussi : un champ vide veut dire « réglage global », donc on n'y
# recopie PAS l'ancienne surcharge.
DIALOG_KEYS = frozenset({
    "type", "source", "dest_parent", "exclusions",
    "conflict_mode", "allow_delete", "delete_mode", "source_kind",
    "excluded_remote", "max_delete_min", "max_delete_ratio",
})


def carry_unknown_keys(old, new):
    """Copy to `new` every key of `old` that the dialog does not manage
    (future keys...). Returns `new`. Keys already present in `new` are never
    overwritten. excluded_remote is a dialog key: an unchecked box must not
    come back from the previous mapping."""
    if not old:
        return new
    for key, value in old.items():
        if key not in DIALOG_KEYS and key not in new:
            new[key] = value
    return new

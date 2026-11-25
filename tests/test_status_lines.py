"""Lines shown in the window's default view.

The default view keeps only status lines, recognised by how they start. A
mapping summary that is not an error but must be seen starts with "ℹ": the
symbolic links left aside, for instance. It used to stay in the log only.
"""


def _line_is_status():
    import pathlib
    source = (pathlib.Path(__file__).resolve().parents[1]
              / "proton_mapping_editor.py").read_text(encoding="utf-8")
    start = source.index("_EDITOR_STATUS_PREFIXES = (")
    end = source.index("\ndef ", source.index("def line_is_status(") + 10)
    namespace = {}
    exec(source[start:end], namespace)
    return namespace["line_is_status"]


def test_the_symbolic_link_summary_is_shown_by_default():
    is_status = _line_is_status()
    for line in ("ℹ  2 symbolic link(s) not sent: the Proton CLI does not upload "
                 "links (details in verbose mode).",
                 "ℹ  2 lien(s) symbolique(s) non envoyé(s) : le CLI Proton "
                 "n'envoie pas les liens (détail en mode « Détaillé »)."):
        assert is_status(line), line


def test_the_detail_of_each_link_stays_in_verbose_mode():
    is_status = _line_is_status()
    assert not is_status("⏭  symbolic link not sent: /a/l.txt → /a/r.txt")
    assert not is_status("⏭  lien symbolique non envoyé : /a/l.txt → /a/r.txt")

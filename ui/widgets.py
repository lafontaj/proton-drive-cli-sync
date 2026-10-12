"""Boîtes et boutons partagés. Les textes viennent de _()."""

try:
    from i18n import _
except ImportError:
    def _(s):
        return s


def qt_parent(parent):
    """QWidget réel. La fenêtre est une enveloppe : son widget est ``_qt``."""
    inner = getattr(parent, "_qt", None)
    return inner if inner is not None else parent


def info(parent, text, title):
    _notice(parent, title, text, _("OK"))


def warn(parent, text, title):
    _notice(parent, title, text, _("OK"))


def error(parent, text, title):
    _notice(parent, title, text, _("OK"))


def confirm(parent, text, title, ok_text, cancel_text):
    """True si l'utilisateur confirme."""
    return _notice(parent, title, text, ok_text, cancel_text)


def confirm_check(parent, text, title, ok_text, cancel_text, check_text,
                  checked=False):
    """(accepté, case cochée). ``checked`` est l'état initial de la case."""
    return _notice(parent, title, text, ok_text, cancel_text, check_text, checked)


def _notice(parent, title, text, ok_text, cancel_text=None, check_text=None,
            checked=False):
    """Dialogue au même chrome que les pages. Retourne un bool, ou (bool, bool)."""
    from PySide6.QtWidgets import QCheckBox, QDialog, QLabel

    dlg, _body, content, buttons = dialog(parent, title)
    dlg.setMinimumWidth(440)
    label = QLabel(text or "")
    label.setWordWrap(True)
    content.addWidget(label)
    box = None
    if check_text:
        box = QCheckBox(check_text)
        box.setChecked(bool(checked))
        content.addWidget(box)
    if cancel_text:
        action(buttons, cancel_text, dlg.reject)
    action(buttons, ok_text, dlg.accept, primary=True)
    accepted = dlg.exec() == QDialog.Accepted
    if check_text:
        return accepted, bool(box.isChecked()) if box is not None else False
    return accepted


def card(parent):
    from PySide6.QtWidgets import QFrame, QVBoxLayout
    frame = QFrame(parent)
    frame.setObjectName("Card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(16, 14, 16, 14)
    layout.setSpacing(8)
    return frame, layout


def section(parent, title):
    """Carte dont le premier libellé reprend le style Title.

    ``frame.title_label`` permet de changer le titre après coup (temps réel).
    """
    from PySide6.QtWidgets import QLabel
    frame, layout = card(parent)
    label = QLabel(title)
    label.setObjectName("Title")
    label.setWordWrap(True)
    layout.addWidget(label)
    frame.title_label = label
    return frame, layout


def scroll_page(host):
    """(zone, widget interne, layout). La page défile au lieu d'être coupée."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QScrollArea, QVBoxLayout, QWidget
    scroll = QScrollArea(host)
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QScrollArea.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    inner = QWidget()
    inner.setObjectName("PageHost")
    layout = QVBoxLayout(inner)
    layout.setContentsMargins(16, 16, 16, 16)
    layout.setSpacing(12)
    scroll.setWidget(inner)
    outer = QVBoxLayout(host)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.addWidget(scroll)
    return scroll, inner, layout


def dialog(parent, title, scroll=False):
    """(dialogue, corps, layout de contenu, rangée de boutons).

    L'action principale se pose avec ``action(..., primary=True)``. Annuler
    reste un bouton normal. ``scroll`` garde la rangée de boutons visible.
    """
    from PySide6.QtWidgets import QDialog, QHBoxLayout, QScrollArea, QVBoxLayout, QWidget
    dlg = QDialog(qt_parent(parent))
    dlg.setWindowTitle(title or "")
    root = QVBoxLayout(dlg)
    root.setContentsMargins(16, 16, 16, 16)
    root.setSpacing(12)
    body = QWidget()
    content = QVBoxLayout(body)
    content.setContentsMargins(0, 0, 0, 0)
    content.setSpacing(12)
    if scroll:
        area = QScrollArea(dlg)
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.NoFrame)
        body.setObjectName("PageHost")
        area.setWidget(body)
        root.addWidget(area, 1)
    else:
        root.addWidget(body, 1)
    buttons = QHBoxLayout()
    buttons.setSpacing(8)
    buttons.addStretch(1)
    root.addLayout(buttons)
    return dlg, body, content, buttons


def action(buttons, text, slot, primary=False):
    """Bouton ajouté à droite de la rangée. Le primaire valide avec Entrée."""
    from PySide6.QtWidgets import QPushButton
    button = QPushButton(text)
    if primary:
        button.setObjectName("Primary")
        button.setDefault(True)
    button.clicked.connect(slot)
    buttons.addWidget(button)
    return button


def hline_buttons(*buttons):
    from PySide6.QtWidgets import QHBoxLayout, QWidget
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    for button in buttons:
        layout.addWidget(button)
    layout.addStretch(1)
    return row

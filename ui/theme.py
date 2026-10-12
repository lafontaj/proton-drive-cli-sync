"""Jetons visuels. Inspirés de l'identité Proton, sans logo ni marque officielle."""

COLORS = {
    "purple": "#6D4AFF",
    "purple_hover": "#5B3CE0",
    "text": "#1C1B24",
    "muted": "#5C5A66",
    "canvas": "#F5F4FA",
    "card": "#FFFFFF",
    "border": "#E4E1EE",
    "danger": "#D2294B",
    "warning": "#E8A200",
    "success": "#1A9E57",
}

STYLESHEET = """
QMainWindow, QWidget#AppRoot, QWidget#Sidebar, QWidget#PageHost {
    background: %(canvas)s;
    color: %(text)s;
}
QFrame#Card {
    background: %(card)s;
    border: 1px solid %(border)s;
    border-radius: 8px;
}
QFrame#TopBar {
    background: %(card)s;
    border: none;
    border-bottom: 1px solid %(border)s;
}
QLabel {
    color: %(text)s;
    background: transparent;
}
QLabel#Muted, QLabel#Footer {
    color: %(muted)s;
}
QLabel#Title {
    font-size: 18px;
}
QLabel#AccountOk {
    color: %(success)s;
}
QLabel#SignedIn {
    color: %(success)s;
    font-weight: 600;
}
QLabel#AccountBad {
    color: %(danger)s;
}
QLabel#AccountWait {
    color: %(muted)s;
}
QPushButton {
    border-radius: 8px;
    padding: 8px 14px;
    margin: 0px;
    background: %(card)s;
    color: %(text)s;
    border: 1px solid %(border)s;
}
QPushButton:hover {
    background: %(canvas)s;
}
QPushButton#Primary {
    background: %(purple)s;
    color: white;
    border: none;
    padding: 8px 18px;
}
QPushButton#Primary:hover {
    background: %(purple_hover)s;
}
QPushButton#Primary:disabled {
    background: #C4B8F5;
    color: white;
}
QPushButton#Danger {
    background: %(card)s;
    color: %(danger)s;
    border: 1px solid %(danger)s;
}
QPushButton#Danger:hover {
    background: #FDECEF;
}
QPushButton#Nav {
    text-align: left;
    border: none;
    border-radius: 8px;
    padding: 10px 14px;
    background: transparent;
}
QPushButton#Nav:checked {
    background: %(card)s;
    color: %(purple)s;
}
QDialog {
    background: %(canvas)s;
    color: %(text)s;
}
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox {
    background: %(card)s;
    color: %(text)s;
    border: 1px solid %(border)s;
    border-radius: 8px;
    padding: 6px 8px;
}
QComboBox {
    background: %(card)s;
    color: %(text)s;
    border: 1px solid %(border)s;
    border-radius: 8px;
    padding: 8px 12px;
    padding-right: 36px;
    min-height: 22px;
    combobox-popup: 0;
}
QComboBox:hover, QComboBox:focus, QComboBox:on {
    border: 1px solid %(purple)s;
}
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: 32px;
    border: none;
    background: transparent;
}
QComboBox::down-arrow {
    image: url(__CHEVRON__);
    width: 14px;
    height: 14px;
}
QComboBox QAbstractItemView {
    background: %(card)s;
    color: %(text)s;
    border: 1px solid %(border)s;
    border-radius: 8px;
    padding: 4px;
    outline: 0;
}
QComboBox QAbstractItemView::item {
    min-height: 28px;
    padding: 4px 10px;
    border-radius: 6px;
}
QComboBox QAbstractItemView::item:hover {
    background: %(canvas)s;
}
QComboBox QAbstractItemView::item:selected {
    background: #EDE7FF;
    color: %(text)s;
}
QTableWidget {
    background: %(card)s;
    color: %(text)s;
    border: none;
    gridline-color: %(border)s;
    selection-background-color: #EDE7FF;
    selection-color: %(text)s;
}
QTreeWidget {
    background: %(card)s;
    color: %(text)s;
    border: 1px solid %(border)s;
    border-radius: 8px;
    selection-background-color: #EDE7FF;
    selection-color: %(text)s;
}
QHeaderView::section {
    background: %(canvas)s;
    color: %(muted)s;
    border: none;
    border-bottom: 1px solid %(border)s;
    padding: 6px;
}
QCheckBox, QRadioButton {
    background: transparent;
    color: %(text)s;
    spacing: 8px;
}
QCheckBox::indicator {
    width: 18px;
    height: 18px;
    border: 1px solid #C8C4D4;
    border-radius: 5px;
    background: %(card)s;
}
QCheckBox::indicator:hover {
    border: 1px solid %(purple)s;
}
QCheckBox::indicator:checked {
    background: %(purple)s;
    border: 1px solid %(purple)s;
    image: url(__CHECK__);
}
QRadioButton::indicator {
    width: 18px;
    height: 18px;
    border: 2px solid #C8C4D4;
    border-radius: 9px;
    background: %(card)s;
}
QRadioButton::indicator:hover {
    border: 2px solid %(purple)s;
}
QRadioButton::indicator:checked {
    border: 2px solid %(purple)s;
    background: %(purple)s;
}
QScrollArea {
    border: none;
    background: transparent;
}
QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 2px;
}
QScrollBar::handle:vertical {
    background: %(border)s;
    border-radius: 4px;
    min-height: 24px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
    background: transparent;
}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background: transparent;
}
QScrollBar:horizontal {
    background: transparent;
    height: 10px;
    margin: 2px;
}
QScrollBar::handle:horizontal {
    background: %(border)s;
    border-radius: 4px;
    min-width: 24px;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    width: 0px;
    background: transparent;
}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
    background: transparent;
}
""" % COLORS


def _paint_icons():
    """Chevron et coche, dessinés une fois. Le chemin va dans la feuille de style."""
    import os
    import tempfile

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QImage, QPainter, QPen

    folder = os.path.join(tempfile.gettempdir(), "proton-drive-sync-ui")
    os.makedirs(folder, exist_ok=True)
    chevron = os.path.join(folder, "chevron.png")
    check = os.path.join(folder, "check.png")

    image = QImage(24, 24, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor("#5C5A66"))
    pen.setWidthF(2.2)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.drawLine(6, 9, 12, 15)
    painter.drawLine(12, 15, 18, 9)
    painter.end()
    image.save(chevron)

    mark = QImage(24, 24, QImage.Format.Format_ARGB32)
    mark.fill(Qt.GlobalColor.transparent)
    painter = QPainter(mark)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor("#FFFFFF"))
    pen.setWidthF(2.4)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.drawLine(6, 12, 10, 16)
    painter.drawLine(10, 16, 18, 7)
    painter.end()
    mark.save(check)
    return chevron.replace("\\", "/"), check.replace("\\", "/")


def apply(app):
    """Fusion + feuille de style. Inter si elle est installée, sinon DejaVu Sans."""
    from PySide6.QtGui import QFont, QFontDatabase
    from PySide6.QtWidgets import QApplication

    if not isinstance(app, QApplication):
        return
    app.setStyle("Fusion")
    families = set(QFontDatabase.families())
    if "Inter" in families:
        app.setFont(QFont("Inter", 10))
    elif "DejaVu Sans" in families:
        app.setFont(QFont("DejaVu Sans", 10))
    chevron, check = _paint_icons()
    sheet = STYLESHEET.replace("__CHEVRON__", chevron).replace("__CHECK__", check)
    app.setStyleSheet(sheet)

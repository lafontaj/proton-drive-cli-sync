"""Point d'entrée : ``python -m ui [fichier-de-mappings]``."""

import sys

from ui import theme
from ui.shell import MainWindow, listen_raises, signal_existing, try_become_primary


def main(argv=None):
    from PySide6.QtWidgets import QApplication
    args = list(sys.argv[1:] if argv is None else argv)
    primary = try_become_primary()
    if primary is None and signal_existing():
        return 0
    app = QApplication(sys.argv[:1] + args)
    app.setQuitOnLastWindowClosed(False)
    theme.apply(app)
    path = args[0] if args else None
    window = MainWindow(path)
    if primary not in (None, "skip"):
        listen_raises(window, primary)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

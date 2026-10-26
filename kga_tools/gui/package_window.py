# -*- coding: utf-8 -*-
"""The common shell of the layer package windows.

Create, Open and Add Layers to Layer Package are small modeless windows rather
than Processing dialogs: the same look, a status line instead of a log, and a
window that stays open so several packages can be handled in a row. This holds
what they share - the style, the button row, the status line and the
open-or-raise / close-on-unload plumbing.
"""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (QDialog, QFileDialog, QHBoxLayout, QLabel,
                                 QLineEdit, QPushButton)

from ..branding import help_button
from ..core import output_defaults as od

try:
    from qgis.utils import iface
except ImportError:                         # pragma: no cover - head-less
    iface = None

BLUE = '#174592'

STYLE = """
QPushButton#kgaPrimary { background: %(blue)s; color: white; font-weight: bold;
    padding: 5px 22px; border: none; border-radius: 3px; }
QPushButton#kgaPrimary:disabled { background: palette(mid); color: palette(light); }
QLabel#kgaHint { color: palette(mid); }
""" % {'blue': BLUE}


def tr(text):
    return text


class PackageWindow(QDialog):
    """Subclasses set TITLE, ALG_NAME and RUN_TEXT and implement `run`."""

    TITLE = ''
    ALG_NAME = ''
    RUN_TEXT = 'Run'
    _instance = None

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.TITLE)
        self.setStyleSheet(STYLE)
        self.setObjectName('Kga' + type(self).__name__)

    def add_footer(self, outer):
        """The status line and the Help / run / Close row."""
        self.status = QLabel('', self)
        self.status.setObjectName('kgaHint')
        self.status.setWordWrap(True)
        outer.addWidget(self.status)

        buttons = QHBoxLayout()
        buttons.addWidget(help_button(self.ALG_NAME, self))
        buttons.addStretch(1)
        self.run_button = QPushButton(tr(self.RUN_TEXT), self)
        self.run_button.setObjectName('kgaPrimary')
        self.run_button.setDefault(True)
        self.run_button.clicked.connect(self.run)
        buttons.addWidget(self.run_button)
        close = QPushButton(tr('Close'), self)
        close.clicked.connect(self.close)
        buttons.addWidget(close)
        outer.addLayout(buttons)

    def path_row(self, outer, label, placeholder, chooser, on_change=None):
        """A label, a line edit and a '...' button; returns the line edit."""
        outer.addWidget(QLabel(tr(label), self))
        row = QHBoxLayout()
        edit = QLineEdit(self)
        edit.setPlaceholderText(tr(placeholder))
        if on_change is not None:
            edit.textChanged.connect(on_change)
        row.addWidget(edit, 1)
        button = QPushButton(tr('...'), self)
        button.setMaximumWidth(36)
        button.clicked.connect(lambda: chooser(edit))
        row.addWidget(button)
        outer.addLayout(row)
        return edit

    def choose_package(self, edit, save=False):
        flt = tr('KGA layer package (*.kgalp)')
        start = edit.text().strip() or od.project_folder()
        if save:
            path, _f = QFileDialog.getSaveFileName(
                self, tr('Layer package'), start, flt)
        else:
            path, _f = QFileDialog.getOpenFileName(
                self, tr('Layer package'), start, flt)
        if path:
            edit.setText(path)

    def choose_folder(self, edit):
        folder = QFileDialog.getExistingDirectory(
            self, tr('Choose a folder'),
            edit.text().strip() or od.project_folder())
        if folder:
            edit.setText(folder)

    @staticmethod
    def text_of(edit):
        return edit.text().strip().strip('"')

    def say(self, text, error=False):
        self.status.setStyleSheet('color: #B3261E;' if error else '')
        self.status.setText(text)

    def run(self):                           # pragma: no cover - overridden
        raise NotImplementedError

    def refresh_state(self, *_args):         # pragma: no cover - overridden
        pass

    def shutdown(self):
        self.close()
        self.deleteLater()

    # ------------------------------------------------------- open / close

    @classmethod
    def _alive(cls):
        if cls._instance is None:
            return False
        try:
            cls._instance.objectName()
        except RuntimeError:                # deleted by Qt
            return False
        return True

    @classmethod
    def show_window(cls):
        """Open the window, or raise the one that is already open."""
        if not cls._alive():
            cls._instance = cls(iface.mainWindow() if iface is not None else None)
        window = cls._instance
        if hasattr(window, 'follow_project'):
            window.follow_project()
        window.refresh_state()
        window.setWindowModality(Qt.WindowModality.NonModal)
        window.show()
        window.raise_()
        window.activateWindow()
        return window

    @classmethod
    def close_window(cls):
        """Close the window on plugin unload."""
        window, cls._instance = cls._instance, None
        if window is not None and cls._alive_obj(window):
            window.shutdown()

    @staticmethod
    def _alive_obj(window):
        try:
            window.objectName()
        except RuntimeError:
            return False
        return True


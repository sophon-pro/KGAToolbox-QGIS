# -*- coding: utf-8 -*-
"""The About Us dialog.

Replaces the old About Us action, which only opened khmergrs.com in a browser.
The dialog names the plugin, its version and author, lists what it does, and
carries the links to every channel we publish on.

Version and author are read from metadata.txt rather than duplicated here, so
a release only has to bump the one file QGIS already reads.

Enum members are written in their scoped form (`Qt.TextFormat.RichText`)
because the unscoped spelling is gone in PyQt6, which QGIS 4 moves to.
"""

import os
from contextlib import suppress

try:
    import configparser
except ImportError:  # pragma: no cover - Python 2 never runs this plugin
    configparser = None

from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices, QPixmap
from qgis.PyQt.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..branding import ICON_DIR, KGA_LINKS, PLUGIN_DIR

# What the toolbox does, in the order the toolbar presents it. Kept short:
# this is a "what is this plugin" answer, not the manual.
FEATURES = [
    ('Spatial Data Manager',
     'Browse, load and organise project data from one window'),
    ('Data Conversion',
     'Two-way File Geodatabase and GeoPackage conversion carrying field '
     'domains, aliases, Z/M values and curves, plus KML export and a safe '
     'attribute round-trip to Excel'),
    ('Schema Tools',
     'Field domain editor, domain libraries, validation against domains, and '
     'append with a real field mapping'),
    ('Editing Tools',
     'Copy Parallel, Buffer, Split into COGO Lines, Merge, Divide and Clip, '
     'plus Copy-Paste Feature and Sequential Numbering - editing tools that '
     'will be familiar to ArcGIS Pro users'),
    ('Topology',
     'Check overlaps and gaps, check points on boundary vertices, and step '
     'through what they found in the Error Inspector'),
    ('Geometry Utilities',
     'Planarize lines, find duplicates, and update X/Y and geometry fields'),
    ('Irrigation Tools',
     'DEM and contour analysis and reservoir rating curves'),
]

LICENSE_TEXT = 'Licensed under the MIT License'

# Named so users know what to expect the tools to feel like. Esri has no hand
# in this plugin, and saying so plainly is what keeps naming them fair use.
DISCLAIMER = ('KGA Toolbox is an independent plugin, not affiliated with or '
              'endorsed by Esri. ArcGIS and ArcGIS Pro are trademarks of Esri.')


def _metadata():
    """(version, author) from metadata.txt, with fallbacks if it cannot be read."""
    version, author = '', 'Khmer GRS Academy (KGA)'
    path = os.path.join(PLUGIN_DIR, 'metadata.txt')
    if configparser is None or not os.path.exists(path):
        return version, author
    parser = configparser.ConfigParser(interpolation=None)
    # A malformed metadata.txt should not stop the dialog from opening.
    with suppress(Exception):
        parser.read(path, encoding='utf-8')
        version = parser.get('general', 'version', fallback=version)
        author = parser.get('general', 'author', fallback=author)
    return version, author


def _bullets(items):
    """Rich-text <ul> for a list of already-escaped strings."""
    rows = ''.join('<li style="margin-bottom:4px;">{}</li>'.format(i)
                   for i in items)
    return '<ul style="margin-left:0px; -qt-list-indent:1;">{}</ul>'.format(rows)


class AboutDialog(QDialog):

    def __init__(self, parent=None):
        super().__init__(parent)
        version, author = _metadata()

        self.setWindowTitle('About KGA Toolbox')
        self.setMinimumWidth(560)
        self.resize(620, 750)

        # --- header: logo beside the name, version and author ----------------
        header = QHBoxLayout()
        header.setSpacing(16)

        logo = QLabel(self)
        pixmap = QPixmap(os.path.join(ICON_DIR, 'kga_logo.png'))
        if pixmap.isNull():
            pixmap = QPixmap(os.path.join(ICON_DIR, 'kga.png'))
        if not pixmap.isNull():
            logo.setPixmap(pixmap.scaled(
                72, 72, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
        logo.setAlignment(Qt.AlignmentFlag.AlignTop)
        header.addWidget(logo)

        title = QLabel(self)
        title.setTextFormat(Qt.TextFormat.RichText)
        title.setText(
            '<div style="font-size:18pt; font-weight:bold;">KGA Toolbox</div>'
            '<div style="margin-top:8px;">Version: {}</div>'
            '<div style="margin-top:4px;">Author: {}</div>'.format(
                version or 'unknown', author))
        title.setAlignment(Qt.AlignmentFlag.AlignTop)
        header.addWidget(title, 1)

        # --- body: features and links, scrollable so a small screen still fits
        body = QLabel()
        body.setTextFormat(Qt.TextFormat.RichText)
        body.setWordWrap(True)
        body.setOpenExternalLinks(False)
        body.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction)
        body.linkActivated.connect(self.open_link)
        body.setAlignment(Qt.AlignmentFlag.AlignTop)
        body.setText(
            '<div style="font-size:13pt; font-weight:bold;">Features:</div>'
            + _bullets('<b>{}:</b> {}'.format(name, text)
                       for name, text in FEATURES)
            + '<div style="font-size:13pt; font-weight:bold; margin-top:12px;">'
              'Links:</div>'
            + _bullets('{}: <a href="{}">{}</a>'.format(label, url, url)
                       for label, url in KGA_LINKS)
        )

        inner = QWidget()
        inner_layout = QVBoxLayout(inner)
        # Reserve the vertical scrollbar's width on the right. Without it the
        # label wraps to the full viewport, the scrollbar then appears over the
        # last few pixels, and every long line loses its final word.
        inner_layout.setContentsMargins(0, 0, 18, 0)
        inner_layout.addWidget(body)
        inner_layout.addStretch(1)

        scroll = QScrollArea(self)
        scroll.setWidget(inner)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        # Rich text asks for whatever width its longest line wants. Without
        # this the dialog grows a horizontal scrollbar instead of wrapping.
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        license_label = QLabel(LICENSE_TEXT, self)

        disclaimer_label = QLabel(DISCLAIMER, self)
        disclaimer_label.setWordWrap(True)
        # Fine print: smaller and greyed, so it reads as a footnote rather than
        # competing with the feature list.
        disclaimer_label.setStyleSheet('color: palette(mid); font-size: 8pt;')

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok, parent=self)
        buttons.accepted.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(12)
        layout.addLayout(header)
        layout.addWidget(scroll, 1)
        layout.addWidget(license_label)
        layout.addWidget(disclaimer_label)
        layout.addWidget(buttons)

    def open_link(self, url):
        QDesktopServices.openUrl(QUrl(url))

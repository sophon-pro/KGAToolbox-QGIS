# -*- coding: utf-8 -*-
"""Plain text files (`.txt`) in the Browser and the Catalog.

QGIS lists `.csv` but not `.txt`, so a text table was invisible. This provider
lists each `.txt` as a table layer item over the `delimitedtext` provider, so
"Add to Map" and double-click work like they do for any other table. The
delimiter is guessed from the first line; columns named like longitude and
latitude become a point layer. Plain `QgsLayerItem`s, for the sip reason the
other Browser items avoid Python subclasses.
"""

import os
import re
from urllib.parse import quote

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsDataItemProvider,
    QgsLayerItem,
)

PROVIDER_KEY = 'kga_text_file'
EXTENSIONS = ('.txt',)
_DELIMITERS = ('\t', ',', ';', '|')
_X = re.compile(r'^(lon|long|lng|longitude|x|easting|x_coord\w*)$', re.I)
_Y = re.compile(r'^(lat|latitude|y|northing|y_coord\w*)$', re.I)


def _head(path, size=4096):
    try:
        with open(path, 'rb') as handle:
            raw = handle.read(size)
    except OSError:
        return ''
    for encoding in ('utf-8-sig', 'utf-16', 'cp1252'):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return ''


def guess_delimiter(text):
    """The delimiter that splits the first line into the most columns."""
    first = text.splitlines()[0] if text.strip() else ''
    best = max(_DELIMITERS, key=first.count)
    return best if first.count(best) else ''


def uri_for(path):
    """A `delimitedtext` URI for the text file at `path`."""
    text = _head(path)
    delimiter = guess_delimiter(text)
    url = 'file:///' + quote(os.path.abspath(path).replace('\\', '/'), safe='/:')
    parts = ['detectTypes=yes', 'subsetIndex=no', 'watchFile=no']
    # the provider reads a backslash-t as a tab; a one-column file defaults to it
    shown = '\\t' if delimiter in ('', '\t') else delimiter
    parts[:0] = ['type=csv', 'delimiter=' + shown]
    names = []
    if delimiter and text.strip():
        names = [n.strip().strip('"') for n in
                 text.splitlines()[0].split(delimiter)]
    x = next((n for n in names if _X.match(n)), None)
    y = next((n for n in names if _Y.match(n)), None)
    if x and y:
        parts += ['geomType=point', 'xField=' + quote(x), 'yField=' + quote(y),
                  'crs=EPSG:4326']
    else:
        parts.append('geomType=none')
    return url + '?' + '&'.join(parts)


def text_item(path, parent):
    name = os.path.basename(path)
    item = QgsLayerItem(parent, name, path, uri_for(path),
                        Qgis.BrowserLayerType.TableLayer, 'delimitedtext')
    item.setIcon(QgsApplication.getThemeIcon('/mIconTableLayer.svg'))
    item.setToolTip(path)
    return item


class TextFileProvider(QgsDataItemProvider):

    def name(self):
        return PROVIDER_KEY

    def capabilities(self):
        return Qgis.DataItemProviderCapability.Files

    def createDataItem(self, path, parentItem):     # noqa: N802 - Qt naming
        if path and path.lower().endswith(EXTENSIONS) and os.path.isfile(path):
            return text_item(path, parentItem)
        return None


class TextFileInstaller(object):

    def __init__(self):
        self.provider = None

    def install(self):
        if self.provider is None:
            self.provider = TextFileProvider()
            QgsApplication.dataItemProviderRegistry().addProvider(self.provider)

    def uninstall(self):
        if self.provider is not None:
            try:
                QgsApplication.dataItemProviderRegistry().removeProvider(
                    self.provider)
            finally:
                self.provider = None

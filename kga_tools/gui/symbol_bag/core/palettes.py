# -*- coding: utf-8 -*-
"""
Symbol Bag – colour palettes, favourite colours and recent colours.

Stored in the Symbol Bag library (SQLite, QGIS profile) so they survive
restarts and are shared by every project. Palettes import / export as GIMP
palette files (.gpl) – the format QGIS itself uses – and can be published to
QGIS's own colour buttons.
"""

import os

from qgis.PyQt.QtCore import QFile
from qgis.PyQt.QtGui import QColor
from qgis.core import QgsApplication, QgsSymbolLayerUtils

from .log import log_warning
from .storage import LibraryError

RECENT_MAX = 16

DEFAULT_PALETTES = [
    ("Basic Colors", ["#000000", "#ffffff", "#808080", "#ff0000", "#ff8000", "#ffff00", "#80ff00", "#00c000",
                      "#00ffff", "#0080ff", "#0000ff", "#8000ff", "#ff00ff", "#804000", "#c0c0c0", "#400000"]),
    ("Professional GIS", ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b", "#e377c2",
                          "#7f7f7f", "#bcbd22", "#17becf", "#e41a1c", "#377eb8", "#4daf4a", "#984ea3",
                          "#ff7f00", "#a65628"]),
    ("Pastel", ["#fbb4ae", "#b3cde3", "#ccebc5", "#decbe4", "#fed9a6", "#ffffcc", "#e5d8bd", "#fddaec",
                "#f2f2f2", "#b3e2cd", "#fdcdac", "#cbd5e8", "#f4cae4", "#e6f5c9", "#fff2ae", "#f1e2cc"]),
    ("Earth & Terrain", ["#1a9850", "#66bd63", "#a6d96a", "#d9ef8b", "#fee08b", "#fdae61", "#d8b365",
                         "#a6611a", "#8c510a", "#543005", "#f5f5f5", "#bababa", "#4d7a3a", "#d9c58b",
                         "#a67c52", "#5b3a29"]),
    ("Agriculture", ["#f9e79f", "#f4d03f", "#b7950b", "#7dcea0", "#52be80", "#1e8449", "#a9dfbf", "#fad7a0",
                     "#ca6f1e", "#e59866", "#a04000", "#d4ac0d", "#9acd32", "#6b8e23", "#556b2f", "#c2b280"]),
    ("Transportation", ["#e31a1c", "#fd8d3c", "#fecc5c", "#ffffb2", "#b03a2e", "#f8c471", "#7f8c8d",
                        "#ffffff", "#2d2d2d", "#8d6e63", "#4a4a4a", "#1f78b4", "#6a3d9a", "#ff7f00",
                        "#b15928", "#c0c0c0"]),
    ("Satellite & Remote Sensing", ["#0c0c8a", "#08306b", "#2171b5", "#6baed6", "#d7c9a9", "#f6e8a1",
                                    "#a6d96a", "#1a9641", "#004d1a", "#d7191c", "#fdae61", "#ffffbf",
                                    "#abd9e9", "#2c7bb6", "#ff0000", "#00ff00"]),
    ("Dark", ["#1b1b1b", "#2d3436", "#353b48", "#2f3640", "#192a56", "#273c75", "#0c2461", "#1e3799",
              "#3c1361", "#4a0e0e", "#5d1d1d", "#1e3d32", "#145a32", "#4d3227", "#3d3d3d", "#000000"]),
    ("Light", ["#ffffff", "#f8f9fa", "#f1f3f5", "#e9ecef", "#fff5f5", "#fff0f6", "#f8f0fc", "#f3f0ff",
               "#edf2ff", "#e7f5ff", "#e3fafc", "#e6fcf5", "#ebfbee", "#f4fce3", "#fff9db", "#fff4e6"]),
]


def to_hex(c):
    """#AARRGGBB when translucent, #RRGGBB otherwise."""
    c = QColor(c)
    return c.name(QColor.NameFormat.HexArgb) if c.alpha() < 255 else c.name()


def from_hex(text):
    c = QColor(text)
    return c if c.isValid() else None


class PaletteStore(object):
    """All colour persistence goes through here."""

    def __init__(self, store):
        self.store = store
        if self.store.get_setting("palettes", None) is None:
            self.restore_defaults(replace=True)

    # --- palettes --------------------------------------------------------
    def palettes(self):
        return list(self.store.get_setting("palettes", []) or [])

    def names(self):
        return [p["name"] for p in self.palettes()]

    def colors(self, name):
        for p in self.palettes():
            if p["name"] == name:
                return [c for c in (from_hex(h) for h in p.get("colors", [])) if c is not None]
        return []

    def _save(self, pals):
        self.store.set_setting("palettes", pals)

    def _find(self, pals, name):
        for p in pals:
            if p["name"] == name:
                return p
        raise LibraryError("Palette '{}' not found.".format(name))

    def unique_name(self, base):
        names = set(self.names())
        if base not in names:
            return base
        i = 2
        while "{} {}".format(base, i) in names:
            i += 1
        return "{} {}".format(base, i)

    def add_palette(self, name, colors=()):
        name = (name or "").strip()
        if not name:
            raise LibraryError("A palette name cannot be empty.")
        pals = self.palettes()
        if any(p["name"].lower() == name.lower() for p in pals):
            raise LibraryError("A palette named '{}' already exists.".format(name))
        pals.append({"name": name, "colors": [to_hex(c) for c in colors]})
        self._save(pals)
        return name

    def rename_palette(self, old, new):
        new = (new or "").strip()
        if not new:
            raise LibraryError("A palette name cannot be empty.")
        pals = self.palettes()
        if any(p["name"].lower() == new.lower() and p["name"] != old for p in pals):
            raise LibraryError("A palette named '{}' already exists.".format(new))
        self._find(pals, old)["name"] = new
        self._save(pals)

    def delete_palette(self, name):
        pals = [p for p in self.palettes() if p["name"] != name]
        self._save(pals)

    def add_color(self, name, color):
        pals = self.palettes()
        p = self._find(pals, name)
        h = to_hex(color)
        if h not in p["colors"]:
            p["colors"].append(h)
            self._save(pals)
            return True
        return False

    def remove_color(self, name, color):
        pals = self.palettes()
        p = self._find(pals, name)
        h = to_hex(color)
        if h in p["colors"]:
            p["colors"].remove(h)
            self._save(pals)

    def restore_defaults(self, replace=False):
        pals = [] if replace else self.palettes()
        have = {p["name"] for p in pals}
        for name, cols in DEFAULT_PALETTES:
            if name not in have:
                pals.append({"name": name, "colors": list(cols)})
        self._save(pals)

    # --- favourites & recent ------------------------------------------------
    def favorites(self):
        return [c for c in (from_hex(h) for h in self.store.get_setting("fav_colors", []) or []) if c]

    def is_favorite(self, color):
        return to_hex(color) in (self.store.get_setting("fav_colors", []) or [])

    def set_favorite(self, color, value):
        favs = list(self.store.get_setting("fav_colors", []) or [])
        h = to_hex(color)
        if value and h not in favs:
            favs.append(h)
        elif not value and h in favs:
            favs.remove(h)
        self.store.set_setting("fav_colors", favs)

    def recent(self):
        return [c for c in (from_hex(h) for h in self.store.get_setting("recent_colors", []) or []) if c]

    def push_recent(self, color):
        h = to_hex(color)
        rec = [x for x in (self.store.get_setting("recent_colors", []) or []) if x != h]
        rec.insert(0, h)
        self.store.set_setting("recent_colors", rec[:RECENT_MAX])

    # --- import / export ------------------------------------------------------
    def import_gpl(self, path):
        """Import a .gpl palette; returns the new palette name."""
        if not os.path.isfile(path):
            raise LibraryError("File not found: {}".format(path))
        f = QFile(path)            # QGIS opens the file itself
        try:
            colors, ok = QgsSymbolLayerUtils.importColorsFromGpl(f, os.path.splitext(os.path.basename(path))[0])
        finally:
            if f.isOpen():
                f.close()
        if not ok or not colors:
            raise LibraryError("'{}' is not a valid GIMP/QGIS palette (.gpl) file.".format(os.path.basename(path)))
        name = self._gpl_name(path) or os.path.splitext(os.path.basename(path))[0]
        name = self.unique_name(name)
        self.add_palette(name, [c for c, _label in colors])
        return name

    @staticmethod
    def _gpl_name(path):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.lower().startswith("name:"):
                        return line.split(":", 1)[1].strip()
        except OSError:
            pass
        return None

    def export_gpl(self, name, path):
        cols = self.colors(name)
        try:
            if os.path.exists(path):
                os.remove(path)        # QGIS writes without truncating
        except OSError as e:
            raise LibraryError("Cannot write '{}': {}".format(path, e))
        f = QFile(path)                # QGIS opens the file itself
        try:
            ok = QgsSymbolLayerUtils.saveColorsToGpl(f, name, [(c, to_hex(c)) for c in cols])
        finally:
            if f.isOpen():
                f.close()
        if not ok:
            raise LibraryError("QGIS could not write the palette.")
        return len(cols)

    def publish_to_qgis(self, name):
        """Make the palette available in every QGIS colour button
        (written to <profile>/palettes and registered as a colour scheme)."""
        from qgis.core import QgsUserColorScheme
        folder = os.path.join(QgsApplication.qgisSettingsDirPath(), "palettes")
        os.makedirs(folder, exist_ok=True)
        safe = "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in name).strip() or "palette"
        filename = "SymbolBag - {}.gpl".format(safe)
        self.export_gpl(name, os.path.join(folder, filename))
        reg = QgsApplication.colorSchemeRegistry()
        try:
            for scheme in reg.schemes():
                if isinstance(scheme, QgsUserColorScheme) and scheme.schemeName() == name:
                    reg.removeColorScheme(scheme)
            scheme = QgsUserColorScheme(filename)
            scheme.setShowSchemeInMenu(True)
            reg.addColorScheme(scheme)
        except Exception as e:  # registry differences between versions
            log_warning("Palette written but not registered: {}".format(e))
        return os.path.join(folder, filename)

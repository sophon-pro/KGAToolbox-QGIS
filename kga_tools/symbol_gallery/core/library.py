# -*- coding: utf-8 -*-
"""
Icon libraries: large open-licence SVG icon sets shipped as compact JSON.

Each library file  libraries/<name>.json  looks like
    {"title": ..., "source": ..., "licence": ..., "url": ...,
     "items": [{"n": name, "c": category, "t": [tags], "s": "<svg…>", "z": size_mm}]}

Icons are turned into SVG marker symbols on demand (embedded, so styles stay
portable).  Single-colour icons use the QGIS param(fill) convention and can be
recoloured in the gallery or any symbol editor.
"""
import gzip
import json
import os

import base64

from qgis.core import QgsRasterFillSymbolLayer, QgsSVGFillSymbolLayer

from .catalog import Entry, MARKER, LINE, FILL
from .builders import (marker_symbol, svg_marker, line_symbol, fill_symbol, simple_fill,
                       marker_line)

PX_MM = 0.2645 * 0.6       # texture tiles: CSS px -> mm (slightly reduced for maps)

LIB_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "libraries")

_meta = []


def _slug(text):
    out = []
    for ch in text.lower():
        out.append(ch if ch.isalnum() else "_")
    return "".join(out).strip("_")


def _builder(svg, size, colour):
    def build():
        return marker_symbol(svg_marker(svg, size, colour, "#ffffff", 0))
    return build


def _texture_builder(it):
    kind = it["k"]
    if kind == "rfill":
        def build():
            lyr = QgsRasterFillSymbolLayer.create({
                "imageFile": "base64:" + it["img"], "width": str(it["w"] * PX_MM),
                "width_unit": "MM", "coordinate_mode": "0", "alpha": "1"})
            return fill_symbol(simple_fill(it["bg"], "#000000", 0, outline_style="no"), lyr)
        return FILL, build
    if kind == "sfill":
        def build():
            data = "base64:" + base64.b64encode(it["svg"].encode("utf-8")).decode("ascii")
            lyr = QgsSVGFillSymbolLayer.create({"svgFile": data, "width": str(it["w"] * 0.6),
                                                "width_unit": "MM", "angle": "0"})
            return fill_symbol(simple_fill(it["bg"], "#000000", 0, outline_style="no"), lyr)
        return FILL, build
    if kind == "lpat":
        def build():
            w = it["w"] * PX_MM * 1.4
            mk = marker_symbol(svg_marker(it["svg"], w))
            return line_symbol(marker_line(mk, interval=w, offset_along=w / 2))
        return LINE, build
    return None, None


def load_all():
    """Return Entry objects for every bundled library (fast: JSON only)."""
    del _meta[:]
    entries = []
    from . import catalog
    # seed with built-in names so library icons never reuse a built-in name
    used = {}                       # name (lower) -> count, across everything
    for b in catalog.ENTRIES[:catalog.BUILTIN_COUNT]:
        used[b.name.lower()] = 1
    if not os.path.isdir(LIB_DIR):
        return entries
    for fname in sorted(os.listdir(LIB_DIR)):
        if not fname.endswith((".json", ".json.gz")):
            continue
        path = os.path.join(LIB_DIR, fname)
        try:
            # a library that embeds images is shipped gzipped (long base64 text trips secret scanners)
            opener = gzip.open if fname.endswith(".gz") else open
            with opener(path, "rt", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        lib = fname.split(".json")[0]
        version = str(int(os.path.getmtime(path)))
        meta = {k: data.get(k, "") for k in ("title", "source", "licence", "url")}
        meta["count"] = len(data.get("items", []))
        _meta.append(meta)
        for it in data.get("items", []):
            name = it["n"]
            base = name.lower()
            used[base] = used.get(base, 0) + 1
            if used[base] > 1:
                name = "%s %d" % (it["n"], used[base])
                while name.lower() in used:
                    used[base] += 1
                    name = "%s %d" % (it["n"], used[base])
                used[name.lower()] = 1
            colour = it.get("col", "#333333")
            if "k" in it:
                kind, build = _texture_builder(it)
                if kind is None:
                    continue
                desc = "%s (%s, %s)." % (data.get("title", "Texture"), data.get("source", ""),
                                         data.get("licence", ""))
                tags = list(it.get("t", [])) + ["texture"]
            else:
                kind, build = MARKER, _builder(it["s"], float(it.get("z", 4.5)), colour)
                desc = "%s icon (%s, %s)." % (data.get("title", "Map"),
                                              data.get("source", ""), data.get("licence", ""))
                tags = list(it.get("t", [])) + ["icon"]
            e = Entry("lib:%s:%s" % (lib, _slug(name)), name, it["c"], kind, tags, desc, build)
            e.version = version
            entries.append(e)
    return entries


def libraries():
    """Metadata of loaded libraries (for the About / credits view)."""
    return list(_meta)

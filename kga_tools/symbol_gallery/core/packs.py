# -*- coding: utf-8 -*-
"""
Style packs: extra symbol sets loaded from QGIS style XML files.

* Packs shipped with the plugin live in ``<plugin>/packs``.
* Packs the user adds (``Add style pack…``) are copied to
  ``<QGIS profile>/symbol_gallery/packs`` so they survive plugin upgrades.

A pack is ``name.xml`` (a normal QGIS style export) with an optional
``name.json`` sidecar: {"title": "...", "description": "...", "tags": [...],
"category": "..."}.  ``category`` merges the pack into an existing field
(e.g. "Military & Tactical"); without it the pack gets its own field.
"""
import json
import os
import re
import shutil

from qgis.core import QgsApplication, QgsStyle

from . import catalog
from .catalog import Entry, MARKER, LINE, FILL, RAMP
from .compat import SYMBOL_MARKER, SYMBOL_LINE, SYMBOL_FILL, STYLE_SYMBOL, STYLE_RAMP

PLUGIN_PACKS = os.path.join(os.path.dirname(os.path.dirname(__file__)), "packs")

_styles = []        # keep QgsStyle objects alive
_errors = []


def user_pack_dir(create=False):
    d = os.path.join(QgsApplication.qgisSettingsDirPath(), "symbol_gallery", "packs")
    if create:
        os.makedirs(d, exist_ok=True)
    return d


def _meta(xml_path):
    side = os.path.splitext(xml_path)[0] + ".json"
    meta = {}
    if os.path.exists(side):
        try:
            with open(side, encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            meta = {}
    stem = os.path.splitext(os.path.basename(xml_path))[0]
    meta.setdefault("title", stem.replace("_", " ").title() + " (pack)")
    meta.setdefault("description", "")
    meta.setdefault("tags", [])
    return meta


def _kind(symbol):
    t = symbol.type()
    if t == SYMBOL_MARKER:
        return MARKER
    if t == SYMBOL_LINE:
        return LINE
    if t == SYMBOL_FILL:
        return FILL
    return None


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def load_pack(xml_path, taken=None):
    style = QgsStyle()
    if not style.createMemoryDatabase() or not style.importXml(xml_path):
        _errors.append("Could not read %s" % xml_path)
        return []
    _styles.append(style)
    meta = _meta(xml_path)
    title = meta["title"]
    category = meta.get("category") or title
    label = meta.get("label") or ""
    stem = _slug(os.path.splitext(os.path.basename(xml_path))[0])
    entries = []
    if taken is None:   # names already in use (built-in + libraries + earlier packs)
        taken = {e.name.lower() for e in catalog.ENTRIES[:catalog.BUILTIN_COUNT]}
    for name in style.symbolNames():
        sym = style.symbol(name)
        kind = _kind(sym) if sym is not None else None
        if kind is None:
            continue
        tags = list(dict.fromkeys(style.tagsOfSymbol(STYLE_SYMBOL, name)
                                  + meta["tags"]))
        shown = name
        if label and shown.lower() in taken:
            shown = "%s (%s)" % (name, label)     # e.g. "Main canal (CISIS)"
        taken.add(shown.lower())
        entries.append(Entry(
            "pack:%s:%s" % (stem, name), shown, category, kind, tags,
            meta["description"] or "From the %s." % title,
            (lambda s=style, n=name: s.symbol(n))))
    for name in style.colorRampNames():
        tags = list(dict.fromkeys(style.tagsOfSymbol(STYLE_RAMP, name)
                                  + meta["tags"]))
        entries.append(Entry(
            "pack:%s:ramp:%s" % (stem, name), name, category, RAMP, tags,
            meta["description"] or "From the %s." % title,
            (lambda s=style, n=name: s.colorRamp(n))))
    return entries


def pack_files():
    files = []
    for d in (PLUGIN_PACKS, user_pack_dir()):
        if os.path.isdir(d):
            files += sorted(os.path.join(d, f) for f in os.listdir(d)
                            if f.lower().endswith(".xml"))
    return files


def reload_packs():
    """(Re)load every pack into the catalog.  Returns number of pack items."""
    del _styles[:]
    del _errors[:]
    entries = []
    try:
        from . import library
        entries += library.load_all()
    except Exception as ex:  # pragma: no cover
        _errors.append("icon libraries: %s" % ex)
    taken = {e.name.lower() for e in catalog.ENTRIES[:catalog.BUILTIN_COUNT]}
    taken |= {e.name.lower() for e in entries}
    for f in pack_files():
        try:
            entries += load_pack(f, taken)
        except Exception as ex:  # a broken pack must never break the gallery
            _errors.append("%s: %s" % (os.path.basename(f), ex))
    catalog.set_pack_entries(entries)
    return len(entries)


def errors():
    return list(_errors)


def add_pack(path, title=None):
    """Install a QGIS style .xml or an ArcGIS Pro .stylx as a user pack.

    Returns (title, added, skipped).
    """
    ext = os.path.splitext(path)[1].lower()
    dest_dir = user_pack_dir(create=True)
    stem = _slug(os.path.splitext(os.path.basename(path))[0]) or "pack"
    dest = os.path.join(dest_dir, stem + ".xml")
    n = 1
    while os.path.exists(dest):
        n += 1
        dest = os.path.join(dest_dir, "%s_%d.xml" % (stem, n))
    title = title or os.path.splitext(os.path.basename(path))[0].replace("_", " ") + " (pack)"

    if ext == ".xml":
        test = QgsStyle()
        test.createMemoryDatabase()
        if not test.importXml(path):
            raise ValueError("This XML is not a QGIS style library.")
        added = test.symbolCount() + test.colorRampCount()
        skipped = 0
        shutil.copyfile(path, dest)
    elif ext == ".stylx":
        from .cim import stylx_to_style
        style = QgsStyle()
        style.createMemoryDatabase()
        added, skipped = stylx_to_style(path, style, tag=title)
        if not added:
            raise ValueError("No convertible symbols were found in this .stylx.")
        style.exportXml(dest)
    elif ext == ".style":
        raise ValueError("ArcMap .style databases need converting first. Use the free "
                         "SLYR plugin (Processing ▸ SLYR ▸ Convert ESRI style to QGIS "
                         "style XML), then add the resulting .xml here.")
    else:
        raise ValueError("Unsupported file type: %s" % ext)

    with open(os.path.splitext(dest)[0] + ".json", "w", encoding="utf-8") as fh:
        json.dump({"title": title, "description": "Imported from %s."
                   % os.path.basename(path), "tags": []}, fh, indent=2)
    return title, added, skipped


def remove_user_pack(title):
    """Delete a user-installed pack by its title.  Returns True if removed."""
    d = user_pack_dir()
    if not os.path.isdir(d):
        return False
    for f in os.listdir(d):
        if f.lower().endswith(".xml"):
            p = os.path.join(d, f)
            if _meta(p)["title"] == title:
                os.remove(p)
                side = os.path.splitext(p)[0] + ".json"
                if os.path.exists(side):
                    os.remove(side)
                return True
    return False


def is_user_pack(title):
    d = user_pack_dir()
    if not os.path.isdir(d):
        return False
    return any(_meta(os.path.join(d, f))["title"] == title
               for f in os.listdir(d) if f.lower().endswith(".xml"))

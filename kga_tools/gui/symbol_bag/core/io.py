# -*- coding: utf-8 -*-
"""
Symbol Bag – importing and exporting symbols.

Importers produce lists of Candidate objects. The UI shows them with
previews so the user can pick which to keep; `store_candidates` then
writes the chosen ones to the library (duplicates are skipped).
"""

from contextlib import suppress
import datetime
import json
import os

from qgis.PyQt.QtXml import QDomDocument
from qgis.core import (
    QgsFeatureRenderer,
    QgsGradientColorRamp,
    QgsGradientStop,
    QgsProject,
    QgsRasterLayer,
    QgsReadWriteContext,
    QgsRenderContext,
    QgsStyle,
    QgsVectorLayer,
)

from ....core.compat import scoped
from .log import log_warning
from .storage import ALL_KINDS, KIND_RASTER, VECTOR_KINDS
from .symbols import (
    is_raster_style_xml,
    raster_style_from_layer,
    raster_style_info,
    svg_fill_symbol,
    svg_marker_symbol,
    symbol_from_xml,
    symbol_kind,
    symbol_to_xml,
)

SBAG_FORMAT = "symbolbag"
SBAG_VERSION = 1

IMPORT_FILE_FILTER = ("All supported (*.qml *.xml *.sld *.svg *.db *.sbag);;"
                      "QGIS layer style (*.qml);;QGIS style library (*.xml);;"
                      "QGIS style database (*.db);;SLD (*.sld);;SVG graphic (*.svg);;"
                      "Symbol Bag collection (*.sbag)")

try:
    SYMBOL_ENTITY = QgsStyle.StyleEntity.SymbolEntity
except AttributeError:  # pragma: no cover
    SYMBOL_ENTITY = scoped(QgsStyle, 'StyleEntity', 'SymbolEntity')


class ImportFailed(Exception):
    """Raised with a user-readable message when a file cannot be imported."""


class Candidate(object):
    """A symbol ready to be stored, produced by an importer."""

    def __init__(self, name, kind, xml, subtype="", tags="", description="",
                 collection_path=None, favorite=False, source=""):
        self.name = name
        self.kind = kind
        self.xml = xml
        self.subtype = subtype
        self.tags = tags if isinstance(tags, str) else ", ".join(tags or [])
        self.description = description
        self.collection_path = collection_path or []
        self.favorite = favorite
        self.source = source
        self.selected = True

    @classmethod
    def from_symbol(cls, name, symbol, **kw):
        return cls(name, symbol_kind(symbol), symbol_to_xml(symbol), **kw)


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def _read_dom(path):
    doc = QDomDocument()
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as e:
        raise ImportFailed("Cannot read '{}': {}".format(os.path.basename(path), e))
    res = doc.setContent(data)
    ok = res[0] if isinstance(res, tuple) else res
    if not ok:
        msg = res[1] if isinstance(res, tuple) and len(res) > 1 else ""
        raise ImportFailed("'{}' is not a valid XML file. {}".format(os.path.basename(path), msg))
    return doc


def renderer_symbols(renderer, base_name):
    """Return [(label, QgsSymbol)] for every symbol of a vector renderer."""
    out = []
    depth = 0
    while renderer is not None and hasattr(renderer, "embeddedRenderer") \
            and renderer.embeddedRenderer() is not None and depth < 5:
        renderer = renderer.embeddedRenderer()
        depth += 1
    if renderer is None:
        return out
    t = renderer.type()
    if t == "singleSymbol":
        if renderer.symbol() is not None:
            out.append((base_name, renderer.symbol().clone()))
    elif t == "categorizedSymbol":
        for cat in renderer.categories():
            label = cat.label() or str(cat.value())
            if cat.symbol() is not None:
                out.append(("{} – {}".format(base_name, label), cat.symbol().clone()))
    elif t == "graduatedSymbol":
        for rng in renderer.ranges():
            if rng.symbol() is not None:
                out.append(("{} – {}".format(base_name, rng.label()), rng.symbol().clone()))
    elif t == "RuleRenderer":
        for rule in renderer.rootRule().descendants():
            if rule.symbol() is not None:
                label = rule.label() or rule.filterExpression() or "rule"
                out.append(("{} – {}".format(base_name, label), rule.symbol().clone()))
    else:
        try:
            syms = renderer.symbols(QgsRenderContext())
        except Exception:
            syms = []
        for i, s in enumerate(syms):
            out.append(("{} {}".format(base_name, i + 1) if len(syms) > 1 else base_name, s.clone()))
    return [(n, s) for n, s in out if s is not None]


KGA_COLLECTION = "KGA Layer Symbol"


def _kga_catalog():
    """The KGA Layer Symbol catalog (built-in symbols, icon libraries and style packs), loaded once."""
    from ....symbol_gallery.core import catalog, packs
    if len(catalog.ENTRIES) <= catalog.BUILTIN_COUNT:
        packs.reload_packs()
    return catalog


def kga_layer_symbol_categories():
    """[(category, number of symbols)] of the KGA Layer Symbol catalog (colour ramps left out)."""
    catalog = _kga_catalog()
    counts = {}
    for e in catalog.ENTRIES:
        if not e.is_ramp:
            counts[e.category] = counts.get(e.category, 0) + 1
    return sorted(counts.items(), key=lambda kv: kv[0].lower())


def from_kga_layer_symbol(categories):
    """Candidates for the symbols of the chosen KGA Layer Symbol categories, each kept in a
    sub-collection named after its category."""
    catalog = _kga_catalog()
    wanted = set(categories)
    out = []
    for e in catalog.ENTRIES:
        if e.is_ramp or e.category not in wanted:
            continue
        symbol = None
        with suppress(Exception):
            symbol = e.build()
        if symbol is None:
            continue
        out.append(Candidate.from_symbol(e.name, symbol, tags=e.tags, description=e.description,
                                         collection_path=[KGA_COLLECTION, e.category],
                                         source="KGA Layer Symbol"))
    return out


def _dedupe(cands):
    seen = set()
    out = []
    for c in cands:
        key = (c.kind, c.xml)
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


# ----------------------------------------------------------------------
# importers
# ----------------------------------------------------------------------
def from_style(style, source="QGIS Style Manager"):
    cands = []
    for name in style.symbolNames():
        sym = style.symbol(name)
        if sym is None or symbol_kind(sym) is None:
            continue
        try:
            tags = style.tagsOfSymbol(SYMBOL_ENTITY, name)
        except Exception:
            tags = []
        try:
            fav = style.isFavorite(SYMBOL_ENTITY, name)
        except Exception:
            fav = False
        cands.append(Candidate.from_symbol(name, sym, tags=tags, favorite=fav, source=source))
    return cands


def from_default_style():
    return from_style(QgsStyle.defaultStyle())


def from_style_file(path):
    """QGIS XML style library or .db style database."""
    style = QgsStyle()
    ext = os.path.splitext(path)[1].lower()
    if ext == ".db":
        if not style.load(path):
            raise ImportFailed("'{}' is not a readable QGIS style database.".format(os.path.basename(path)))
    else:
        style.createMemoryDatabase()
        doc = _read_dom(path)
        if doc.documentElement().tagName() != "qgis_style":
            raise ImportFailed("'{}' is not a QGIS style library (expected <qgis_style> root)."
                               .format(os.path.basename(path)))
        if not style.importXml(path):
            raise ImportFailed("QGIS could not import the style library '{}'.".format(os.path.basename(path)))
    cands = from_style(style, source=os.path.basename(path))
    if not cands:
        raise ImportFailed("No point, line or polygon symbols were found in '{}'."
                           .format(os.path.basename(path)))
    return cands


def from_qml(path):
    doc = _read_dom(path)
    root = doc.documentElement()
    base = os.path.splitext(os.path.basename(path))[0]
    if root.tagName() != "qgis":
        raise ImportFailed("'{}' is not a QGIS style file (expected <qgis> root).".format(os.path.basename(path)))
    if is_raster_style_xml(doc.toString()):
        info = raster_style_info(doc.toString())
        return [Candidate(base, KIND_RASTER, doc.toString(), subtype=info["renderer"],
                          source=os.path.basename(path))]
    rend_elem = root.firstChildElement("renderer-v2")
    if rend_elem.isNull():
        raise ImportFailed("'{}' contains no vector renderer or raster style.".format(os.path.basename(path)))
    renderer = QgsFeatureRenderer.load(rend_elem, QgsReadWriteContext())
    if renderer is None:
        raise ImportFailed("QGIS could not read the renderer in '{}'.".format(os.path.basename(path)))
    cands = [Candidate.from_symbol(n, s, source=os.path.basename(path)) for n, s in renderer_symbols(renderer, base)]
    if not cands:
        raise ImportFailed("No symbols found in '{}'.".format(os.path.basename(path)))
    return _dedupe(cands)


_SLD_GEOMS = (("PolygonSymbolizer", "Polygon"), ("LineSymbolizer", "LineString"), ("PointSymbolizer", "Point"))


def from_sld(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as e:
        raise ImportFailed("Cannot read '{}': {}".format(os.path.basename(path), e))
    _read_dom(path)  # validates XML
    geoms = [g for tag, g in _SLD_GEOMS if tag in text]
    if not geoms:
        if "RasterSymbolizer" in text:
            raise ImportFailed("Raster SLD styles are not supported by QGIS import. "
                               "Export the raster style as QML instead.")
        raise ImportFailed("No Point, Line or Polygon symbolizer found in '{}'.".format(os.path.basename(path)))
    base = os.path.splitext(os.path.basename(path))[0]
    cands = []
    errors = []
    for g in geoms:
        layer = QgsVectorLayer("{}?crs=EPSG:4326".format(g), "sld", "memory")
        msg, ok = layer.loadSldStyle(path)
        if not ok:
            errors.append(msg)
            continue
        want = {"Polygon": "fill", "LineString": "line", "Point": "marker"}[g]
        for n, s in renderer_symbols(layer.renderer(), base):
            if symbol_kind(s) == want:
                cands.append(Candidate.from_symbol(n, s, source=os.path.basename(path)))
    if not cands:
        raise ImportFailed("QGIS could not convert '{}'. {}".format(os.path.basename(path), " ".join(errors)))
    return _dedupe(cands)


def from_svg(path, as_marker=True, as_fill=False):
    _read_dom(path)
    base = os.path.splitext(os.path.basename(path))[0]
    cands = []
    if as_marker:
        cands.append(Candidate.from_symbol(base, svg_marker_symbol(path), tags="svg",
                                           source=os.path.basename(path)))
    if as_fill:
        cands.append(Candidate.from_symbol(base + " fill", svg_fill_symbol(path), tags="svg, pattern",
                                           source=os.path.basename(path)))
    return cands


def from_sbag(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        raise ImportFailed("'{}' is not a valid Symbol Bag collection: {}".format(os.path.basename(path), e))
    if not isinstance(data, dict) or data.get("format") != SBAG_FORMAT:
        raise ImportFailed("'{}' is not a Symbol Bag collection file.".format(os.path.basename(path)))
    if int(data.get("version", 0)) > SBAG_VERSION:
        raise ImportFailed("This collection was created by a newer Symbol Bag version.")
    cands = []
    for item in data.get("symbols", []):
        kind = item.get("kind")
        xml = item.get("xml")
        if kind not in ALL_KINDS or not xml:
            continue
        cands.append(Candidate(item.get("name", "Untitled"), kind, xml, item.get("subtype", ""),
                               item.get("tags", ""), item.get("description", ""),
                               item.get("collection") or [], bool(item.get("favorite")),
                               source=os.path.basename(path)))
    if not cands:
        raise ImportFailed("'{}' contains no symbols.".format(os.path.basename(path)))
    return cands


def from_layer(layer):
    """Every symbol / style used by a project layer."""
    if isinstance(layer, QgsRasterLayer):
        xml = raster_style_from_layer(layer)
        return [Candidate(layer.name(), KIND_RASTER, xml, subtype=layer.renderer().type()
                          if layer.renderer() else "", source=layer.name())]
    if isinstance(layer, QgsVectorLayer) and layer.isSpatial():
        return _dedupe([Candidate.from_symbol(n, s, source=layer.name())
                        for n, s in renderer_symbols(layer.renderer(), layer.name())])
    return []


def from_project(project=None):
    project = project or QgsProject.instance()
    cands = []
    for layer in project.mapLayers().values():
        try:
            cands.extend(from_layer(layer))
        except Exception as e:
            log_warning("Skipping layer {}: {}".format(layer.name(), e))
    return _dedupe(cands)


def candidates_from_file(path, svg_mode=("marker",)):
    """Dispatch on file extension. svg_mode: subset of ('marker', 'fill')."""
    if not os.path.isfile(path):
        raise ImportFailed("File not found: {}".format(path))
    ext = os.path.splitext(path)[1].lower()
    if ext == ".qml":
        return from_qml(path)
    if ext in (".xml", ".db"):
        return from_style_file(path)
    if ext == ".sld":
        return from_sld(path)
    if ext == ".svg":
        return from_svg(path, "marker" in svg_mode, "fill" in svg_mode)
    if ext == ".sbag":
        return from_sbag(path)
    raise ImportFailed("Unsupported file type '{}'. Supported: .qml, .xml, .db, .sld, .svg, .sbag".format(ext))


def store_candidates(store, cands, collection_id=None, keep_paths=True):
    """Write candidates to the library. Returns (added_uids, duplicate_count)."""
    added, dupes = [], 0
    for c in cands:
        if not c.selected:
            continue
        cid = collection_id
        if keep_paths and c.collection_path:
            path = list(c.collection_path)
            if collection_id is not None:
                path = store.collection_path(collection_id) + path
            cid = store.ensure_collection_path(path)
        subtype = c.subtype
        if c.kind == KIND_RASTER and not subtype:
            subtype = raster_style_info(c.xml)["renderer"]
        uid, created = store.add_symbol(c.name, c.kind, c.xml, subtype, c.description, c.tags,
                                        cid, c.favorite, notify=False)
        if created:
            added.append(uid)
        else:
            dupes += 1
    store.notify()
    return added, dupes


# ----------------------------------------------------------------------
# exporters
# ----------------------------------------------------------------------
def export_sbag(store, uids, path, root_collection=None):
    """Portable JSON collection (SVGs are already embedded in the XML)."""
    root_path = store.collection_path(root_collection) if root_collection else []
    symbols = []
    for uid in uids:
        rec = store.record(uid)
        if rec is None:
            continue
        cpath = store.collection_path(rec.collection_id) if rec.collection_id else []
        if root_path and cpath[:len(root_path)] == root_path:
            cpath = cpath[len(root_path) - 1:]  # keep the exported collection's own name
        symbols.append({
            "name": rec.name, "kind": rec.kind, "subtype": rec.subtype,
            "xml": store.symbol_xml(uid), "description": rec.description,
            "tags": rec.tags, "favorite": bool(rec.favorite), "collection": cpath,
        })
    data = {"format": SBAG_FORMAT, "version": SBAG_VERSION,
            "exported": datetime.datetime.now().isoformat(timespec="seconds"),
            "count": len(symbols), "symbols": symbols}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, path)
    return len(symbols)


def export_qgis_xml(store, uids, path):
    """QGIS style library (.xml) – loadable in the Style Manager. Vector only."""
    style = QgsStyle()
    style.createMemoryDatabase()
    n = 0
    used = set()
    for uid in uids:
        rec = store.record(uid)
        if rec is None or rec.kind not in VECTOR_KINDS:
            continue
        sym = symbol_from_xml(store.symbol_xml(uid))
        if sym is None:
            continue
        name = rec.name
        i = 2
        while name in used:
            name = "{} ({})".format(rec.name, i)
            i += 1
        used.add(name)
        style.addSymbol(name, sym, True)
        if rec.tag_list:
            style.tagSymbol(SYMBOL_ENTITY, name, rec.tag_list)
        n += 1
    if n == 0:
        raise ImportFailed("Nothing to export: QGIS style libraries only hold point, line and polygon symbols.")
    if not style.exportXml(path):
        raise ImportFailed("QGIS could not write '{}'.".format(path))
    return n


def export_raster_qml(xml, path):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(xml)


def save_to_qgis_style(name, kind, xml, tags=(), overwrite=False):
    """Add a symbol to the QGIS Style Manager (default style database).

    Raster pseudocolor styles are saved as a colour ramp.
    Returns a short description of what was saved.
    """
    style = QgsStyle.defaultStyle()
    if kind in VECTOR_KINDS:
        sym = symbol_from_xml(xml)
        if sym is None:
            raise ImportFailed("The symbol definition could not be read.")
        if name in style.symbolNames() and not overwrite:
            raise FileExistsError(name)
        if not style.addSymbol(name, sym, True):
            raise ImportFailed("QGIS refused to save the symbol.")
        tags = list(tags) + ["Symbol Bag"]
        style.tagSymbol(SYMBOL_ENTITY, name, tags)
        return "symbol"
    # raster: convert pseudocolor ramp
    from .symbols import sample_raster_layer, apply_raster_style
    layer = sample_raster_layer()
    apply_raster_style(layer, xml, stretch=False)
    r = layer.renderer()
    if r is None or r.type() != "singlebandpseudocolor":
        raise ImportFailed("Only pseudocolor raster styles can be saved to the Style Manager "
                           "(as a colour ramp). Export this style as QML instead.")
    items = r.shader().rasterShaderFunction().colorRampItemList()
    if len(items) < 2:
        raise ImportFailed("This raster style has fewer than two colours.")
    vmin, vmax = items[0].value, items[-1].value
    span = (vmax - vmin) or 1.0
    stops = [QgsGradientStop(min(1.0, max(0.0, (it.value - vmin) / span)), it.color) for it in items[1:-1]]
    ramp = QgsGradientColorRamp(items[0].color, items[-1].color, False, stops)
    if name in style.colorRampNames() and not overwrite:
        raise FileExistsError(name)
    style.addColorRamp(name, ramp, True)
    return "color ramp"

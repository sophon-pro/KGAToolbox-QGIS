"""
Layer Auto-Sort: ordering logic for the Layers panel (no GUI code here).

ArcGIS Pro style: points on top, then lines, polygons, rasters at the bottom.
Uses the scoped enums Qgis.LayerType / Qgis.GeometryType (QGIS 3.30+, Qt5 and Qt6).
"""
from contextlib import contextmanager

from qgis.core import Qgis, QgsLayerTree

# (key, label) in the default top -> bottom order, ArcGIS Pro style.
CATEGORIES = [
    ("annotation", "Annotation layers"),
    ("table", "Tables (no geometry)"),
    ("point", "Point layers"),
    ("line", "Line layers"),
    ("polygon", "Polygon layers"),
    ("vector_other", "Vector, unknown geometry"),
    ("vector_tile", "Vector tiles"),
    ("point_cloud", "Point clouds"),
    ("tiled_scene", "3D tiles / tiled scenes"),
    ("mesh", "Mesh layers"),
    ("raster", "Rasters (local files: DEM, ortho, imagery)"),
    ("web_raster", "Web rasters / basemaps (WMS, WMTS, XYZ, WCS, ArcGIS)"),
    ("other", "Other layer types"),
]
DEFAULT_ORDER = [key for key, _ in CATEGORIES]

# Raster data providers treated as online basemaps (kept below local rasters).
WEB_RASTER_PROVIDERS = {"wms", "wcs", "arcgismapserver"}


_paused = 0


@contextmanager
def paused():
    """Layers added inside this block are left where the code put them.
    For code that rebuilds a saved layout, such as opening a layer package."""
    global _paused
    _paused += 1
    try:
        yield
    finally:
        _paused -= 1


def is_paused():
    return _paused > 0


def layer_category(layer):
    """Return the category key for a map layer."""
    if layer is None:
        return "other"
    lt = Qgis.LayerType
    t = layer.type()

    if t == lt.Vector:
        if not layer.isSpatial():
            return "table"
        gt = Qgis.GeometryType
        g = layer.geometryType()
        if g == gt.Point:
            return "point"
        if g == gt.Line:
            return "line"
        if g == gt.Polygon:
            return "polygon"
        return "vector_other"
    if t == lt.Raster:
        if layer.providerType() in WEB_RASTER_PROVIDERS:
            return "web_raster"
        return "raster"
    if t == lt.VectorTile:
        return "vector_tile"
    if t == lt.PointCloud:
        return "point_cloud"
    if t == lt.TiledScene:
        return "tiled_scene"
    if t == lt.Mesh:
        return "mesh"
    if t == lt.Annotation:
        return "annotation"
    return "other"


class Ranker:
    """Maps a layer to its position in the user's category order.
    Lower rank = higher in the Layers panel = drawn on top."""

    def __init__(self, order=None):
        order = [k for k in (order or []) if k in DEFAULT_ORDER]
        order += [k for k in DEFAULT_ORDER if k not in order]
        self.order = order
        self._index = {k: i for i, k in enumerate(order)}

    def __call__(self, layer):
        return self._index[layer_category(layer)]

    def node_rank(self, node):
        return self(node.layer())


def _move_up_to(parent, node, index):
    """Move `node` (currently at or below `index`) to `index` in `parent`.
    The clone is inserted before the original is removed, so the layer is
    never dropped from the project; visibility, expansion and properties
    travel with the clone."""
    if parent.children()[index] is node:
        return node
    clone = node.clone()
    parent.insertChildNode(index, clone)
    parent.removeChildNode(node)
    return clone


def sort_group(group, ranker, recursive=True):
    """Stable-sort the layers of `group` by category. Sub-groups keep their
    slots; their contents are sorted too when `recursive`.
    Returns how many nodes were moved."""
    children = group.children()
    slots = [i for i, c in enumerate(children) if QgsLayerTree.isLayer(c)]
    ordered = sorted((children[i] for i in slots), key=ranker.node_rank)

    target = list(children)
    for slot, node in zip(slots, ordered):
        target[slot] = node

    moved = 0
    for i, node in enumerate(target):
        if group.children()[i] is not node:
            _move_up_to(group, node, i)
            moved += 1

    if recursive:
        for child in group.children():
            if QgsLayerTree.isGroup(child):
                moved += sort_group(child, ranker, True)
    return moved


def place_layer(root, layer, ranker):
    """Place a newly added layer at the top of its category band inside the
    group it was added to (ArcGIS Pro behaviour). Returns its tree node."""
    node = root.findLayer(layer.id())
    if node is None:
        return None
    parent = node.parent()
    rank = ranker(layer)
    children = parent.children()
    idx = children.index(node)

    def is_layer(c):
        return c is not node and QgsLayerTree.isLayer(c)

    # Already fine? Every layer above ranks higher, the next one below
    # ranks the same or lower.
    above_ok = all(ranker.node_rank(c) < rank for c in children[:idx] if is_layer(c))
    below = next((c for c in children[idx + 1:] if is_layer(c)), None)
    if above_ok and (below is None or ranker.node_rank(below) >= rank):
        return node

    # Insert just above the first layer of the same or a lower band.
    anchor = next((c for c in children if is_layer(c) and ranker.node_rank(c) >= rank), None)
    insert_at = children.index(anchor) if anchor is not None else len(children)
    clone = node.clone()
    parent.insertChildNode(insert_at, clone)
    parent.removeChildNode(node)
    return clone


def sort_custom_order(root, ranker, new_ids=()):
    """If the Layer Order panel's 'Control rendering order' is on, sort that
    order too. Newly added layers go to the top of their band."""
    if not root.hasCustomLayerOrder():
        return False
    current = root.customLayerOrder()
    new_ids = set(new_ids)
    ordered = sorted(current, key=lambda lyr: (ranker(lyr), 0 if lyr.id() in new_ids else 1))
    if [lyr.id() for lyr in ordered] != [lyr.id() for lyr in current]:
        root.setCustomLayerOrder(ordered)
        return True
    return False


# ---------------------------------------------------------------- settings --
SETTINGS_PREFIX = "KGA/LayerAutoSort/"


def load_settings():
    from qgis.core import QgsSettings
    s = QgsSettings()
    raw = s.value(SETTINGS_PREFIX + "order", "", type=str)
    return {
        "order": [k for k in raw.split(",") if k] if raw else list(DEFAULT_ORDER),
        # A background feature: on from the start, switched off in the
        # KGA Toolbox > Layer Auto-Sort menu.
        "auto_sort": s.value(SETTINGS_PREFIX + "autoSort", True, type=bool),
        "sync_layer_order": s.value(SETTINGS_PREFIX + "syncLayerOrderPanel", True, type=bool),
    }


def save_setting(name, value):
    from qgis.core import QgsSettings
    QgsSettings().setValue(SETTINGS_PREFIX + name, value)

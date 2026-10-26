# -*- coding: utf-8 -*-
"""
Print Layout support.

* "Fit for" a layout map: computes label spacing in real paper millimetres
  for a given layout map item (instead of the screen canvas), so organized
  columns / key boxes fit the printed scale.
* "Put key in Print Layout": writes the number key as layout text boxes next
  to the map frame, grouped so they move together and can be freely edited.
"""

from contextlib import suppress
from qgis.PyQt.QtGui import QColor
from qgis.core import (
    Qgis,
    QgsCoordinateTransform,
    QgsLayoutItemLabel,
    QgsLayoutItemMap,
    QgsLayoutMeasurement,
    QgsLayoutPoint,
    QgsLayoutSize,
    QgsPointXY,
    QgsPrintLayout,
    QgsProject,
    QgsUnitTypes,
)

from . import engine as E
from . import numberkey as K
from ..compat import enum_value

KEY_ID_PREFIX = "Callout Studio key"


def _mm():
    return enum_value((Qgis, 'LayoutUnit.Millimeters'), (QgsUnitTypes, 'LayoutMillimeters'))


def _to_mm(layout, size_or_point):
    conv = layout.renderContext().measurementConverter()
    return conv.convert(size_or_point, _mm())


# --------------------------------------------------------------------------
# finding layouts / map frames
# --------------------------------------------------------------------------

def layout_maps():
    """[(display text, layout name, map uuid)] for every map frame in the project."""
    out = []
    for lay in QgsProject.instance().layoutManager().printLayouts():
        maps = [i for i in lay.items() if isinstance(i, QgsLayoutItemMap)]
        for m in maps:
            label = lay.name() if len(maps) == 1 else "{} — {}".format(lay.name(), m.displayName())
            out.append((label, lay.name(), m.uuid()))
    return out


def find_map(layout_name, uuid):
    lay = QgsProject.instance().layoutManager().layoutByName(layout_name)
    if lay is None:
        return None, None
    item = lay.itemByUuid(uuid)
    return (lay, item) if isinstance(item, QgsLayoutItemMap) else (lay, None)


def mm_units_for_map_item(map_item, layer):
    """Layer-CRS units covered by one millimetre of paper in this map frame."""
    lay = map_item.layout()
    w_mm = _to_mm(lay, map_item.sizeWithUnits()).width()
    ext = map_item.extent()
    if w_mm <= 0 or ext.width() <= 0:
        return None
    per_mm = ext.width() / w_mm
    try:
        ct = QgsCoordinateTransform(map_item.crs(), layer.crs(), QgsProject.instance())
        c = ext.center()
        p0 = ct.transform(c)
        p1 = ct.transform(QgsPointXY(c.x() + per_mm * 10, c.y()))
        d = ((p1.x() - p0.x()) ** 2 + (p1.y() - p0.y()) ** 2) ** 0.5 / 10.0
        return d if d > 0 else per_mm
    except Exception:
        return per_mm


def create_layout_for(layer, name="Callout Studio map"):
    """A4 landscape layout with one map frame zoomed to the layer, leaving room on the right for a key."""
    mgr = QgsProject.instance().layoutManager()
    base, n = name, 2
    while mgr.layoutByName(name) is not None:
        name = "{} {}".format(base, n)
        n += 1
    lay = QgsPrintLayout(QgsProject.instance())
    lay.initializeDefaults()          # A4 landscape
    lay.setName(name)
    mgr.addLayout(lay)
    m = QgsLayoutItemMap(lay)
    m.attemptMove(QgsLayoutPoint(10, 10, _mm()))
    m.attemptResize(QgsLayoutSize(190, 190, _mm()))
    lay.addLayoutItem(m)
    crs = QgsProject.instance().crs()
    ext = layer.extent()
    if crs.isValid() and crs != layer.crs():
        ext = QgsCoordinateTransform(layer.crs(), crs, QgsProject.instance()).transformBoundingBox(ext)
        m.setCrs(crs)
    else:
        m.setCrs(layer.crs())
    ext.scale(1.1)
    m.zoomToExtent(ext)
    m.setFrameEnabled(True)
    return lay, m


# --------------------------------------------------------------------------
# key as layout text boxes
# --------------------------------------------------------------------------

def remove_existing_key(layout):
    """Delete key boxes created earlier, so re-sending replaces them."""
    from qgis.core import QgsLayoutItem, QgsLayoutItemGroup
    for g in [i for i in layout.items()
              if isinstance(i, QgsLayoutItemGroup) and i.id().startswith(KEY_ID_PREFIX)]:
        layout.ungroupItems(g)
    for i in [i for i in layout.items()
              if isinstance(i, QgsLayoutItem) and i.id().startswith(KEY_ID_PREFIX)]:
        layout.removeLayoutItem(i)


def send_key_to_layout(layer, cfg, expression, is_expression, filter_expression, layout, map_item):
    """Create (or replace) the key boxes beside map_item. Returns (n_boxes, overflows_page)."""
    cfg = E.merged({}, cfg)
    kind = E.geometry_kind(layer)
    res = K.number_key_entries(layer, cfg, expression, is_expression, filter_expression, kind)
    if res is None:
        return 0, False
    ordered, codes, names, _ = res
    columns = K.key_columns(cfg, ordered, codes, names)

    remove_existing_key(layout)

    pos = _to_mm(layout, map_item.positionWithUnits())
    size = _to_mm(layout, map_item.sizeWithUnits())
    mx, my, mw, mh = pos.x(), pos.y(), size.width(), size.height()
    gap = 4.0

    fmt = E.build_text_format(dict(cfg, buffer=False))
    items = []
    for i, (_, lines) in enumerate(columns):
        lab = QgsLayoutItemLabel(layout)
        lab.setText("\n".join(lines))
        try:
            lab.setTextFormat(fmt)
        except AttributeError:          # QGIS < 3.24
            lab.setFont(fmt.font())
            lab.setFontColor(QColor(cfg["text_color"]))
        lab.setMarginX(2.0)
        lab.setMarginY(1.5)
        lab.setBackgroundEnabled(bool(cfg["key_box"]))
        lab.setBackgroundColor(QColor(cfg["key_box_fill"]))
        lab.setFrameEnabled(bool(cfg["key_box"]))
        lab.setFrameStrokeColor(QColor(cfg["key_box_stroke"]))
        lab.setFrameStrokeWidth(QgsLayoutMeasurement(0.3, _mm()))
        lab.setId("{} {}".format(KEY_ID_PREFIX, i + 1))
        layout.addLayoutItem(lab)
        lab.adjustSizeToText()
        items.append(lab)

    sizes = [_to_mm(layout, it.sizeWithUnits()) for it in items]
    total_w = sum(s.width() for s in sizes) + gap * (len(sizes) - 1)
    max_h = max(s.height() for s in sizes)
    where = cfg["key_position"]
    if where == "left":
        x, y = mx - gap - total_w, my
    elif where == "below":
        x, y = mx, my + mh + gap
    elif where == "above":
        x, y = mx, my - gap - max_h
    else:
        x, y = mx + mw + gap, my
    for it, s in zip(items, sizes):
        it.attemptMove(QgsLayoutPoint(x, y, _mm()), page=map_item.page())
        x += s.width() + gap

    # does it still fit on the page?
    overflow = False
    with suppress(Exception):
        page = layout.pageCollection().page(map_item.page())
        psize = _to_mm(layout, page.pageSize())
        last = items[-1]
        lp = _to_mm(layout, last.positionWithUnits())
        first = _to_mm(layout, items[0].positionWithUnits())
        overflow = (first.x() < 0 or first.y() < 0 or
                    lp.x() + sizes[-1].width() > psize.width() + 0.01 or
                    first.y() + max_h > psize.height() + 0.01)

    if len(items) > 1:
        grp = layout.groupItems(items)
        if grp is not None:
            grp.setId(KEY_ID_PREFIX)
    layout.refresh()
    return len(items), overflow

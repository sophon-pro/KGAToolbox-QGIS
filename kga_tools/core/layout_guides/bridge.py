# -*- coding: utf-8 -*-
"""
Layout Guides - everything that talks to the QGIS layout API.

Works with QGIS 3.22+ (Qt5) and QGIS 4.x (Qt6). All values coming in and
going out of this module are millimetres, whatever units the layout uses.
"""

from contextlib import suppress
from contextlib import contextmanager

from qgis.PyQt.QtCore import Qt
from qgis.core import Qgis, QgsLayoutGuide, QgsLayoutMeasurement, QgsUnitTypes

from ..compat import enum_value

MM = enum_value((Qgis, 'LayoutUnit.Millimeters'), (QgsUnitTypes, 'LayoutMillimeters'))   # Qgis.LayoutUnit: QGIS 3.30+

H = Qt.Orientation.Horizontal  # horizontal line -> Y position
V = Qt.Orientation.Vertical    # vertical line   -> X position


def to_qt(o):
    return H if o == "h" else V


def from_qt(orientation):
    return "h" if orientation == H else "v"


# ---------------------------------------------------------------------------
# unit helpers
# ---------------------------------------------------------------------------
def layout_to_mm(layout, value):
    """Layout units -> mm."""
    return layout.convertFromLayoutUnits(value, MM).length()


def mm_to_layout(layout, value):
    """mm -> layout units."""
    return layout.convertToLayoutUnits(QgsLayoutMeasurement(value, MM))


def measurement_to_mm(layout, measurement):
    conv = layout.renderContext().measurementConverter()
    return conv.convert(measurement, MM).length()


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------
def page_count(layout):
    if layout is None:
        return 0
    return layout.pageCollection().pageCount()


def page_size_mm(layout, index):
    """(width_mm, height_mm) of a page."""
    page = layout.pageCollection().page(index)
    if page is None:
        return 0.0, 0.0
    r = page.rect()
    return layout_to_mm(layout, r.width()), layout_to_mm(layout, r.height())


def page_description(layout, index):
    w, h = page_size_mm(layout, index)
    name = ""
    with suppress(Exception):
        from qgis.core import QgsApplication
        size = QgsApplication.pageSizeRegistry().find(
            layout.pageCollection().page(index).pageSize())
        if size:
            name = size + " "
    orient = "Landscape" if w > h else ("Portrait" if h > w else "Square")
    return "%s%s  %s x %s mm" % (name, orient, _f(w), _f(h))


def _f(v):
    return ("%.1f" % v).rstrip("0").rstrip(".")


# ---------------------------------------------------------------------------
# undo
# ---------------------------------------------------------------------------
@contextmanager
def undo_command(layout, text, merge_id=0):
    """Make a whole batch of guide edits one Ctrl+Z step.
    Commands with the same non-zero merge_id in a row collapse into one step
    (so scrolling a spin box does not flood the undo history)."""
    stack = None
    try:
        stack = layout.undoStack()
        try:
            stack.beginCommand(layout.guides(), text, merge_id)
        except TypeError:
            stack.beginCommand(layout.guides(), text)
    except Exception:
        stack = None
    try:
        yield
    finally:
        if stack is not None:
            with suppress(Exception):
                stack.endCommand()


# ---------------------------------------------------------------------------
# guides
# ---------------------------------------------------------------------------
def make_guide(layout, page_index, orientation, pos_mm):
    page = layout.pageCollection().page(page_index)
    return QgsLayoutGuide(to_qt(orientation), QgsLayoutMeasurement(float(pos_mm), MM), page)


def guide_page_index(layout, guide):
    try:
        return layout.pageCollection().pageNumber(guide.page())
    except Exception:
        return 0


def list_guides(layout):
    """[(guide_object, page_index, 'h'|'v', pos_mm)] sorted for display."""
    out = []
    if layout is None:
        return out
    for g in layout.guides().guides():
        out.append((g, guide_page_index(layout, g), from_qt(g.orientation()),
                    measurement_to_mm(layout, g.position())))
    out.sort(key=lambda t: (t[1], 0 if t[2] == "v" else 1, t[3]))
    return out


def clear_page(layout, page_index):
    coll = layout.guides()
    for g in list(coll.guidesOnPage(page_index)):
        coll.removeGuide(g)


def add_guides(layout, page_index, guides):
    """guides: iterable of (orientation, pos_mm[, tag]). Returns count added.
    Skips guides that already exist at the same spot."""
    coll = layout.guides()
    existing = set()
    for g in coll.guidesOnPage(page_index):
        existing.add((from_qt(g.orientation()), round(measurement_to_mm(layout, g.position()), 2)))
    added = 0
    for item in guides:
        o, pos = item[0], item[1]
        key = (o, round(pos, 2))
        if key in existing:
            continue
        existing.add(key)
        coll.addGuide(make_guide(layout, page_index, o, pos))
        added += 1
    return added


def set_guide_position(layout, guide, pos_mm):
    guide.setPosition(QgsLayoutMeasurement(float(pos_mm), MM))
    layout.guides().update()


def remove_guides(layout, guides):
    coll = layout.guides()
    for g in guides:
        coll.removeGuide(g)


def clear_all(layout):
    layout.guides().clear()


def copy_page_to_all(layout, source_page):
    layout.guides().applyGuidesToAllOtherPages(source_page)


# ---------------------------------------------------------------------------
# view / snapping
# ---------------------------------------------------------------------------
def guides_visible(layout):
    return layout.guides().visible()


def set_guides_visible(layout, on):
    layout.guides().setVisible(bool(on))


def snapper(layout):
    return layout.snapper()


# ---------------------------------------------------------------------------
# selection
# ---------------------------------------------------------------------------
def selected_item_rects(layout):
    """[(page_index, x, y, w, h)] in mm, relative to each item's page."""
    out = []
    for item in layout.selectedLayoutItems(False):
        with suppress(Exception):
            page = item.page()
            pos = item.pagePos()
            r = item.rect()
            out.append((page,
                        layout_to_mm(layout, pos.x()), layout_to_mm(layout, pos.y()),
                        layout_to_mm(layout, r.width()), layout_to_mm(layout, r.height())))
    return out

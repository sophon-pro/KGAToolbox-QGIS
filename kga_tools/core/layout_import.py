# -*- coding: utf-8 -*-
"""Create a layout, or bring one in from a template or another project.

Two sources are understood: a QGIS layout template (`.qpt`), and a QGIS project
(`.qgz` / `.qgs`), from which chosen print layouts are copied. Either way the
result is a new print layout of the open project with a name that is not taken.
"""

import os
import tempfile
from contextlib import suppress

from qgis.core import (
    Qgis,
    QgsFillSymbol,
    QgsLayoutItemMap,
    QgsLayoutItemShape,
    QgsLayoutSize,
    QgsMapLayer,
    QgsPrintLayout,
    QgsProject,
    QgsReadWriteContext,
    QgsUnitTypes,
    QgsVectorFileWriter,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QRectF
from qgis.PyQt.QtXml import QDomDocument

from . import output_defaults as od
from .compat import enum_value

TEMPLATE_EXT = '.qpt'
PROJECT_EXTS = ('.qgz', '.qgs')


def unique_name(project, base):
    """`base`, or `base 1`, `base 2`... when a layout of that name exists."""
    manager = project.layoutManager()
    base = (base or '').strip() or 'Layout'
    name, number = base, 1
    while manager.layoutByName(name) is not None:
        name = '{} {}'.format(base, number)
        number += 1
    return name


#: Commonly used page sizes as (width, height) in millimetres, portrait.
PAGE_SIZES = (
    ('A0', 841.0, 1189.0),
    ('A1', 594.0, 841.0),
    ('A2', 420.0, 594.0),
    ('A3', 297.0, 420.0),
    ('A4', 210.0, 297.0),
    ('A5', 148.0, 210.0),
    ('Letter', 215.9, 279.4),
    ('Legal', 215.9, 355.6),
    ('Tabloid', 279.4, 431.8),
)
DEFAULT_SIZE = 'A4'
NEATLINE_INSET = 5.0  # mm between the page edge and the neatline
NEATLINE_WIDTH = 0.5  # mm


def page_dimensions(size, landscape, width=None, height=None):
    """(width, height) in mm for a named size, or for `width` x `height`."""
    if size is None or size == 'Custom':
        w, h = float(width or 0), float(height or 0)
    else:
        w, h = _named(size)
    short, long_ = min(w, h), max(w, h)
    return (long_, short) if landscape else (short, long_)


def _named(size):
    for name, w, h in PAGE_SIZES:
        if name == size:
            return w, h
    return 210.0, 297.0


def apply_page_options(layout, width, height, background=None, neatline=False,
                       neatline_offset=NEATLINE_INSET):
    """Size the first page, colour its background and optionally frame it."""
    page = layout.pageCollection().page(0)
    page.setPageSize(QgsLayoutSize(width, height, enum_value(
        (Qgis, 'LayoutUnit.Millimeters'), (QgsUnitTypes, 'LayoutMillimeters'))))
    if background is not None and background.isValid():
        symbol = QgsFillSymbol.createSimple({
            'color': background.name(),
            'outline_style': 'no',
        })
        page.setPageStyleSymbol(symbol)
    if neatline:
        add_neatline(layout, width, height, neatline_offset)


def add_neatline(layout, width, height, inset=NEATLINE_INSET):
    """A thin unfilled rectangle `inset` mm in from the page edge."""
    frame = QgsLayoutItemShape(layout)
    frame.setShapeType(QgsLayoutItemShape.Shape.Rectangle)
    frame.attemptSetSceneRect(QRectF(
        inset, inset, width - 2 * inset, height - 2 * inset))
    frame.setSymbol(QgsFillSymbol.createSimple({
        'color': '0,0,0,0',
        'outline_color': '0,0,0,255',
        'outline_width': str(NEATLINE_WIDTH),
        'outline_width_unit': 'MM',
    }))
    frame.setId('Neatline')
    layout.addLayoutItem(frame)
    frame.setLocked(True)
    return frame


def new_layout(project, name, width=None, height=None, background=None,
               neatline=False, neatline_offset=NEATLINE_INSET):
    """A blank print layout added to `project`; None if QGIS refuses it.

    Without a size it is one A4 portrait page, as QGIS makes it.
    """
    layout = QgsPrintLayout(project)
    layout.initializeDefaults()
    layout.setName(unique_name(project, name))
    if width and height:
        apply_page_options(layout, width, height, background, neatline,
                           neatline_offset)
    return layout if project.layoutManager().addLayout(layout) else None


def from_template(project, path, name=None):
    """A layout added to `project` from the `.qpt` file at `path`, or None."""
    try:
        with open(path, 'rb') as handle:
            content = handle.read()
    except OSError:
        return None
    document = QDomDocument()
    if not document.setContent(content):
        return None
    layout = QgsPrintLayout(project)
    _items, loaded = layout.loadFromTemplate(document, QgsReadWriteContext())
    if not loaded:
        return None
    base = name or os.path.splitext(os.path.basename(path))[0]
    layout.setName(unique_name(project, base))
    return layout if project.layoutManager().addLayout(layout) else None


def read_project(path):
    """The project at `path` as a separate QgsProject, or None if unreadable."""
    if not path or not os.path.isfile(path):
        return None
    other = QgsProject()
    return other if other.read(path) else None


def print_layouts(other):
    """[name] of the print layouts of a project read with `read_project`."""
    return sorted(layout.name() for layout in other.layoutManager().printLayouts())


def from_project(project, other, name):
    """Copy the print layout `name` of project `other` into `project`."""
    source = other.layoutManager().layoutByName(name)
    if source is None:
        return None
    handle, path = tempfile.mkstemp(suffix=TEMPLATE_EXT)
    os.close(handle)
    try:
        if not source.saveAsTemplate(path, QgsReadWriteContext()):
            return None
        return from_template(project, path, name)
    finally:
        with suppress(OSError):
            os.remove(path)


# ------------------------------------------------- layout data and styles

def _map_items(layout):
    return [item for item in layout.items() if isinstance(item, QgsLayoutItemMap)]


def _item_key(item):
    """Where a map frame is; the copy of a layout keeps its frames in place."""
    rect = item.sceneBoundingRect()
    return (item.page(), round(rect.x(), 1), round(rect.y(), 1),
            round(rect.width(), 1), round(rect.height(), 1))


def item_layers(other, item):
    """The layers of project `other` a map frame draws."""
    if item.keepLayerSet():
        return [layer for layer in item.layers() if layer is not None]
    if item.followVisibilityPreset():
        return list(other.mapThemeCollection().mapThemeVisibleLayers(
            item.followVisibilityPresetName()))
    return list(other.layerTreeRoot().checkedLayers())


def layout_layers(other, layout):
    """The layers `layout` shows, without repeats, in drawing order."""
    found, seen = [], set()
    for item in _map_items(layout):
        for layer in item_layers(other, item):
            if layer.id() not in seen:
                seen.add(layer.id())
                found.append(layer)
    return found


def _copy_style(source, target):
    document = QDomDocument()
    source.exportNamedStyle(document)
    target.importNamedStyle(document)
    # the GeoPackage keeps it too, so the style travels with the table
    with suppress(AttributeError, RuntimeError):
        target.saveStyleToDatabase(target.name(), '', True, '')


def _write_vector(layer, database, table, project):
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = 'GPKG'
    options.layerName = table
    options.fileEncoding = 'UTF-8'
    options.actionOnExistingFile = enum_value(
        (QgsVectorFileWriter, 'ActionOnExistingFile.CreateOrOverwriteLayer'),
        (QgsVectorFileWriter, 'CreateOrOverwriteLayer'))
    result = QgsVectorFileWriter.writeAsVectorFormatV3(
        layer, database, project.transformContext(), options)
    return result[0] == enum_value(
        (Qgis, 'VectorExportResult.Success'), (QgsVectorFileWriter, 'WriterError.NoError'),
        (QgsVectorFileWriter, 'NoError'))


def bring_layer(project, layer, database, taken=(), in_tree=True):
    """Copy `layer` of another project into `project`; (new layer, note).

    A vector layer is written to a new table of `database`, named after the
    layer and made unique, and added with its style. Anything else (rasters,
    web layers) is added where it is, as a copy of the layer definition. The
    note says what happened, for the user; the layer is None if it failed.
    With `in_tree` False the layer joins the project only, not its layer tree.
    """
    in_place = (not database or layer.type() != enum_value((Qgis, 'LayerType.Vector'), (QgsMapLayer, 'VectorLayer'))
                or not layer.isValid())
    if not in_place:
        table = od.unique_table(database, od.safe_stem(layer.name(), 'Layer'),
                                taken)
        if _write_vector(layer, database, table, project):
            uri = '{}|layername={}'.format(database.replace('\\', '/'), table)
            new = QgsVectorLayer(uri, layer.name(), 'ogr')
            if new.isValid():
                names = {lyr.name() for lyr in project.mapLayers().values()}
                if new.name() in names:
                    new.setName(table)
                _copy_style(layer, new)
                project.addMapLayer(new, in_tree)
                return new, 'copied to table {}'.format(table), table
        in_place = True         # the copy failed: refer to the original data
    new = layer.clone()
    if new is None:
        return None, 'could not be copied', ''
    project.addMapLayer(new, in_tree)
    return new, 'referenced in place', ''


def import_with_data(project, other, name, database, cache, group=None):
    """Import layout `name` of `other` together with the layers it draws.

    `cache` maps a source layer id to the copy made earlier in the same
    import, so layouts that share data share the copy. Returns
    (layout or None, [(layer name, what happened)]). With a layer tree `group`
    the layers go there (all of the layout's layers, shared ones included)
    instead of into the project's own tree.
    """
    source = other.layoutManager().layoutByName(name)
    if source is None:
        return None, []
    notes = []
    layers = layout_layers(other, source)
    for layer in layers:
        if layer.id() in cache:
            continue
        taken = [table for _lyr, table in cache.values() if table]
        new, note, table = bring_layer(project, layer, database, taken,
                                       in_tree=group is None)
        notes.append((layer.name(), note))
        if new is not None:
            cache[layer.id()] = (new, table)
    if group is not None:
        for layer in layers:
            if layer.id() in cache:
                group.addLayer(cache[layer.id()][0])
    layout = from_project(project, other, name)
    if layout is None:
        return None, notes
    frames = {_item_key(item): item for item in _map_items(source)}
    for item in _map_items(layout):
        original = frames.get(_item_key(item))
        if original is None or not (original.keepLayerSet()
                                    or original.followVisibilityPreset()):
            continue
        copies = [cache[lyr.id()][0] for lyr in item_layers(other, original)
                  if lyr.id() in cache]
        item.setFollowVisibilityPreset(False)
        item.setKeepLayerSet(True)
        item.setLayers(copies)
        item.setLayerStyleOverrides({})
    return layout, notes

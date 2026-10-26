# -*- coding: utf-8 -*-
"""Add Quick Template: the logic behind the window.

No widgets here. This builds an empty point, line or polygon layer from a list
of field specs, and writes it to a Shapefile, GeoPackage or File Geodatabase
when the user does not want a temporary layer.

The default fields are the ones every KGA survey layer carries: ``name``, ``id``
and ``note``, plus one measurement that depends on the geometry. The
measurement is filled in by a default-value expression, so it is correct the
moment a feature is drawn.
"""

import os
from collections import namedtuple

from qgis.core import (QgsCoordinateReferenceSystem, QgsDefaultValue,
                       QgsVectorFileWriter, QgsVectorLayer)

from . import compat

GEOM_POINT, GEOM_LINE, GEOM_POLYGON = 'point', 'line', 'polygon'

#: (key, label, memory-provider geometry name, default layer name). Every
#: layer is multipart, as in ArcGIS Pro, so nobody has to ask whether a
#: feature may need more than one part.
GEOMETRIES = (
    (GEOM_POINT, 'Point', 'MultiPoint', 'Point Template'),
    (GEOM_LINE, 'Line', 'MultiLineString', 'Line Template'),
    (GEOM_POLYGON, 'Polygon', 'MultiPolygon', 'Polygon Template'),
)

DEFAULT_CRS = 'EPSG:32648'            # WGS 84 / UTM zone 48N

OUT_TEMP, OUT_SHP, OUT_GPKG, OUT_GDB = 'temp', 'shp', 'gpkg', 'gdb'
#: (key, label, file suffix, file dialog filter)
OUTPUTS = (
    (OUT_TEMP, 'Temporary layer (memory)', '', ''),
    (OUT_SHP, 'ESRI Shapefile (.shp)', '.shp', 'ESRI Shapefile (*.shp)'),
    (OUT_GPKG, 'GeoPackage (.gpkg)', '.gpkg', 'GeoPackage (*.gpkg)'),
    (OUT_GDB, 'File Geodatabase (.gdb)', '.gdb', 'File Geodatabase (*.gdb)'),
)

#: Names a File Geodatabase or GeoPackage adds on its own. Offered, but off.
SYSTEM_FIELDS = frozenset(('fid', 'objectid', 'oid', 'shape', 'shape_length',
                           'shape_area', 'shape_leng', 'globalid'))

FieldSpec = namedtuple(
    'FieldSpec', 'name type_id length precision expression note')


class TemplateError(Exception):
    """Something the user can fix; the message is shown as it is."""


def spec(name, type_id, length=0, precision=0, expression='', note=''):
    return FieldSpec(name, type_id, length, precision, expression, note)


def geometry_label(key):
    return next(g[1] for g in GEOMETRIES if g[0] == key)


def default_layer_name(key):
    return next(g[3] for g in GEOMETRIES if g[0] == key)


def default_fields(key):
    """The fields used when nothing is copied from another layer."""
    fields = [
        spec('name', compat.T_STRING, 100, note='text'),
        spec('id', compat.T_INT, 10, note='integer'),
        spec('note', compat.T_STRING, 254, note='text'),
    ]
    if key == GEOM_LINE:
        fields.append(spec('length_m', compat.T_DOUBLE, 20, 3,
                           'round($length, 3)', 'real, filled in on drawing'))
    elif key == GEOM_POLYGON:
        fields.append(spec('area_m2', compat.T_DOUBLE, 20, 3,
                           'round($area, 3)', 'real, filled in on drawing'))
    else:
        fields.append(spec('xy', compat.T_STRING, 60,
                           expression="to_string(round($x, 3)) || ', ' || "
                                      "to_string(round($y, 3))",
                           note='text, filled in on drawing'))
    return fields


def fields_of_layer(layer):
    """Every field of `layer` as specs, skipping nothing; see `is_system`."""
    return [spec(f.name(), int(f.type()), f.length(), f.precision(),
                 note=f.typeName() or compat.type_name(int(f.type())))
            for f in layer.fields()]


def is_system(layer, name):
    """True for fields the container maintains itself (id, Shape_Area ...)."""
    if name.lower() in SYSTEM_FIELDS:
        return True
    try:
        pk = {layer.fields().at(i).name() for i in layer.primaryKeyAttributes()}
    except (AttributeError, RuntimeError):
        pk = set()
    return name in pk


def problems(fields):
    """Reasons `fields` cannot be created, as a list of messages."""
    found = []
    seen = set()
    for f in fields:
        low = f.name.lower()
        if not f.name.strip():
            found.append('A field has no name.')
        elif low in seen:
            found.append('Field "{}" appears twice.'.format(f.name))
        seen.add(low)
    return found


def warnings(fields, output):
    """Things worth a second look that do not stop the layer being made."""
    found = []
    if output == OUT_SHP:
        long_names = [f.name for f in fields if len(f.name) > 10]
        if long_names:
            found.append('A Shapefile cuts field names to 10 characters, so '
                         'these will be shortened: {}.'.format(
                             ', '.join(long_names[:6])))
    return found


def build_layer(key, name, crs, fields):
    """An empty in-memory layer of the right geometry with `fields`."""
    geometry = next(g[2] for g in GEOMETRIES if g[0] == key)
    if not crs.isValid():
        raise TemplateError('Choose a valid coordinate reference system.')
    layer = QgsVectorLayer('{}?crs={}'.format(geometry, crs.authid() or
                                              crs.toWkt()), name, 'memory')
    if not layer.isValid():
        raise TemplateError('QGIS could not create the temporary layer.')
    if not crs.authid():
        layer.setCrs(crs)
    if fields:
        layer.dataProvider().addAttributes(
            [compat.make_field(f.name, f.type_id, f.length, f.precision)
             for f in fields])
        layer.updateFields()
    return layer


def apply_defaults(layer, fields):
    """Install the draw-time expressions on `layer`'s own fields."""
    for f in fields:
        if not f.expression:
            continue
        index = layer.fields().indexOf(f.name)
        if index >= 0:
            layer.setDefaultValueDefinition(
                index, QgsDefaultValue(f.expression, True))


def has_gdb_writer():
    try:
        from . import gdb_convert
        return gdb_convert.has_filegdb_write()
    except Exception:                        # noqa: BLE001 - no GDAL bindings
        return False


def suffix_of(output):
    return next(o[2] for o in OUTPUTS if o[0] == output)


def with_suffix(path, output):
    suffix = suffix_of(output)
    if suffix and not path.lower().endswith(suffix):
        path += suffix
    return path


def check_target(path, output):
    """Raise `TemplateError` unless `path` can be written for `output`."""
    if not path.strip():
        raise TemplateError('Choose where to save the layer, or switch the '
                            'output back to a temporary layer.')
    if output == OUT_GDB and not has_gdb_writer():
        raise TemplateError(
            'This QGIS cannot create a File Geodatabase (it needs GDAL 3.6 '
            'or newer). Use a GeoPackage or Shapefile instead.')
    folder = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(folder):
        raise TemplateError('The folder does not exist: {}'.format(folder))


def writes_into_existing(path, output):
    """True when saving would add to (not replace) a container that exists."""
    return output in (OUT_GPKG, OUT_GDB) and os.path.exists(path)


def container_name(name, output):
    """The name a layer gets inside its container.

    A File Geodatabase only allows letters, digits and underscores, and starts
    with a letter, and rewrites anything else itself - which would leave the
    name asked for unfindable afterwards. Done here so every step agrees.
    """
    if output != OUT_GDB:
        return name
    import re
    clean = re.sub(r'[^0-9A-Za-z_]', '_', name).strip() or 'Layer'
    return clean if clean[0].isalpha() else 'L_' + clean


def layer_exists(path, name):
    """True when the container at `path` already holds a layer `name`."""
    from . import layer_loader
    try:
        names = [n.lower() for n, _uri in layer_loader.sublayers(path)]
    except Exception:                        # noqa: BLE001 - unreadable file
        return False
    return name.lower() in names


def save_layer(layer, path, output, layer_name):
    """Write `layer` to disk and return the layer opened from the new file."""
    from qgis.core import QgsCoordinateTransformContext, QgsProject

    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = {OUT_SHP: 'ESRI Shapefile', OUT_GPKG: 'GPKG',
                          OUT_GDB: 'OpenFileGDB'}[output]
    options.fileEncoding = 'UTF-8'
    if output != OUT_SHP:
        options.layerName = layer_name
        if os.path.exists(path):
            options.actionOnExistingFile = \
                QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer
    context = QgsProject.instance().transformContext() \
        if QgsProject.instance() else QgsCoordinateTransformContext()
    result = QgsVectorFileWriter.writeAsVectorFormatV3(
        layer, path, context, options)
    code, message = result[0], result[1]
    if code != QgsVectorFileWriter.WriterError.NoError:
        raise TemplateError('Could not save {}: {}'.format(
            os.path.basename(path), message or 'error {}'.format(code)))

    # The writer reports the name it really used; prefer that over ours.
    actual = layer_name
    if len(result) > 3 and result[3]:
        actual = result[3]
    uri = path if output == OUT_SHP else '{}|layername={}'.format(
        path, actual)
    saved = QgsVectorLayer(uri, layer_name, 'ogr')
    if not saved.isValid():
        raise TemplateError('The file was saved, but QGIS could not open it '
                            'again: {}'.format(path))
    return saved


def crs_from_authid(authid=DEFAULT_CRS):
    return QgsCoordinateReferenceSystem(authid)

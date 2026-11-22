# -*- coding: utf-8 -*-
"""Copy Coordinate to Excel: the coordinates of features as a table, sent to Excel.

No GUI in here. ``build_table`` turns a layer's features (the selection, or all of
them) into a header and rows; ``send_table`` hands that to Excel - a real .xlsx when
openpyxl is there, a .csv otherwise - and falls back to Notepad (comma delimited)
when Excel is not installed.

What each geometry gives (one row per coordinate):
  point    every point (multi-point: every part)
  line     every vertex, tagged Start / Vertex / End per part
  polygon  Boundary: every vertex of every ring (the closing duplicate is dropped);
           Centre: the centroid (kept inside the polygon when the centroid falls outside)

``latlon`` is WGS84 decimal degrees; ``xy`` is the layer's own CRS, untouched.
"""

import csv
import os
import sys
import tempfile
import time
from contextlib import suppress

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       QgsFeatureRequest, QgsProject, QgsWkbTypes)
from qgis.PyQt.QtCore import QProcess

LATLON = 'latlon'
XY = 'xy'
BOUNDARY = 'boundary'
CENTRE = 'centre'

EXCEL_MAX_ROWS = 1048575          # one row is the header
_FOLDER = 'kga_coordinates'
_KEEP_SECONDS = 24 * 3600


class CoordinateError(Exception):
    """Something the user can act on (no CRS, no features...)."""


def geometry_kind(layer):
    """'point', 'line', 'polygon' or None (no geometry / unknown)."""
    if layer is None or not layer.isValid() or not hasattr(layer, 'geometryType'):
        return None
    kind = layer.geometryType()
    if kind == QgsWkbTypes.PointGeometry:
        return 'point'
    if kind == QgsWkbTypes.LineGeometry:
        return 'line'
    if kind == QgsWkbTypes.PolygonGeometry:
        return 'polygon'
    return None


def options(layer):
    """The menu entries for a layer: [(label, system, mode)]."""
    kind = geometry_kind(layer)
    if kind is None:
        return []
    xy_label = 'X/Y ({})'.format(layer.crs().authid() or 'layer CRS')
    if kind != 'polygon':
        return [('Lat/Lon (WGS84)', LATLON, None), (xy_label, XY, None)]
    return [('Lat/Lon (WGS84) - Boundary', LATLON, BOUNDARY),
            ('Lat/Lon (WGS84) - Centre', LATLON, CENTRE),
            ('{} - Boundary'.format(xy_label), XY, BOUNDARY),
            ('{} - Centre'.format(xy_label), XY, CENTRE)]


# ---------------------------------------------------------------- geometry walking
def _ring_points(ring, drop_closing):
    points = [(p.x(), p.y(), p.z() if p.is3D() else None) for p in ring.vertices()]
    if drop_closing and len(points) > 1 and points[0][:2] == points[-1][:2]:
        points.pop()
    return points


def _part_rows(part, kind, mode):
    """[(part_label, point_type, x, y, z)] for one single-part geometry."""
    out = []
    abstract = part.constGet()
    if abstract is None:
        return out
    if kind == 'point':
        for p in abstract.vertices():
            out.append(('Point', p.x(), p.y(), p.z() if p.is3D() else None))
    elif kind == 'line':
        points = [(p.x(), p.y(), p.z() if p.is3D() else None) for p in abstract.vertices()]
        last = len(points) - 1
        for i, (x, y, z) in enumerate(points):
            label = 'Start' if i == 0 else 'End' if i == last else 'Vertex'
            out.append((label, x, y, z))
    elif mode == CENTRE:
        centre = part.centroid()
        if centre.isNull() or not part.contains(centre):
            inside = part.pointOnSurface()
            if not inside.isNull():
                centre = inside
        if not centre.isNull():
            p = centre.asPoint()
            out.append(('Centre', p.x(), p.y(), None))
    else:
        rings = [('Boundary', abstract.exteriorRing())]
        for i in range(abstract.numInteriorRings()):
            rings.append(('Boundary (hole {})'.format(i + 1), abstract.interiorRing(i)))
        for label, ring in rings:
            if ring is not None:
                for x, y, z in _ring_points(ring, True):
                    out.append((label, x, y, z))
    return out


def _transform(layer, system):
    if system == XY:
        return None
    crs = layer.crs()
    if not crs.isValid():
        raise CoordinateError('"{}" has no coordinate system, so Lat/Lon cannot be worked out. '
                              'Use X/Y instead.'.format(layer.name()))
    target = QgsCoordinateReferenceSystem('EPSG:4326')
    if crs == target:
        return None
    return QgsCoordinateTransform(crs, target, QgsProject.instance().transformContext())


def _decimals(layer, system):
    if system == LATLON or layer.crs().isGeographic():
        return 8
    return 3


def build_table(layer, system, mode=None, fids=None, progress=None):
    """(header, rows) for ``layer``. ``fids`` None = every feature.

    Raises CoordinateError when there is nothing to copy."""
    kind = geometry_kind(layer)
    if kind is None:
        raise CoordinateError('"{}" has no geometry.'.format(layer.name() if layer else ''))
    if kind == 'polygon' and mode not in (BOUNDARY, CENTRE):
        mode = BOUNDARY
    transform = _transform(layer, system)
    places = _decimals(layer, system)
    has_z = QgsWkbTypes.hasZ(layer.wkbType()) and mode != CENTRE

    request = QgsFeatureRequest()
    if fids is not None:
        request.setFilterFids(list(fids))
    request.setSubsetOfAttributes([])           # geometry only

    if system == LATLON:
        axes = ['Latitude', 'Longitude']
    else:
        authid = layer.crs().authid()
        axes = ['X ({})'.format(authid), 'Y ({})'.format(authid)] if authid else ['X', 'Y']
    header = ['Layer', 'Feature ID', 'Part', 'Point Type', 'Vertex'] + axes
    if has_z:
        header.append('Z')

    rows = []
    name = layer.name()
    for count, feature in enumerate(layer.getFeatures(request)):
        if progress is not None and count % 500 == 0 and progress(count):
            raise CoordinateError('Cancelled.')
        geometry = feature.geometry()
        if geometry is None or geometry.isNull():
            continue
        parts = geometry.asGeometryCollection() if geometry.isMultipart() else [geometry]
        for part_no, part in enumerate(parts, 1):
            for vertex_no, (label, x, y, z) in enumerate(_part_rows(part, kind, mode), 1):
                if transform is not None:
                    try:
                        point = transform.transform(x, y)
                        x, y = point.x(), point.y()
                    except Exception:           # a vertex outside the projection's range
                        x = y = None
                if x is None:
                    first = second = ''
                elif system == LATLON:
                    first, second = round(y, places), round(x, places)
                else:
                    first, second = round(x, places), round(y, places)
                row = [name, feature.id(), part_no, label, vertex_no, first, second]
                if has_z:
                    row.append(z if z is not None else '')
                rows.append(row)
    if not rows:
        raise CoordinateError('"{}" has no coordinates to copy.'.format(name))
    return header, rows


# ---------------------------------------------------------------------- sending
def find_excel():
    """Full path to excel.exe, or None. Windows only."""
    if sys.platform != 'win32':
        return None
    with suppress(Exception):
        import winreg
        keys = [(winreg.HKEY_LOCAL_MACHINE, 0), (winreg.HKEY_CURRENT_USER, 0),
                (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
                (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY)]
        for hive, view in keys:
            try:
                with winreg.OpenKey(
                        hive, r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\excel.exe',
                        0, winreg.KEY_READ | view) as key:
                    path = winreg.QueryValueEx(key, '')[0]
            except OSError:
                continue
            path = os.path.expandvars(str(path).strip().strip('"'))
            if path and os.path.isfile(path):
                return path
    return None


def _work_folder():
    folder = os.path.join(tempfile.gettempdir(), _FOLDER)
    os.makedirs(folder, exist_ok=True)
    now = time.time()
    with suppress(OSError):                      # tidy up what earlier runs left behind
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            if now - os.path.getmtime(path) > _KEEP_SECONDS:
                with suppress(OSError):
                    os.remove(path)
    return folder


def _file_name(title, extension):
    safe = ''.join(c if c.isalnum() or c in ' _-' else '_' for c in title).strip() or 'coordinates'
    return '{}_{}.{}'.format(safe, time.strftime('%Y%m%d_%H%M%S'), extension)


def write_csv(path, header, rows):
    with open(path, 'w', newline='', encoding='utf-8-sig') as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def write_xlsx(path, header, rows):
    """True when written. False when openpyxl is not available."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font
        from openpyxl.utils import get_column_letter
    except ImportError:
        return False
    book = Workbook(write_only=False)
    sheet = book.active
    sheet.title = 'Coordinates'
    sheet.append(header)
    for row in rows:
        sheet.append(row)
    bold = Font(bold=True)
    for cell in sheet[1]:
        cell.font = bold
    sheet.freeze_panes = 'A2'
    for index, title in enumerate(header, 1):
        sheet.column_dimensions[get_column_letter(index)].width = max(12, min(30, len(str(title)) + 4))
    book.save(path)
    return True


def _launch(program, path):
    return QProcess.startDetached(program, [path])


def send_table(title, header, rows):
    """Open the table in Excel, or Notepad if there is no Excel.

    Returns (app, path) with app 'Excel', 'Notepad' or 'default'."""
    folder = _work_folder()
    excel = find_excel()
    if excel and len(rows) <= EXCEL_MAX_ROWS:
        path = os.path.join(folder, _file_name(title, 'xlsx'))
        if not write_xlsx(path, header, rows):
            path = os.path.join(folder, _file_name(title, 'csv'))
            write_csv(path, header, rows)
        if _launch(excel, path):
            return 'Excel', path
    path = os.path.join(folder, _file_name(title, 'csv'))
    write_csv(path, header, rows)
    if sys.platform == 'win32':
        notepad = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'notepad.exe')
        if _launch(notepad if os.path.isfile(notepad) else 'notepad.exe', path):
            return 'Notepad', path
    from qgis.PyQt.QtCore import QUrl
    from qgis.PyQt.QtGui import QDesktopServices
    QDesktopServices.openUrl(QUrl.fromLocalFile(path))
    return 'default', path

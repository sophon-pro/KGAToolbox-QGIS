"""Duplicate a layer together with its data.

QGIS's own Duplicate Layer makes a second layer pointing at the SAME source, so
editing the copy edits the original. These helpers write the data out as well:
vector layers to a GeoPackage, shapefile, File Geodatabase, GeoJSON or KML/KMZ,
file-based rasters to GeoTIFF, COG, IMG, PNG, JPEG, JPEG 2000 or ASCII grid
(whole, or cut to an area), and memory layers are cloned in memory.
"""

import os
import re
import shutil
import tempfile
import zipfile

from osgeo import gdal

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsMapLayerStyle,
    QgsProject,
    QgsProviderRegistry,
    QgsRasterLayer,
    QgsVectorFileWriter,
    QgsVectorLayer,
)

gdal.UseExceptions()

COPY_SUFFIX = ' (copy)'

GPKG, SHP, GDB, GEOJSON, KML, KMZ = 'gpkg', 'shp', 'gdb', 'geojson', 'kml', 'kmz'

#: (key, label, GDAL driver, extension) in the order the window lists them.
VECTOR_FORMATS = [
    (GPKG, 'GeoPackage (.gpkg)', 'GPKG', '.gpkg'),
    (SHP, 'Shapefile (.shp)', 'ESRI Shapefile', '.shp'),
    (GDB, 'File Geodatabase (.gdb)', 'OpenFileGDB', '.gdb'),
    (GEOJSON, 'GeoJSON (.geojson)', 'GeoJSON', '.geojson'),
    (KML, 'KML (.kml)', 'KML', '.kml'),
    (KMZ, 'KMZ (.kmz)', 'KML', '.kmz'),
]

TIF, COG, IMG, PNG, JPG, JP2, ASC = ('tif', 'cog', 'img', 'png', 'jpg', 'jp2',
                                     'asc')

#: (key, label, GDAL driver, extension, creation options, byte only, max bands).
RASTER_FORMATS = [
    (TIF, 'GeoTIFF (.tif)', 'GTiff', '.tif',
     ['COMPRESS=LZW', 'TILED=YES', 'BIGTIFF=IF_SAFER'], False, None),
    (COG, 'Cloud Optimized GeoTIFF (.tif)', 'COG', '.tif',
     ['COMPRESS=LZW', 'BIGTIFF=IF_SAFER'], False, None),
    (IMG, 'Erdas IMAGINE (.img)', 'HFA', '.img', ['COMPRESSED=YES'], False,
     None),
    (PNG, 'PNG (.png)', 'PNG', '.png', [], False, 4),
    (JPG, 'JPEG (.jpg)', 'JPEG', '.jpg', ['QUALITY=90'], True, 3),
    (JP2, 'JPEG 2000 (.jp2)', 'JP2OpenJPEG', '.jp2', ['QUALITY=90'], False,
     None),
    (ASC, 'ASCII Grid (.asc)', 'AAIGrid', '.asc', [], False, 1),
]

RASTER_LABEL = RASTER_FORMATS[0][1]


def raster_spec(key):
    return next(spec for spec in RASTER_FORMATS if spec[0] == key)


def raster_format_unavailable(key):
    """Why this build cannot write raster format `key`, or ''."""
    _key, label, driver, _ext, _opts, _byte, _bands = raster_spec(key)
    try:
        drv = gdal.GetDriverByName(driver)
    except RuntimeError:
        drv = None
    if drv is None:
        return 'This QGIS has no {} driver.'.format(label)
    if not (drv.GetMetadataItem('DCAP_CREATE') == 'YES'
            or drv.GetMetadataItem('DCAP_CREATECOPY') == 'YES'):
        return 'This QGIS can read {} but not write it.'.format(label)
    return ''

_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def format_spec(key):
    return next(spec for spec in VECTOR_FORMATS if spec[0] == key)


def format_unavailable(key):
    """Why this build cannot write `key`, or '' when it can."""
    _key, label, driver, _ext = format_spec(key)
    try:
        drv = gdal.GetDriverByName(driver)
    except RuntimeError:
        drv = None
    if drv is None:
        return 'This QGIS has no {} driver.'.format(label)
    if drv.GetMetadataItem('DCAP_CREATE') != 'YES':
        return ('This QGIS can read {} but not write it (File Geodatabase '
                'writing needs GDAL 3.6 or newer).'.format(label))
    return ''


def is_memory_layer(layer):
    return isinstance(layer, QgsVectorLayer) and layer.providerType() == 'memory'


def extension_for(layer, fmt=GPKG):
    """The extension of the copy: the chosen vector or raster format's."""
    if isinstance(layer, QgsVectorLayer):
        return format_spec(fmt)[3]
    return raster_spec(fmt)[3]


def safe_name(name):
    """A layer name made safe for a file name.

    Only characters Windows forbids are replaced, so a Khmer or other non-Latin
    name survives instead of collapsing to underscores.
    """
    cleaned = _ILLEGAL.sub('_', name).strip(' ._')
    return cleaned or 'layer'


def container_name(name):
    """A layer name a File Geodatabase accepts: letters, digits, underscores."""
    clean = re.sub(r'[^0-9A-Za-z_]', '_', name).strip('_') or 'Layer'
    return clean if clean[0].isalpha() else 'L_' + clean


def source_path(layer):
    """The file behind a layer, or '' when it is not file based."""
    try:
        parts = QgsProviderRegistry.instance().decodeUri(
            layer.providerType(), layer.source())
        path = parts.get('path') or ''
    except Exception:
        path = ''
    if not path and layer.providerType() == 'gdal':
        path = (layer.source() or '').split('|', 1)[0]
    return path


def source_folder(layer):
    """Folder of the layer's file, else the project folder, else home."""
    path = source_path(layer)
    if path and os.path.exists(path):
        return os.path.dirname(os.path.abspath(path))
    return QgsProject.instance().homePath() or os.path.expanduser('~')


def unique_path(folder, base, ext):
    """folder/base.ext, or base_2.ext, base_3.ext ... if that exists."""
    path = os.path.join(folder, base + ext)
    index = 2
    while os.path.exists(path):
        path = os.path.join(folder, '{}_{}{}'.format(base, index, ext))
        index += 1
    return path


def output_path(layer, fmt=None, folder=''):
    """Where the copy will be written.

    `folder` empty means beside the original. The file name is always
    <layer>_copy, numbered when taken, so a copy never overwrites anything.
    """
    if fmt is None:
        fmt = GPKG if isinstance(layer, QgsVectorLayer) else TIF
    folder = folder.strip() or source_folder(layer)
    return unique_path(folder, safe_name(layer.name()) + '_copy',
                       extension_for(layer, fmt))


def copy_style(source, target):
    """Symbology, labels, aliases, form setup: everything the style carries."""
    style = QgsMapLayerStyle()
    style.readFromLayer(source)
    style.writeToLayer(target)
    target.triggerRepaint()


def add_above(original, new_layer, project=None):
    """Add `new_layer` to the project, directly above `original`."""
    project = project or QgsProject.instance()
    project.addMapLayer(new_layer, False)
    root = project.layerTreeRoot()
    node = root.findLayer(original.id())
    if node is None:
        root.insertLayer(0, new_layer)
        return
    parent = node.parent()
    parent.insertLayer(parent.children().index(node), new_layer)


def clone_memory(layer, selected_only=False):
    """An independent in-memory copy; the features are copied by clone().

    With `selected_only` the copy keeps just the features selected on `layer`.
    """
    new_layer = layer.clone()
    if selected_only:
        keep = set(layer.selectedFeatureIds())
        drop = [fid for fid in new_layer.allFeatureIds() if fid not in keep]
        new_layer.dataProvider().deleteFeatures(drop)
        new_layer.updateExtents()
    new_layer.setName(layer.name() + COPY_SUFFIX)
    return new_layer


def _write_options(layer, fmt, layer_name, transform_context,
                   selected_only=False):
    """SaveVectorOptions for one format."""
    _key, _label, driver, _ext = format_spec(fmt)
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = driver
    options.fileEncoding = 'UTF-8'
    options.onlySelectedFeatures = bool(selected_only)
    if fmt != SHP:
        options.layerName = layer_name

    if fmt == GPKG:
        # GeoPackage keeps its feature id in an integer column called `fid`. A
        # source with its own `fid` attribute of another type (File Geodatabase
        # and shapefile data often have one) makes the writer fail on that
        # field, so the id column gets a free name and the attribute is kept.
        names = {name.lower() for name in layer.fields().names()}
        if 'fid' in names:
            key, index = 'fid_gpkg', 2
            while key in names:
                key = 'fid_gpkg{}'.format(index)
                index += 1
            options.layerOptions = ['FID=' + key]

    if fmt in (KML, KMZ):
        # Google Earth reads longitude/latitude only.
        wgs84 = QgsCoordinateReferenceSystem('EPSG:4326')
        if layer.crs() != wgs84:
            options.ct = QgsCoordinateTransform(
                layer.crs(), wgs84, transform_context)
    return options


def _check(result, what):
    if result[0] != QgsVectorFileWriter.WriterError.NoError:
        raise RuntimeError('Could not write the {}: {}'.format(what, result[1]))


def copy_vector(layer, out_path, fmt, transform_context, selected_only=False):
    """Write every feature of `layer` to `out_path` in `fmt` and load it.

    A filter on the layer limits what is copied, and unsaved edits are included,
    because the writer reads through the layer rather than the file. With
    `selected_only` just the selected features are written.
    """
    problem = format_unavailable(fmt)
    if problem:
        raise RuntimeError(problem)

    name = safe_name(layer.name())
    if fmt == GDB:
        name = container_name(layer.name())
    options = _write_options(layer, fmt, name, transform_context,
                             selected_only)

    if fmt == KMZ:
        # A .kmz is a zip holding doc.kml. Written as KML in a scratch folder
        # and zipped, which needs no driver beyond the plain KML one.
        with tempfile.TemporaryDirectory() as scratch:
            kml_path = os.path.join(scratch, 'doc.kml')
            _check(QgsVectorFileWriter.writeAsVectorFormatV3(
                layer, kml_path, transform_context, options), 'KML data')
            with zipfile.ZipFile(out_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                zf.write(kml_path, 'doc.kml')
        uri = '/vsizip/{}/doc.kml'.format(out_path)
    else:
        _check(QgsVectorFileWriter.writeAsVectorFormatV3(
            layer, out_path, transform_context, options), 'vector data')
        if fmt in (GPKG, GDB):
            uri = '{}|layername={}'.format(out_path, name)
        else:
            uri = out_path

    new_layer = QgsVectorLayer(uri, layer.name() + COPY_SUFFIX, 'ogr')
    if not new_layer.isValid():
        raise RuntimeError(
            'The copy was written but could not be loaded: {}'.format(out_path))
    return new_layer


class Region(object):
    """An area to cut a raster to: a geometry in `crs`.

    `clip` False means just its bounding box; True means cut to the shape.
    """

    def __init__(self, geometry, crs, clip, label=''):
        self.geometry = geometry
        self.crs = crs
        self.clip = clip
        self.label = label


def _describe(path):
    """(bands, GDAL type name, nodata, geotransform, width, height)."""
    ds = gdal.Open(path)
    try:
        band = ds.GetRasterBand(1)
        return (ds.RasterCount, gdal.GetDataTypeName(band.DataType),
                band.GetNoDataValue(), ds.GetGeoTransform(),
                ds.RasterXSize, ds.RasterYSize)
    finally:
        band = None
        ds = None


def raster_problem(layer, fmt):
    """Why `layer` cannot be written as `fmt`, or ''."""
    why = raster_format_unavailable(fmt)
    if why:
        return why
    _key, label, _drv, _ext, _opts, byte_only, max_bands = raster_spec(fmt)
    if layer.providerType() != 'gdal':
        return ('Only file-based rasters can be copied, not web services such '
                'as WMS or XYZ tiles.')
    try:
        bands, dtype, _nodata, _gt, _x, _y = _describe(
            layer.source().split('|', 1)[0])
    except RuntimeError as error:
        return 'The raster could not be read: {}'.format(error)
    if max_bands and bands > max_bands:
        return '{} takes at most {} band(s); this raster has {}.'.format(
            label, max_bands, bands)
    if byte_only and dtype != 'Byte':
        return '{} takes 8-bit data; this raster is {}.'.format(label, dtype)
    accepted = (gdal.GetDriverByName(_drv).GetMetadataItem(
        'DMD_CREATIONDATATYPES') or '').split()
    if accepted and dtype not in accepted:
        return '{} cannot hold {} data (it takes {}). Use GeoTIFF instead.'.format(
            label, dtype, ', '.join(accepted))
    return ''


def _fallback_nodata(dtype):
    """NoData for outside a cut shape when the source has none."""
    return -9999.0 if dtype in ('Float32', 'Float64') else 0


def _is_box(geometry):
    area = geometry.boundingBox().area()
    return area > 0 and abs(geometry.area() - area) <= area * 1e-9


def _region_in(layer, region):
    """(bounding box, cutting geometry or None) in the raster's own CRS."""
    transform = QgsCoordinateTransform(
        region.crs, layer.crs(), QgsProject.instance().transformContext())
    geometry = QgsGeometry(region.geometry)
    if region.clip:
        geometry = geometry.densifyByCount(16)
        geometry.transform(transform)
        if not geometry.isGeosValid():
            geometry = geometry.makeValid()
        return geometry.boundingBox(), (None if _is_box(geometry) else geometry)
    return transform.transformBoundingBox(geometry.boundingBox()), None


def _remove(path):
    for extra in ('', '.aux.xml'):
        try:
            if os.path.exists(path + extra):
                os.remove(path + extra)
        except OSError:
            pass


def _write_cutline(path, geometry, crs):
    """Write `geometry` to a one-feature GeoPackage for gdal.Warp to cut with."""
    from osgeo import ogr, osr
    ds = ogr.GetDriverByName('GPKG').CreateDataSource(path)
    srs = osr.SpatialReference()
    srs.ImportFromWkt(crs.toWkt())
    layer = ds.CreateLayer('cut', srs=srs, geom_type=ogr.wkbUnknown)
    feature = ogr.Feature(layer.GetLayerDefn())
    feature.SetGeometry(ogr.CreateGeometryFromWkb(bytes(geometry.asWkb())))
    layer.CreateFeature(feature)
    feature = None
    layer = None
    ds = None


def copy_raster(layer, out_path, fmt=TIF, region=None, feedback=None):
    """Write a file-based raster to `out_path` in `fmt` and load it.

    With a `region` the copy covers just that area: its bounding box, or - for
    a shape - the box with everything outside the shape set to NoData. GDAL does
    the work, so no-data, data type, band descriptions and colour tables come
    across, and the pixels are never resampled.
    """
    problem = raster_problem(layer, fmt)
    if problem:
        raise RuntimeError(problem)
    _key, _label, driver, _ext, creation, _byte, _bands = raster_spec(fmt)
    source = layer.source().split('|', 1)[0]
    tif_opts = ['COMPRESS=LZW', 'TILED=YES', 'BIGTIFF=IF_SAFER']

    def progress(low, span):
        def callback(complete, _message, _data):
            if feedback is not None:
                feedback.setProgress(low + complete * span)
                return 0 if feedback.isCanceled() else 1
            return 1
        return callback

    scratch = tempfile.mkdtemp(prefix='kga_dup_')
    try:
        current = source
        two_step = region is not None and driver != 'GTiff'
        if region is not None:
            box, cutter = _region_in(layer, region)
            _bands_n, dtype, nodata, gt, _w, _h = _describe(source)
            inter = box.intersect(layer.extent())
            if inter.isEmpty() or inter.width() <= 0 \
                    or inter.height() <= 0:
                raise ValueError('That area does not overlap the raster.')
            step = os.path.join(scratch, 'crop.tif') if two_step \
                else out_path
            span = 50 if two_step else 100
            if cutter is None:
                gdal.Translate(
                    step, source, format='GTiff',
                    projWin=[inter.xMinimum(), inter.yMaximum(),
                             inter.xMaximum(), inter.yMinimum()],
                    creationOptions=tif_opts,
                    callback=progress(0, span))
            else:
                cutline = os.path.join(scratch, 'cut.gpkg')
                _write_cutline(cutline, cutter, layer.crs())
                gdal.Warp(
                    step, source, format='GTiff', cutlineDSName=cutline,
                    cropToCutline=True, targetAlignedPixels=True,
                    xRes=abs(gt[1]), yRes=abs(gt[5]),
                    dstNodata=(nodata if nodata is not None
                               else _fallback_nodata(dtype)),
                    resampleAlg='near', creationOptions=tif_opts,
                    callback=progress(0, span))
            current = step
        if current != out_path:
            gdal.Translate(
                out_path, current, format=driver, creationOptions=creation,
                callback=progress(50 if two_step else 0,
                                  50 if two_step else 100))
    except ValueError as error:
        _remove(out_path)
        raise RuntimeError(str(error))
    except RuntimeError as error:
        _remove(out_path)
        if feedback is not None and feedback.isCanceled():
            raise RuntimeError('Cancelled. The unfinished file was removed.')
        raise RuntimeError('Could not write the raster data: {}'.format(error))
    finally:
        # GDAL may still hold the intermediate open for a moment (Windows will
        # not delete an open file), so a failure to clean up is not an error.
        shutil.rmtree(scratch, ignore_errors=True)

    new_layer = QgsRasterLayer(out_path, layer.name() + COPY_SUFFIX, 'gdal')
    if not new_layer.isValid():
        raise RuntimeError(
            'The copy was written but could not be loaded: {}'.format(out_path))
    return new_layer


def ensure_folder(path):
    """Create the folder that will hold `path`."""
    folder = os.path.dirname(path)
    if folder and not os.path.isdir(folder):
        os.makedirs(folder, exist_ok=True)

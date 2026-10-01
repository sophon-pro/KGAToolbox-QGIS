# -*- coding: utf-8 -*-
"""Turn the finished EPSG:3857 work file into the file the user asked for.

Crop to the requested extent, reproject to the output CRS, mask to a polygon,
convert to the target format, write the georeferencing and build overviews.
Runs on the task thread; needs GDAL only.
"""

import json
import os
from contextlib import suppress
from dataclasses import dataclass, field

import numpy as np
from osgeo import gdal, osr

from . import tile_math as tm

gdal.UseExceptions()

#: key -> (label, extension, GDAL driver names to try, has alpha, embedded georef)
FORMATS = {
    'tif': ('GeoTIFF (.tif)', '.tif', ('GTiff',), True, True),
    'jpg': ('JPEG (.jpg)', '.jpg', ('JPEG',), False, False),
    'png': ('PNG (.png)', '.png', ('PNG',), True, False),
    'jp2': ('JPEG 2000 (.jp2)', '.jp2', ('JP2OpenJPEG', 'JPEG2000'), True, True),
    'ecw': ('ECW (.ecw)', '.ecw', ('ECW',), False, True),
}
WORLD_EXT = {'jpg': '.jgw', 'png': '.pgw'}


class ExportError(Exception):
    pass


class ExportStopped(Exception):
    pass


@dataclass
class ExportResult:
    path: str = ''
    width: int = 0
    height: int = 0
    pixel_size: float = 0.0
    files: list = field(default_factory=list)


def driver_for(fmt):
    """The usable GDAL driver name for `fmt`, or None if this build cannot write it."""
    entry = FORMATS.get(fmt)
    if entry is None:
        return None
    if fmt == 'ecw' and not (gdal.GetConfigOption('ECW_ENCODE_KEY') and
                             gdal.GetConfigOption('ECW_ENCODE_COMPANY')):
        # The ECW SDK refuses to write without a licence key pair.
        return None
    for name in entry[2]:
        drv = gdal.GetDriverByName(name)
        if drv is None:
            continue
        meta = drv.GetMetadata()
        if meta.get('DCAP_CREATE') == 'YES' or meta.get('DCAP_CREATECOPY') == 'YES':
            return name
    return None


def available_formats():
    return [k for k in FORMATS if driver_for(k)]


def output_path(folder, name, fmt):
    return os.path.join(folder, name + FORMATS[fmt][1])


def _srs(text):
    srs = osr.SpatialReference()
    if text.upper().startswith(('EPSG:', 'ESRI:', 'IGNF:')):
        if srs.SetFromUserInput(text) != 0:
            raise ExportError('Unknown coordinate system %s.' % text)
    elif srs.ImportFromWkt(text) != 0:
        raise ExportError('Unreadable coordinate system.')
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return srs


def is_3857(text):
    return text.strip().upper() in ('EPSG:3857', 'EPSG:900913')


class _Progress:
    """Maps a GDAL step's 0..1 onto a slice of the whole export."""

    def __init__(self, report, should_stop):
        self.report = report
        self.should_stop = should_stop
        self.lo, self.hi, self.text = 0.0, 1.0, ''

    def phase(self, lo, hi, text):
        self.lo, self.hi, self.text = lo, hi, text
        self.report(0.0, text)

    def callback(self):
        def cb(fraction, _msg, _data):
            self.report(fraction, self.text)
            return 0 if self.should_stop() else 1
        return cb

    def check(self):
        if self.should_stop():
            raise ExportStopped()


def _run(prog, func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except RuntimeError as exc:
        if prog.should_stop():
            raise ExportStopped()
        raise ExportError(str(exc))


def _write_cutline(path, mask_wkt):
    from osgeo import ogr

    geom = ogr.CreateGeometryFromWkt(mask_wkt)
    if geom is None:
        raise ExportError('The clip polygon could not be read.')
    doc = {'type': 'FeatureCollection', 'features': [{
        'type': 'Feature', 'properties': {},
        'geometry': json.loads(geom.ExportToJson())}]}
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(doc, fh)


def _composite_on_white(src_path, dst_path, prog):
    """RGBA GeoTIFF -> RGB GeoTIFF, transparent pixels white. Strip by strip."""
    src = gdal.Open(src_path)
    drv = gdal.GetDriverByName('GTiff')
    dst = drv.Create(dst_path, src.RasterXSize, src.RasterYSize, 3,
                     gdal.GDT_Byte, ['TILED=YES', 'COMPRESS=DEFLATE',
                                     'BIGTIFF=IF_SAFER'])
    dst.SetGeoTransform(src.GetGeoTransform())
    dst.SetProjection(src.GetProjection())
    w, h = src.RasterXSize, src.RasterYSize
    step = 256
    bands = [src.GetRasterBand(i + 1) for i in range(4)]
    out = [dst.GetRasterBand(i + 1) for i in range(3)]
    for y in range(0, h, step):
        prog.check()
        rows = min(step, h - y)
        a = bands[3].ReadAsArray(0, y, w, rows).astype(np.uint16)
        inv = 255 - a
        for i in range(3):
            c = bands[i].ReadAsArray(0, y, w, rows).astype(np.uint16)
            out[i].WriteArray(((c * a + 255 * inv + 127) // 255).astype(np.uint8),
                              0, y)
        prog.report((y + rows) / float(h), prog.text)
    dst.FlushCache()
    dst = None
    src = None


def _overview_levels(width, height):
    levels, f = [], 2
    while min(width, height) / f >= 256:
        levels.append(f)
        f *= 2
    return levels


def _write_world_file(path, ds, fmt):
    gt = ds.GetGeoTransform()
    stem = os.path.splitext(path)[0]
    world = stem + WORLD_EXT[fmt]
    with open(world, 'w', encoding='ascii') as fh:
        fh.write('%.12f\n%.12f\n%.12f\n%.12f\n%.12f\n%.12f\n' % (
            gt[1], gt[4], gt[2], gt[5],
            gt[0] + gt[1] / 2.0 + gt[2] / 2.0,
            gt[3] + gt[4] / 2.0 + gt[5] / 2.0))
    prj = stem + '.prj'
    srs = osr.SpatialReference()
    srs.ImportFromWkt(ds.GetProjection())
    with suppress(Exception):
        srs.MorphToESRI()
    with open(prj, 'w', encoding='ascii', errors='replace') as fh:
        fh.write(srs.ExportToWkt())
    return [world, prj]


def export(spec, has_failed_tiles, report, should_stop):
    """Build the final image. `report(fraction, text)` gets progress 0..1."""
    fmt = spec.fmt
    label, ext, _drivers, fmt_alpha, embedded = FORMATS[fmt]
    driver = driver_for(fmt)
    if driver is None:
        raise ExportError('This GDAL build cannot write %s.' % label)

    prog = _Progress(report, should_stop)
    work = spec.work_file
    dst_srs = _srs(spec.output_crs)
    reproject = not is_3857(spec.output_crs)
    masked = bool(spec.mask_wkt)
    invalid_areas = masked or reproject or has_failed_tiles
    keep_alpha = fmt_alpha and invalid_areas and (
        getattr(spec, 'transparent', True) or not masked)
    final = output_path(spec.folder, spec.name, fmt)
    created = []
    temps = []
    stage_base = os.path.join(spec.work_dir, spec.name)

    try:
        gdal.SetConfigOption('GDAL_CACHEMAX', '512')
        cb = prog.callback()
        x_min, _xm, y_min, _ym = spec.tile_range

        # 1. crop / reproject / mask -> RGBA GeoTIFF (or straight to final)
        if not reproject and not masked:
            win = tm.crop_window(spec.bbox, spec.tile_range, spec.zoom,
                                 spec.source.tile_size)
            stage = work
            src_kwargs = {'srcWin': list(win)}
            prog.phase(0.0, 0.05, 'Cropping...')
            if invalid_areas and not keep_alpha:
                # JPEG / ECW with holes: crop to a scratch RGBA file first so
                # the empty areas can be flattened onto white.
                stage = stage_base + '_stage.tif'
                temps.append(stage)
                _run(prog, gdal.Translate, stage, work, format='GTiff',
                     srcWin=list(win), callback=cb,
                     creationOptions=['TILED=YES', 'COMPRESS=DEFLATE',
                                      'BIGTIFF=IF_SAFER'])
                src_kwargs = {}
        else:
            stage = stage_base + '_stage.tif'
            temps.append(stage)
            opts = dict(format='GTiff', dstSRS=dst_srs.ExportToWkt(),
                        resampleAlg='bilinear' if reproject else 'near',
                        multithread=True, callback=None,
                        creationOptions=['TILED=YES', 'COMPRESS=DEFLATE',
                                         'BIGTIFF=IF_SAFER', 'ZLEVEL=1'])
            if masked:
                cutline = stage_base + '_cutline.geojson'
                temps.append(cutline)
                _write_cutline(cutline, spec.mask_wkt)
                opts.update(cutlineDSName=cutline, cropToCutline=True,
                            cutlineSRS='EPSG:4326')
            else:
                w, s, e, n = spec.bbox
                opts.update(outputBounds=[w, s, e, n],
                            outputBoundsSRS='EPSG:4326')
            prog.phase(0.0, 0.55, 'Building image...')
            opts['callback'] = cb
            if os.path.exists(stage):
                os.remove(stage)
            _run(prog, gdal.Warp, stage, work, **opts)
            src_kwargs = {}
        prog.check()

        # 2. jpg / ecw: no alpha to keep, so flatten onto white
        source = stage
        if not keep_alpha and invalid_areas:
            flat = stage_base + '_flat.tif'
            temps.append(flat)
            prog.phase(0.55, 0.7, 'Filling empty areas...')
            _composite_on_white(stage, flat, prog)
            source = flat
            band_list = [1, 2, 3]
        else:
            band_list = [1, 2, 3, 4] if keep_alpha else [1, 2, 3]

        # 3. convert to the target format
        if os.path.exists(final):
            _remove_output(final, fmt)
        creation = []
        if fmt == 'tif':
            if keep_alpha:
                creation = ['COMPRESS=DEFLATE', 'PREDICTOR=2', 'TILED=YES',
                            'BIGTIFF=IF_SAFER']
            else:
                creation = ['COMPRESS=JPEG', 'JPEG_QUALITY=90',
                            'PHOTOMETRIC=YCBCR', 'TILED=YES',
                            'BIGTIFF=IF_SAFER']
        elif fmt == 'jpg':
            creation = ['QUALITY=90']
        prog.phase(0.7 if invalid_areas else 0.05, 0.95, 'Writing %s...' % label)
        translate = dict(format=driver, bandList=band_list,
                         creationOptions=creation, callback=cb)
        translate.update(src_kwargs)
        _run(prog, gdal.Translate, final, source, **translate)
        created.append(final)
        prog.check()

        # 4. georeference + verify
        ds = gdal.Open(final)
        try:
            width, height = ds.RasterXSize, ds.RasterYSize
            gt = ds.GetGeoTransform()
            if fmt in WORLD_EXT:
                # CreateCopy from a georeferenced source stores it in a PAM
                # .aux.xml; add the world file and .prj other software reads.
                created.extend(_write_world_file(final, ds, fmt))
        finally:
            ds = None
        aux = final + '.aux.xml'
        if os.path.exists(aux):
            created.append(aux)
        check = gdal.Open(final)
        try:
            ok = bool(check.GetProjection()) and \
                tuple(check.GetGeoTransform()) != (0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
        finally:
            check = None
        if not ok:
            raise ExportError('The result has no georeferencing.')

        # 5. overviews
        if fmt == 'tif' and spec.pyramids:
            levels = _overview_levels(width, height)
            if levels:
                prog.phase(0.95, 1.0, 'Building pyramids...')
                gdal.SetConfigOption('COMPRESS_OVERVIEW',
                                     'JPEG' if not keep_alpha else 'DEFLATE')
                if not keep_alpha:
                    gdal.SetConfigOption('PHOTOMETRIC_OVERVIEW', 'YCBCR')
                    gdal.SetConfigOption('JPEG_QUALITY_OVERVIEW', '85')
                try:
                    ds = gdal.Open(final, gdal.GA_Update)
                    ds.BuildOverviews('AVERAGE', levels, callback=cb)
                    ds = None
                finally:
                    for opt in ('COMPRESS_OVERVIEW', 'PHOTOMETRIC_OVERVIEW',
                                'JPEG_QUALITY_OVERVIEW'):
                        gdal.SetConfigOption(opt, None)

        result = ExportResult(path=final, width=width, height=height,
                              pixel_size=abs(gt[1]), files=created)
        report(1.0, 'Done')
        return result
    except (ExportStopped, ExportError, RuntimeError):
        for path in created:
            try:
                os.remove(path)
            except OSError:
                pass
        raise
    finally:
        gdal.SetConfigOption('GDAL_CACHEMAX', None)
        for path in temps:
            try:
                os.remove(path)
            except OSError:
                pass


def _remove_output(path, fmt):
    stem = os.path.splitext(path)[0]
    for candidate in (path, path + '.aux.xml', path + '.ovr',
                      stem + WORLD_EXT.get(fmt, '.none'), stem + '.prj'):
        try:
            os.remove(candidate)
        except OSError:
            pass

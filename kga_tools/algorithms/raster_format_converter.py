# -*- coding: utf-8 -*-
"""Raster Format Converter.

Converts any GDAL-readable raster into any GDAL-writable format - ECW to
GeoTIFF, GeoTIFF to JPEG 2000, IMG to GeoTIFF, and so on.

Built for orthophotos too large to convert in one pass. The source is read in
rectangular pixel blocks written as temporary GeoTIFF tiles - GeoTIFF because
it is the one format that takes robust windowed writes, whatever the final
format is - and a tiny `<output>.checkpoint.json` records how far the run got.
Cancel the run, lose QGIS or lose the machine, and re-running with the same
output path picks up at the next unfinished block. Once every block is on disk
they are mosaicked and written once into the chosen format, which is fast
because it reads local uncompressed tiles instead of decoding the original
again.

Writing ECW and JPEG 2000 through the Hexagon SDK is licensed separately. QGIS
ships the SDK and the driver advertises write support, but the encoder refuses
to run unless `ECW_ENCODE_KEY` and `ECW_ENCODE_COMPANY` are set - and it refuses
at the final write, which on a multi-hour job is the worst possible moment. So
the licence, not just the driver, is checked before the first block is read.
"""

import json
import os
import shutil
from contextlib import suppress

from osgeo import gdal

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingContext,
    QgsProcessingException,
    QgsProcessingOutputNumber,
    QgsProcessingOutputString,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFileDestination,
    QgsProcessingParameterNumber,
    QgsProcessingParameterRasterLayer,
    QgsProject,
)

from ..branding import docs_url
from ..core.compat import mark_advanced

gdal.UseExceptions()

#: Bumped when the tiling scheme changes, so a checkpoint written by an older
#: build is treated as stale instead of resuming onto tiles that do not line up.
CHECKPOINT_VERSION = 1

# ---------------------------------------------------------------- formats
#
# One entry per offered output format, in the order the enum lists them. Add a
# format here and it appears in the dialog; nothing else needs touching.
#
#   drivers    - GDAL driver short names, best first. The first one this build
#                can actually write with is used.
#   ext        - extension the output is normalised to.
#   also       - other extensions accepted without being rewritten.
#   compress   - honours the Compression parameter.
#   quality    - honours the Quality parameter.
#   overviews  - can carry internal overviews.
#   bands      - band counts the format accepts, or None for "anything".
#   max_px     - largest side the format can address, or None.
#
# Accepted data types are not listed: the driver publishes them itself in its
# DMD_CREATIONDATATYPES metadata, which is right for the build in front of us
# rather than for the build this was written against.

FORMATS = [
    ('GeoTIFF (.tif)', {
        'drivers': ('GTiff',), 'ext': '.tif', 'also': ('.tiff',),
        'compress': True, 'quality': False, 'overviews': True,
        'bands': None, 'max_px': None,
    }),
    ('ECW (.ecw)', {
        'drivers': ('ECW',), 'ext': '.ecw', 'also': (),
        'compress': False, 'quality': True, 'overviews': False,
        'bands': None, 'max_px': None,
    }),
    ('JPEG 2000 (.jp2)', {
        'drivers': ('JP2OpenJPEG', 'JP2ECW'), 'ext': '.jp2', 'also': ('.j2k',),
        'compress': False, 'quality': True, 'overviews': False,
        'bands': None, 'max_px': None,
    }),
    ('Erdas IMAGINE (.img)', {
        'drivers': ('HFA',), 'ext': '.img', 'also': (),
        'compress': True, 'quality': False, 'overviews': True,
        'bands': None, 'max_px': None,
    }),
    ('PNG (.png)', {
        'drivers': ('PNG',), 'ext': '.png', 'also': (),
        'compress': False, 'quality': False, 'overviews': False,
        'bands': (1, 2, 3, 4), 'max_px': None,
    }),
    ('JPEG (.jpg)', {
        'drivers': ('JPEG',), 'ext': '.jpg', 'also': ('.jpeg',),
        'compress': False, 'quality': True, 'overviews': False,
        'bands': (1, 3, 4), 'max_px': 65535,
    }),
]

FORMAT_LABELS = [label for label, _spec in FORMATS]
FORMAT_SPECS = [spec for _label, spec in FORMATS]

#: Compression choices offered for GeoTIFF. Erdas IMAGINE has only on/off, so
#: it reads NONE as off and everything else as on.
COMPRESSIONS = ['LZW', 'DEFLATE', 'ZSTD', 'JPEG', 'NONE']

THREADS = ['ALL_CPUS', '1', '2', '4', '8']

FILE_FILTER = (
    'GeoTIFF (*.tif *.tiff);;ECW (*.ecw);;JPEG 2000 (*.jp2);;'
    'Erdas IMAGINE (*.img);;PNG (*.png);;JPEG (*.jpg *.jpeg)'
)


#: Drivers from the Hexagon SDK. It is built into QGIS and advertises write
#: support, but refuses to encode unless the licence details are in GDAL's
#: config - which is why "the driver is there" is not the same as "this will
#: work", and why that has to be checked before a long run rather than at the
#: final write.
HEXAGON_DRIVERS = ('ECW', 'JP2ECW')
HEXAGON_KEYS = ('ECW_ENCODE_KEY', 'ECW_ENCODE_COMPANY')


def driver_metadata(driver_name):
    """A driver's metadata dict, or None when this build has no such driver."""
    try:
        drv = gdal.GetDriverByName(driver_name)
    except RuntimeError:                    # pragma: no cover - UseExceptions
        return None
    return None if drv is None else drv.GetMetadata()


def driver_can_write(driver_name):
    """(True, '') when this build can really write `driver_name` files."""
    md = driver_metadata(driver_name)
    if md is None:
        return False, 'this GDAL build has no "{}" driver'.format(driver_name)
    if not (md.get(gdal.DCAP_CREATE) == 'YES'
            or md.get(gdal.DCAP_CREATECOPY) == 'YES'):
        return False, 'the "{}" driver in this GDAL build can read the format ' \
            'but not write it'.format(driver_name)
    if driver_name in HEXAGON_DRIVERS:
        missing = [key for key in HEXAGON_KEYS if not gdal.GetConfigOption(key)]
        if missing:
            return False, (
                'the "{driver}" driver is present but unlicensed for writing - '
                '{keys} {verb} not set'.format(
                    driver=driver_name, keys=' and '.join(missing),
                    verb='is' if len(missing) == 1 else 'are'))
    return True, ''


def driver_data_types(driver_name):
    """Data types a driver will create, e.g. ('Byte', 'UInt16'), or ()."""
    md = driver_metadata(driver_name) or {}
    return tuple(md.get('DMD_CREATIONDATATYPES', '').split())


def resolve_driver(spec):
    """(driver, '') for the first writable driver of `spec`, else (None, why)."""
    reasons = []
    for driver_name in spec['drivers']:
        ok, why = driver_can_write(driver_name)
        if ok:
            return driver_name, ''
        reasons.append(why)
    return None, '; '.join(reasons)


def normalised_output(path, spec):
    """`path` with the extension the chosen format expects."""
    base, ext = os.path.splitext(path)
    if ext.lower() == spec['ext'] or ext.lower() in spec['also']:
        return path
    return base + spec['ext']


def human_bytes(count):
    """12345678 -> '11.8 MB'. Plain ASCII, for the run log."""
    value = float(count)
    for unit in ('bytes', 'KB', 'MB', 'GB', 'TB'):
        if value < 1024.0 or unit == 'TB':
            return '{:.1f} {}'.format(value, unit) if unit != 'bytes' \
                else '{:.0f} bytes'.format(value)
        value /= 1024.0


# --------------------------------------------------------------- disk space
#
# The estimates below are multiples of the total size of the blocks on disk.
# The blocks are LZW GeoTIFF of the whole image, so their total is a fair
# stand-in for what the merged output will take; for a lossy target it is an
# over-estimate, which is the safe direction to be wrong in.
#
# Running out of space during the final write is the expensive failure: it
# happens after every block has been converted, which on a large orthophoto is
# hours of work, and leaves a truncated output behind. So the space is checked
# before the merge, again before overviews, and once more after them.

#: Room the merged output needs, as a multiple of the blocks it is built from.
FINAL_WRITE_MARGIN = 1.15

#: Room an overview pyramid needs on top of that. A full set adds about a third
#: of the image again.
OVERVIEW_MARGIN = 0.35


def free_space(path):
    """Bytes free on the volume that will hold `path`.

    Walks up to the nearest directory that exists, so it works before the
    output is written, and on a UNC share where splitting the drive letter off
    would not.
    """
    target = os.path.dirname(os.path.abspath(path)) or os.path.abspath(path)
    while target and not os.path.isdir(target):
        parent = os.path.dirname(target)
        if parent == target:
            break
        target = parent
    return shutil.disk_usage(target).free


def overview_levels(width, height):
    """Pyramid levels down to roughly a 256 px thumbnail."""
    levels = []
    factor = 2
    while max(width, height) // factor > 256 and factor <= 4096:
        levels.append(factor)
        factor *= 2
    return levels


def gdal_progress(feedback, start, span):
    """A GDAL progress callback that drives `feedback` over start..start+span."""
    def callback(complete, _message, _data):
        feedback.setProgress(start + complete * span)
        return 0 if feedback.isCanceled() else 1
    return callback


class RasterFormatConverter(QgsProcessingAlgorithm):
    """Tiled, resumable conversion between raster formats."""

    INPUT = 'INPUT'
    FORMAT = 'FORMAT'
    OUTPUT = 'OUTPUT'
    COMPRESSION = 'COMPRESSION'
    QUALITY = 'QUALITY'
    OVERVIEWS = 'OVERVIEWS'
    LOAD = 'LOAD'
    BLOCK_SIZE = 'BLOCK_SIZE'
    THREADS = 'THREADS'
    RESUME = 'RESUME'

    OUTPUT_PATH = 'OUTPUT_PATH'
    BLOCKS_DONE = 'BLOCKS_DONE'

    # ------------------------------------------------------------- metadata

    def tr(self, string):
        return QCoreApplication.translate('KgaRasterFormatConverter', string)

    def createInstance(self):
        return RasterFormatConverter()

    def name(self):
        return 'raster_format_converter'

    def displayName(self):
        return self.tr('Raster Format Converter')

    def group(self):
        return self.tr('KGA Data Conversion')

    def groupId(self):
        return 'kgadataconversion'

    def helpUrl(self):
        return docs_url('raster_format_converter')

    def shortHelpString(self):
        return self.tr(
            '<b>Convert a raster from any format GDAL can read into any format '
            'it can write</b> - ECW to GeoTIFF, GeoTIFF to JPEG 2000, IMG to '
            'GeoTIFF, and back.<br><br>'
            'Built for orthophotos big enough that the conversion runs for '
            'hours. The source is read in square blocks, each written straight '
            'to disk, and a small <code>.checkpoint.json</code> beside the '
            'output records how far the run got. <b>Cancel it, lose QGIS or '
            'lose the machine, then run it again with the same output path and '
            'it carries on from the next unfinished block.</b> When every block '
            'is down they are mosaicked and written once into the format you '
            'chose.<br><br>'
            '<b>Which setting applies to which format</b>'
            '<table>'
            '<tr><th align="left">Format&nbsp;&nbsp;</th>'
            '<th align="left">Compression&nbsp;&nbsp;</th>'
            '<th align="left">Quality&nbsp;&nbsp;</th>'
            '<th align="left">Overviews</th></tr>'
            '<tr><td>GeoTIFF</td><td>yes</td><td>-</td><td>yes</td></tr>'
            '<tr><td>Erdas IMAGINE</td><td>on / off</td><td>-</td>'
            '<td>yes</td></tr>'
            '<tr><td>ECW</td><td>-</td><td>yes (target ratio)</td>'
            '<td>-</td></tr>'
            '<tr><td>JPEG 2000</td><td>-</td><td>yes</td><td>-</td></tr>'
            '<tr><td>JPEG</td><td>-</td><td>yes</td><td>-</td></tr>'
            '<tr><td>PNG</td><td>-</td><td>-</td><td>-</td></tr>'
            '</table>'
            'Settings that do not apply to the chosen format are ignored, not '
            'an error.<br><br>'
            '<b>Watch out for</b><br>'
            '&bull; <b>Writing ECW needs a Hexagon licence.</b> QGIS reads ECW '
            'out of the box but only writes it once the key you bought from '
            'Hexagon is set as the <code>ECW_ENCODE_KEY</code> and '
            '<code>ECW_ENCODE_COMPANY</code> environment variables. The tool '
            'checks that when you press Run, rather than letting GDAL refuse '
            'three hours in at the final write. JPEG 2000 goes through the '
            'open-source JP2OpenJPEG driver instead and needs no licence.<br>'
            '&bull; <b>Temporary disk space.</b> The blocks are LZW GeoTIFF, so '
            'budget roughly the uncompressed size of the source next to the '
            'output. The log prints the estimate before it starts, and the '
            'space is checked again before the final write and before the '
            'overviews - running out during the final write is the one failure '
            'that costs the whole run.<br>'
            '&bull; <b>Overviews are written into the file as it is created</b>, '
            'not edited into it afterwards: they are built on the throwaway '
            'mosaic first and copied in. If there is no room for both at once '
            'they are skipped, and tried again as a sidecar .ovr after the '
            'blocks are cleared, with the finished raster opened read-only. '
            'Whatever happens to the pyramid, the raster itself is never '
            're-opened for writing.<br>'
            '&bull; <b>The block folder and checkpoint are only deleted on a '
            'clean finish.</b> That is what makes resuming work. A cancelled '
            'run leaves them on purpose.<br>'
            '&bull; <b>Changing the block size or the output path starts '
            'over</b> - the old blocks no longer line up, and the tool says so '
            'instead of mixing them.<br>'
            '&bull; <b>JPEG and PNG have limits</b>: JPEG takes 8-bit data, 1, '
            '3 or 4 bands and no side over 65535 pixels; PNG takes 8- or 16-bit '
            'data and up to 4 bands. Both are checked before the run.<br>'
            '&bull; <b>A source with no georeferencing</b> (a plain PNG or JPEG '
            'with no world file) cannot be mosaicked back from blocks, so it is '
            'converted in one pass and cannot be resumed.'
        )

    # ----------------------------------------------------------- parameters

    def _add(self, param, help_text, advanced=False):
        """Add a parameter, with its help shown as the widget tooltip."""
        param.setHelp(help_text)
        if advanced:
            mark_advanced(param)
        self.addParameter(param)

    def initAlgorithm(self, config=None):
        self._add(QgsProcessingParameterRasterLayer(
            self.INPUT, self.tr('Source | Raster to convert')),
            self.tr('Any raster GDAL can read - ECW, GeoTIFF, JPEG 2000, IMG, '
                    'SID, PNG, JPEG. Pick a layer already in the project or '
                    'browse to a file.'))

        self._add(QgsProcessingParameterEnum(
            self.FORMAT, self.tr('Output | Format'),
            options=FORMAT_LABELS, defaultValue=0),
            self.tr('The format to write. The output file extension is '
                    'corrected to match this, so the two can never disagree.'))

        self._add(QgsProcessingParameterFileDestination(
            self.OUTPUT, self.tr('Output | Converted raster'),
            fileFilter=FILE_FILTER),
            self.tr('Where to write. Re-run with this same path to resume an '
                    'interrupted conversion.'))

        self._add(QgsProcessingParameterEnum(
            self.COMPRESSION,
            self.tr('Options | Compression (GeoTIFF, Erdas IMAGINE)'),
            options=COMPRESSIONS, defaultValue=0),
            self.tr('GeoTIFF: LZW and DEFLATE are lossless and widely read, '
                    'ZSTD is lossless and faster where GDAL has it, JPEG is '
                    'lossy but small for photography. Erdas IMAGINE reads '
                    'anything but NONE as "compressed". Ignored by the other '
                    'formats.'))

        self._add(QgsProcessingParameterNumber(
            self.QUALITY,
            self.tr('Options | Quality (ECW, JPEG 2000, JPEG)'),
            type=QgsProcessingParameterNumber.Type.Integer,
            minValue=1, maxValue=100, defaultValue=75),
            self.tr('JPEG and JPEG 2000: higher is better quality and a bigger '
                    'file. ECW reads this as a target compression ratio, where '
                    'higher means a smaller file - the opposite direction. '
                    'Ignored by the other formats.'))

        self._add(QgsProcessingParameterBoolean(
            self.OVERVIEWS, self.tr('Options | Build overviews (pyramids)'),
            defaultValue=True),
            self.tr('Overviews are what make a large raster draw quickly when '
                    'zoomed out. Only GeoTIFF and Erdas IMAGINE can carry them '
                    'internally; for the others this is ignored.'))

        self._add(QgsProcessingParameterBoolean(
            self.LOAD, self.tr('Options | Add the result to the project'),
            defaultValue=False),
            self.tr('Load the converted raster when the run finishes.'))

        self._add(QgsProcessingParameterNumber(
            self.BLOCK_SIZE, self.tr('Advanced | Processing block size (px)'),
            type=QgsProcessingParameterNumber.Type.Integer,
            minValue=512, maxValue=32768, defaultValue=4096),
            self.tr('How much of the source is converted between checkpoints. '
                    'Smaller blocks lose less work when a run is interrupted '
                    'but cost more overhead; larger blocks are faster and need '
                    'more memory. Changing it invalidates an existing '
                    'checkpoint.'), advanced=True)

        self._add(QgsProcessingParameterEnum(
            self.THREADS, self.tr('Advanced | GDAL threads'),
            options=THREADS, defaultValue=0),
            self.tr('Threads GDAL may use for decoding and compression. Drop '
                    'it below ALL_CPUS to leave the machine usable while a '
                    'long conversion runs.'), advanced=True)

        self._add(QgsProcessingParameterBoolean(
            self.RESUME, self.tr('Advanced | Resume from an existing checkpoint'),
            defaultValue=True),
            self.tr('On, an interrupted run continues where it stopped. Off, '
                    'the blocks already on disk are thrown away and the '
                    'conversion starts over.'), advanced=True)

        self.addOutput(QgsProcessingOutputString(
            self.OUTPUT_PATH, self.tr('Converted raster path')))
        self.addOutput(QgsProcessingOutputNumber(
            self.BLOCKS_DONE, self.tr('Blocks converted')))

    # ------------------------------------------------------------ validation

    def checkParameterValues(self, parameters, context):
        spec = FORMAT_SPECS[self.parameterAsEnum(parameters, self.FORMAT,
                                                 context)]

        label = FORMAT_LABELS[FORMAT_SPECS.index(spec)]
        driver, why = resolve_driver(spec)
        if driver is None:
            hint = self.tr(
                'Writing ECW and JPEG 2000 needs the licensed Hexagon SDK: the '
                'reader ships with QGIS, the encoder has to be bought from '
                'Hexagon and its key entered as the ECW_ENCODE_KEY and '
                'ECW_ENCODE_COMPANY environment variables (Settings > Options > '
                'System > Environment, then restart QGIS). '
            ) if any(name in HEXAGON_DRIVERS for name in spec['drivers']) else ''
            return False, self.tr(
                'This QGIS cannot write {label}: {why}. {hint}Choose another '
                'format - GeoTIFF is the one every build can write.').format(
                    label=label, why=why, hint=hint)

        layer = self.parameterAsRasterLayer(parameters, self.INPUT, context)
        raw_output = self.parameterAsOutputLayer(parameters, self.OUTPUT,
                                                 context)
        if layer is not None and raw_output:
            source = self.source_path(layer)
            output = normalised_output(raw_output, spec)
            if source and os.path.abspath(source) == os.path.abspath(output):
                return False, self.tr(
                    'The output is the source file. Convert to a different '
                    'path.')

            problem = self.format_rejects(layer, spec, driver, label)
            if problem:
                return False, problem

        return super().checkParameterValues(parameters, context)

    def format_rejects(self, layer, spec, driver, label):
        """A message when the format cannot hold this raster, else ''."""
        provider = layer.dataProvider()
        if provider is None:
            return ''

        bands = layer.bandCount()
        if spec['bands'] and bands not in spec['bands']:
            return self.tr(
                '{label} cannot hold {bands} bands - it takes {allowed}. '
                'Convert to GeoTIFF, or drop bands first.').format(
                    label=label, bands=bands,
                    allowed=', '.join(str(b) for b in spec['bands']))

        accepted = driver_data_types(driver)
        if accepted:
            try:
                type_name = gdal.GetDataTypeName(
                    provider.dataType(1)) if bands else ''
            except Exception:               # pragma: no cover - provider drift
                type_name = ''
            # The provider's type ids line up with GDAL's for every type these
            # formats accept; an unreadable one is left to GDAL to complain about.
            if type_name and type_name not in accepted:
                return self.tr(
                    '{label} cannot hold {type} data - it takes {allowed}. '
                    'Convert to GeoTIFF instead.').format(
                        label=label, type=type_name,
                        allowed=' or '.join(accepted))

        if spec['max_px']:
            side = max(layer.width(), layer.height())
            if side > spec['max_px']:
                return self.tr(
                    '{label} cannot address a side of {side} pixels - its '
                    'limit is {limit}. Convert to GeoTIFF instead.').format(
                        label=label, side=side, limit=spec['max_px'])
        return ''

    # ------------------------------------------------------------------ run

    def source_path(self, layer):
        """The file GDAL should open for `layer`, or '' if it is not a file."""
        source = layer.source() or ''
        return source.split('|', 1)[0]

    def processAlgorithm(self, parameters, context, feedback):
        layer = self.parameterAsRasterLayer(parameters, self.INPUT, context)
        if layer is None:
            raise QgsProcessingException(self.tr('No source raster.'))
        if layer.providerType() != 'gdal':
            raise QgsProcessingException(self.tr(
                'This tool converts file-based rasters. "{name}" comes from '
                'the {provider} provider, which has no file to read - export '
                'it to disk first.').format(
                    name=layer.name(), provider=layer.providerType()))

        source = self.source_path(layer)
        if not source or not os.path.exists(source):
            raise QgsProcessingException(self.tr(
                'Could not find the source file on disk: {path}').format(
                    path=source or layer.source()))

        spec = FORMAT_SPECS[self.parameterAsEnum(parameters, self.FORMAT,
                                                 context)]
        label = FORMAT_LABELS[FORMAT_SPECS.index(spec)]
        driver, why = resolve_driver(spec)
        if driver is None:
            # checkParameterValues catches this from the dialog; a model or a
            # script that skips validation lands here instead.
            raise QgsProcessingException(self.tr(
                'This QGIS cannot write {label}: {why}.').format(
                    label=label, why=why))

        raw_output = self.parameterAsOutputLayer(parameters, self.OUTPUT,
                                                 context)
        output = normalised_output(raw_output, spec)
        if output != raw_output:
            feedback.pushInfo(self.tr(
                'Output renamed to {name} so the extension matches {label}.'
            ).format(name=os.path.basename(output), label=label))

        block = self.parameterAsInt(parameters, self.BLOCK_SIZE, context)
        threads = THREADS[self.parameterAsEnum(parameters, self.THREADS,
                                               context)]
        compression = COMPRESSIONS[self.parameterAsEnum(
            parameters, self.COMPRESSION, context)]
        quality = self.parameterAsInt(parameters, self.QUALITY, context)
        overviews = (self.parameterAsBool(parameters, self.OVERVIEWS, context)
                     and spec['overviews'])
        resume = self.parameterAsBool(parameters, self.RESUME, context)

        folder = os.path.dirname(output)
        if folder and not os.path.isdir(folder):
            os.makedirs(folder, exist_ok=True)

        src_ds = gdal.Open(source, gdal.GA_ReadOnly)
        if src_ds is None:                  # pragma: no cover - UseExceptions
            raise QgsProcessingException(self.tr(
                'GDAL could not open {path}.').format(path=source))

        width = src_ds.RasterXSize
        height = src_ds.RasterYSize
        bands = src_ds.RasterCount
        dtype = gdal.GetDataTypeName(src_ds.GetRasterBand(1).DataType) \
            if bands else 'unknown'
        georeferenced = self.is_georeferenced(src_ds)

        creation = self.creation_options(driver, compression, quality, threads,
                                         bands, dtype)

        self.log_header(feedback, layer, source, output, label, driver, width,
                        height, bands, dtype, creation)

        gdal.SetConfigOption('GDAL_NUM_THREADS', threads)
        try:
            # `baked` says the pyramid was written into the file as it was
            # created, which only the mosaic path can do.
            baked = False
            if not georeferenced:
                feedback.pushWarning(self.tr(
                    'The source carries no georeferencing, so its blocks could '
                    'not be put back together. Converting in one pass instead - '
                    'this run cannot be resumed.'))
                done = self.convert_whole(src_ds, output, driver, creation,
                                          feedback)
            elif width <= block and height <= block:
                feedback.pushInfo(self.tr(
                    'The raster fits in one block, so it is converted in one '
                    'pass.'))
                done = self.convert_whole(src_ds, output, driver, creation,
                                          feedback)
            else:
                result = self.convert_tiled(src_ds, source, output, driver,
                                            creation, block, resume, width,
                                            height, overviews, feedback)
                if result is None:          # cancelled, checkpoint kept
                    return {self.OUTPUT: output, self.OUTPUT_PATH: output,
                            self.BLOCKS_DONE: 0}
                done, baked = result
        finally:
            src_ds = None
            gdal.SetConfigOption('GDAL_NUM_THREADS', None)

        # Anything the merge could not bake in is tried again here, as a
        # sidecar, now that the blocks have been cleared and their space is
        # back. The finished raster is only ever opened read-only for this.
        if overviews and not baked:
            self.external_overviews(output, width, height, feedback)

        feedback.setProgress(100)
        feedback.pushInfo('')
        feedback.pushInfo(self.tr('Done. Wrote {path} ({size}).').format(
            path=output, size=human_bytes(os.path.getsize(output))
            if os.path.exists(output) else '?'))

        if self.parameterAsBool(parameters, self.LOAD, context):
            self.queue_layer(output, context, feedback)

        return {self.OUTPUT: output, self.OUTPUT_PATH: output,
                self.BLOCKS_DONE: done}

    # --------------------------------------------------------------- helpers

    def is_georeferenced(self, ds):
        """True when the blocks could be mosaicked back into one image."""
        if ds.GetGCPCount():
            return True
        try:
            transform = ds.GetGeoTransform(can_return_null=True)
        except TypeError:                   # pragma: no cover - old bindings
            transform = ds.GetGeoTransform()
        return transform is not None and transform != (0.0, 1.0, 0.0, 0.0, 0.0,
                                                       1.0)

    def creation_options(self, driver, compression, quality, threads, bands,
                         dtype):
        """Driver creation options for the final write."""
        if driver == 'GTiff':
            options = ['TILED=YES', 'BIGTIFF=IF_SAFER',
                       'NUM_THREADS={}'.format(threads)]
            if compression != 'NONE':
                options.append('COMPRESS={}'.format(compression))
            if compression in ('LZW', 'DEFLATE', 'ZSTD'):
                # Horizontal differencing before compression: 3 is the
                # floating-point variant, 2 the integer one. Both are lossless.
                options.append('PREDICTOR={}'.format(
                    3 if dtype in ('Float32', 'Float64') else 2))
            if compression == 'JPEG' and bands == 3 and dtype == 'Byte':
                # Roughly halves the file on RGB photography, and GDAL converts
                # back on read, so the raster still looks like RGB.
                options.append('PHOTOMETRIC=YCBCR')
            return options
        if driver == 'HFA':
            return ['COMPRESSED={}'.format(
                'NO' if compression == 'NONE' else 'YES')]
        if driver == 'ECW':
            return ['TARGET={}'.format(quality)]
        if driver in ('JP2OpenJPEG', 'JPEG'):
            return ['QUALITY={}'.format(quality)]
        return []

    def log_header(self, feedback, layer, source, output, label, driver,
                   width, height, bands, dtype, creation):
        """The plain-ASCII run header."""
        pixels = width * height * bands
        try:
            depth = max(1, gdal.GetDataTypeSize(
                gdal.GetDataTypeByName(dtype)) // 8)
        except Exception:                   # pragma: no cover - unknown type
            depth = 1
        crs = layer.crs()
        feedback.pushInfo('Raster Format Converter')
        feedback.pushInfo('-----------------------')
        feedback.pushInfo('Source     : {}'.format(source))
        feedback.pushInfo('Size       : {} x {} px, {} band(s), {}'.format(
            width, height, bands, dtype))
        feedback.pushInfo('CRS        : {}'.format(
            crs.authid() or crs.description() or 'none'))
        feedback.pushInfo('Output     : {}'.format(output))
        feedback.pushInfo('Format     : {} [{}]'.format(label, driver))
        feedback.pushInfo('Options    : {}'.format(
            ', '.join(creation) if creation else 'driver defaults'))
        feedback.pushInfo('Temp space : up to {} of blocks beside the '
                          'output'.format(human_bytes(pixels * depth)))
        feedback.pushInfo('')

    def discard_partial(self, output):
        """Delete a half-written output and the sidecars GDAL left beside it."""
        for path in (output, output + '.aux.xml', output + '.ovr'):
            with suppress(OSError):
                if os.path.exists(path):
                    os.remove(path)

    def convert_whole(self, src_ds, output, driver, creation, feedback):
        """One-pass conversion, for a small or ungeoreferenced source."""
        # With exceptions on, GDAL does not return when the progress callback
        # asks it to stop - it raises. So cancelling lands here, not after the
        # call, and is told apart from a real failure by the feedback flag.
        try:
            gdal.Translate(output, src_ds, format=driver,
                           creationOptions=creation,
                           callback=gdal_progress(feedback, 0, 95))
        except RuntimeError as error:
            self.discard_partial(output)
            if feedback.isCanceled():
                raise QgsProcessingException(self.tr(
                    'Cancelled. This source could not be converted in blocks, '
                    'so there is nothing to resume from. The unfinished file '
                    'was removed.'))
            raise QgsProcessingException(self.tr(
                'GDAL could not write {path}: {error}').format(
                    path=output, error=error))
        if feedback.isCanceled():
            self.discard_partial(output)
            raise QgsProcessingException(self.tr(
                'Cancelled. This source could not be converted in blocks, so '
                'there is nothing to resume from. The unfinished file was '
                'removed.'))
        return 1

    # --------------------------------------------------------- tiled path

    def blocks_for(self, width, height, block):
        """The (x, y, xsize, ysize) blocks, in the order they are written."""
        blocks = []
        for y in range(0, height, block):
            rows = min(block, height - y)
            for x in range(0, width, block):
                cols = min(block, width - x)
                blocks.append((x, y, cols, rows))
        return blocks

    def checkpoint_paths(self, output):
        return output + '.checkpoint.json', output + '_blocks'

    def read_checkpoint(self, path, fingerprint, feedback):
        """How many blocks a previous run finished, or 0."""
        if not os.path.exists(path):
            return 0
        try:
            with open(path, 'r') as handle:
                saved = json.load(handle)
        except (OSError, ValueError):
            feedback.pushWarning(self.tr(
                'The checkpoint file could not be read, so this run starts '
                'over.'))
            return 0
        if saved.get('version') != CHECKPOINT_VERSION:
            feedback.pushWarning(self.tr(
                'The checkpoint was written by another version of this tool, '
                'so this run starts over.'))
            return 0
        if saved.get('fingerprint') != fingerprint:
            feedback.pushWarning(self.tr(
                'A checkpoint is there but the source, output or block size '
                'has changed since, so its blocks no longer line up - this run '
                'starts over.'))
            return 0
        return int(saved.get('done', 0))

    def write_checkpoint(self, path, fingerprint, done, total):
        """Record progress atomically, so a kill mid-write cannot corrupt it."""
        payload = {'version': CHECKPOINT_VERSION, 'fingerprint': fingerprint,
                   'done': done, 'total': total}
        temporary = path + '.tmp'
        with open(temporary, 'w') as handle:
            json.dump(payload, handle)
        os.replace(temporary, path)

    def convert_tiled(self, src_ds, source, output, driver, creation, block,
                      resume, width, height, overviews, feedback):
        """Block pass, then one merge.

        Returns (blocks, overviews_baked_in), or None when cancelled.
        """
        blocks = self.blocks_for(width, height, block)
        total = len(blocks)
        checkpoint, blocks_dir = self.checkpoint_paths(output)
        # Source size and mtime rather than a hash: hashing a 200 GB ECW to
        # check whether it changed would cost more than the conversion.
        fingerprint = {
            'source': os.path.abspath(source),
            'size': os.path.getsize(source),
            'mtime': int(os.path.getmtime(source)),
            'block': block,
            'total': total,
        }

        done = self.read_checkpoint(checkpoint, fingerprint, feedback) \
            if resume else 0
        if done and resume:
            feedback.pushInfo(self.tr(
                'Resuming: {done} of {total} blocks were already converted.'
            ).format(done=done, total=total))
        elif not resume and os.path.exists(checkpoint):
            feedback.pushInfo(self.tr(
                'Resume is off, so the blocks already on disk are ignored.'))

        feedback.pushInfo(self.tr(
            'Converting in {total} blocks of {block} px ({across} across, '
            '{down} down).').format(
                total=total, block=block,
                across=(width + block - 1) // block,
                down=(height + block - 1) // block))
        if total > 20000:
            feedback.pushWarning(self.tr(
                'That is a lot of blocks. A larger block size would run '
                'faster; the trade is that an interrupted run loses more '
                'work.'))

        os.makedirs(blocks_dir, exist_ok=True)
        if not done:
            # Starting fresh. Blocks left by a run at a different block size
            # have different names, so the merge would ignore them and they
            # would sit there for good - clear them out now.
            self.clear_blocks(blocks_dir)
        block_options = ['COMPRESS=LZW', 'TILED=YES', 'BIGTIFF=IF_SAFER']

        for index, (x, y, cols, rows) in enumerate(blocks):
            if feedback.isCanceled():
                self.write_checkpoint(checkpoint, fingerprint, index, total)
                feedback.pushInfo('')
                feedback.pushInfo(self.tr(
                    'Cancelled after {done} of {total} blocks. Nothing is '
                    'lost: run this again with the same output path to carry '
                    'on from block {next}.').format(
                        done=index, total=total, next=index + 1))
                return None

            path = os.path.join(blocks_dir, 'block_{}_{}.tif'.format(x, y))
            if index < done and os.path.exists(path):
                continue

            # Written under a temporary name and moved into place, so a block
            # interrupted halfway is never mistaken for a finished one.
            partial = path + '.part'
            try:
                gdal.Translate(partial, src_ds, srcWin=[x, y, cols, rows],
                               format='GTiff', creationOptions=block_options)
            except RuntimeError as error:
                with suppress(OSError):
                    os.remove(partial)
                # Progress up to the block before this one is already saved.
                self.write_checkpoint(checkpoint, fingerprint, index, total)
                raise QgsProcessingException(self.tr(
                    'Block {number} of {total} could not be converted: {error}'
                    '. The {done} blocks before it are kept - run this again '
                    'with the same output path to carry on.').format(
                        number=index + 1, total=total, error=error,
                        done=index))
            os.replace(partial, path)

            self.write_checkpoint(checkpoint, fingerprint, index + 1, total)
            feedback.setProgress((index + 1) / total * 75.0)
            if total <= 40 or (index + 1) % 10 == 0 or index + 1 == total:
                feedback.pushInfo(self.tr('  block {done}/{total}').format(
                    done=index + 1, total=total))

        feedback.pushInfo('')
        feedback.pushInfo(self.tr(
            'All blocks converted. Merging into the final {driver} file...'
        ).format(driver=driver))

        vrt = output + '.vrt'
        paths = [os.path.join(blocks_dir, 'block_{}_{}.tif'.format(x, y))
                 for (x, y, _cols, _rows) in blocks]
        blocks_bytes = sum(os.path.getsize(path) for path in paths
                           if os.path.exists(path))
        self.report_space(output, blocks_bytes * FINAL_WRITE_MARGIN,
                          self.tr('the final write'), feedback)
        gdal.BuildVRT(vrt, paths)

        # Overviews are built on the VRT and copied into the output as it is
        # written, rather than edited into the finished file afterwards. The
        # VRT is disposable - a failure there costs a rebuild, not the data -
        # whereas re-opening the real output for update puts hours of work
        # under a write that can run out of space halfway.
        baked = False
        if overviews and driver == 'GTiff':
            baked = self.bake_overviews(vrt, output, blocks_bytes, width,
                                        height, feedback)

        final = list(creation) + (['COPY_SRC_OVERVIEWS=YES'] if baked else [])
        feedback.pushInfo(self.tr('Writing the final {driver} file{extra}...')
                          .format(driver=driver,
                                  extra=self.tr(' with its overviews')
                                  if baked else ''))
        try:
            gdal.Translate(output, vrt, format=driver, creationOptions=final,
                           callback=gdal_progress(feedback, 85, 12))
        except RuntimeError as error:
            # Stopping from the progress callback raises rather than returns,
            # so a cancel arrives here; the feedback flag tells it from a real
            # failure such as a full disk.
            self.discard_partial(output)
            if not feedback.isCanceled():
                raise QgsProcessingException(self.tr(
                    'GDAL could not write the final file: {error}. The blocks '
                    'are kept, so run this again with the same output path '
                    'once the problem is fixed.').format(error=error))
        else:
            if not feedback.isCanceled():
                self.clean_up(vrt, paths, blocks_dir, checkpoint, feedback)
                return total, baked
            self.discard_partial(output)

        # The blocks are all still there, so the next run only redoes the
        # merge - which is the cheap half.
        feedback.pushInfo(self.tr(
            'Cancelled while merging. The blocks are kept: run this again '
            'with the same output path to finish.'))
        return None

    def report_space(self, output, needed, what, feedback):
        """Say whether the output drive has room for `needed` bytes."""
        try:
            free = free_space(output)
        except OSError:                     # pragma: no cover - unreadable drive
            return
        if free < needed:
            feedback.pushWarning(self.tr(
                'Low disk space before {what}: about {needed} needed, {free} '
                'free on the output drive. Carrying on, but it may not '
                'fit.').format(what=what, needed=human_bytes(needed),
                               free=human_bytes(free)))
        else:
            feedback.pushInfo(self.tr(
                'Disk space for {what}: about {needed} needed, {free} '
                'free.').format(what=what, needed=human_bytes(needed),
                                free=human_bytes(free)))

    def bake_overviews(self, vrt, output, blocks_bytes, width, height,
                       feedback):
        """Build the pyramid on the VRT so the final write can copy it in.

        True when the output should be written with COPY_SRC_OVERVIEWS.
        """
        levels = overview_levels(width, height)
        if not levels:
            return False

        # The overview file and the output both sit on the drive at once,
        # before any cleanup runs, so the room needed is for both together.
        # Counting only the overview is what lets a job die during the final
        # write having just spent an hour building the pyramid.
        try:
            free = free_space(output)
        except OSError:                     # pragma: no cover - unreadable drive
            return False
        needed = blocks_bytes * (FINAL_WRITE_MARGIN + OVERVIEW_MARGIN)
        if free < needed:
            feedback.pushInfo(self.tr(
                'Not building overviews into the output: that needs about '
                '{needed} for the pyramid and the file together, and {free} '
                'is free. The file is written without them and they are tried '
                'again once the blocks are cleared.').format(
                    needed=human_bytes(needed), free=human_bytes(free)))
            return False

        feedback.pushInfo(self.tr('Building overviews {levels} on the '
                                  'mosaic...').format(
                                      levels=', '.join(str(l) for l in levels)))
        previous = gdal.GetConfigOption('BIGTIFF_OVERVIEW')
        try:
            # A pyramid for an orthophoto this size will not fit in a classic
            # TIFF's 4 GB offsets.
            gdal.SetConfigOption('BIGTIFF_OVERVIEW', 'YES')
            vrt_ds = gdal.Open(vrt, gdal.GA_Update)
            vrt_ds.BuildOverviews('AVERAGE', levels,
                                  callback=gdal_progress(feedback, 75, 10))
            vrt_ds = None
        except RuntimeError as error:
            feedback.pushWarning(self.tr(
                'Overviews could not be built on the mosaic ({error}). The '
                'file is written without them.').format(error=error))
            return False
        finally:
            gdal.SetConfigOption('BIGTIFF_OVERVIEW', previous)

        # The pyramid is real disk space the estimate above could only guess
        # at. Check again now, while dropping it is still free.
        try:
            free = free_space(output)
        except OSError:                     # pragma: no cover - unreadable drive
            return True
        if free < blocks_bytes * FINAL_WRITE_MARGIN:
            feedback.pushWarning(self.tr(
                'Only {free} is free after building the pyramid, and the '
                'final write needs about {needed}. Dropping the pyramid to '
                'keep the write small - it is tried again afterwards.').format(
                    free=human_bytes(free),
                    needed=human_bytes(blocks_bytes * FINAL_WRITE_MARGIN)))
            try:
                os.remove(vrt + '.ovr')
            except OSError:
                pass
            return False
        return True

    def clear_blocks(self, blocks_dir):
        """Empty the block folder of anything a previous run left in it."""
        for name in os.listdir(blocks_dir):
            if name.endswith('.tif') or name.endswith('.part'):
                try:
                    os.remove(os.path.join(blocks_dir, name))
                except OSError:
                    pass                    # reported later, if it matters

    def clean_up(self, vrt, paths, blocks_dir, checkpoint, feedback):
        """Remove the blocks, the VRT and the checkpoint after a clean finish."""
        leftovers = 0
        for path in [vrt, vrt + '.ovr'] + paths + [checkpoint]:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                leftovers += 1
        try:
            os.rmdir(blocks_dir)
        except OSError:
            leftovers += 1
        if leftovers:
            feedback.pushWarning(self.tr(
                'The conversion finished but {count} temporary file(s) could '
                'not be deleted. Remove {folder} by hand when nothing is '
                'holding it.').format(count=leftovers, folder=blocks_dir))

    def external_overviews(self, output, width, height, feedback):
        """Build the pyramid as a sidecar .ovr beside a finished file.

        The fallback for when the pyramid could not be written into the output
        as it was created: after a one-pass conversion, which has no mosaic to
        build it on, or when there was no room for it at the time and the
        blocks have since been cleared.

        The output is opened **read-only**, which is what makes GDAL write the
        pyramid to a separate .ovr file. The finished raster is never reopened
        for update, so nothing that happens here - including running out of
        space - can damage it.
        """
        levels = overview_levels(width, height)
        if not levels:
            return

        try:
            size = os.path.getsize(output)
            free = free_space(output)
        except OSError:                     # pragma: no cover - unreadable drive
            size = free = None
        if size is not None and free < size * OVERVIEW_MARGIN:
            feedback.pushWarning(self.tr(
                'Not enough space left for overviews: about {needed} needed, '
                '{free} free. The raster itself is complete and correct - '
                'build the pyramids later from Raster > Build Overviews once '
                'there is room.').format(
                    needed=human_bytes(size * OVERVIEW_MARGIN),
                    free=human_bytes(free)))
            return

        feedback.pushInfo(self.tr(
            'Building overviews {levels} as a sidecar .ovr file...').format(
                levels=', '.join(str(level) for level in levels)))
        previous = gdal.GetConfigOption('BIGTIFF_OVERVIEW')
        try:
            gdal.SetConfigOption('BIGTIFF_OVERVIEW', 'YES')
            ds = gdal.Open(output, gdal.GA_ReadOnly)
            ds.BuildOverviews('AVERAGE', levels,
                              callback=gdal_progress(feedback, 97, 3))
            ds = None
        except RuntimeError as error:
            # A missing pyramid is a slow raster, not a failed conversion, and
            # the raster itself was never opened for writing.
            feedback.pushWarning(self.tr(
                'The raster was written but its overviews could not be built: '
                '{error}. The file itself is untouched and valid - build the '
                'pyramids separately from Raster > Build Overviews.').format(
                    error=error))
        finally:
            gdal.SetConfigOption('BIGTIFF_OVERVIEW', previous)

    def queue_layer(self, output, context, feedback):
        """Hand the result to Processing to load when the run ends.

        Going through the context rather than QgsProject keeps this algorithm
        able to run on a worker thread.
        """
        project = context.project() or QgsProject.instance()
        name = os.path.splitext(os.path.basename(output))[0]
        try:
            details = QgsProcessingContext.LayerDetails(name, project, name)
            context.addLayerToLoadOnCompletion(output, details)
        except Exception as error:          # pragma: no cover - API drift
            feedback.pushWarning(self.tr(
                'Could not queue the result for loading: {error}').format(
                    error=error))

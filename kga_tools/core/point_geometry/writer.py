"""Output management: plan output locations and write features in bulk."""

import os

from qgis.core import QgsMemoryProviderUtils, QgsVectorFileWriter

from .compat import sink_fast_insert_flag
from .config import FORMAT_GPKG, FORMAT_MEMORY, OutputInfo

_CHUNK = 5000


def _enum(owner, group, name):
    try:
        return getattr(getattr(owner, group), name)
    except AttributeError:
        return getattr(owner, name)


_CREATE_FILE = _enum(QgsVectorFileWriter, "ActionOnExistingFile", "CreateOrOverwriteFile")
_CREATE_LAYER = _enum(QgsVectorFileWriter, "ActionOnExistingFile", "CreateOrOverwriteLayer")
_NO_ERROR = _enum(QgsVectorFileWriter, "WriterError", "NoError")


def boundary_name(name):
    return name + "_boundary"


def planned_outputs(settings):
    """List of OutputInfo describing where each output will go.

    Order matches the engine's outputs: polygons (if written) then boundary.
    With "boundary line only" the boundary layer takes the chosen name/path.
    """
    name = settings.output_layer_name.strip()
    roles = []
    if settings.mode != "polygon" or settings.writes_polygons:
        roles.append("main")
    if settings.writes_boundary:
        roles.append("boundary")
    suffix_boundary = "main" in roles
    plans = []
    for role in roles:
        renamed = role == "boundary" and suffix_boundary
        lname = boundary_name(name) if renamed else name
        if settings.output_format == FORMAT_MEMORY:
            plans.append(OutputInfo(role, lname))
        elif settings.output_format == FORMAT_GPKG:
            path = settings.output_path
            plans.append(OutputInfo(role, lname, uri="{}|layername={}".format(path, lname), path=path))
        else:
            path = settings.output_path
            if renamed:
                base, ext = os.path.splitext(path)
                path = boundary_name(base) + (ext or ".shp")
            plans.append(OutputInfo(role, lname, uri=path, path=path))
    return plans


def normalize_output_path(path, fmt):
    path = path.strip()
    if not path or fmt == FORMAT_MEMORY:
        return path
    ext = ".gpkg" if fmt == FORMAT_GPKG else ".shp"
    if os.path.splitext(path)[1].lower() != ext:
        path += ext
    return path


def existing_gpkg_layers(path):
    """Names of the layers already stored in a GeoPackage (empty if none)."""
    if not os.path.exists(path):
        return []
    try:
        import warnings

        from osgeo import ogr

        with warnings.catch_warnings():  # GDAL 3.9+ FutureWarning about exceptions
            warnings.simplefilter("ignore", FutureWarning)
            ds = ogr.Open(path)
        if ds is None:
            return []
        names = [ds.GetLayerByIndex(i).GetName() for i in range(ds.GetLayerCount())]
        ds = None
        return names
    except Exception:  # pragma: no cover - GDAL bindings always ship with QGIS
        return []


def write_layer(layer_output, plan, settings, crs, transform_context, progress):
    """Write one LayerOutput; returns the updated OutputInfo.

    ``progress(fraction)`` reports 0..1. Writing is not interrupted once it has
    started, so a file is never left half-written by a cancel request.
    Raises RuntimeError on writer errors.
    """
    feats = layer_output.features
    flag = sink_fast_insert_flag()
    if settings.output_format == FORMAT_MEMORY:
        layer = QgsMemoryProviderUtils.createMemoryLayer(
            plan.layer_name, layer_output.fields, layer_output.wkb_type, crs)
        provider = layer.dataProvider()
        for start in range(0, len(feats), _CHUNK):
            if not provider.addFeatures(feats[start:start + _CHUNK], flag)[0]:
                raise RuntimeError("Could not add features to temporary layer")
            progress((start + _CHUNK) / max(1, len(feats)))
        layer.updateExtents()
        plan.memory_layer = layer
        plan.feature_count = layer.featureCount()
        return plan

    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = settings.output_format
    options.fileEncoding = "UTF-8"
    if settings.output_format == FORMAT_GPKG:
        options.layerName = plan.layer_name
        options.actionOnExistingFile = _CREATE_LAYER if os.path.exists(plan.path) else _CREATE_FILE
    else:
        options.actionOnExistingFile = _CREATE_FILE

    writer = QgsVectorFileWriter.create(
        plan.path, layer_output.fields, layer_output.wkb_type, crs, transform_context, options)
    try:
        if writer.hasError() != _NO_ERROR:
            raise RuntimeError("Cannot create {}: {}".format(plan.path, writer.errorMessage()))
        for start in range(0, len(feats), _CHUNK):
            if not writer.addFeatures(feats[start:start + _CHUNK], flag):
                raise RuntimeError("Write error in {}: {}".format(plan.path, writer.errorMessage()))
            progress((start + _CHUNK) / max(1, len(feats)))
    finally:
        del writer  # flushes and closes the file
    plan.feature_count = len(feats)
    return plan


def shapefile_name_warnings(fields):
    long_names = [f.name() for f in fields if len(f.name()) > 10]
    if long_names:
        return ("Shapefile field names are limited to 10 characters; these were shortened "
                "by GDAL: " + ", ".join(long_names))
    return ""

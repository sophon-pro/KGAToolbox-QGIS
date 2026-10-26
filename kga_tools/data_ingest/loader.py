"""Adds ingested items to the project: bins (layer-tree groups),
interpretation overrides, provenance metadata and proxy toggling."""

import datetime
import os

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsLayerTreeGroup,
    QgsProject,
    QgsRasterLayer,
    QgsRasterRange,
    QgsVectorLayer,
)

from . import inspector
from . import presets as P
from .compat import ignore

PROP_FULL = "kga_data_ingest/full_uri"
PROP_PROXY = "kga_data_ingest/proxy_uri"
PROP_USING_PROXY = "kga_data_ingest/using_proxy"
PROP_SOURCE = "kga_data_ingest/original_source"


def bin_path_for(job, settings, root_folder=None):
    mode = settings.get("bin_mode", P.BIN_TYPE)
    if mode == P.BIN_NONE:
        return []
    if mode == P.BIN_CUSTOM:
        return [settings.get("custom_bin") or "Imported"]
    if mode == P.BIN_FOLDER:
        folder = os.path.dirname(job["source_path"].rstrip("/\\"))
        if root_folder and folder.startswith(root_folder):
            rel = os.path.relpath(folder, os.path.dirname(root_folder.rstrip("/\\")))
            return [p for p in rel.replace("\\", "/").split("/") if p and p != "."]
        return [os.path.basename(folder) or folder]
    return [inspector.KIND_LABELS.get(job["kind"], "Other")]


def ensure_group(path):
    group = QgsProject.instance().layerTreeRoot()
    for name in path:
        child = group.findGroup(name) if hasattr(group, "findGroup") else None
        if child is None or child.parent() is not group:
            child = None
            for c in group.children():
                if isinstance(c, QgsLayerTreeGroup) and c.name() == name:
                    child = c
                    break
        if child is None:
            child = group.addGroup(name)
        group = child
    return group


def apply_overrides(layer, overrides):
    if not overrides:
        return
    if overrides.get("crs"):
        crs = QgsCoordinateReferenceSystem(overrides["crs"])
        if crs.isValid():
            layer.setCrs(crs)
    if isinstance(layer, QgsVectorLayer) and overrides.get("encoding") \
            and layer.providerType() == "ogr":
        layer.setProviderEncoding(overrides["encoding"])
    if isinstance(layer, QgsRasterLayer) and overrides.get("nodata") not in (None, ""):
        value = float(overrides["nodata"])
        prov = layer.dataProvider()
        for b in range(1, layer.bandCount() + 1):
            prov.setUserNoDataValue(b, [QgsRasterRange(value, value)])
    if overrides.get("subset") and isinstance(layer, QgsVectorLayer):
        layer.setSubsetString(overrides["subset"])


def write_history(layer, job, settings, notes):
    md = layer.metadata()
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    text = "%s - Data Explorer from %s (preset: %s; %s)" % (
        stamp, job["source_path"], settings.get("name", "custom"),
        "; ".join(notes) if notes else P.MODE_LABELS.get(settings["mode"], ""))
    try:
        md.addHistoryItem(text)
        layer.setMetadata(md)
    except Exception as exc:
        ignore(exc)


def build_layer(result, settings):
    """Create and configure the layer for one ingest result.

    Thread-safe (touches no project or GUI state), so it runs inside the
    background tasks; opening big CSV/GeoJSON files can take many seconds.
    Returns (layer, error)."""
    job = result["job"]
    uri = result["uri"]
    provider = result["provider"]
    overrides = job.get("overrides", {})
    name = overrides.get("name") or job["name"]

    layer = inspector.make_layer(provider, uri, name)
    if layer is None or not layer.isValid():
        return None, "%s: QGIS could not open the ingested data (%s)" % (name, uri)

    # Converted data already carries the override (CRS baked in); for
    # linked/copied data the override is applied as an interpretation.
    if settings["mode"] not in (P.MODE_CONVERT, P.MODE_CONVERT_PROXY) or \
            job["kind"] not in (inspector.KIND_VECTOR, inspector.KIND_TABLE, inspector.KIND_RASTER):
        apply_overrides(layer, overrides)
    elif overrides.get("subset"):
        apply_overrides(layer, {"subset": overrides["subset"]})

    layer.setCustomProperty(PROP_SOURCE, job["source_path"])
    if result.get("proxy_uri"):
        layer.setCustomProperty(PROP_FULL, result["full_uri"] or uri)
        layer.setCustomProperty(PROP_PROXY, result["proxy_uri"])
        layer.setCustomProperty(PROP_USING_PROXY, False)
    if settings.get("write_history", True):
        write_history(layer, job, settings, result.get("notes"))
    layer.extent()  # warm the extent cache off the GUI thread (full scan for GeoJSON/CSV)
    return layer, None


def place_layer(layer, job, settings, root_folder=None):
    """Add a ready layer to the project in its bin (GUI thread only)."""
    project = QgsProject.instance()
    project.addMapLayer(layer, False)
    path = job.get("bin_path")
    if path is None:
        path = bin_path_for(job, settings, root_folder)
    if path:
        ensure_group(path).addLayer(layer)
    else:
        project.layerTreeRoot().insertLayer(0, layer)
    return layer


def add_result(result, settings, iface=None, root_folder=None, layer=None, error=None):
    """Add one ingest result; builds the layer here if a task didn't."""
    if layer is None and error is None:
        layer, error = build_layer(result, settings)
    if error or layer is None:
        return None, error or "could not open %s" % result.get("uri")
    return place_layer(layer, result["job"], settings, root_folder), None


def toggle_proxies(layers, use_proxy=None):
    """Swap layers between full-resolution and proxy sources,
    like Premiere Pro's Toggle Proxies button."""
    switched = 0
    for layer in layers:
        full = layer.customProperty(PROP_FULL)
        proxy = layer.customProperty(PROP_PROXY)
        if not full or not proxy:
            continue
        using = str(layer.customProperty(PROP_USING_PROXY)).lower() == "true"
        target_proxy = (not using) if use_proxy is None else use_proxy
        if target_proxy == using:
            continue
        uri = proxy if target_proxy else full
        renderer = layer.renderer().clone() if hasattr(layer, "renderer") and layer.renderer() else None
        layer.setDataSource(uri, layer.name(), layer.providerType())
        if renderer is not None:
            try:
                layer.setRenderer(renderer)
            except Exception as exc:
                ignore(exc)
        layer.setCustomProperty(PROP_USING_PROXY, target_proxy)
        layer.triggerRepaint()
        switched += 1
    return switched


def proxy_layers():
    return [lyr for lyr in QgsProject.instance().mapLayers().values()
            if lyr.customProperty(PROP_PROXY)]

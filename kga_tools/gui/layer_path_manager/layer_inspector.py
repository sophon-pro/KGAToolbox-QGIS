# -*- coding: utf-8 -*-
"""
Layer Path Manager - layer inspection engine.

Scanning happens in two phases so that large projects stay responsive and
scans can be cancelled:

1. ``collect_records`` (main thread, fast): reads layer metadata from the
   QgsMapLayer objects - provider, URI, validity, geometry, tree position.
2. ``check_filesystem`` (thread safe, may run in a QgsTask): touches only the
   plain ``LayerRecord`` objects and the filesystem - existence, permissions,
   sidecar files, sizes - and derives the final status and error details.
   Network shares that hang only block this background phase.
"""

import os
from collections import defaultdict
from typing import Dict, Iterable, List, Optional

from qgis.core import QgsFeedback, QgsMapLayer, QgsProject

from .utils import (
    KIND_DATABASE, KIND_DIRECTORY, KIND_FILE, KIND_MEMORY, KIND_REMOTE,
    STATUS_BROKEN, STATUS_REMOTE, STATUS_UNSUPPORTED, STATUS_VALID,
    STORAGE_ABSOLUTE, STORAGE_NA, STORAGE_RELATIVE,
    LayerRecord, classify_provider, clean_path, dataset_size, decode_source, error_summary,
    format_for_path, geometry_type_name, layer_type_name, logger,
    looks_absolute, missing_sidecars, project_uses_relative_paths,
    sublayer_description, IS_WINDOWS,
)


def active_map_layer_ids():
    """Ids of the active map's layers, or None when Map Tabs is off (every layer counts)."""
    try:
        from ..map_tabs import active_map_layer_ids as ids
        return ids()
    except Exception:
        return None


class LayerInspector:
    """Builds ``LayerRecord`` snapshots for project layers."""

    def __init__(self, project: Optional[QgsProject] = None):
        self.project = project or QgsProject.instance()

    # ------------------------------------------------------------------
    # Phase 1 - main thread
    # ------------------------------------------------------------------
    def project_layers(self) -> List[QgsMapLayer]:
        """Layers in layer-tree order (layers not in the tree last).

        While Map Tabs is on, only the layers of the active map are listed.
        """
        only = active_map_layer_ids()
        root = self.project.layerTreeRoot()
        ordered, seen = [], set()
        for node in root.findLayers():
            lyr = node.layer()
            if lyr is not None and lyr.id() not in seen and (only is None or lyr.id() in only):
                ordered.append(lyr)
                seen.add(lyr.id())
        if only is None:
            for lyr in self.project.mapLayers().values():
                if lyr.id() not in seen:
                    ordered.append(lyr)
        return ordered

    def collect_records(self, layers: Optional[Iterable[QgsMapLayer]] = None) -> List[LayerRecord]:
        layers = list(layers) if layers is not None else self.project_layers()
        records = []
        resolver = self.project.pathResolver()
        saved = bool(self.project.fileName())
        relative_mode = project_uses_relative_paths(self.project)
        root = self.project.layerTreeRoot()
        for layer in layers:
            try:
                records.append(self._record_for(layer, resolver, saved, relative_mode, root))
            except Exception as exc:  # one bad layer must never break the scan
                logger.exception("Could not inspect layer %s", layer.name())
                records.append(LayerRecord(
                    layer_id=layer.id(), name=layer.name(), status=STATUS_BROKEN,
                    error=f"Inspection error: {exc}"))
        return records

    def _record_for(self, layer, resolver, saved, relative_mode, root) -> LayerRecord:
        provider = layer.providerType() or ""
        source = layer.source() or ""
        parts = decode_source(provider, source)
        path = parts.get("path", "")
        kind = classify_provider(provider, source, path)
        if kind not in (KIND_FILE, KIND_DIRECTORY):
            path_for_record = path if kind == KIND_REMOTE else ""
        else:
            path_for_record = clean_path(path)

        ltype = layer_type_name(layer)
        if ltype == "Vector":
            geom = geometry_type_name(layer)
        elif ltype == "Raster":
            geom = "N/A (raster)"
            try:
                if layer.isValid():
                    geom = f"N/A (raster, {layer.bandCount()} band(s))"
            except Exception:  # band count unavailable: keep the plain label
                geom = "N/A (raster)"
        else:
            geom = "N/A"

        rec = LayerRecord(
            layer_id=layer.id(), name=layer.name(), layer_type=ltype,
            provider=provider, geometry=geom, source=source,
            path=path_for_record, source_kind=kind, layer_valid=layer.isValid(),
            sublayer=sublayer_description(parts),
        )

        if rec.is_file_based:
            rec.file_format = format_for_path(rec.path, provider)
            stored = resolver.writePath(rec.path) if saved else rec.path
            rec.stored_path = stored
            if not saved:
                rec.path_storage = STORAGE_ABSOLUTE
                rec.notes = "Project not saved - paths are kept absolute until saved"
            elif relative_mode and not looks_absolute(stored):
                rec.path_storage = STORAGE_RELATIVE
            else:
                rec.path_storage = STORAGE_ABSOLUTE
                if relative_mode:
                    rec.notes = "Cannot be stored relative (different drive or share)"
        else:
            rec.file_format = {
                KIND_DATABASE: "Database table",
                KIND_REMOTE: "Web service" if not path else format_for_path(path, provider),
                KIND_MEMORY: "Memory / virtual",
            }.get(kind, provider.upper())
            rec.path_storage = STORAGE_NA

        # layer tree position, e.g. "Basemap / Rivers"
        node = root.findLayer(layer.id())
        names = []
        parent = node.parent() if node else None
        while parent is not None and parent.parent() is not None:
            names.append(parent.name())
            parent = parent.parent()
        rec.group_path = " / ".join(reversed(names))

        if not layer.isValid():
            rec.error = error_summary(layer)
        return rec

    # ------------------------------------------------------------------
    # Phase 2 - thread safe (plain data + filesystem only)
    # ------------------------------------------------------------------
    @staticmethod
    def check_filesystem(feedback: Optional[QgsFeedback], records: List[LayerRecord],
                         compute_sizes: bool = True) -> List[LayerRecord]:
        total = max(1, len(records))
        for i, rec in enumerate(records):
            if feedback is not None:
                if feedback.isCanceled():
                    break
                feedback.setProgress(100.0 * i / total)
            try:
                LayerInspector._check_one(rec, compute_sizes)
            except Exception as exc:
                rec.status = STATUS_BROKEN
                rec.error = f"Filesystem check failed: {exc}"
        LayerInspector.mark_duplicates(records)
        if feedback is not None and not feedback.isCanceled():
            feedback.setProgress(100.0)
        return records

    @staticmethod
    def _check_one(rec: LayerRecord, compute_sizes: bool):
        provider_error = rec.error
        if rec.source_kind == KIND_REMOTE:
            rec.status = STATUS_REMOTE
            if not rec.layer_valid:
                rec.error = ("Remote source could not be opened (service unreachable, "
                             "authentication or URL problem)" +
                             (f": {provider_error}" if provider_error else ""))
            return
        if rec.source_kind == KIND_DATABASE:
            rec.status = STATUS_VALID if rec.layer_valid else STATUS_BROKEN
            if not rec.layer_valid:
                rec.error = ("Database connection failed - check the connection settings "
                             "(file-path repair does not apply)")
            rec.notes = "Database connection (credentials/host required on other machines)"
            return
        if rec.source_kind == KIND_MEMORY:
            rec.status = STATUS_UNSUPPORTED
            if rec.provider == "memory":
                rec.notes = "Temporary scratch layer - its features are NOT saved with the project"
            else:
                rec.notes = "Virtual/in-memory layer - no file path to manage"
            return
        if not rec.is_file_based:
            rec.status = STATUS_UNSUPPORTED
            rec.notes = rec.notes or "No local file path in this data source"
            if not rec.layer_valid and rec.layer_type not in ("Annotation", "Group"):
                rec.error = provider_error or "Layer is not valid"
            return

        # ---- local file or directory dataset ----
        path = rec.path
        exists = os.path.exists(path)
        rec.exists = exists
        rec.is_directory = exists and os.path.isdir(path)
        if rec.is_directory and rec.source_kind == KIND_FILE:
            rec.source_kind = KIND_DIRECTORY
            rec.file_format = format_for_path(path, rec.provider, is_dir=True)

        if not exists:
            rec.status = STATUS_BROKEN
            parent = os.path.dirname(path)
            if parent and not os.path.isdir(parent):
                rec.error = f"File not found - folder is also missing: {parent}"
            else:
                rec.error = "File not found at the referenced location"
            if path.startswith(("//", "\\\\")):
                rec.error += " (network share may be offline)"
            return
        if not os.access(path, os.R_OK):
            rec.status = STATUS_BROKEN
            rec.error = "Permission denied - the file exists but is not readable"
            return
        sidecars = missing_sidecars(path)
        if compute_sizes:
            rec.size_bytes = dataset_size(path)
        if not rec.layer_valid:
            rec.status = STATUS_BROKEN
            msg = "File exists but QGIS could not open it"
            if sidecars:
                msg += f" - missing required file(s): {', '.join(sidecars)}"
            elif rec.sublayer:
                msg += f" - check the sublayer ({rec.sublayer})"
            if provider_error:
                msg += f" [{provider_error}]"
            rec.error = msg
            return
        rec.status = STATUS_VALID
        rec.error = ""
        if sidecars:
            rec.notes = (rec.notes + "; " if rec.notes else "") + \
                f"Missing sidecar(s): {', '.join(sidecars)}"

    @staticmethod
    def mark_duplicates(records: List[LayerRecord]) -> Dict[str, List[LayerRecord]]:
        """Flag layers that reference exactly the same data source."""
        groups = defaultdict(list)
        for rec in records:
            if rec.is_file_based or rec.source_kind in (KIND_DATABASE, KIND_REMOTE):
                key = clean_path(rec.source) if rec.is_file_based else rec.source
                if IS_WINDOWS:
                    key = key.lower()
                groups[key].append(rec)
        dups = {k: v for k, v in groups.items() if len(v) > 1}
        for group in dups.values():
            for rec in group:
                others = [r.name for r in group if r is not rec]
                note = "Same data source as: " + ", ".join(others[:5])
                if len(others) > 5:
                    note += f" (+{len(others) - 5} more)"
                if note not in rec.notes:
                    rec.notes = (rec.notes + "; " if rec.notes else "") + note
        return dups

    # ------------------------------------------------------------------
    def scan(self, layers=None, feedback: Optional[QgsFeedback] = None,
             compute_sizes: bool = True) -> List[LayerRecord]:
        """Synchronous full scan (both phases)."""
        records = self.collect_records(layers)
        return self.check_filesystem(feedback, records, compute_sizes)


def summarize(records: List[LayerRecord]) -> Dict[str, int]:
    s = {"total": len(records), "valid": 0, "broken": 0, "remote": 0,
         "unsupported": 0, "file_based": 0, "repaired": 0,
         "broken_file": 0, "relative": 0, "absolute": 0}
    for r in records:
        if r.status == STATUS_VALID:
            s["valid"] += 1
        elif r.status == STATUS_BROKEN:
            s["broken"] += 1
            if r.is_file_based:
                s["broken_file"] += 1
        elif r.status == STATUS_REMOTE:
            s["remote"] += 1
        else:
            s["unsupported"] += 1
        if r.is_file_based:
            s["file_based"] += 1
            if r.path_storage == STORAGE_RELATIVE:
                s["relative"] += 1
            elif r.path_storage == STORAGE_ABSOLUTE:
                s["absolute"] += 1
        if r.repaired:
            s["repaired"] += 1
    return s

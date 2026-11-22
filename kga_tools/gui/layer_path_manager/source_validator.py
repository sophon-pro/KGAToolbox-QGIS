# -*- coding: utf-8 -*-
"""
Layer Path Manager - data source validation.

Before any layer is re-pointed, the proposed source is opened in a temporary,
throw-away layer object with the original provider.  The temporary layer is
never added to the project and the dataset is opened read-only, so validation
cannot modify data.

Validation also compares the candidate with the original layer (geometry type,
band count, CRS and attribute fields used by the layer) and reports
incompatibilities as warnings so the user can decide.
"""

import os
from typing import List, Optional, Tuple

from qgis.core import (
    QgsDataProvider, QgsMapLayer, QgsProject, QgsProviderRegistry,
    QgsRasterLayer, QgsVectorLayer,
)

from .utils import (
    ChangeProposal, STATUS_BROKEN, STATUS_VALID, decode_source, error_summary,
    geometry_type_name, layer_type_name, logger,
)

VALID_OK = "OK"
VALID_WARN = "Warning"
VALID_FAIL = "Failed"


class SourceValidator:
    """Test-opens data sources without touching project layers."""

    def __init__(self, project: Optional[QgsProject] = None):
        self.project = project or QgsProject.instance()

    # ------------------------------------------------------------------
    def open_temporary(self, uri: str, provider: str, layer_type: str):
        """Open a temporary (unregistered) layer. Returns the layer or None."""
        if layer_type == "Vector":
            opts = QgsVectorLayer.LayerOptions(self.project.transformContext())
            opts.loadDefaultStyle = False
            return QgsVectorLayer(uri, "lpm_validation", provider, opts)
        if layer_type == "Raster":
            # Open the *provider* only: a QgsRasterLayer would build a default
            # renderer, compute statistics and make GDAL write a .aux.xml
            # sidecar next to the dataset. Validation must leave no trace.
            return QgsProviderRegistry.instance().createProvider(
                provider, uri, QgsDataProvider.ProviderOptions())
        if layer_type == "Mesh":
            from qgis.core import QgsMeshLayer
            return QgsMeshLayer(uri, "lpm_validation", provider)
        if layer_type == "Point cloud":
            from qgis.core import QgsPointCloudLayer
            return QgsPointCloudLayer(uri, "lpm_validation", provider)
        if layer_type == "Vector tile":
            from qgis.core import QgsVectorTileLayer
            return QgsVectorTileLayer(uri, "lpm_validation")
        prov = QgsProviderRegistry.instance().createProvider(
            provider, uri, QgsDataProvider.ProviderOptions())
        return prov

    # ------------------------------------------------------------------
    def validate_proposal(self, proposal: ChangeProposal,
                          original_layer: Optional[QgsMapLayer] = None) -> Tuple[str, str]:
        """Validate one proposal in place; returns (state, message)."""
        state, msg = self._validate(proposal, original_layer)
        proposal.validation_state = state
        proposal.validation_message = msg
        return state, msg

    def _validate(self, p: ChangeProposal, original_layer) -> Tuple[str, str]:
        if not p.new_path or not p.new_source:
            return VALID_FAIL, "No replacement path selected"
        if not os.path.exists(p.new_path):
            return VALID_FAIL, "Replacement path does not exist"
        if not os.access(p.new_path, os.R_OK):
            return VALID_FAIL, "Replacement path is not readable (permission denied)"
        if p.is_directory and not os.path.isdir(p.new_path):
            return VALID_FAIL, "The original source is a folder dataset but a file was selected"

        try:
            tmp = self.open_temporary(p.new_source, p.provider, p.layer_type)
        except Exception as exc:
            return VALID_FAIL, f"Provider error while opening: {exc}"
        if tmp is None or not tmp.isValid():
            detail = error_summary(tmp)
            sub = decode_source(p.provider, p.new_source)
            hint = ""
            if sub.get("layerName") not in (None, "", "NULL"):
                hint = f" - does it contain the sublayer '{sub.get('layerName')}'?"
            return VALID_FAIL, ("Dataset could not be opened by the '%s' provider%s%s" %
                                (p.provider, hint, f" ({detail})" if detail else ""))

        warnings: List[str] = []
        details: List[str] = []
        try:
            if isinstance(tmp, QgsVectorLayer):
                new_geom = geometry_type_name(tmp)
                details.append(new_geom)
                if original_layer is not None and isinstance(original_layer, QgsVectorLayer):
                    old_gt = original_layer.geometryType()
                    if p.expected_geometry and not p.expected_geometry.startswith("Unknown") \
                            and old_gt != tmp.geometryType():
                        warnings.append(f"Geometry type differs (layer: {p.expected_geometry}, "
                                        f"dataset: {new_geom}) - symbology may not apply")
                    old_fields = set(original_layer.fields().names())
                    if old_fields:
                        missing = sorted(old_fields - set(tmp.fields().names()))
                        # expression/virtual fields are not expected in the dataset
                        virtual = {original_layer.fields().at(i).name()
                                   for i in range(original_layer.fields().count())
                                   if original_layer.fields().fieldOrigin(i) != 0
                                   and original_layer.fields().fieldOrigin(i) != 1}
                        missing = [m for m in missing if m not in virtual]
                        if missing:
                            warnings.append("Fields missing in replacement: " +
                                            ", ".join(missing[:8]) +
                                            ("..." if len(missing) > 8 else ""))
            elif p.layer_type == "Raster" and hasattr(tmp, "bandCount"):
                details.append(f"{tmp.bandCount()} band(s), {tmp.xSize()}x{tmp.ySize()} px")
                if original_layer is not None and isinstance(original_layer, QgsRasterLayer) \
                        and original_layer.isValid() and original_layer.bandCount() != tmp.bandCount():
                    warnings.append(f"Band count differs ({original_layer.bandCount()} -> "
                                    f"{tmp.bandCount()}) - renderer may be reset")
            crs = tmp.crs() if hasattr(tmp, "crs") else None
            if crs is not None and crs.isValid():
                details.append(crs.authid() or crs.description())
                if original_layer is not None and original_layer.crs().isValid() \
                        and original_layer.crs() != crs:
                    warnings.append(f"CRS differs ({original_layer.crs().authid()} -> {crs.authid()})")
        except Exception as exc:  # comparison problems are not fatal
            logger.warning("Comparison during validation failed: %s", exc)
        finally:
            del tmp

        if warnings:
            return VALID_WARN, "Readable; " + "; ".join(warnings)
        return VALID_OK, "Readable" + (f" ({', '.join(details)})" if details else "")

    # ------------------------------------------------------------------
    def check_layer(self, layer: QgsMapLayer) -> Tuple[str, str]:
        """Re-check a project layer's *current* source by test-opening it."""
        if layer is None:
            return VALID_FAIL, "Layer no longer exists"
        p = ChangeProposal(
            layer_id=layer.id(), layer_name=layer.name(), provider=layer.providerType(),
            layer_type=layer_type_name(layer), original_source=layer.source(),
            original_path="", new_source=layer.source())
        parts = decode_source(p.provider, p.new_source)
        p.new_path = parts.get("path", "")
        if not p.new_path or not (os.path.isabs(p.new_path) or p.new_path.startswith("/")):
            return (VALID_OK if layer.isValid() else VALID_FAIL,
                    "Layer valid (non-file source)" if layer.isValid() else "Layer is not valid")
        p.is_directory = os.path.isdir(p.new_path)
        return self._validate(p, None)

    @staticmethod
    def status_from_state(state: str) -> str:
        return STATUS_VALID if state in (VALID_OK, VALID_WARN) else STATUS_BROKEN

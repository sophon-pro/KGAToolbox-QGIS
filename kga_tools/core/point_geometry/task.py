"""Background QgsTask wrapping the engine and writer.

Thread-safety rules followed here:
* Everything taken from the QgsVectorLayer / QgsProject (feature source
  snapshot, fields, CRS, selection, transform context, expression context) is
  captured in ``__init__``, which runs on the main thread.
* ``run()`` only uses those copies and creates new objects.
* Temporary layers created in the worker are moved to the main thread before
  ``finished()`` adds them to the project. Project and GUI changes happen only
  in ``finished()``, which QGIS calls on the main thread.
"""

import time
import traceback
from contextlib import suppress

from qgis.core import (
    QgsApplication,
    QgsExpressionContext,
    QgsExpressionContextUtils,
    QgsMessageLog,
    QgsProject,
    QgsTask,
    QgsVectorLayer,
    QgsVectorLayerFeatureSource,
)

from ...branding import LOG_TAG
from . import writer
from .compat import message_level, task_can_cancel_flag
from .config import FORMAT_SHP, MODE_POLYGON, LINE_SEGMENTS, ConversionResult
from .engine import ConversionEngine, EngineCancelled, EngineInput


def primary_key_names(layer):
    names = []
    with suppress(Exception):   # a provider without keys just has none
        fields = layer.fields()
        for idx in layer.dataProvider().pkAttributeIndexes():
            if 0 <= idx < fields.count():
                names.append(fields.at(idx).name())
    names.append("fid")  # GeoPackage key column; meaningless after aggregation
    return names


def mode_label(settings):
    if settings.mode == MODE_POLYGON:
        if settings.writes_boundary and not settings.writes_polygons:
            return "Points to Polygon (boundary line only)"
        return "Points to Polygon" + (" + boundary line" if settings.writes_boundary else "")
    if settings.line_structure == LINE_SEGMENTS:
        return "Points to Line (individual segments)"
    return "Points to Line (continuous polyline)"


class _TaskFeedback:
    def __init__(self, task):
        self.task = task

    def is_canceled(self):
        return self.task.isCanceled()

    def set_progress(self, value):
        self.task.setProgress(value)


class ConversionTask(QgsTask):
    def __init__(self, layer, settings, on_finished=None):
        super().__init__("Point to Geometry: " + layer.name(), task_can_cancel_flag())
        self.settings = settings
        self.on_finished = on_finished
        self.result = ConversionResult(
            input_layer_name=layer.name(),
            scope="Selected features only" if settings.selected_only else "All features",
            mode_label=mode_label(settings),
        )
        fid_filter = None
        count = layer.featureCount()
        if settings.selected_only:
            fid_filter = set(layer.selectedFeatureIds())
            count = len(fid_filter)
        ctx = QgsExpressionContext(QgsExpressionContextUtils.globalProjectLayerScopes(layer))
        self.engine_input = EngineInput(
            source=QgsVectorLayerFeatureSource(layer),
            fields=layer.fields(),
            wkb_type=layer.wkbType(),
            layer_name=layer.name(),
            feature_count=count,
            fid_filter=fid_filter,
            expression_context=ctx,
            excluded_field_names=primary_key_names(layer),
        )
        self.crs = layer.crs()
        self.transform_context = QgsProject.instance().transformContext()
        self.plans = writer.planned_outputs(settings)
        self.main_thread = QgsApplication.instance().thread()
        self.loaded_layers = []

    # ------------------------------------------------------------ worker
    def run(self):
        start = time.time()
        r = self.result
        try:
            engine = ConversionEngine(self.engine_input, self.settings, r, _TaskFeedback(self))
            outputs = engine.run()
            if self.isCanceled():
                raise EngineCancelled()
            if not outputs[0].features:
                r.error("", "No valid geometry could be created; no output was written.")
                r.elapsed = time.time() - start
                return True
            if self.settings.output_format == FORMAT_SHP:
                w = writer.shapefile_name_warnings(outputs[0].fields)
                if w:
                    r.warning("", w)
            n = len(outputs)
            for i, (layer_output, plan) in enumerate(zip(outputs, self.plans)):
                base = 85 + 15.0 * i / n

                def prog(frac, base=base):
                    self.setProgress(min(100, base + 15.0 / n * min(1.0, frac)))

                info = writer.write_layer(layer_output, plan, self.settings, self.crs,
                                          self.transform_context, prog)
                if info.memory_layer is not None:
                    info.memory_layer.moveToThread(self.main_thread)
                r.outputs.append(info)
        except EngineCancelled:
            r.cancelled = True
        except Exception as e:  # report, never crash QGIS
            r.fatal_error = str(e) or e.__class__.__name__
            QgsMessageLog.logMessage(traceback.format_exc(), LOG_TAG, message_level("Critical"))
        r.elapsed = time.time() - start
        return r.fatal_error is None and not r.cancelled

    # -------------------------------------------------------- main thread
    def finished(self, ok):
        r = self.result
        if ok and self.settings.add_to_project:
            for info in r.outputs:
                if info.memory_layer is not None:
                    layer = info.memory_layer
                else:
                    layer = QgsVectorLayer(info.uri, info.layer_name, "ogr")
                if layer.isValid():
                    QgsProject.instance().addMapLayer(layer)
                    self.loaded_layers.append(layer)
                else:
                    r.error("", "Output written but could not be loaded: " + info.uri)
        for issue in r.issues:
            level = {"info": "Info", "warning": "Warning", "error": "Critical"}[issue.level]
            QgsMessageLog.logMessage("Point to Geometry: " + issue.text(), LOG_TAG, message_level(level))
        if r.fatal_error:
            QgsMessageLog.logMessage("Point to Geometry: " + r.fatal_error, LOG_TAG, message_level("Critical"))
        if self.on_finished is not None:
            self.on_finished(self)


def run_synchronously(layer, settings):
    """Run a conversion in the calling thread (used by tests and scripts)."""
    task = ConversionTask(layer, settings)
    ok = task.run()
    task.finished(ok)
    return task

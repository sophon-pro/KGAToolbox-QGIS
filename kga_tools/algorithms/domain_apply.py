# -*- coding: utf-8 -*-
"""Batch domain tools: apply a library, and validate data against domains.

Both are plain parameter-driven algorithms, so they work in a model, in batch
mode, and from `processing.run()` — which is the point of having them alongside
the dialog. The dialog is for exploring; these are for repeating.
"""

from qgis.core import (
    Qgis,
    QgsFeature,
    QgsFeatureSink,
    QgsMessageLog,
    QgsProcessingAlgorithm,
    QgsProcessingOutputNumber,
    QgsProcessingParameterFeatureSink,
    QgsProcessingException,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFile,
    QgsProcessingParameterFileDestination,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterVectorLayer,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QCoreApplication

from ..branding import LOG_TAG
from ..core import domains as D
from ..core.changelog import ChangeReport, violation_fields
from ..core.compat import no_threading, point_wkb_type, source_type


class ApplyDomainLibraryAlgorithm(QgsProcessingAlgorithm):
    """Bring a JSON domain library into a GeoPackage."""

    LIBRARY = 'LIBRARY'
    TARGET = 'TARGET'
    MODE = 'MODE'
    ATTACH_BY_NAME = 'ATTACH_BY_NAME'
    DRY_RUN = 'DRY_RUN'
    OUTPUT_HTML = 'OUTPUT_HTML'

    MODES = ('Merge — add and update, keep the rest',
             'Replace — make the target match the library exactly')

    def tr(self, string):
        return QCoreApplication.translate('KgaDomainApply', string)

    def createInstance(self):
        return ApplyDomainLibraryAlgorithm()

    def name(self):
        return 'apply_domain_library'

    def displayName(self):
        return self.tr('Apply Domain Library')

    def group(self):
        return self.tr('KGA Schema Tools')

    def groupId(self):
        return 'kgaschematools'

    def helpUrl(self):
        return 'https://khmergrs.com/docs/qgis/apply_domain_library'

    def shortHelpString(self):
        return self.tr(
            'Import a domain library — the JSON file the Domain & Schema '
            'Manager exports — into a GeoPackage, and attach its domains to '
            'the fields that should carry them.\n\n'
            '<b>Merge</b> adds and updates domains, leaving anything the '
            'library does not mention alone. <b>Replace</b> also deletes those, '
            'so the target ends up matching the library exactly.\n\n'
            '<b>Attach to matching field names</b> gives every field with the '
            'same name the same domain. In practice the same column recurs '
            'across layers — status, owner, material — so this saves the bulk '
            'of the work.\n\n'
            'Runs as a dry run by default: you get the full report of what '
            'would change without anything being written.'
        )

    def flags(self):
        # Touches layers that may be open in the project.
        return no_threading(super().flags())

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFile(
            self.LIBRARY, self.tr('Domain library (JSON)'),
            extension='json'))
        self.addParameter(QgsProcessingParameterFile(
            self.TARGET, self.tr('Target GeoPackage'), extension='gpkg'))
        self.addParameter(QgsProcessingParameterEnum(
            self.MODE, self.tr('Mode'),
            options=[self.tr(m) for m in self.MODES], defaultValue=0))
        self.addParameter(QgsProcessingParameterBoolean(
            self.ATTACH_BY_NAME,
            self.tr('Attach each domain to every field with a matching name'),
            defaultValue=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.DRY_RUN, self.tr('Dry run (report only, write nothing)'),
            defaultValue=True))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.OUTPUT_HTML, self.tr('Change report'),
            self.tr('HTML files (*.html)'), optional=True,
            createByDefault=False))

    def processAlgorithm(self, parameters, context, feedback):
        library_path = self.parameterAsFile(parameters, self.LIBRARY, context)
        target_path = self.parameterAsFile(parameters, self.TARGET, context)
        mode = (D.MODE_MERGE, D.MODE_REPLACE)[
            self.parameterAsEnum(parameters, self.MODE, context)]
        attach = self.parameterAsBool(parameters, self.ATTACH_BY_NAME, context)
        dry_run = self.parameterAsBool(parameters, self.DRY_RUN, context)
        report_path = self.parameterAsFileOutput(
            parameters, self.OUTPUT_HTML, context)

        try:
            data = D.load_library(library_path)
            conn = D.connection_for(target_path)
            result = D.import_library(conn, data, target_path, mode, attach,
                                      dry_run=dry_run)
        except D.DomainError as exc:
            raise QgsProcessingException(str(exc))

        report = ChangeReport(
            self.tr('Apply Domain Library'),
            self.tr('{library} into {target}').format(
                library=library_path, target=target_path),
            dry_run=dry_run)
        report.set_count(self.tr('domains created'), len(result.created))
        report.set_count(self.tr('domains updated'), len(result.updated))
        report.set_count(self.tr('domains unchanged'), len(result.unchanged))
        report.set_count(self.tr('domains deleted'), len(result.deleted))
        report.set_count(self.tr('fields attached'), len(result.attached))

        if result.attached:
            report.add_table(
                self.tr('Attachments'),
                [self.tr('Layer'), self.tr('Field'), self.tr('Domain')],
                [list(row) for row in result.attached])
        for name, why in result.skipped:
            report.warn('{}: {}'.format(name, why))
        for message in result.errors:
            report.error(message)

        report.push_to(feedback)
        written = report.write_html(report_path)

        if not dry_run and not result.errors:
            touched = D.refresh_project_layers(target_path)
            if touched:
                feedback.pushInfo(self.tr(
                    'Refreshed {n} layer(s) already open in the project.'
                ).format(n=touched))

        if result.errors:
            raise QgsProcessingException('; '.join(result.errors))

        return {self.OUTPUT_HTML: written}


class ValidateAgainstDomainsAlgorithm(QgsProcessingAlgorithm):
    """Report every attribute value its field's domain rejects."""

    INPUT = 'INPUT'
    FAIL_ON_NULL = 'FAIL_ON_NULL'
    OUTPUT = 'OUTPUT'
    OUTPUT_POINTS = 'OUTPUT_POINTS'
    OUTPUT_HTML = 'OUTPUT_HTML'
    VIOLATION_COUNT = 'VIOLATION_COUNT'

    def tr(self, string):
        return QCoreApplication.translate('KgaDomainApply', string)

    def createInstance(self):
        return ValidateAgainstDomainsAlgorithm()

    def name(self):
        return 'validate_against_domains'

    def displayName(self):
        return self.tr('Validate Against Domains')

    def group(self):
        return self.tr('KGA Schema Tools')

    def groupId(self):
        return 'kgaschematools'

    def helpUrl(self):
        return 'https://khmergrs.com/docs/qgis/validate_against_domains'

    def shortHelpString(self):
        return self.tr(
            'Check every feature against the field domains its layer declares, '
            'and list the values that break them.\n\n'
            'A domain constrains new edits, but data that predates the domain, '
            'or that arrived through an import, is never re-checked. This finds '
            'that data.\n\n'
            'Produces a violations table with one row per bad value — layer, '
            'feature, field, value, domain and reason — and, optionally, a '
            'point layer of the offending features so they can be zoomed to on '
            'the map.\n\n'
            'Only layers stored in a GeoPackage or File Geodatabase can carry '
            'domains; anything else is skipped with a note.'
        )

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterMultipleLayers(
            self.INPUT, self.tr('Input layer(s)'),
            source_type('VectorAnyGeometry')))
        self.addParameter(QgsProcessingParameterBoolean(
            self.FAIL_ON_NULL, self.tr('Report empty values as violations'),
            defaultValue=False))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, self.tr('Violations'), source_type('Vector')))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT_POINTS, self.tr('Violation locations'),
            source_type('VectorPoint'), optional=True,
            createByDefault=False))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.OUTPUT_HTML, self.tr('Change report'),
            self.tr('HTML files (*.html)'), optional=True,
            createByDefault=False))

        self.addOutput(QgsProcessingOutputNumber(
            self.VIOLATION_COUNT, self.tr('Number of violations')))

    def processAlgorithm(self, parameters, context, feedback):
        layers = self.parameterAsLayerList(parameters, self.INPUT, context)
        fail_on_null = self.parameterAsBool(parameters, self.FAIL_ON_NULL,
                                            context)
        report_path = self.parameterAsFileOutput(
            parameters, self.OUTPUT_HTML, context)

        report = ChangeReport(self.tr('Validate Against Domains'), dry_run=False)
        fields = violation_fields()

        sink, sink_id = self.parameterAsSink(
            parameters, self.OUTPUT, context, fields)
        if sink is None:
            raise QgsProcessingException(
                self.invalidSinkError(parameters, self.OUTPUT))

        point_sink, point_id = self.parameterAsSink(
            parameters, self.OUTPUT_POINTS, context, fields,
            point_wkb_type(), layers[0].crs() if layers else None)

        total = 0
        checked = 0
        step = max(1, len(layers))

        for index, layer in enumerate(layers):
            if feedback.isCanceled():
                break
            feedback.setProgress(index * 100.0 / step)

            if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
                report.warn(self.tr('Skipped "{name}": not a valid vector '
                                    'layer.').format(name=layer.name()))
                continue

            specs = self._specs_for(layer, report)
            if not specs:
                continue
            checked += 1
            feedback.pushInfo(self.tr('Checking {name} ({n} field(s) with a '
                                      'domain)…').format(name=layer.name(),
                                                         n=len(specs)))

            try:
                violations = D.validate_layer(layer, specs, fail_on_null)
            except Exception as exc:        # pragma: no cover - defensive
                report.error('{}: {}'.format(layer.name(), exc))
                QgsMessageLog.logMessage(str(exc), LOG_TAG,
                                         Qgis.MessageLevel.Warning)
                continue

            geometries = {}
            if point_sink is not None and violations:
                wanted = {v.feature_id for v in violations}
                for feature in layer.getFeatures():
                    if feature.id() in wanted and feature.hasGeometry():
                        geometries[feature.id()] = feature.geometry()

            for violation in violations:
                if feedback.isCanceled():
                    break
                report.add_violation(violation)
                out = QgsFeature(fields)
                out.setAttributes(violation.as_row())
                sink.addFeature(out, QgsFeatureSink.FastInsert)
                if point_sink is not None:
                    geometry = geometries.get(violation.feature_id)
                    if geometry is not None and not geometry.isEmpty():
                        point = QgsFeature(fields)
                        point.setAttributes(violation.as_row())
                        point.setGeometry(geometry.centroid())
                        point_sink.addFeature(point, QgsFeatureSink.FastInsert)
                total += 1

            report.count(self.tr('violations in {name}').format(
                name=layer.name()), len(violations))

        report.set_count(self.tr('layers checked'), checked)
        report.set_count(self.tr('violations'), total)
        if not checked:
            report.note(self.tr(
                'None of the input layers has a field domain attached. Define '
                'one with the Domain & Schema Manager first.'))
        report.push_to(feedback)
        written = report.write_html(report_path)

        return {self.OUTPUT: sink_id, self.OUTPUT_POINTS: point_id,
                self.OUTPUT_HTML: written, self.VIOLATION_COUNT: total}

    def _specs_for(self, layer, report):
        """`{field name: DomainSpec}` for one layer, or `{}` when it has none."""
        source = layer.source().split('|')[0]
        if not source or not source.lower().endswith(('.gpkg', '.gdb')):
            return {}
        try:
            conn = D.connection_for(source)
            assignments = D.field_domain_map(source)
            specs = {s.name: s for s in D.list_domains(conn)}
        except D.DomainError as exc:
            report.warn('{}: {}'.format(layer.name(), exc))
            return {}

        table = _layer_name(layer.source()) or layer.name()
        result = {}
        for (a_table, field), domain in assignments.items():
            if a_table == table and domain in specs:
                result[field] = specs[domain]
        return result


def _layer_name(uri):
    for part in uri.split('|')[1:]:
        if part.lower().startswith('layername='):
            return part.split('=', 1)[1]
    return None

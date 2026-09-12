# -*- coding: utf-8 -*-

import os

from qgis.core import (
    Qgis,
    QgsFeatureRequest,
    QgsMessageLog,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingOutputNumber,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterEnum,
    QgsProcessingParameterField,
    QgsProcessingParameterFile,
    QgsProcessingParameterFileDestination,
    QgsProcessingParameterVectorLayer,
)
from qgis.PyQt.QtCore import QCoreApplication

from ..branding import LOG_TAG, docs_url
from ..core import schema, xlsx_io
from ..core.changelog import ChangeReport, FieldChange
from ..core.compat import no_threading, parameter_as_field_list

# ON_MISSING
MISSING_IGNORE, MISSING_REPORT, MISSING_FAIL = range(3)


class AttributesToXlsxAlgorithm(QgsProcessingAlgorithm):
    """Export attributes to a workbook."""

    INPUT = 'INPUT'
    FIELDS = 'FIELDS'
    KEY_FIELD = 'KEY_FIELD'
    SELECTED_ONLY = 'SELECTED_ONLY'
    INCLUDE_DOMAINS = 'INCLUDE_DOMAINS'
    OPEN_AFTER = 'OPEN_AFTER'
    OUTPUT = 'OUTPUT'
    ROW_COUNT = 'ROW_COUNT'

    def tr(self, string):
        return QCoreApplication.translate('KgaAttributesXlsx', string)

    def createInstance(self):
        return AttributesToXlsxAlgorithm()

    def name(self):
        return 'attributes_to_xlsx'

    def displayName(self):
        return self.tr('Export Attributes to Excel')

    def group(self):
        return self.tr('KGA Data Conversion')

    def groupId(self):
        return 'kgadataconversion'

    def helpUrl(self):
        return docs_url('attributes_to_xlsx')

    def shortHelpString(self):
        return self.tr(
            'Write a layer\'s attributes to an Excel workbook a colleague can '
            'edit without QGIS, in a shape <b>Import Attributes from Excel</b> '
            'can bring back safely.\n\n'
            'The workbook holds a <b>data</b> sheet — the key column first, '
            'then the fields you chose — and a hidden <b>_kga_meta</b> sheet '
            'recording which layer it came from, so the import can tell whether '
            'a sheet belongs to the layer it is being applied to.\n\n'
            'Where a field has a value map — from a field domain, or set by '
            'hand — that column becomes a <b>dropdown</b> in Excel, so the data '
            'comes back already valid. The key column is shaded and locked, '
            'because an edited key is a row that matches nothing on the way '
            'back.\n\n'
            'Those touches need the <i>openpyxl</i> library. Without it the '
            'workbook is written plain and the round trip still works; the log '
            'says which happened.\n\n'
            'The workbook <b>opens straight away</b> when it is written, and is '
            'remembered: when you are done editing, <b>Import Attributes from '
            'Excel</b> already has this file and this layer filled in, so there '
            'is nothing to browse for.'
        )

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.INPUT, self.tr('Input layer')))
        self.addParameter(QgsProcessingParameterField(
            self.KEY_FIELD, self.tr('Key field (identifies each row)'),
            parentLayerParameterName=self.INPUT, optional=True))
        self.addParameter(QgsProcessingParameterField(
            self.FIELDS, self.tr('Fields to export (all if left empty)'),
            parentLayerParameterName=self.INPUT, allowMultiple=True,
            optional=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.SELECTED_ONLY, self.tr('Selected features only'),
            defaultValue=False))
        self.addParameter(QgsProcessingParameterBoolean(
            self.INCLUDE_DOMAINS,
            self.tr('Turn value maps into Excel dropdowns'),
            defaultValue=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.OPEN_AFTER, self.tr('Open the workbook when it is written'),
            defaultValue=True))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.OUTPUT, self.tr('Workbook'),
            self.tr('Excel workbook (*.xlsx)')))

        self.addOutput(QgsProcessingOutputNumber(
            self.ROW_COUNT, self.tr('Rows written')))

    def processAlgorithm(self, parameters, context, feedback):
        layer = self.parameterAsVectorLayer(parameters, self.INPUT, context)
        if layer is None:
            raise QgsProcessingException(
                self.invalidSourceError(parameters, self.INPUT))

        key_field = self.parameterAsString(parameters, self.KEY_FIELD, context)
        chosen = parameter_as_field_list(self, parameters, self.FIELDS, context)
        selected_only = self.parameterAsBool(parameters, self.SELECTED_ONLY,
                                             context)
        include_domains = self.parameterAsBool(parameters,
                                               self.INCLUDE_DOMAINS, context)
        path = self.parameterAsFileOutput(parameters, self.OUTPUT, context)

        key_field = key_field or _default_key_field(layer)
        if not key_field:
            raise QgsProcessingException(self.tr(
                'This layer has no primary key to use as the key column. '
                'Choose a field that identifies each feature.'))

        field_names = list(chosen) if chosen else [f.name() for f in layer.fields()]
        missing = [n for n in field_names if layer.fields().indexOf(n) < 0]
        if missing:
            raise QgsProcessingException(self.tr(
                'The layer has no field(s) {names}.').format(
                    names=', '.join(missing)))

        value_maps = {}
        if include_domains:
            value_maps = xlsx_io.value_maps_for(layer, field_names)
            if value_maps:
                feedback.pushInfo(self.tr(
                    'Dropdowns for: {names}').format(
                        names=', '.join(sorted(value_maps))))

        if selected_only and not layer.selectedFeatureCount():
            raise QgsProcessingException(self.tr('Nothing is selected.'))

        features = (layer.getSelectedFeatures() if selected_only
                    else layer.getFeatures())

        try:
            rows = xlsx_io.write_workbook(
                path, layer, field_names, key_field,
                _with_progress(features, feedback,
                               layer.selectedFeatureCount() if selected_only
                               else layer.featureCount()),
                value_maps, feedback)
        except xlsx_io.XlsxError as exc:
            raise QgsProcessingException(str(exc))

        feedback.pushInfo(self.tr('Wrote {n} row(s) to {path}.').format(
            n=rows, path=path))
        if not xlsx_io.HAS_OPENPYXL:
            feedback.pushInfo(self.tr(
                'Install openpyxl for a frozen header row, a locked key column '
                'and dropdowns.'))

        # Recorded before opening, so the import side offers this workbook back
        # even if launching the spreadsheet fails.
        xlsx_io.remember_export(path, layer, key_field, rows)
        feedback.pushInfo(self.tr(
            'Import Attributes from Excel will offer this workbook and the '
            'layer "{name}" automatically — you do not have to browse for '
            'it.').format(name=layer.name()))

        if self.parameterAsBool(parameters, self.OPEN_AFTER, context):
            opened, reason = xlsx_io.open_externally(path)
            if opened:
                feedback.pushInfo(self.tr('Opening the workbook…'))
            else:
                feedback.pushInfo(self.tr(
                    'Did not open the workbook: {reason}.').format(
                        reason=reason))

        return {self.OUTPUT: path, self.ROW_COUNT: rows}


class XlsxToAttributesAlgorithm(QgsProcessingAlgorithm):
    """Bring edited attributes back from a workbook."""

    INPUT_XLSX = 'INPUT_XLSX'
    TARGET = 'TARGET'
    KEY_FIELD = 'KEY_FIELD'
    FIELDS = 'FIELDS'
    ON_MISSING = 'ON_MISSING'
    EXCEL_DATES = 'EXCEL_DATES'
    DRY_RUN = 'DRY_RUN'
    OUTPUT_HTML = 'OUTPUT_HTML'
    CHANGED = 'CHANGED'

    MISSING_OPTIONS = ('Ignore them',
                       'Report them',
                       'Fail if there are any')

    def __init__(self):
        super().__init__()
        # What the last export recorded, filled in by initAlgorithm.
        self._remembered = {}

    def tr(self, string):
        return QCoreApplication.translate('KgaAttributesXlsx', string)

    def createInstance(self):
        return XlsxToAttributesAlgorithm()

    def name(self):
        return 'xlsx_to_attributes'

    def displayName(self):
        return self.tr('Import Attributes from Excel')

    def group(self):
        return self.tr('KGA Data Conversion')

    def groupId(self):
        return 'kgadataconversion'

    def helpUrl(self):
        return docs_url('xlsx_to_attributes')

    def shortHelpString(self):
        return self.tr(
            'Apply a workbook of edited attributes back onto the layer they '
            'came from, matching rows on the key column.\n\n'
            'The <b>workbook and the layer are already filled in</b> from your '
            'last export, so the normal round trip — export, edit in Excel, '
            'save, come back — needs no browsing. Both stay filled in after a '
            'QGIS restart, and clear themselves if the file is moved or '
            'deleted. Pick a different file whenever you need to.\n\n'
            '<b>Reports first, writes second.</b> The report gives a per-field '
            'count and a table of changed values, old next to new, so the '
            'change can be reviewed before anything is committed. It runs as a '
            'dry run by default; turn that off deliberately.\n\n'
            'Excel\'s habits are handled rather than absorbed: a text code '
            'turned into a number comes back as text, a date turned into the '
            'serial 45231 is decoded when <b>Excel date serials</b> is on, and '
            'a value that will not fit the field is reported instead of landing '
            'as a NULL.\n\n'
            'If the workbook records a different layer than the one selected, '
            'you get a warning rather than a refusal — applying a sheet to a '
            'copy is legitimate — but read the report before turning the dry '
            'run off.\n\n'
            'The target must have no unsaved edits.'
        )

    def flags(self):
        # Writes into a layer that is loaded in the project.
        return no_threading(super().flags())

    def initAlgorithm(self, config=None):
        # Prefilled from the last export, so the usual case — export, edit in
        # Excel, come back — needs no file dialog at all. initAlgorithm runs
        # when the dialog is built, so this is always the most recent export.
        # `last_export` returns nothing once the file has been moved or
        # deleted, so a stale path can never be offered.
        remembered = xlsx_io.last_export()
        self._remembered = remembered

        self.addParameter(QgsProcessingParameterFile(
            self.INPUT_XLSX, self.tr('Workbook'), extension='xlsx',
            defaultValue=remembered.get('path') or None))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.TARGET, self.tr('Target layer'),
            defaultValue=remembered.get('layer_id') or None))
        self.addParameter(QgsProcessingParameterField(
            self.KEY_FIELD,
            self.tr('Key field (taken from the workbook if left empty)'),
            parentLayerParameterName=self.TARGET, optional=True))
        self.addParameter(QgsProcessingParameterField(
            self.FIELDS,
            self.tr('Fields to apply (all the workbook holds if left empty)'),
            parentLayerParameterName=self.TARGET, allowMultiple=True,
            optional=True))
        self.addParameter(QgsProcessingParameterEnum(
            self.ON_MISSING,
            self.tr('Rows whose key is not in the layer'),
            options=[self.tr(o) for o in self.MISSING_OPTIONS],
            defaultValue=MISSING_REPORT))
        self.addParameter(QgsProcessingParameterBoolean(
            self.EXCEL_DATES,
            self.tr('Read bare numbers in date fields as Excel day serials'),
            defaultValue=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.DRY_RUN, self.tr('Dry run (report only, write nothing)'),
            defaultValue=True))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.OUTPUT_HTML, self.tr('Change report'),
            self.tr('HTML files (*.html)'), optional=True,
            createByDefault=False))

        self.addOutput(QgsProcessingOutputNumber(
            self.CHANGED, self.tr('Values changed')))

    def processAlgorithm(self, parameters, context, feedback):
        path = self.parameterAsFile(parameters, self.INPUT_XLSX, context)
        target = self.parameterAsVectorLayer(parameters, self.TARGET, context)
        if target is None:
            raise QgsProcessingException(
                self.invalidSourceError(parameters, self.TARGET))

        if target.isEditable() and target.isModified():
            raise QgsProcessingException(self.tr(
                'The target layer has unsaved edits. Save or discard them '
                'first — otherwise this write and your edit buffer would '
                'fight over the same features.'))

        on_missing = self.parameterAsEnum(parameters, self.ON_MISSING, context)
        excel_dates = self.parameterAsBool(parameters, self.EXCEL_DATES, context)
        dry_run = self.parameterAsBool(parameters, self.DRY_RUN, context)
        chosen = parameter_as_field_list(self, parameters, self.FIELDS, context)
        report_path = self.parameterAsFileOutput(parameters, self.OUTPUT_HTML,
                                                 context)

        remembered = self._remembered or {}
        if remembered.get('path') and os.path.normcase(os.path.abspath(path)) \
                == os.path.normcase(remembered['path']):
            feedback.pushInfo(self.tr(
                'This is the workbook exported from "{name}" on {when}.'
            ).format(name=remembered.get('layer_name', '?'),
                     when=remembered.get('exported', '?')))

        try:
            rows, metadata = xlsx_io.read_workbook(path)
        except xlsx_io.XlsxError as exc:
            raise QgsProcessingException(str(exc))
        if not rows:
            raise QgsProcessingException(self.tr(
                '{path} has no data rows.').format(path=path))

        report = ChangeReport(
            self.tr('Import Attributes from Excel'),
            self.tr('{book} → {layer}').format(book=os.path.basename(path),
                                               layer=target.name()),
            dry_run=dry_run)

        for problem in xlsx_io.describe_mismatch(metadata, target):
            report.warn(problem)

        key_field = (self.parameterAsString(parameters, self.KEY_FIELD, context)
                     or metadata.get('key_field')
                     or _default_key_field(target))
        if not key_field:
            raise QgsProcessingException(self.tr(
                'No key field. The workbook does not name one and the layer '
                'has no primary key, so rows cannot be matched.'))
        if target.fields().indexOf(key_field) < 0:
            raise QgsProcessingException(self.tr(
                'The target layer has no field "{name}" to match on.').format(
                    name=key_field))
        if key_field not in rows[0]:
            raise QgsProcessingException(self.tr(
                'The workbook has no "{name}" column, so its rows cannot be '
                'matched to the layer.').format(name=key_field))

        fields = target.fields()
        columns = [name for name in rows[0]
                   if name != key_field and fields.indexOf(name) >= 0]
        if chosen:
            columns = [name for name in columns if name in chosen]
        ignored = [name for name in rows[0]
                   if name != key_field and fields.indexOf(name) < 0]
        if ignored:
            report.warn(self.tr(
                'The layer has no field(s) {names}; those columns are '
                'ignored.').format(names=', '.join(sorted(ignored))))
        if not columns:
            raise QgsProcessingException(self.tr(
                'None of the workbook\'s columns matches a field of the '
                'target layer.'))

        feedback.pushInfo(self.tr('Comparing {n} column(s): {names}').format(
            n=len(columns), names=', '.join(columns)))

        by_key, duplicates = self._index_target(target, key_field)
        for key in duplicates:
            report.warn(self.tr(
                'The key "{key}" appears more than once in the layer; those '
                'features are skipped.').format(key=key))

        changes = self._diff(rows, columns, key_field, target, by_key,
                             duplicates, excel_dates, report, feedback,
                             on_missing)
        if changes is None:                 # cancelled
            return self._finish(report, report_path, feedback, 0)

        report.set_count(self.tr('rows read'), len(rows))
        report.set_count(self.tr('values changed'), len(changes))
        touched = len({c.feature_id for c in changes})
        report.set_count(self.tr('features affected'), touched)

        per_field = {}
        for change in changes:
            per_field[change.field] = per_field.get(change.field, 0) + 1
        if per_field:
            report.add_table(
                self.tr('Changes per field'),
                [self.tr('Field'), self.tr('Changed')],
                sorted(per_field.items()))

        if dry_run:
            report.note(self.tr(
                'Dry run. Turn off "Dry run" to write these {n} change(s).'
            ).format(n=len(changes)))
            return self._finish(report, report_path, feedback, len(changes))

        written = self._write(target, changes, report)
        return self._finish(report, report_path, feedback, written, target)

    # -------------------------------------------------------------- internals

    def _index_target(self, target, key_field):
        """`{key: fid}`, plus the keys that appear more than once.

        A duplicated key cannot be matched unambiguously, so those features are
        left alone and named in the report rather than updated arbitrarily.
        """
        index = target.fields().indexOf(key_field)
        request = QgsFeatureRequest().setSubsetOfAttributes([index])
        request.setFlags(QgsFeatureRequest.Flag.NoGeometry)

        by_key = {}
        duplicates = set()
        for feature in target.getFeatures(request):
            key = _key_of(feature.attribute(index))
            if key in by_key:
                duplicates.add(key)
            else:
                by_key[key] = feature.id()
        for key in duplicates:
            by_key.pop(key, None)
        return by_key, duplicates

    def _diff(self, rows, columns, key_field, target, by_key, duplicates,
              excel_dates, report, feedback, on_missing):
        """Every value in the workbook that differs from the layer."""
        fields = target.fields()
        indexes = {name: fields.indexOf(name) for name in columns}
        wanted_fids = set()
        keyed_rows = []
        not_found = []

        for row in rows:
            key = _key_of(row.get(key_field))
            if key is None:
                report.warn(self.tr('A row has an empty key and was skipped.'))
                continue
            if key in duplicates:
                continue
            fid = by_key.get(key)
            if fid is None:
                not_found.append(key)
                continue
            keyed_rows.append((key, fid, row))
            wanted_fids.add(fid)

        if not_found:
            if on_missing == MISSING_FAIL:
                raise QgsProcessingException(self.tr(
                    '{n} row(s) have a key that is not in the layer, and '
                    '"Fail" was chosen: {keys}').format(
                        n=len(not_found), keys=', '.join(not_found[:10])))
            if on_missing == MISSING_REPORT:
                report.add_table(
                    self.tr('Workbook rows with no matching feature'),
                    [self.tr('Key')], [[k] for k in not_found[:200]],
                    self.tr('{n} row(s) in total.').format(n=len(not_found)))
                report.set_count(self.tr('rows with no matching feature'),
                                 len(not_found))

        # One pass over the features that are actually referenced.
        current = {}
        request = QgsFeatureRequest().setFilterFids(list(wanted_fids))
        request.setFlags(QgsFeatureRequest.Flag.NoGeometry)
        for feature in target.getFeatures(request):
            current[feature.id()] = {
                name: feature.attribute(index)
                for name, index in indexes.items() if index >= 0}

        changes = []
        total = len(keyed_rows) or 1
        for position, (key, fid, row) in enumerate(keyed_rows):
            if feedback.isCanceled():
                report.warn(self.tr('Cancelled; nothing was written.'))
                return None
            feedback.setProgress(position * 90.0 / total)

            existing = current.get(fid, {})
            for name in columns:
                index = indexes[name]
                if index < 0:
                    continue
                raw = row.get(name)
                try:
                    value, note = schema.coerce(raw, fields.at(index),
                                                excel_dates=excel_dates)
                except schema.CoercionError as exc:
                    report.add_coercion(name, raw, None, str(exc))
                    continue
                if note:
                    report.add_coercion(name, raw, value, note)

                old = existing.get(name)
                if _same(old, value):
                    continue
                changes.append(FieldChange(key, fid, name, _plain(old), value))

        report.changes = changes
        return changes

    def _write(self, target, changes, report):
        started_here = not target.isEditable()
        if started_here and not target.startEditing():
            raise QgsProcessingException(self.tr(
                'Could not start editing "{name}".').format(name=target.name()))

        fields = target.fields()
        written = 0
        try:
            for change in changes:
                index = fields.indexOf(change.field)
                if index < 0:
                    continue
                if not target.changeAttributeValue(change.feature_id, index,
                                                   change.new):
                    raise QgsProcessingException(self.tr(
                        'Could not set {field} on feature {fid}.').format(
                            field=change.field, fid=change.feature_id))
                written += 1
            if started_here and not target.commitChanges():
                raise QgsProcessingException(self.tr(
                    'Commit failed: {err}').format(
                        err='; '.join(target.commitErrors())))
        except Exception:
            if started_here:
                target.rollBack()
                report.error(self.tr(
                    'The write failed and was rolled back; the layer is '
                    'unchanged.'))
            raise
        return written

    def _finish(self, report, report_path, feedback, changed, target=None):
        report.push_to(feedback)
        written = report.write_html(report_path)
        if written:
            feedback.pushInfo(self.tr('Report written to {path}').format(
                path=written))
        if target is not None:
            target.triggerRepaint()
        return {self.OUTPUT_HTML: written, self.CHANGED: changed}


# ------------------------------------------------------------------- helpers

def _with_progress(features, feedback, total):
    """Wrap a feature iterator so a long export reports progress and can be
    cancelled. Yields nothing further once the user cancels, which leaves the
    writer with a short but well-formed workbook."""
    total = total or 0
    for index, feature in enumerate(features):
        if feedback.isCanceled():
            return
        if total:
            feedback.setProgress(index * 95.0 / total)
        yield feature
    feedback.setProgress(100)


def _default_key_field(layer):
    """The layer's primary key, or its `fid`, or nothing."""
    try:
        attributes = layer.primaryKeyAttributes()
    except Exception:                       # pragma: no cover - defensive
        attributes = []
    if len(attributes) == 1:
        field = layer.fields().at(attributes[0])
        if field is not None:
            return field.name()
    for candidate in ('fid', 'id', 'ID', 'OBJECTID'):
        if layer.fields().indexOf(candidate) >= 0:
            return candidate
    return ''


def _key_of(value):
    """A hashable key. 1 and "1" are the same, because a spreadsheet round trip
    routinely turns one into the other."""
    value = _plain(value)
    if value is None:
        return None
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    text = str(value).strip()
    return text or None


def _plain(value):
    from qgis.PyQt.QtCore import QVariant
    if isinstance(value, QVariant):
        return None if value.isNull() else value.value()
    return value


def _same(old, new):
    """True when writing `new` over `old` would not change anything.

    Numbers are compared as numbers so 1 and 1.0 do not read as a change, which
    would otherwise make every numeric column look edited after a round trip.
    """
    old = _plain(old)
    if old is None and new is None:
        return True
    if old is None or new is None:
        return False
    if isinstance(old, bool) or isinstance(new, bool):
        return bool(old) == bool(new)
    if isinstance(old, (int, float)) and isinstance(new, (int, float)):
        return abs(float(old) - float(new)) < 1e-9
    return old == new

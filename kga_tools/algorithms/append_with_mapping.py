# -*- coding: utf-8 -*-

import json
import os

from qgis.core import (
    Qgis,
    QgsCoordinateTransform,
    QgsExpression,
    QgsExpressionContext,
    QgsExpressionContextUtils,
    QgsFeature,
    QgsFeatureRequest,
    QgsGeometry,
    QgsMessageLog,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterEnum,
    QgsProcessingParameterField,
    QgsProcessingParameterFieldMapping,
    QgsProcessingParameterFile,
    QgsProcessingParameterFileDestination,
    QgsProcessingParameterVectorLayer,
    QgsProject,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QCoreApplication

from ..branding import LOG_TAG, docs_url
from ..core import schema
from ..core.changelog import ChangeReport, FieldChange
from ..core.compat import no_threading

# ON_DUPLICATE
DUP_SKIP, DUP_APPEND, DUP_UPDATE = range(3)

# GEOMETRY_HANDLING
GEOM_KEEP, GEOM_REPROJECT, GEOM_MULTI, GEOM_DROP = range(4)


class AppendWithMappingAlgorithm(QgsProcessingAlgorithm):
    """Append `SOURCE` into `TARGET`, mapping fields on the way."""

    SOURCE = 'SOURCE'
    MAPPING = 'MAPPING'
    MAPPING_FILE = 'MAPPING_FILE'
    TARGET = 'TARGET'
    KEY_FIELD = 'KEY_FIELD'
    ON_DUPLICATE = 'ON_DUPLICATE'
    GEOMETRY_HANDLING = 'GEOMETRY_HANDLING'
    SELECTED_ONLY = 'SELECTED_ONLY'
    DRY_RUN = 'DRY_RUN'
    OUTPUT_HTML = 'OUTPUT_HTML'

    DUPLICATE_OPTIONS = ('Skip the incoming feature',
                         'Append it anyway',
                         'Update the existing feature')
    GEOMETRY_OPTIONS = ('Keep as it is',
                        'Reproject to the target CRS',
                        'Reproject and force multipart',
                        'Drop the geometry')

    def tr(self, string):
        return QCoreApplication.translate('KgaAppendMapping', string)

    def createInstance(self):
        return AppendWithMappingAlgorithm()

    def name(self):
        return 'append_with_mapping'

    def displayName(self):
        return self.tr('Append with Field Mapping')

    def group(self):
        return self.tr('KGA Schema Tools')

    def groupId(self):
        return 'kgaschematools'

    def helpUrl(self):
        return docs_url('append_with_mapping')

    def shortHelpString(self):
        return self.tr(
            'Append the features of one layer into another that already '
            'exists, mapping the fields explicitly on the way.\n\n'
            'Each row of the mapping table names a field of the <b>target</b> '
            'and an expression evaluated against the <b>source</b> feature, so '
            'renaming, combining and converting all happen in one pass. Target '
            'fields with no mapping row keep their own defaults.\n\n'
            '<b>Runs as a dry run by default.</b> The report tells you exactly '
            'what would be written — features read, duplicates found, every '
            'value that had to change type, and every target field left at its '
            'default. Read it, then turn the dry run off.\n\n'
            'Values that cannot fit the target field are reported rather than '
            'written as NULL: text that is not a number, a number too large '
            'for the column, text longer than the column, a date that will not '
            'parse.\n\n'
            'If the target is already in edit mode the features are added to '
            'your edit buffer and left uncommitted, so your own undo stack '
            'still owns them. Otherwise the write is committed, and rolled back '
            'whole if anything fails.'
        )

    def flags(self):
        # Writes into a layer that is loaded in the project.
        return no_threading(super().flags())

    # --------------------------------------------------------------- parameters

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.SOURCE, self.tr('Source layer')))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.TARGET, self.tr('Target layer (appended to in place)')))
        self.addParameter(QgsProcessingParameterFieldMapping(
            self.MAPPING, self.tr('Field mapping'),
            parentLayerParameterName=self.SOURCE, optional=True))
        # The mapping parameter itself only accepts a list of dicts, so a saved
        # mapping needs its own parameter rather than being pasted into that
        # one. It also makes reuse visible in the dialog, which is the point of
        # having Generate Field Mapping at all.
        self.addParameter(QgsProcessingParameterFile(
            self.MAPPING_FILE,
            self.tr('…or a saved mapping file (overrides the table above)'),
            extension='json', optional=True))
        self.addParameter(QgsProcessingParameterField(
            self.KEY_FIELD, self.tr('Key field for duplicate detection'),
            parentLayerParameterName=self.TARGET, optional=True))
        self.addParameter(QgsProcessingParameterEnum(
            self.ON_DUPLICATE, self.tr('When the key already exists'),
            options=[self.tr(o) for o in self.DUPLICATE_OPTIONS],
            defaultValue=DUP_SKIP))
        self.addParameter(QgsProcessingParameterEnum(
            self.GEOMETRY_HANDLING, self.tr('Geometry'),
            options=[self.tr(o) for o in self.GEOMETRY_OPTIONS],
            defaultValue=GEOM_REPROJECT))
        self.addParameter(QgsProcessingParameterBoolean(
            self.SELECTED_ONLY,
            self.tr('Use only the selected features of the source'),
            defaultValue=False))
        self.addParameter(QgsProcessingParameterBoolean(
            self.DRY_RUN, self.tr('Dry run (report only, write nothing)'),
            defaultValue=True))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.OUTPUT_HTML, self.tr('Change report'),
            self.tr('HTML files (*.html)'), optional=True,
            createByDefault=False))

    # ------------------------------------------------------------------- run

    def processAlgorithm(self, parameters, context, feedback):
        source = self.parameterAsVectorLayer(parameters, self.SOURCE, context)
        target = self.parameterAsVectorLayer(parameters, self.TARGET, context)
        if source is None:
            raise QgsProcessingException(
                self.invalidSourceError(parameters, self.SOURCE))
        if target is None:
            raise QgsProcessingException(
                self.invalidSourceError(parameters, self.TARGET))
        if source.id() == target.id():
            raise QgsProcessingException(self.tr(
                'The source and the target are the same layer.'))

        mapping_file = self.parameterAsFile(parameters, self.MAPPING_FILE,
                                            context)
        if mapping_file:
            mapping = _mapping_from_file(mapping_file)
            feedback.pushInfo(self.tr('Using the mapping from {path}.').format(
                path=mapping_file))
        else:
            mapping = _normalise_mapping(parameters.get(self.MAPPING))
        if not mapping:
            raise QgsProcessingException(self.tr(
                'The field mapping is empty; nothing would be written. Fill in '
                'the mapping table, or point at a mapping file written by '
                '"Generate Field Mapping".'))

        key_field = self.parameterAsString(parameters, self.KEY_FIELD, context)
        on_duplicate = self.parameterAsEnum(parameters, self.ON_DUPLICATE,
                                            context)
        geometry_mode = self.parameterAsEnum(parameters,
                                             self.GEOMETRY_HANDLING, context)
        selected_only = self.parameterAsBool(parameters, self.SELECTED_ONLY,
                                             context)
        dry_run = self.parameterAsBool(parameters, self.DRY_RUN, context)
        report_path = self.parameterAsFileOutput(parameters, self.OUTPUT_HTML,
                                                 context)

        target_fields = target.fields()

        # -- 1. the mapping has to fit the target before anything is read -----
        missing = schema.validate_mapping(mapping, target_fields)
        if missing:
            raise QgsProcessingException(self.tr(
                'The mapping writes to field(s) the target layer does not '
                'have: {names}. Fix the mapping — appending would silently '
                'drop them.').format(names=', '.join(missing)))

        if not target.isEditable() and not (
                target.dataProvider().capabilities()
                & target.dataProvider().AddFeatures):
            raise QgsProcessingException(self.tr(
                'The target layer "{name}" does not accept new features.'
            ).format(name=target.name()))

        report = ChangeReport(
            self.tr('Append with Field Mapping'),
            self.tr('{source} → {target}').format(source=source.name(),
                                                  target=target.name()),
            dry_run=dry_run)

        unmapped = schema.unmapped_target_names(mapping, target_fields)
        if unmapped:
            report.add_table(
                self.tr('Target fields left at their default'),
                [self.tr('Field')], [[name] for name in unmapped],
                self.tr('No mapping row writes to these.'))

        # -- 2. prepare ------------------------------------------------------
        transform = self._transform(source, target, geometry_mode, report,
                                    context)
        existing_keys = self._existing_keys(target, key_field, report)
        expressions = self._compile(mapping, source, report)
        exp_context = QgsExpressionContext()
        exp_context.appendScopes(
            QgsExpressionContextUtils.globalProjectLayerScopes(source))

        features = (source.getSelectedFeatures() if selected_only
                    else source.getFeatures())
        total = (source.selectedFeatureCount() if selected_only
                 else source.featureCount()) or 0
        if selected_only and not total:
            raise QgsProcessingException(self.tr(
                'Nothing is selected in the source layer.'))

        new_features = []
        updates = {}                       # target fid -> {index: value}
        read = skipped = 0

        for index, feature in enumerate(features):
            if feedback.isCanceled():
                report.warn(self.tr('Cancelled after {n} feature(s); nothing '
                                    'was written.').format(n=read))
                return self._finish(report, report_path, feedback, None)
            if total:
                feedback.setProgress(index * 95.0 / total)
            read += 1

            exp_context.setFeature(feature)
            values, notes = self._map_feature(feature, expressions,
                                              target_fields, exp_context,
                                              report)

            key_value = None
            duplicate = False
            if key_field:
                key_index = target_fields.indexOf(key_field)
                if key_index >= 0:
                    key_value = values.get(key_index)
                    duplicate = _key_of(key_value) in existing_keys

            if duplicate:
                if on_duplicate == DUP_SKIP:
                    skipped += 1
                    continue
                if on_duplicate == DUP_UPDATE:
                    fid = existing_keys.get(_key_of(key_value))
                    if fid is not None:
                        updates[fid] = values
                        report.add_change(FieldChange(
                            _text(key_value), fid, key_field, '', '',
                            self.tr('existing feature updated')))
                        continue

            out = QgsFeature(target_fields)
            for field_index, value in values.items():
                out.setAttribute(field_index, value)
            geometry = self._geometry(feature, transform, geometry_mode,
                                      target, report)
            if geometry is not None:
                out.setGeometry(geometry)
            new_features.append(out)

            if key_value is not None:
                # A source that repeats a key must not sneak past the check
                # just because the target did not have it to begin with.
                existing_keys.setdefault(_key_of(key_value), None)

            for note in notes:
                report.count(note)

        report.set_count(self.tr('features read'), read)
        report.set_count(self.tr('features to append'), len(new_features))
        report.set_count(self.tr('duplicates skipped'), skipped)
        if updates:
            report.set_count(self.tr('existing features to update'), len(updates))

        # -- 3. write --------------------------------------------------------
        if dry_run:
            report.note(self.tr(
                'Dry run. Turn off "Dry run" to write these {n} feature(s).'
            ).format(n=len(new_features) + len(updates)))
            return self._finish(report, report_path, feedback, target)

        written = self._write(target, new_features, updates, report, feedback)
        report.set_count(self.tr('features written'), written)
        feedback.setProgress(100)
        return self._finish(report, report_path, feedback, target)

    # -------------------------------------------------------------- internals

    def _compile(self, mapping, source, report):
        """`[(target_field_index_placeholder, name, QgsExpression), ...]`.

        Compiled once rather than per feature; a broken expression is reported
        here and the row dropped rather than throwing on feature one.
        """
        compiled = []
        for entry in mapping:
            name = entry.get('name')
            text = entry.get('expression')
            if not text:
                # An empty expression means "leave the target field alone",
                # which is how the native mapping widget spells a skipped row.
                continue
            expression = QgsExpression(text)
            if expression.hasParserError():
                report.error(self.tr(
                    'The expression for "{field}" will not parse: {err}'
                ).format(field=name, err=expression.parserErrorString()))
                continue
            expression.prepare(QgsExpressionContext(
                QgsExpressionContextUtils.globalProjectLayerScopes(source)))
            compiled.append((name, expression))
        if report.errors:
            raise QgsProcessingException('; '.join(report.errors))
        return compiled

    def _map_feature(self, feature, expressions, target_fields, exp_context,
                     report):
        """One source feature -> `{target field index: value}`."""
        values = {}
        notes = []
        for name, expression in expressions:
            index = target_fields.indexOf(name)
            if index < 0:                   # already validated; belt and braces
                continue
            raw = expression.evaluate(exp_context)
            if expression.hasEvalError():
                report.add_coercion(name, '', None,
                                    expression.evalErrorString())
                values[index] = None
                continue
            try:
                value, note = schema.coerce(raw, target_fields.at(index))
            except schema.CoercionError as exc:
                report.add_coercion(name, raw, None, str(exc))
                values[index] = None
                continue
            if note:
                report.add_coercion(name, raw, value, note)
                notes.append(self.tr('values coerced'))
            values[index] = value
        return values, notes

    def _existing_keys(self, target, key_field, report):
        """`{key: fid}` for the target, so duplicates can be found in one scan."""
        keys = {}
        if not key_field:
            return keys
        index = target.fields().indexOf(key_field)
        if index < 0:
            report.warn(self.tr(
                'The target has no field "{name}"; duplicate detection is '
                'off.').format(name=key_field))
            return keys
        request = QgsFeatureRequest().setSubsetOfAttributes([index])
        request.setFlags(QgsFeatureRequest.Flag.NoGeometry)
        for feature in target.getFeatures(request):
            keys[_key_of(feature.attribute(index))] = feature.id()
        return keys

    def _transform(self, source, target, mode, report, context):
        if mode in (GEOM_KEEP, GEOM_DROP):
            return None
        if source.crs() == target.crs():
            return None
        try:
            transform = QgsCoordinateTransform(
                source.crs(), target.crs(),
                context.transformContext() if context
                else QgsProject.instance())
        except Exception as exc:            # pragma: no cover - defensive
            report.warn(self.tr('Could not build a transform: {err}').format(
                err=exc))
            return None
        report.note(self.tr('Reprojecting from {a} to {b}.').format(
            a=source.crs().authid() or self.tr('unknown'),
            b=target.crs().authid() or self.tr('unknown')))
        return transform

    def _geometry(self, feature, transform, mode, target, report):
        if mode == GEOM_DROP or not feature.hasGeometry():
            return None
        geometry = QgsGeometry(feature.geometry())

        if transform is not None:
            try:
                geometry.transform(transform)
            except Exception as exc:
                report.warn(self.tr(
                    'Feature {fid} could not be reprojected: {err}').format(
                        fid=feature.id(), err=exc))
                return None

        target_is_multi = QgsWkbTypes.isMultiType(target.wkbType())
        if mode == GEOM_MULTI or target_is_multi:
            if target_is_multi and not geometry.isMultipart():
                # A single-part geometry written to a multipart layer is a
                # frequent silent failure: the provider rejects it and the
                # feature lands with no geometry at all.
                if geometry.convertToMultiType():
                    report.count(self.tr('geometries promoted to multipart'))
        elif not target_is_multi and geometry.isMultipart():
            parts = geometry.asGeometryCollection()
            if len(parts) == 1:
                geometry = parts[0]
                report.count(self.tr('single-part geometries unwrapped'))
            else:
                report.warn(self.tr(
                    'Feature {fid} has {n} parts but the target is '
                    'single-part; only the first part was kept.').format(
                        fid=feature.id(), n=len(parts)))
                geometry = parts[0] if parts else geometry
        return geometry

    def _write(self, target, new_features, updates, report, feedback):
        """Commit, or roll the whole thing back.

        If the user already had the layer in edit mode when the algorithm
        started, their edit session owns the buffer: the features go in but
        nothing is committed, so their undo stack still works and they decide
        when to save.
        """
        user_owns_the_buffer = target.isEditable()
        if not user_owns_the_buffer and not target.startEditing():
            raise QgsProcessingException(self.tr(
                'Could not start editing "{name}".').format(name=target.name()))

        written = 0
        try:
            if new_features:
                if not target.addFeatures(new_features):
                    raise QgsProcessingException(self.tr(
                        'The target layer rejected the new features: {err}'
                    ).format(err=target.commitErrors() or
                             self.tr('no reason given')))
                written += len(new_features)

            for fid, values in updates.items():
                for index, value in values.items():
                    if not target.changeAttributeValue(fid, index, value):
                        raise QgsProcessingException(self.tr(
                            'Could not update feature {fid}.').format(fid=fid))
                written += 1

            if user_owns_the_buffer:
                report.note(self.tr(
                    'The target was already in edit mode, so the features were '
                    'added to your edit buffer and not committed. Save the '
                    'layer when you are ready.'))
            elif not target.commitChanges():
                raise QgsProcessingException(self.tr(
                    'Commit failed: {err}').format(
                        err='; '.join(target.commitErrors())))
        except Exception:
            if not user_owns_the_buffer:
                target.rollBack()
                report.error(self.tr(
                    'The write failed and was rolled back; the target is '
                    'unchanged.'))
            else:
                report.error(self.tr(
                    'The write failed. Your edit buffer may hold partial '
                    'changes — undo them before saving.'))
            report.write_html(_fallback_report_path(report))
            raise

        return written

    def _finish(self, report, report_path, feedback, target):
        report.push_to(feedback)
        written = report.write_html(report_path)
        if written:
            feedback.pushInfo(self.tr('Report written to {path}').format(
                path=written))
        if target is not None:
            target.triggerRepaint()
        return {self.OUTPUT_HTML: written}


class GenerateFieldMappingAlgorithm(QgsProcessingAlgorithm):
    """Work out a mapping between two schemas and save it as JSON.

    Separate from the append tool on purpose: the same two schemas recur
    constantly in a consulting workflow, and a mapping you can save, review in
    a diff and re-use is worth more than the mapping widget itself.
    """

    SOURCE = 'SOURCE'
    TARGET = 'TARGET'
    STRATEGY = 'STRATEGY'
    OUTPUT_JSON = 'OUTPUT_JSON'
    OUTPUT_HTML = 'OUTPUT_HTML'
    MATCHED = 'MATCHED'

    STRATEGIES = ('Exact name',
                  'Case-insensitive name',
                  'Fuzzy (ignore case, spaces and underscores)')
    STRATEGY_KEYS = (schema.STRATEGY_EXACT, schema.STRATEGY_CASE_INSENSITIVE,
                     schema.STRATEGY_FUZZY)

    def tr(self, string):
        return QCoreApplication.translate('KgaAppendMapping', string)

    def createInstance(self):
        return GenerateFieldMappingAlgorithm()

    def name(self):
        return 'generate_field_mapping'

    def displayName(self):
        return self.tr('Generate Field Mapping')

    def group(self):
        return self.tr('KGA Schema Tools')

    def groupId(self):
        return 'kgaschematools'

    def helpUrl(self):
        return docs_url('generate_field_mapping')

    def shortHelpString(self):
        return self.tr(
            'Work out which source field belongs in which target field, and '
            'write the result as a JSON mapping file.\n\n'
            'The file is in the format the <b>Append with Field Mapping</b> '
            'tool takes, so a mapping between two schemas can be saved once, '
            'kept in version control, reviewed in a diff and reused — which is '
            'the part that actually saves time when the same two schemas come '
            'round every month.\n\n'
            '<b>Fuzzy</b> matching lowercases names and ignores spaces and '
            'underscores, so <i>Parcel_ID</i> finds <i>parcelid</i>. Each '
            'target field is claimed at most once, so two source fields can '
            'never collapse onto one column.\n\n'
            'Fields left unmatched on either side are listed in the report; '
            'those are the ones to fix by hand.'
        )

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.SOURCE, self.tr('Source layer')))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.TARGET, self.tr('Target layer')))
        self.addParameter(QgsProcessingParameterEnum(
            self.STRATEGY, self.tr('Match field names by'),
            options=[self.tr(s) for s in self.STRATEGIES], defaultValue=2))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.OUTPUT_JSON, self.tr('Field mapping'),
            self.tr('JSON files (*.json)')))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.OUTPUT_HTML, self.tr('Change report'),
            self.tr('HTML files (*.html)'), optional=True,
            createByDefault=False))

        from qgis.core import QgsProcessingOutputNumber
        self.addOutput(QgsProcessingOutputNumber(
            self.MATCHED, self.tr('Number of matched fields')))

    def processAlgorithm(self, parameters, context, feedback):
        source = self.parameterAsVectorLayer(parameters, self.SOURCE, context)
        target = self.parameterAsVectorLayer(parameters, self.TARGET, context)
        if source is None or target is None:
            raise QgsProcessingException(self.tr(
                'Both a source and a target layer are needed.'))

        strategy = self.STRATEGY_KEYS[
            self.parameterAsEnum(parameters, self.STRATEGY, context)]
        json_path = self.parameterAsFileOutput(parameters, self.OUTPUT_JSON,
                                               context)
        report_path = self.parameterAsFileOutput(parameters, self.OUTPUT_HTML,
                                                 context)

        pairs, unmatched_source, unmatched_target = schema.match_fields(
            source.fields(), target.fields(), strategy)
        mapping = schema.mapping_from_pairs(pairs, target.fields())

        report = ChangeReport(
            self.tr('Generate Field Mapping'),
            self.tr('{source} → {target}').format(source=source.name(),
                                                  target=target.name()),
            dry_run=False)
        report.set_count(self.tr('fields matched'), len(pairs))
        report.set_count(self.tr('source fields unmatched'),
                         len(unmatched_source))
        report.set_count(self.tr('target fields unmatched'),
                         len(unmatched_target))

        if pairs:
            rows = []
            for source_name, target_name in pairs:
                source_field = source.fields().field(source_name)
                target_field = target.fields().field(target_name)
                rows.append([
                    source_name, _describe(source_field),
                    target_name, _describe(target_field),
                    '' if _compatible(source_field, target_field)
                    else self.tr('type differs — values will be converted')])
            report.add_table(
                self.tr('Matches'),
                [self.tr('Source'), self.tr('Type'), self.tr('Target'),
                 self.tr('Type'), self.tr('Note')], rows)

        if unmatched_source:
            report.add_table(
                self.tr('Source fields with no target'),
                [self.tr('Field')], [[n] for n in unmatched_source],
                self.tr('These will not be written. Add a row by hand if they '
                        'belong somewhere.'))
        if unmatched_target:
            report.add_table(
                self.tr('Target fields with no source'),
                [self.tr('Field')], [[n] for n in unmatched_target],
                self.tr('These keep their own default values.'))

        payload = {
            'format': 1,
            'generator': 'KGA Toolbox',
            'source': source.name(),
            'target': target.name(),
            'strategy': strategy,
            'mapping': mapping,
            'unmatched_source': unmatched_source,
            'unmatched_target': unmatched_target,
        }
        _write_json(json_path, payload)
        feedback.pushInfo(self.tr('Mapping written to {path}').format(
            path=json_path))
        report.push_to(feedback)

        return {self.OUTPUT_JSON: json_path,
                self.OUTPUT_HTML: report.write_html(report_path),
                self.MATCHED: len(pairs)}


# ------------------------------------------------------------------- helpers

def _normalise_mapping(value):
    """The mapping parameter's value as a plain list of dicts."""
    if isinstance(value, (list, tuple)):
        return [entry for entry in value
                if isinstance(entry, dict) and entry.get('name')]
    return []


def _mapping_from_file(path):
    """Read a mapping written by `generate_field_mapping`.

    Accepts either the whole file that tool writes (an object with a `mapping`
    key) or a bare list, so a hand-edited fragment works too.
    """
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise QgsProcessingException(
            'Could not read the mapping file {}: {}'.format(path, exc))
    if isinstance(data, dict):
        data = data.get('mapping', [])
    if not isinstance(data, list):
        raise QgsProcessingException(
            '{} does not contain a field mapping.'.format(path))
    return _normalise_mapping(data)


def _key_of(value):
    """A hashable, type-insensitive key. 1 and "1" are the same key, because a
    spreadsheet round trip routinely turns one into the other."""
    if value is None:
        return None
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value).strip()


def _text(value):
    return '' if value is None else str(value)


def _describe(field):
    from ..core.compat import type_name
    if field is None:
        return ''
    length = field.length()
    return '{}{}'.format(type_name(field.type()),
                         '({})'.format(length) if length else '')


def _compatible(source_field, target_field):
    from ..core.compat import NUMERIC_TYPES
    a, b = int(source_field.type()), int(target_field.type())
    if a == b:
        return True
    return a in NUMERIC_TYPES and b in NUMERIC_TYPES


def _write_json(path, payload):
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)


def _fallback_report_path(report):
    """Somewhere to put the report when a write fails and the user still needs
    to know how far it got."""
    import tempfile
    return os.path.join(tempfile.gettempdir(), 'kga_append_failure.html')

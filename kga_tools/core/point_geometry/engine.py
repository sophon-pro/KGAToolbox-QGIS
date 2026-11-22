"""Conversion engine: read points once, group and order them in memory, build
and validate geometries, and return ready-to-write features.

The engine never touches the GUI or the QgsProject, so it is safe to run inside
a QgsTask worker thread when given a QgsVectorLayerFeatureSource.
"""

import math
from operator import itemgetter

from qgis.core import (
    QgsExpression,
    QgsExpressionContext,
    QgsFeature,
    QgsFeatureRequest,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsWkbTypes,
)

from . import builder, sequencer
from .compat import (
    WKB_LINESTRING,
    WKB_MULTILINESTRING,
    WKB_MULTIPOLYGON,
    WKB_POLYGON,
    make_field,
    output_wkb_type,
)
from .config import (
    LINE_SEGMENTS,
    MODE_LINE,
    MODE_POLYGON,
    NULL_GROUP_SKIP,
    ORDER_EXPRESSION,
    ORDER_FID,
    ORDER_FIELD,
)

# QVariant / QMetaType ids that cannot be copied to GPKG/SHP columns
# (Map, List, StringList, ByteArray). Values are identical in Qt5 and Qt6.
_UNSUPPORTED_TYPE_IDS = {8, 9, 11, 12}
_MAX_LISTED_FIDS = 10


class EngineCancelled(Exception):
    pass


class EngineInput:
    """Everything the engine needs, captured on the main thread."""

    def __init__(
        self,
        source,
        fields,
        wkb_type,
        layer_name,
        feature_count,
        fid_filter=None,
        expression_context=None,
        excluded_field_names=(),
    ):
        self.source = source
        self.fields = QgsFields(fields)
        self.wkb_type = wkb_type
        self.layer_name = layer_name
        self.feature_count = feature_count
        self.fid_filter = fid_filter
        self.expression_context = expression_context
        self.excluded_field_names = {n.lower() for n in excluded_field_names}


class LayerOutput:
    def __init__(self, role, fields, wkb_type, features):
        self.role = role
        self.fields = fields
        self.wkb_type = wkb_type
        self.features = features


class _Point:
    __slots__ = ("fid", "sort_key", "coords", "attrs")

    def __init__(self, fid, sort_key, coords, attrs):
        self.fid = fid
        self.sort_key = sort_key
        self.coords = coords
        self.attrs = attrs


class _Group:
    __slots__ = ("raw_value", "points")

    def __init__(self, raw_value):
        self.raw_value = raw_value
        self.points = []


class _Feedback:
    """Minimal feedback adapter (overridden by the task)."""

    def is_canceled(self):
        return False

    def set_progress(self, value):
        pass


def _type_id(field):
    t = field.type()
    try:
        return int(t)
    except TypeError:
        return getattr(t, "value", -1)


def _fid_list(fids):
    shown = ", ".join(str(f) for f in fids[:_MAX_LISTED_FIDS])
    if len(fids) > _MAX_LISTED_FIDS:
        shown += ", ... (+{})".format(len(fids) - _MAX_LISTED_FIDS)
    return shown


def _unique_name(base, taken):
    name = base
    i = 1
    while name.lower() in taken:
        name = "{}_{}".format(base, i)
        i += 1
    taken.add(name.lower())
    return name


class ConversionEngine:
    def __init__(self, engine_input, settings, result, feedback=None):
        self.inp = engine_input
        self.s = settings
        self.r = result
        self.fb = feedback or _Feedback()
        self.has_z = QgsWkbTypes.hasZ(engine_input.wkb_type)
        self.has_m = QgsWkbTypes.hasM(engine_input.wkb_type)
        self._last_pct = -1

    # ------------------------------------------------------------------ utils
    def _progress(self, pct):
        pct = int(pct)
        if pct != self._last_pct:
            self._last_pct = pct
            self.fb.set_progress(pct)

    def _check_cancel(self):
        if self.fb.is_canceled():
            raise EngineCancelled()

    # --------------------------------------------------------------- fields
    def _resolve_fields(self):
        fields = self.inp.fields
        s = self.s
        self.group_idx = -1
        if s.grouped:
            self.group_idx = fields.lookupField(s.group_field)
            if self.group_idx < 0:
                raise ValueError("Group field '{}' not found".format(s.group_field))
        self.order_idx = -1
        if s.order_method == ORDER_FIELD:
            self.order_idx = fields.lookupField(s.order_field)
            if self.order_idx < 0:
                raise ValueError("Order field '{}' not found".format(s.order_field))

        self.preserve_idx = []
        if s.preserve_attributes:
            skipped_types = []
            for i, f in enumerate(fields):
                if i in (self.group_idx, self.order_idx):
                    continue
                if f.name().lower() in self.inp.excluded_field_names:
                    continue
                if _type_id(f) in _UNSUPPORTED_TYPE_IDS or _type_id(f) >= 1024:
                    skipped_types.append(f.name())
                    continue
                self.preserve_idx.append(i)
            if skipped_types:
                self.r.info(
                    "",
                    "Fields not copied (list/map/binary types are not supported by "
                    "file outputs): " + ", ".join(skipped_types),
                )

    # ---------------------------------------------------------------- reading
    def _prepare_expression(self):
        self.expr = None
        if self.s.order_method != ORDER_EXPRESSION:
            return
        expr = QgsExpression(self.s.order_expression)
        if expr.hasParserError():
            raise ValueError("Order expression error: " + expr.parserErrorString())
        ctx = (
            QgsExpressionContext(self.inp.expression_context)
            if self.inp.expression_context is not None
            else QgsExpressionContext()
        )
        ctx.setFields(self.inp.fields)
        expr.prepare(ctx)
        self.expr = expr
        self.expr_ctx = ctx

    def _build_request(self):
        req = QgsFeatureRequest()
        if self.inp.fid_filter is not None:
            req.setFilterFids(list(self.inp.fid_filter))  # Qt6 bindings reject set()
        needed = set(self.preserve_idx)
        for idx in (self.group_idx, self.order_idx):
            if idx >= 0:
                needed.add(idx)
        all_attrs = False
        if self.expr is not None:
            cols = self.expr.referencedColumns()
            if QgsFeatureRequest.ALL_ATTRIBUTES in cols:
                all_attrs = True
            else:
                for c in cols:
                    i = self.inp.fields.lookupField(c)
                    if i >= 0:
                        needed.add(i)
        if not all_attrs:
            req.setSubsetOfAttributes(sorted(needed))
        return req

    def _read(self):
        s = self.s
        r = self.r
        groups = {}
        total = max(1, self.inp.feature_count)
        no_geom, bad_coord, null_group = [], [], []
        multi_part_features = 0
        expr_errors = 0
        first_expr_error = ""
        text_order_samples = []
        grouped = s.grouped
        has_z, has_m = self.has_z, self.has_m
        order_method = s.order_method
        group_idx, order_idx = self.group_idx, self.order_idx
        preserve_idx = self.preserve_idx
        skip_null_groups = s.null_group_policy == NULL_GROUP_SKIP
        single_group_key = ("__all__",)
        if len(preserve_idx) == 0:
            take_pres = lambda a: ()  # noqa: E731
        elif len(preserve_idx) == 1:
            i0 = preserve_idx[0]
            take_pres = lambda a: (a[i0],)  # noqa: E731
        else:
            take_pres = itemgetter(*preserve_idx)
        multi_layer = QgsWkbTypes.isMultiType(self.inp.wkb_type)
        normalize_group = sequencer.normalize_group_value
        normalize_sort = sequencer.normalize_sort_value
        null_key = sequencer.NULL_GROUP_KEY
        isfinite = math.isfinite

        n = 0
        for feat in self.inp.source.getFeatures(self._build_request()):
            n += 1
            if n % 2000 == 0:
                self._check_cancel()
                self._progress(55.0 * n / total)
            fid = feat.id()
            geom = feat.geometry()
            if geom is None or geom.isNull() or geom.isEmpty():
                no_geom.append(fid)
                continue
            abstract = geom.constGet()
            if multi_layer and geom.isMultipart():
                parts = [abstract.geometryN(i) for i in range(abstract.numGeometries())]
                if len(parts) > 1:
                    multi_part_features += 1
            else:
                parts = [abstract]
            coords = []
            for p in parts:
                x, y = p.x(), p.y()
                if not (isfinite(x) and isfinite(y)):
                    continue
                coords.append(
                    (x, y, p.z() if has_z else 0.0, p.m() if has_m else 0.0)
                )
            if not coords:
                bad_coord.append(fid)
                continue

            attrs = feat.attributes()
            if grouped:
                raw_group = attrs[group_idx]
                key = normalize_group(raw_group)
                if key is null_key:
                    if skip_null_groups:
                        null_group.append(fid)
                        continue
                    raw_group = None
            else:
                key, raw_group = single_group_key, None

            if order_method == ORDER_FID:
                sort_key = (0, fid)
            elif order_method == ORDER_FIELD:
                v = attrs[order_idx]
                sort_key = normalize_sort(v)
                if len(text_order_samples) < 200 and isinstance(v, str):
                    text_order_samples.append(v)
            else:
                self.expr_ctx.setFeature(feat)
                v = self.expr.evaluate(self.expr_ctx)
                if self.expr.hasEvalError():
                    expr_errors += 1
                    if not first_expr_error:
                        first_expr_error = self.expr.evalErrorString()
                    sort_key = None
                else:
                    sort_key = sequencer.normalize_sort_value(v)
                    if len(text_order_samples) < 200 and isinstance(v, str):
                        text_order_samples.append(v)

            pres = take_pres(attrs)
            grp = groups.get(key)
            if grp is None:
                grp = groups[key] = _Group(raw_group)
            grp.points.append(_Point(fid, sort_key, coords, pres))

        r.points_read = n
        skipped = len(no_geom) + len(bad_coord) + len(null_group)
        if no_geom:
            r.warning("", "{} point(s) without geometry skipped (fid: {})".format(
                len(no_geom), _fid_list(no_geom)))
        if bad_coord:
            r.warning("", "{} point(s) with non-finite coordinates skipped (fid: {})".format(
                len(bad_coord), _fid_list(bad_coord)))
        if null_group:
            r.warning("", "{} point(s) with NULL/empty group value skipped by setting "
                          "(fid: {})".format(len(null_group), _fid_list(null_group)))
        if multi_part_features:
            r.info("", "{} multipoint feature(s): their parts were used as consecutive "
                       "vertices in part order".format(multi_part_features))
        if expr_errors:
            r.warning("", "Order expression failed for {} point(s): {}".format(
                expr_errors, first_expr_error))
        if text_order_samples and sequencer.looks_numeric_text(text_order_samples):
            r.warning(
                "",
                "Order values are text that looks numeric; text sorts alphabetically "
                "('10' before '2'). Use an expression such as to_real(\"field\") for "
                "numeric order.",
            )
        self.points_skipped_read = skipped
        return groups

    # -------------------------------------------------------------- building
    def _vertices(self, ordered):
        verts = []
        for pidx, p in enumerate(ordered):
            for x, y, z, m in p.coords:
                verts.append((x, y, z, m, p.fid, pidx))
        return verts

    def run(self):
        s = self.s
        r = self.r
        self._resolve_fields()
        self._prepare_expression()
        groups = self._read()
        self._check_cancel()

        is_polygon = s.mode == MODE_POLYGON
        segments = s.mode == MODE_LINE and s.line_structure == LINE_SEGMENTS
        min_vertices = 3 if is_polygon else 2
        npres = len(self.preserve_idx)

        main_rows = []  # (geometry, generated_values, preserved_values, conflict_flags)
        boundary_geoms = []
        out_id = 0
        points_used = 0
        points_excluded = 0
        keys = sorted(groups.keys(), key=sequencer.group_sort_key)
        r.groups = len(keys)
        total_groups = max(1, len(keys))

        for gi, key in enumerate(keys):
            if gi % 50 == 0:
                self._check_cancel()
                self._progress(55 + 30.0 * gi / total_groups)
            grp = groups[key]
            label = sequencer.group_label(key, grp.raw_value, s.grouped)

            ordered, excluded, ties = sequencer.order_points(grp.points, s.reverse_order)
            if excluded:
                points_excluded += len(excluded)
                r.warning(label, "{} point(s) with NULL order value excluded (fid: {})".format(
                    len(excluded), _fid_list([p.fid for p in excluded])))
            if ties and s.order_method != ORDER_FID:
                r.warning(label, "{} tie(s) in order value; feature ID used as tie-breaker".format(ties))

            verts = self._vertices(ordered)
            dups = sequencer.count_consecutive_duplicates(verts)
            if dups:
                if s.remove_duplicates:
                    verts, _ = sequencer.remove_consecutive_duplicates(verts)
                    r.info(label, "{} duplicate consecutive vertex/vertices removed".format(dups))
                else:
                    r.warning(label, "{} duplicate consecutive vertex/vertices kept".format(dups))
            if is_polygon:
                verts, stripped = sequencer.strip_explicit_closure(verts)
                if stripped:
                    r.info(label, "last point repeats the first; ring closed once automatically")

            distinct = sequencer.count_distinct_xy(verts)
            if distinct < min_vertices:
                r.groups_failed += 1
                r.invalid_geometries += 1
                r.error(label, "only {} distinct vertex/vertices ({} point(s)); at least {} "
                               "required for a {}".format(distinct, len(ordered), min_vertices,
                                                          "polygon" if is_polygon else "line"))
                continue
            points_used += len(ordered)
            gval = [grp.raw_value] if s.grouped else []

            if segments:
                for i in range(len(verts) - 1):
                    a, b = verts[i], verts[i + 1]
                    geom = builder.build_segment(a, b, self.has_z, self.has_m)
                    status, geom, msg = builder.check_line(geom, s.validate)
                    if status == builder.STATUS_INVALID:
                        r.invalid_geometries += 1
                        r.error(label, "segment {} (fid {} -> {}) skipped: {}".format(
                            i + 1, a[4], b[4], msg))
                        continue
                    out_id += 1
                    if a[5] == b[5]:
                        rows = [ordered[a[5]].attrs]
                    else:
                        rows = [ordered[a[5]].attrs, ordered[b[5]].attrs]
                    pv, pc = sequencer.merge_attributes(rows, npres)
                    main_rows.append((geom, [out_id] + gval + [i + 1, a[4], b[4]], pv, pc))
                continue

            if is_polygon:
                geom = builder.build_polygon(verts, self.has_z, self.has_m)
                status, geom, msg = builder.check_polygon(geom, s.validate, s.repair)
            else:
                geom = builder.build_line(verts, self.has_z, self.has_m)
                status, geom, msg = builder.check_line(geom, s.validate)
            if status == builder.STATUS_INVALID:
                r.invalid_geometries += 1
                r.error(label, "geometry not written: " + msg)
                continue
            if status == builder.STATUS_REPAIRED:
                r.repaired_geometries += 1
                r.warning(label, msg)
            out_id += 1
            pv, pc = sequencer.merge_attributes([p.attrs for p in ordered], npres)
            generated = [out_id] + gval + [len(verts)]
            if is_polygon and s.validate and s.repair:
                generated.append(1 if status == builder.STATUS_REPAIRED else 0)
            main_rows.append((geom, generated, pv, pc))
            if is_polygon and s.writes_boundary:
                boundary_geoms.append(builder.polygon_boundary(geom))

        r.points_used = points_used
        r.points_skipped = self.points_skipped_read + points_excluded
        self._check_cancel()
        self._progress(85)
        return self._assemble(main_rows, boundary_geoms, is_polygon, segments)

    # -------------------------------------------------------------- assembly
    def _assemble(self, rows, boundary_geoms, is_polygon, segments):
        s = self.s
        src = self.inp.fields
        npres = len(self.preserve_idx)

        # Drop preserved fields that conflict in every output feature
        keep = list(range(npres))
        if rows and npres:
            conflict_counts = [0] * npres
            for _, _, _, pc in rows:
                for i, c in enumerate(pc):
                    if c:
                        conflict_counts[i] += 1
            dropped = [src.at(self.preserve_idx[i]).name()
                       for i in range(npres) if conflict_counts[i] == len(rows)]
            keep = [i for i in range(npres) if conflict_counts[i] < len(rows)]
            partial = ["{} ({}x)".format(src.at(self.preserve_idx[i]).name(), conflict_counts[i])
                       for i in keep if conflict_counts[i]]
            if dropped:
                self.r.info("", "Fields omitted because values differ within every output "
                                "feature: " + ", ".join(dropped))
            if partial:
                self.r.info("", "Fields set to NULL where values differ within a feature: "
                                + ", ".join(partial))

        out_fields = QgsFields()
        taken = {src.at(self.preserve_idx[i]).name().lower() for i in keep}
        if s.grouped:
            taken.add(s.group_field.lower())

        def add_generated(name, kind):
            out_fields.append(make_field(_unique_name(name, taken), kind))

        add_generated("out_id", "int")
        if s.grouped:
            out_fields.append(QgsField(src.at(self.group_idx)))
        if segments:
            add_generated("seg_seq", "int")
            add_generated("from_fid", "long")
            add_generated("to_fid", "long")
        else:
            add_generated("pt_count", "int")
            if is_polygon and s.validate and s.repair:
                add_generated("repaired", "int")
        for i in keep:
            out_fields.append(QgsField(src.at(self.preserve_idx[i])))

        any_multi = any(g.isMultipart() for g, _, _, _ in rows)
        if is_polygon:
            base = WKB_MULTIPOLYGON if any_multi else WKB_POLYGON
        else:
            base = WKB_LINESTRING
        main_wkb = output_wkb_type(base, self.has_z, self.has_m)

        features = []
        for geom, generated, pv, _ in rows:
            if any_multi and not geom.isMultipart():
                geom = QgsGeometry(geom)
                geom.convertToMultiType()
            f = QgsFeature(out_fields)
            f.setGeometry(geom)
            f.setAttributes(generated + [pv[i] for i in keep])
            features.append(f)
        outputs = [LayerOutput("main", out_fields, main_wkb, features)]

        if is_polygon and s.writes_boundary:
            b_multi = any(g is not None and g.isMultipart() for g in boundary_geoms)
            b_wkb = output_wkb_type(
                WKB_MULTILINESTRING if b_multi else WKB_LINESTRING, self.has_z, self.has_m)
            b_features = []
            for (poly, generated, pv, _), bg in zip(rows, boundary_geoms):
                if bg is None:
                    continue
                if b_multi and not bg.isMultipart():
                    bg = QgsGeometry(bg)
                    bg.convertToMultiType()
                f = QgsFeature(out_fields)
                f.setGeometry(bg)
                f.setAttributes(generated + [pv[i] for i in keep])
                b_features.append(f)
            outputs.append(LayerOutput("boundary", out_fields, b_wkb, b_features))

        if is_polygon and not s.writes_polygons:
            outputs = outputs[1:]  # boundary line only
        primary = outputs[0].features
        self.r.output_features = len(primary)
        self.r.successful_geometries = len(primary)
        return outputs

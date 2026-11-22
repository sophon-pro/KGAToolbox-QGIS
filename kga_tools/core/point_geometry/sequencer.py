"""Pure-Python grouping, ordering and vertex-cleaning rules.

This module has no QGIS dependency so its rules can be unit-tested in
isolation. The rules implemented here are the documented, predictable
behaviour of the plugin:

* NULL values  - ``None`` or a null QVariant.
* Group keys   - NULL and empty / whitespace-only text are both "no group
                 value". Depending on the user's choice those points form one
                 shared group or are skipped (and reported).
* Ordering     - points are sorted by (order value, feature id). Feature id is
                 the deterministic tie-breaker; ties are counted and reported.
                 Points whose order value is NULL cannot be placed and are
                 excluded (and reported).
* Mixed types  - values are compared within type families
                 (numbers < text < date-times < dates < times < other).
* Duplicates   - two consecutive vertices are duplicates when their X and Y
                 coordinates are exactly equal (Z/M are not compared).
* Attributes   - a source attribute is kept for an output feature only when
                 all contributing points share the same value; otherwise the
                 output value is NULL and the conflict is counted.
"""

from contextlib import suppress

NULL_GROUP_KEY = ("__p2g_null_group__",)


def is_null(value):
    if value is None:
        return True
    is_null_method = getattr(value, "isNull", None)
    # QVariant / QDate / QDateTime all expose isNull(); plain Python types do not
    if is_null_method is not None and type(value).__name__ == "QVariant":
        try:
            return bool(is_null_method())
        except Exception:  # pragma: no cover
            return False
    if type(value).__name__ in ("QDate", "QDateTime", "QTime"):
        try:
            return bool(value.isNull()) or not value.isValid()
        except Exception:  # pragma: no cover
            return False
    return False


def normalize_sort_value(value):
    """Return a totally ordered key for an attribute/expression value, or None."""
    t = type(value)
    if t is int:  # fast paths for the common cases
        return (0, value)
    if t is float:
        return (0, value) if value == value else None
    if t is str:
        return (1, value)
    if is_null(value):
        return None
    if isinstance(value, bool):
        return (0, int(value))
    if isinstance(value, (int, float)):
        if value != value:  # NaN cannot be ordered
            return None
        return (0, value)
    if isinstance(value, str):
        return (1, value)
    if hasattr(value, "toMSecsSinceEpoch"):  # QDateTime
        return (2, value.toMSecsSinceEpoch())
    if hasattr(value, "toJulianDay"):  # QDate
        return (3, value.toJulianDay())
    if hasattr(value, "msecsSinceStartOfDay"):  # QTime
        return (4, value.msecsSinceStartOfDay())
    if hasattr(value, "timestamp"):  # python datetime
        with suppress(Exception):  # a naive datetime out of range: compared as text below
            return (2, value.timestamp() * 1000.0)
    if hasattr(value, "toordinal"):  # python date
        return (3, value.toordinal())
    return (9, str(value))


def is_empty_group_value(value):
    if is_null(value):
        return True
    return isinstance(value, str) and value.strip() == ""


def normalize_group_value(value):
    """Hashable key for grouping. NULL/empty -> NULL_GROUP_KEY."""
    t = type(value)
    if t is int:
        return (0, value)
    if t is str:
        return (1, value) if value.strip() else NULL_GROUP_KEY
    if is_empty_group_value(value):
        return NULL_GROUP_KEY
    key = normalize_sort_value(value)
    return key if key is not None else NULL_GROUP_KEY


def group_sort_key(group_key):
    """Order output groups predictably; the NULL group goes last."""
    if group_key == NULL_GROUP_KEY:
        return (1, (0, 0))
    return (0, group_key)


def looks_numeric_text(values, sample=200):
    """True if every non-null text value (in a sample) parses as a number."""
    seen = 0
    for v in values:
        if not isinstance(v, str):
            return False
        try:
            float(v)
        except ValueError:
            return False
        seen += 1
        if seen >= sample:
            break
    return seen > 1


def order_points(points, reverse=False):
    """Sort points of one group.

    ``points`` is a list of objects with ``sort_key`` and ``fid`` attributes.
    Returns (ordered_points, excluded_null_points, tie_count). The feature id
    remains ascending within ties even when ``reverse`` is True, so output is
    reproducible.
    """
    valid = [p for p in points if p.sort_key is not None]
    excluded = [p for p in points if p.sort_key is None]
    if reverse:
        valid.sort(key=lambda p: p.fid)
        valid.sort(key=lambda p: p.sort_key, reverse=True)  # stable sort
    else:
        valid.sort(key=lambda p: (p.sort_key, p.fid))
    ties = 0
    for a, b in zip(valid, valid[1:]):
        if a.sort_key == b.sort_key:
            ties += 1
    return valid, excluded, ties


def remove_consecutive_duplicates(vertices):
    """Drop vertices whose X/Y equal the previous kept vertex.

    ``vertices`` are tuples (x, y, z, m, fid). Returns (kept, removed_count).
    """
    kept = []
    removed = 0
    for v in vertices:
        if kept and kept[-1][0] == v[0] and kept[-1][1] == v[1]:
            removed += 1
            continue
        kept.append(v)
    return kept, removed


def count_consecutive_duplicates(vertices):
    return sum(
        1 for a, b in zip(vertices, vertices[1:]) if a[0] == b[0] and a[1] == b[1]
    )


def count_distinct_xy(vertices):
    return len({(v[0], v[1]) for v in vertices})


def strip_explicit_closure(vertices):
    """If the user already repeated the first point at the end, drop it.

    Returns (vertices, stripped_bool). Repeats are stripped until the last
    vertex differs from the first, so the plugin closes the ring exactly once.
    """
    stripped = False
    while len(vertices) > 1 and vertices[-1][0] == vertices[0][0] and vertices[-1][1] == vertices[0][1]:
        vertices = vertices[:-1]
        stripped = True
    return vertices, stripped


def merge_attributes(attr_rows, field_count):
    """Merge attribute tuples. Returns (values_list, conflict_flags_list)."""
    if not attr_rows:
        return [None] * field_count, [False] * field_count
    first = attr_rows[0]
    values = list(first)
    conflicts = [False] * field_count
    for i in range(field_count):
        ref = first[i]
        ref_null = is_null(ref)
        for row in attr_rows[1:]:
            v = row[i]
            v_null = is_null(v)
            if ref_null and v_null:
                continue
            if ref_null != v_null or v != ref:
                conflicts[i] = True
                values[i] = None
                break
    return values, conflicts


def group_label(group_key, raw_value, grouped):
    if not grouped:
        return "all points"
    if group_key == NULL_GROUP_KEY:
        return "group <NULL/empty>"
    return "group '{}'".format(raw_value)

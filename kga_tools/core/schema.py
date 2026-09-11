# -*- coding: utf-8 -*-
"""Field comparison, mapping generation and type coercion.

`coerce()` is the heart of this module and is shared by Append with Field
Mapping and by the Excel round-trip. QGIS itself will happily write a value
that does not fit a field and leave you with a NULL and no message; every
conversion that loses information has to be visible instead. So `coerce()`
never raises for ordinary bad data — it returns the best value it can plus a
plain-English note, and the caller records the note in the change report.

The rule the whole module follows: a returned note means the user needs to know
something. No note means the value went in unchanged.
"""

import datetime
import math
import re

from .compat import (
    INTEGER_TYPES,
    NUMERIC_TYPES,
    T_BOOL,
    T_DATE,
    T_DATETIME,
    T_DOUBLE,
    T_INT,
    T_LONGLONG,
    T_STRING,
    T_TIME,
    T_UINT,
    T_ULONGLONG,
    type_key,
    type_name,
)

#: Excel counts days from this date (its "1900 system" is off by one for 1900,
#: which is why the epoch is the 30th and not the 31st).
EXCEL_EPOCH = datetime.datetime(1899, 12, 30)

#: Signed ranges. A value outside these is silently truncated by OGR, so it is
#: caught here instead.
INT_RANGE = {
    T_INT: (-2147483648, 2147483647),
    T_UINT: (0, 4294967295),
    T_LONGLONG: (-9223372036854775808, 9223372036854775807),
    T_ULONGLONG: (0, 18446744073709551615),
}

_TRUE_WORDS = frozenset(('1', 'true', 't', 'yes', 'y', 'on'))
_FALSE_WORDS = frozenset(('0', 'false', 'f', 'no', 'n', 'off'))

_DATE_FORMATS = (
    '%Y-%m-%d', '%Y/%m/%d', '%d/%m/%Y', '%d-%m-%Y', '%m/%d/%Y',
    '%d.%m.%Y', '%Y%m%d',
)
_DATETIME_FORMATS = (
    '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%SZ',
    '%Y-%m-%d %H:%M', '%d/%m/%Y %H:%M:%S', '%d/%m/%Y %H:%M',
    '%m/%d/%Y %H:%M:%S', '%Y-%m-%dT%H:%M:%S.%f',
)
_TIME_FORMATS = ('%H:%M:%S', '%H:%M', '%H:%M:%S.%f')

_NUMBER_CLEAN = re.compile(r'[\s ,_]')


class CoercionError(Exception):
    """A value that cannot be represented in the target field at all."""


# --------------------------------------------------------------- null helpers

def is_null(value):
    """True for None, a null QVariant, an empty string, or NaN."""
    if value is None:
        return True
    try:
        from qgis.PyQt.QtCore import QVariant
        if isinstance(value, QVariant):
            return value.isNull()
    except ImportError:                     # pragma: no cover
        pass
    if isinstance(value, str) and value.strip() == '':
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return False


def _plain(value):
    """Unwrap a QVariant so the rest of the module sees a Python value."""
    try:
        from qgis.PyQt.QtCore import QVariant
        if isinstance(value, QVariant):
            return None if value.isNull() else value.value()
    except ImportError:                     # pragma: no cover
        pass
    return value


# -------------------------------------------------------------------- coerce

def coerce(value, target_field, excel_dates=False):
    """Fit `value` into `target_field`.

    Returns ``(coerced_value, note)``. `note` is ``''`` when the value went in
    as it was, and a short explanation otherwise. Raises `CoercionError` only
    when there is no sensible value at all to write, which the caller reports as
    a violation rather than a crash.

    `excel_dates` turns on the interpretation of a bare number in a date field
    as an Excel day serial. It is off by default because in every context other
    than an imported worksheet a number in a date field is a mistake worth
    surfacing, not a serial worth decoding.
    """
    value = _plain(value)
    type_id = int(target_field.type())

    if is_null(value):
        return None, ''

    if type_id == T_STRING:
        return _to_string(value, target_field)
    if type_id in NUMERIC_TYPES:
        return _to_number(value, target_field, type_id)
    if type_id == T_BOOL:
        return _to_bool(value)
    if type_id in (T_DATE, T_DATETIME, T_TIME):
        return _to_temporal(value, type_id, excel_dates)

    # Binary, string lists and anything exotic: pass through untouched rather
    # than guess. The write will fail loudly if it really cannot take it.
    return value, ''


def _to_string(value, field):
    if isinstance(value, float) and value == int(value) and abs(value) < 1e15:
        # Excel turns a text code like "0042" into the number 42. Rendering
        # that back as "42.0" is worse than "42", so drop the empty decimal.
        text = str(int(value))
        note = 'number {} read as text'.format(value)
    elif isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        text = value.isoformat()
        note = ''
    else:
        text = _qt_to_text(value)
        note = '' if isinstance(value, str) else '{} read as text'.format(
            type(value).__name__)

    length = int(field.length() or 0)
    if 0 < length < len(text):
        return text[:length], 'truncated to {} characters (was {})'.format(
            length, len(text))
    return text, note


def _to_number(value, field, type_id):
    if isinstance(value, bool):
        number = 1 if value else 0
        note = 'boolean read as number'
    elif isinstance(value, (int, float)):
        number = value
        note = ''
    elif isinstance(value, (datetime.datetime, datetime.date)):
        raise CoercionError('a date cannot be written to the numeric field '
                            '"{}"'.format(field.name()))
    else:
        text = _NUMBER_CLEAN.sub('', _qt_to_text(value))
        if text in ('', '-', '+'):
            return None, 'empty value read as NULL'
        try:
            number = float(text)
        except ValueError:
            raise CoercionError(
                '"{}" is not a number and field "{}" is {}'.format(
                    _qt_to_text(value), field.name(), type_name(type_id)))
        note = 'text "{}" read as a number'.format(_qt_to_text(value))

    if type_id in INTEGER_TYPES:
        rounded = int(round(number))
        if rounded != number:
            note = _join(note, '{} rounded to {}'.format(number, rounded))
        low, high = INT_RANGE[type_id]
        if not low <= rounded <= high:
            raise CoercionError(
                '{} does not fit field "{}" ({})'.format(
                    rounded, field.name(), type_name(type_id)))
        return rounded, note

    number = float(number)
    precision = int(field.precision() or 0)
    if precision > 0:
        snapped = round(number, precision)
        if snapped != number:
            note = _join(note, 'rounded to {} decimal places'.format(precision))
        number = snapped
    return number, note


def _to_bool(value):
    if isinstance(value, bool):
        return value, ''
    if isinstance(value, (int, float)):
        return bool(value), 'number read as boolean'
    text = _qt_to_text(value).strip().lower()
    if text in _TRUE_WORDS:
        return True, 'text "{}" read as true'.format(text)
    if text in _FALSE_WORDS:
        return False, 'text "{}" read as false'.format(text)
    raise CoercionError('"{}" is not a yes/no value'.format(text))


def _to_temporal(value, type_id, excel_dates):
    from qgis.PyQt.QtCore import QDate, QDateTime, QTime

    note = ''
    parsed = None

    if isinstance(value, (QDate, QDateTime, QTime)):
        parsed = _from_qt_temporal(value)
    elif isinstance(value, datetime.datetime):
        parsed = value
    elif isinstance(value, datetime.date):
        parsed = datetime.datetime(value.year, value.month, value.day)
    elif isinstance(value, datetime.time):
        parsed = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if not excel_dates:
            raise CoercionError(
                '{} is a number, not a date. If it came from a spreadsheet, '
                'enable Excel date serials.'.format(value))
        parsed = EXCEL_EPOCH + datetime.timedelta(days=float(value))
        note = 'Excel day serial {} read as {}'.format(
            value, parsed.date().isoformat())
    else:
        text = _qt_to_text(value).strip()
        parsed = _parse_temporal_text(text, type_id)
        if parsed is None:
            raise CoercionError('"{}" is not a recognised date or time'.format(text))
        note = 'text "{}" parsed as {}'.format(text, parsed.isoformat())

    if type_id == T_DATE:
        if isinstance(parsed, datetime.time):
            raise CoercionError('a time cannot be written to a date field')
        as_date = parsed.date() if isinstance(parsed, datetime.datetime) else parsed
        if isinstance(parsed, datetime.datetime) and (
                parsed.hour or parsed.minute or parsed.second):
            note = _join(note, 'time of day dropped')
        return QDate(as_date.year, as_date.month, as_date.day), note

    if type_id == T_TIME:
        as_time = parsed.time() if isinstance(parsed, datetime.datetime) else parsed
        if not isinstance(as_time, datetime.time):
            raise CoercionError('no time of day in "{}"'.format(value))
        return QTime(as_time.hour, as_time.minute, as_time.second,
                     as_time.microsecond // 1000), note

    if isinstance(parsed, datetime.time):
        raise CoercionError('a time on its own has no date')
    if not isinstance(parsed, datetime.datetime):
        parsed = datetime.datetime(parsed.year, parsed.month, parsed.day)
    return QDateTime(
        QDate(parsed.year, parsed.month, parsed.day),
        QTime(parsed.hour, parsed.minute, parsed.second,
              parsed.microsecond // 1000)), note


def _parse_temporal_text(text, type_id):
    formats = _DATETIME_FORMATS + _DATE_FORMATS
    if type_id == T_TIME:
        formats = _TIME_FORMATS + formats
    elif type_id == T_DATE:
        formats = _DATE_FORMATS + _DATETIME_FORMATS
    for fmt in formats:
        try:
            parsed = datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
        if fmt in _TIME_FORMATS:
            return parsed.time()
        return parsed
    try:
        return datetime.datetime.fromisoformat(text)
    except (ValueError, AttributeError):
        return None


def _from_qt_temporal(value):
    from qgis.PyQt.QtCore import QDate, QDateTime, QTime
    if isinstance(value, QDateTime):
        return value.toPyDateTime()
    if isinstance(value, QDate):
        return datetime.datetime(value.year(), value.month(), value.day())
    if isinstance(value, QTime):
        return datetime.time(value.hour(), value.minute(), value.second(),
                             value.msec() * 1000)
    return None                             # pragma: no cover


def _qt_to_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode('utf-8', 'replace')
    return str(value)


def _join(first, second):
    if not first:
        return second
    if not second:
        return first
    return '{}; {}'.format(first, second)


# ----------------------------------------------------------- field comparison

def field_signature(fields):
    """A JSON-friendly description of a `QgsFields`, used to spot drift."""
    return [
        {
            'name': field.name(),
            'type': type_key(field.type()),
            'length': int(field.length() or 0),
            'precision': int(field.precision() or 0),
        }
        for field in fields
    ]


def normalise(name):
    """Fold a field name for fuzzy comparison: lowercase, no separators."""
    return re.sub(r'[^a-z0-9]', '', (name or '').lower())


STRATEGY_EXACT = 'exact'
STRATEGY_CASE_INSENSITIVE = 'case'
STRATEGY_FUZZY = 'fuzzy'


def match_fields(source_fields, target_fields, strategy=STRATEGY_EXACT):
    """Pair source fields to target fields by name.

    Returns ``(pairs, unmatched_source, unmatched_target)`` where `pairs` is a
    list of ``(source_name, target_name)``. Each target is used at most once,
    so a fuzzy run cannot quietly map two sources onto the same column.
    """
    source_names = [f.name() for f in source_fields]
    target_names = [f.name() for f in target_fields]

    if strategy == STRATEGY_EXACT:
        key = lambda n: n                                        # noqa: E731
    elif strategy == STRATEGY_CASE_INSENSITIVE:
        key = lambda n: n.lower()                                # noqa: E731
    else:
        key = normalise

    # Built in target order so that when two targets fold to the same key the
    # first one declared wins, which is the one a user expects.
    lookup = {}
    for name in target_names:
        lookup.setdefault(key(name), name)

    pairs = []
    taken = set()
    for name in source_names:
        candidate = lookup.get(key(name))
        if candidate is not None and candidate not in taken:
            pairs.append((name, candidate))
            taken.add(candidate)

    matched_sources = {s for s, _ in pairs}
    unmatched_source = [n for n in source_names if n not in matched_sources]
    unmatched_target = [n for n in target_names if n not in taken]
    return pairs, unmatched_source, unmatched_target


def mapping_from_pairs(pairs, target_fields):
    """Build a `QgsProcessingParameterFieldMapping` value from name pairs.

    The parameter's value is a list of dicts with `name`, `type`, `length`,
    `precision` and `expression`; the expression is evaluated against the
    source feature, so a straight copy is just the quoted source field name.
    """
    by_name = {f.name(): f for f in target_fields}
    mapping = []
    for source_name, target_name in pairs:
        field = by_name.get(target_name)
        if field is None:
            continue
        mapping.append({
            'name': target_name,
            'type': int(field.type()),
            'type_name': type_key(field.type()),
            'length': int(field.length() or 0),
            'precision': int(field.precision() or 0),
            'expression': '"{}"'.format(source_name.replace('"', '""')),
        })
    return mapping


def mapping_target_names(mapping):
    """Just the target field names out of a mapping value."""
    names = []
    for entry in mapping or []:
        if isinstance(entry, dict) and entry.get('name'):
            names.append(entry['name'])
    return names


def validate_mapping(mapping, target_fields):
    """Names in `mapping` that the target layer does not actually have.

    Called before a single feature is written. An append that drops a mapped
    field because of a typo is the classic way this kind of tool loses data.
    """
    have = {f.name() for f in target_fields}
    return [name for name in mapping_target_names(mapping) if name not in have]


def unmapped_target_names(mapping, target_fields):
    """Target fields no mapping entry writes to; they keep their defaults."""
    mapped = set(mapping_target_names(mapping))
    return [f.name() for f in target_fields if f.name() not in mapped]

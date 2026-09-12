# -*- coding: utf-8 -*-
"""Small compatibility shims across the QGIS versions the plugin supports.

The plugin declares qgisMinimumVersion 3.28 and has to keep working on 3.40+
and on QGIS 4, which moves to PyQt6. Three things moved in that window:

* ``QgsField`` takes ``QMetaType.Type`` from 3.38 and warns on ``QVariant.Type``.
* Processing algorithm flags moved onto the scoped ``Qgis.ProcessingAlgorithmFlag``.
* ``QgsProcessing.TypeVectorAnyGeometry`` and friends moved onto ``Qgis.ProcessingSourceType``.

Everything here is resolved once at import time, so the algorithm modules can
just use the names and stay readable.

The numeric type ids are stable between ``QVariant.Type`` and ``QMetaType.Type``
for every type a vector field can hold (String is 10 in both, Int 2, Double 6,
LongLong 4, Bool 1, Date 14, Time 15, DateTime 16), so comparing ``field.type()``
against the constants below is safe on either build.
"""

from qgis.core import Qgis, QgsProcessingAlgorithm

try:                                        # QGIS >= 3.38 / Qt6
    from qgis.PyQt.QtCore import QMetaType

    def metatype(name):
        """'QString' -> QMetaType.Type.QString, or None if unknown."""
        return getattr(QMetaType.Type, name, None)

    HAS_QMETATYPE = True
except ImportError:                         # pragma: no cover - old builds
    QMetaType = None
    HAS_QMETATYPE = False

    def metatype(name):
        return None

from qgis.PyQt.QtCore import QVariant


# ------------------------------------------------------------------ type ids
#
# Written as plain ints rather than QVariant.X so that neither spelling is
# baked in. These are the values both enums agree on.

T_BOOL = 1
T_INT = 2
T_UINT = 3
T_LONGLONG = 4
T_ULONGLONG = 5
T_DOUBLE = 6
T_STRING = 10
T_STRINGLIST = 11
T_BYTEARRAY = 12
T_DATE = 14
T_TIME = 15
T_DATETIME = 16

NUMERIC_TYPES = frozenset(
    (T_INT, T_UINT, T_LONGLONG, T_ULONGLONG, T_DOUBLE))
INTEGER_TYPES = frozenset((T_INT, T_UINT, T_LONGLONG, T_ULONGLONG))
TEMPORAL_TYPES = frozenset((T_DATE, T_TIME, T_DATETIME))

TYPE_NAMES = {
    T_BOOL: 'Boolean',
    T_INT: 'Integer',
    T_UINT: 'Integer (unsigned)',
    T_LONGLONG: 'Integer64',
    T_ULONGLONG: 'Integer64 (unsigned)',
    T_DOUBLE: 'Real',
    T_STRING: 'Text',
    T_STRINGLIST: 'Text list',
    T_BYTEARRAY: 'Binary',
    T_DATE: 'Date',
    T_TIME: 'Time',
    T_DATETIME: 'Date & time',
}

#: Names accepted in a JSON field mapping / domain library, both directions.
TYPE_BY_NAME = {
    'bool': T_BOOL, 'boolean': T_BOOL,
    'int': T_INT, 'integer': T_INT,
    'long': T_LONGLONG, 'longlong': T_LONGLONG, 'integer64': T_LONGLONG,
    'double': T_DOUBLE, 'real': T_DOUBLE, 'float': T_DOUBLE,
    'string': T_STRING, 'text': T_STRING, 'str': T_STRING,
    'date': T_DATE,
    'time': T_TIME,
    'datetime': T_DATETIME,
}

NAME_BY_TYPE = {
    T_BOOL: 'bool',
    T_INT: 'int',
    T_UINT: 'int',
    T_LONGLONG: 'long',
    T_ULONGLONG: 'long',
    T_DOUBLE: 'double',
    T_STRING: 'string',
    T_DATE: 'date',
    T_TIME: 'time',
    T_DATETIME: 'datetime',
}


def type_name(type_id):
    """Human-readable name for a field type id."""
    return TYPE_NAMES.get(int(type_id), 'Type {}'.format(int(type_id)))


def type_key(type_id):
    """Round-trip token for a field type id, used in JSON payloads."""
    return NAME_BY_TYPE.get(int(type_id), 'string')


def type_from_key(key, default=T_STRING):
    """Inverse of `type_key`, tolerant of anything a hand-edited file holds."""
    if key is None:
        return default
    if isinstance(key, int):
        return key
    return TYPE_BY_NAME.get(str(key).strip().lower(), default)


def qt_type(type_id):
    """The value to hand to `QgsField(name, <here>)` on this build."""
    if HAS_QMETATYPE:
        return QMetaType.Type(int(type_id))
    return QVariant.Type(int(type_id))


def make_field(name, type_id, length=0, precision=0, type_name_hint=''):
    """QgsField built the way this QGIS wants, without a deprecation warning."""
    from qgis.core import QgsField
    return QgsField(name, qt_type(type_id), type_name_hint, length, precision)


# ------------------------------------------------------------------- flags

def no_threading(base_flags):
    """`base_flags` plus the "must run on the GUI thread" flag.

    Every KGA tool that opens a dialog or writes into a layer that is loaded in
    the project needs this; the spelling moved in 3.36.
    """
    try:
        return base_flags | Qgis.ProcessingAlgorithmFlag.NoThreading
    except AttributeError:                  # pragma: no cover - QGIS < 3.36
        return base_flags | QgsProcessingAlgorithm.Flag.FlagNoThreading


def source_type(name):
    """`QgsProcessing.TypeVectorAnyGeometry` etc., whichever enum holds it."""
    try:
        return getattr(Qgis.ProcessingSourceType, name)
    except AttributeError:                  # pragma: no cover - QGIS < 3.36
        from qgis.core import QgsProcessing
        return getattr(QgsProcessing, 'Type' + name)


def parameter_as_field_list(algorithm, parameters, name, context):
    """The value of a multi-field parameter, as a list of field names.

    `parameterAsFields` is deprecated from 3.40 in favour of
    `parameterAsStrings`, but that only exists from 3.32 and the plugin
    declares 3.28, so neither spelling can be used on its own.
    """
    getter = getattr(algorithm, 'parameterAsStrings', None)
    if getter is None:                      # pragma: no cover - QGIS < 3.32
        getter = algorithm.parameterAsFields
    return getter(parameters, name, context)


def wkb_type(name):
    """`Qgis.WkbType.Point` etc., falling back to `QgsWkbTypes`."""
    try:
        return getattr(Qgis.WkbType, name)
    except AttributeError:                  # pragma: no cover - QGIS < 3.30
        from qgis.core import QgsWkbTypes
        return getattr(QgsWkbTypes, name)


def point_wkb_type():
    return wkb_type('Point')


# --------------------------------------------------------------- field domains

def domain_type_value(name):
    """`Qgis.FieldDomainType.Coded` / `.Range` / `.Glob`, or None if absent."""
    holder = getattr(Qgis, 'FieldDomainType', None)
    if holder is None:                      # pragma: no cover - QGIS < 3.26
        return None
    return getattr(holder, name, None)


def has_field_domain_api():
    """True when this build exposes the field-domain classes at all."""
    try:
        from qgis.core import QgsCodedFieldDomain  # noqa: F401
    except ImportError:                     # pragma: no cover - QGIS < 3.26
        return False
    return domain_type_value('Coded') is not None


def mark_advanced(parameter):
    """Hide `parameter` behind the dialog's "Advanced parameters" section.

    The flag enum moved onto `Qgis.ProcessingParameterFlag` in 3.36 and the old
    spelling warns from 3.40, so neither can be used on its own. Returns the
    parameter, so it can be wrapped around an `addParameter` argument.
    """
    try:
        flag = Qgis.ProcessingParameterFlag.Advanced
    except AttributeError:                  # pragma: no cover - QGIS < 3.36
        from qgis.core import QgsProcessingParameterDefinition
        flag = QgsProcessingParameterDefinition.Flag.FlagAdvanced
    parameter.setFlags(parameter.flags() | flag)
    return parameter

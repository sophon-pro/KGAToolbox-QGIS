# -*- coding: utf-8 -*-
"""Field domains: read, write, apply, validate, and move between containers.

QGIS has supported field domains on GeoPackage and File Geodatabase since 3.26,
but only enough to create one. This module fills the three gaps: editing a
domain's values after creation, attaching one domain to many fields in a pass,
and carrying a domain library from one container to another.

Two implementation notes that matter, both established by probing QGIS 3.44 and
GDAL 3.13 rather than assumed:

**There is no `updateFieldDomain()`.** For GeoPackage the domains live in two
standard tables — `gpkg_data_column_constraints` holds one row per coded value
(or one row carrying min/max for a range), and `gpkg_data_columns` links
`(table_name, column_name)` to a `constraint_name`. Editing means rewriting
those rows. GDAL also keeps a `_<name>_domain_description` row in the
constraints table to carry the domain's description, which is easy to orphan if
you delete by name carelessly; `_delete_domain_rows` handles it explicitly.

**`QgsAbstractDatabaseProviderConnection.executeSql` does not honour
transactions.** A `BEGIN` / `DELETE` / `ROLLBACK` sequence through it leaves the
rows deleted. Since a half-rewritten domain is worse than an unwritten one, the
write path goes through `sqlite3` directly, where the rollback genuinely works.
Reads and attachment still go through the QGIS connection API, which is the
supported surface and handles both GPKG and FileGDB.
"""

import json
import os
import sqlite3
from contextlib import contextmanager, suppress

from qgis.core import (
    Qgis,
    QgsMessageLog,
    QgsProject,
    QgsProviderRegistry,
    QgsVectorLayer,
)

from .changelog import Violation
from .compat import (
    T_DATE,
    T_DATETIME,
    T_DOUBLE,
    T_INT,
    T_LONGLONG,
    T_STRING,
    domain_type_value,
    has_field_domain_api,
    qt_type,
    type_from_key,
    type_key,
)
from .schema import is_null

LOG_TAG = 'KGA Toolbox'

CODED = 'coded'
RANGE = 'range'
GLOB = 'glob'

#: Library files carry a version so a future reader can migrate an old one.
LIBRARY_FORMAT = 1

MODE_MERGE = 'merge'
MODE_REPLACE = 'replace'

#: GeoPackage housekeeping tables, never offered as a domain target.
SYSTEM_TABLE_PREFIXES = ('gpkg_', 'rtree_', 'sqlite_')
SYSTEM_TABLES = frozenset(('layer_styles', 'qgis_projects'))


class DomainError(Exception):
    """Anything that stops a domain operation, phrased for a user."""


# ---------------------------------------------------------------- connections

def connection_for(path):
    """A provider connection for a GPKG / FileGDB path.

    Raises `DomainError` rather than returning None, so callers never have to
    write the same "did that work?" check twice.
    """
    if not path:
        raise DomainError('No database was given.')
    if not os.path.exists(path):
        raise DomainError('{} does not exist.'.format(path))

    metadata = QgsProviderRegistry.instance().providerMetadata('ogr')
    if metadata is None:                    # pragma: no cover - broken install
        raise DomainError('The OGR provider is not available.')
    try:
        conn = metadata.createConnection(path, {})
    except Exception as exc:
        raise DomainError('Could not open {}: {}'.format(path, exc))
    if conn is None:
        raise DomainError('Could not open {} as a database.'.format(path))
    return conn


def is_geopackage(path):
    return bool(path) and path.lower().endswith('.gpkg')


def has_capability(conn, name):
    """True when the connection reports capability `name`.

    OGR reports capabilities per GDAL version, so every domain call is guarded
    by one of these rather than assuming the build can do it.
    """
    from qgis.core import QgsAbstractDatabaseProviderConnection as Conn
    flag = getattr(Conn, name, None)
    if flag is None:
        return False
    try:
        return bool(conn.capabilities() & flag)
    except Exception:                       # pragma: no cover
        return False


def capability_report(conn):
    """What this build can actually do, for the dialog to grey things out."""
    return {
        'list': has_capability(conn, 'ListFieldDomains'),
        'read': has_capability(conn, 'RetrieveFieldDomain'),
        'add': has_capability(conn, 'AddFieldDomain'),
        'attach': has_capability(conn, 'SetFieldDomain'),
        'sql': has_capability(conn, 'ExecuteSql'),
    }


def require(conn, name, what):
    if not has_capability(conn, name):
        raise DomainError(
            'This QGIS/GDAL build cannot {}. Field domain support needs '
            'GDAL 3.3 or newer.'.format(what))


# -------------------------------------------------------------- the data model

class DomainSpec(object):
    """One field domain, independent of the container it came from.

    Deliberately a plain object rather than a `QgsFieldDomain` subclass: it has
    to survive JSON round-trips into a library file, and it has to describe a
    range and a coded domain with the same shape so the dialog can hold one
    editor.
    """

    __slots__ = ('name', 'description', 'domain_type', 'field_type', 'values',
                 'min_value', 'max_value', 'min_inclusive', 'max_inclusive',
                 'glob_pattern', 'split_policy', 'merge_policy')

    def __init__(self, name, description='', domain_type=CODED,
                 field_type=T_STRING, values=None, min_value=None,
                 max_value=None, min_inclusive=True, max_inclusive=True,
                 glob_pattern='', split_policy=0, merge_policy=0):
        self.name = name
        self.description = description or ''
        self.domain_type = domain_type
        self.field_type = int(field_type)
        #: list of (code, label) pairs, in the order they should be offered
        self.values = list(values or [])
        self.min_value = min_value
        self.max_value = max_value
        self.min_inclusive = bool(min_inclusive)
        self.max_inclusive = bool(max_inclusive)
        self.glob_pattern = glob_pattern or ''
        self.split_policy = int(split_policy)
        self.merge_policy = int(merge_policy)

    # -- serialisation ------------------------------------------------------

    def to_dict(self):
        data = {
            'name': self.name,
            'description': self.description,
            'domain_type': self.domain_type,
            'field_type': type_key(self.field_type),
            'split_policy': self.split_policy,
            'merge_policy': self.merge_policy,
        }
        if self.domain_type == CODED:
            data['values'] = [[code, label] for code, label in self.values]
        elif self.domain_type == RANGE:
            data.update({
                'min_value': self.min_value,
                'max_value': self.max_value,
                'min_inclusive': self.min_inclusive,
                'max_inclusive': self.max_inclusive,
            })
        else:
            data['glob_pattern'] = self.glob_pattern
        return data

    @classmethod
    def from_dict(cls, data):
        domain_type = str(data.get('domain_type', CODED)).lower()
        if domain_type not in (CODED, RANGE, GLOB):
            raise DomainError(
                'Unknown domain type "{}" for domain "{}".'.format(
                    domain_type, data.get('name', '?')))
        values = []
        for entry in data.get('values') or []:
            if isinstance(entry, dict):
                values.append((entry.get('code'), entry.get('label', '')))
            else:
                pair = list(entry)
                values.append((pair[0], pair[1] if len(pair) > 1 else ''))
        return cls(
            name=data.get('name', ''),
            description=data.get('description', ''),
            domain_type=domain_type,
            field_type=type_from_key(data.get('field_type')),
            values=values,
            min_value=data.get('min_value'),
            max_value=data.get('max_value'),
            min_inclusive=data.get('min_inclusive', True),
            max_inclusive=data.get('max_inclusive', True),
            glob_pattern=data.get('glob_pattern', ''),
            split_policy=data.get('split_policy', 0),
            merge_policy=data.get('merge_policy', 0),
        )

    # -- behaviour ----------------------------------------------------------

    def codes(self):
        return [code for code, _label in self.values]

    def label_for(self, code):
        text = _text(code)
        for existing, label in self.values:
            if _text(existing) == text:
                return label or text
        return None

    def value_map(self):
        """`{label: code}` in the shape QGIS's ValueMap widget config wants."""
        return [{(label or _text(code)): _text(code)} for code, label in self.values]

    def accepts(self, value):
        """``(ok, reason)`` for one attribute value. NULL always passes here;
        whether NULL is allowed is a separate, caller-owned decision."""
        if is_null(value):
            return True, ''

        if self.domain_type == CODED:
            if _text(value) in {_text(c) for c in self.codes()}:
                return True, ''
            return False, 'not one of the {} coded values'.format(len(self.values))

        if self.domain_type == RANGE:
            try:
                number = float(value)
            except (TypeError, ValueError):
                return False, 'not a number, but the domain is a range'
            if self.min_value is not None:
                if number < self.min_value or (
                        number == self.min_value and not self.min_inclusive):
                    return False, 'below the minimum {}'.format(self.min_value)
            if self.max_value is not None:
                if number > self.max_value or (
                        number == self.max_value and not self.max_inclusive):
                    return False, 'above the maximum {}'.format(self.max_value)
            return True, ''

        import fnmatch
        if fnmatch.fnmatchcase(_text(value), self.glob_pattern or '*'):
            return True, ''
        return False, 'does not match the pattern {}'.format(self.glob_pattern)

    def summary(self):
        if self.domain_type == CODED:
            return '{} coded value{}'.format(
                len(self.values), '' if len(self.values) == 1 else 's')
        if self.domain_type == RANGE:
            low = '-inf' if self.min_value is None else self.min_value
            high = 'inf' if self.max_value is None else self.max_value
            return '{}{}, {}{}'.format(
                '[' if self.min_inclusive else '(', low, high,
                ']' if self.max_inclusive else ')')
        return self.glob_pattern or '*'

    def __repr__(self):
        return '<DomainSpec {} ({})>'.format(self.name, self.domain_type)


class ImportReport(object):
    """What `import_library` did, so the caller can show it before or after."""

    def __init__(self):
        self.created = []
        self.updated = []
        self.unchanged = []
        self.deleted = []
        self.attached = []          # (table, field, domain)
        self.skipped = []           # (name, why)
        self.errors = []

    @property
    def total_changes(self):
        return (len(self.created) + len(self.updated) + len(self.deleted)
                + len(self.attached))

    def lines(self):
        out = []
        for label, items in (('created', self.created), ('updated', self.updated),
                             ('unchanged', self.unchanged),
                             ('deleted', self.deleted)):
            if items:
                out.append('{}: {}'.format(label, ', '.join(sorted(items))))
        for table, field, domain in self.attached:
            out.append('attached {} to {}.{}'.format(domain, table, field))
        for name, why in self.skipped:
            out.append('skipped {}: {}'.format(name, why))
        out.extend('ERROR: ' + e for e in self.errors)
        return out


# ------------------------------------------------------------------- reading

def list_domain_names(conn):
    if not has_capability(conn, 'ListFieldDomains'):
        return []
    try:
        return sorted(conn.fieldDomainNames())
    except Exception as exc:                # pragma: no cover
        raise DomainError('Could not list domains: {}'.format(exc))


def read_domain(conn, name):
    """One domain as a `DomainSpec`."""
    require(conn, 'RetrieveFieldDomain', 'read field domains')
    try:
        domain = conn.fieldDomain(name)
    except Exception as exc:
        raise DomainError('Could not read domain "{}": {}'.format(name, exc))
    if domain is None:
        raise DomainError('No domain named "{}".'.format(name))
    return spec_from_qgis(domain)


def list_domains(conn):
    """Every domain in the container, unreadable ones skipped with a log line."""
    specs = []
    for name in list_domain_names(conn):
        try:
            specs.append(read_domain(conn, name))
        except DomainError as exc:
            QgsMessageLog.logMessage(str(exc), LOG_TAG, Qgis.MessageLevel.Warning)
    return specs


def spec_from_qgis(domain):
    """`QgsFieldDomain` -> `DomainSpec`."""
    coded = domain_type_value('Coded')
    range_ = domain_type_value('Range')

    kind = CODED
    with suppress(Exception):               # pragma: no cover
        if domain.type() == range_:
            kind = RANGE
        elif domain.type() != coded:
            kind = GLOB

    spec = DomainSpec(
        name=domain.name(),
        description=domain.description(),
        domain_type=kind,
        field_type=int(domain.fieldType()),
        split_policy=int(domain.splitPolicy()),
        merge_policy=int(domain.mergePolicy()),
    )
    if kind == CODED:
        spec.values = [(v.code(), v.value()) for v in domain.values()]
    elif kind == RANGE:
        spec.min_value = _number_or_none(domain.minimum())
        spec.max_value = _number_or_none(domain.maximum())
        spec.min_inclusive = bool(domain.minimumIsInclusive())
        spec.max_inclusive = bool(domain.maximumIsInclusive())
    else:
        spec.glob_pattern = domain.glob()
    return spec


def spec_to_qgis(spec):
    """`DomainSpec` -> `QgsFieldDomain`, for `addFieldDomain`."""
    from qgis.core import (
        QgsCodedFieldDomain,
        QgsCodedValue,
        QgsGlobFieldDomain,
        QgsRangeFieldDomain,
    )
    field_type = qt_type(spec.field_type)

    if spec.domain_type == CODED:
        values = [QgsCodedValue(_text(code), label or _text(code))
                  for code, label in spec.values]
        domain = QgsCodedFieldDomain(spec.name, spec.description, field_type,
                                     values)
    elif spec.domain_type == RANGE:
        domain = QgsRangeFieldDomain(
            spec.name, spec.description, field_type,
            0 if spec.min_value is None else spec.min_value, spec.min_inclusive,
            0 if spec.max_value is None else spec.max_value, spec.max_inclusive)
    else:
        domain = QgsGlobFieldDomain(spec.name, spec.description, field_type,
                                    spec.glob_pattern or '*')

    with suppress(Exception):               # pragma: no cover - older builds
        from qgis.core import Qgis as _Q
        domain.setSplitPolicy(_Q.FieldDomainSplitPolicy(spec.split_policy))
        domain.setMergePolicy(_Q.FieldDomainMergePolicy(spec.merge_policy))
    return domain


# ---------------------------------------------------------------- attachments

def field_domain_map(path):
    """``{(table, field): domain_name}`` for the whole container.

    Asked of OGR rather than of QGIS, for two reasons. `QgsField` has no
    `domainName()` on the versions this plugin targets — verified absent on
    3.44 — so a loaded layer cannot be asked which domain a field carries. And
    `QgsAbstractDatabaseProviderConnection.executeSql` prepends an implicit FID
    column to results from any table that has one, so `SELECT a, b, c FROM
    gpkg_data_columns` comes back with four values per row and silently
    misaligns. `OGRFieldDefn.GetDomainName()` has neither problem and works for
    a File Geodatabase as well, which has no `gpkg_` tables at all.
    """
    mapping = {}
    dataset = _ogr_open(path)
    if dataset is None:
        return mapping
    try:
        for index in range(dataset.GetLayerCount()):
            layer = dataset.GetLayer(index)
            name = layer.GetName()
            if name in SYSTEM_TABLES or name.startswith(SYSTEM_TABLE_PREFIXES):
                continue
            definition = layer.GetLayerDefn()
            for field_index in range(definition.GetFieldCount()):
                field = definition.GetFieldDefn(field_index)
                domain = field.GetDomainName()
                if domain:
                    mapping[(name, field.GetName())] = domain
    finally:
        dataset = None
    return mapping


def usage_counts(path):
    """``{domain_name: number of fields using it}``."""
    counts = {}
    for domain in field_domain_map(path).values():
        counts[domain] = counts.get(domain, 0) + 1
    return counts


def _ogr_open(path, update=False):
    """A read-only OGR dataset, or None. Never raises.

    GDAL's Python bindings raise rather than return None once QGIS has enabled
    exceptions, so every open has to be guarded — the same treatment
    `GeoPackage_Data_Manager.open_ds` gives it.
    """
    from osgeo import gdal
    if not path or not os.path.exists(path):
        return None
    flags = gdal.OF_VECTOR | (gdal.OF_UPDATE if update else 0)
    gdal.PushErrorHandler('CPLQuietErrorHandler')
    try:
        try:
            return gdal.OpenEx(path, flags)
        except Exception:
            return None
    finally:
        gdal.PopErrorHandler()


def attach_domain(conn, table, field, domain_name):
    require(conn, 'SetFieldDomain', 'attach field domains')
    try:
        conn.setFieldDomainName(field, '', table, domain_name)
    except Exception as exc:
        raise DomainError('Could not attach "{}" to {}.{}: {}'.format(
            domain_name, table, field, exc))


def detach_domain(conn, table, field, gpkg_path):
    """Clear the domain from one field.

    There is no `clearFieldDomainName()`, and passing an empty name through
    `setFieldDomainName` is not accepted by every GDAL build, so this goes
    straight at `gpkg_data_columns` - through `sqlite3` with bound parameters,
    the same route `delete_domain` and `rename_domain` take. The provider
    connection's `executeSql` has no parameter binding, which would leave the
    table and field names to be pasted into the statement by hand.
    """
    _require_gpkg(gpkg_path, 'change field domain assignments')
    try:
        with _sqlite(gpkg_path) as db:
            db.execute('UPDATE gpkg_data_columns SET constraint_name = NULL '
                       'WHERE table_name = ? AND column_name = ?',
                       (table, field))
    except Exception as exc:
        raise DomainError('Could not detach the domain from {}.{}: {}'.format(
            table, field, exc))


def tables_and_fields(conn, progress=None):
    """``[(table, [field, ...]), ...]`` for the user-facing layers only.

    `conn.fields()` opens the layer, so this costs a fixed slice of time per
    layer and a container with a hundred of them takes visible seconds. Pass
    `progress` - a callable taking ``(done, total, layer_name)`` - to hear
    about each one as it lands, which is what lets a caller keep a progress
    bar moving instead of freezing for the whole read.
    """
    result = []
    try:
        tables = conn.tables()
    except Exception as exc:
        raise DomainError('Could not list the layers: {}'.format(exc))

    wanted = [t for t in tables
              if t.tableName() not in SYSTEM_TABLES
              and not t.tableName().startswith(SYSTEM_TABLE_PREFIXES)]
    total = len(wanted)
    for done, table in enumerate(wanted, 1):
        name = table.tableName()
        if progress is not None:
            progress(done, total, name)
        with suppress(Exception):
            fields = [f.name() for f in conn.fields('', name)]
            result.append((name, fields))
    return sorted(result)


# -------------------------------------------------------------------- writing

def create_domain(conn, spec):
    """Add a domain that does not exist yet."""
    require(conn, 'AddFieldDomain', 'create field domains')
    if not spec.name:
        raise DomainError('A domain needs a name.')
    if spec.name in list_domain_names(conn):
        raise DomainError('A domain called "{}" already exists.'.format(spec.name))
    _validate(spec)
    try:
        conn.addFieldDomain(spec_to_qgis(spec), '')
    except Exception as exc:
        raise DomainError('Could not create "{}": {}'.format(spec.name, exc))


def delete_domain(conn, name, gpkg_path):
    """Remove a domain and every reference to it."""
    _require_gpkg(gpkg_path, 'delete a domain')
    with _sqlite(gpkg_path) as db:
        db.execute('UPDATE gpkg_data_columns SET constraint_name = NULL '
                   'WHERE constraint_name = ?', (name,))
        _delete_domain_rows(db, name)


def update_domain(conn, spec, gpkg_path):
    """Rewrite an existing domain's values in place.

    This is the operation QGIS has no API for. It goes through `sqlite3` and a
    real transaction, because `executeSql` on the provider connection does not
    roll back — a half-rewritten enum is worse than a failed edit.

    The link in `gpkg_data_columns` is by name and does not change, so every
    field already using the domain picks the new values up.
    """
    _require_gpkg(gpkg_path, 'edit a domain')
    _validate(spec)
    guard_no_open_edits(gpkg_path)

    with _sqlite(gpkg_path) as db:
        existing = db.execute(
            'SELECT constraint_name FROM gpkg_data_column_constraints '
            'WHERE constraint_name = ?', (spec.name,)).fetchone()
        if existing is None:
            raise DomainError(
                'No domain named "{}" in this GeoPackage.'.format(spec.name))
        _delete_domain_rows(db, spec.name)
        _insert_domain_rows(db, spec)


def rename_domain(conn, old_name, spec, gpkg_path):
    """Write `spec` under its new name and move every attachment across."""
    _require_gpkg(gpkg_path, 'rename a domain')
    _validate(spec)
    guard_no_open_edits(gpkg_path)
    if spec.name != old_name and spec.name in list_domain_names(conn):
        raise DomainError('A domain called "{}" already exists.'.format(spec.name))

    with _sqlite(gpkg_path) as db:
        _delete_domain_rows(db, old_name)
        _insert_domain_rows(db, spec)
        db.execute('UPDATE gpkg_data_columns SET constraint_name = ? '
                   'WHERE constraint_name = ?', (spec.name, old_name))


def write_domain(conn, spec, gpkg_path, original_name=None):
    """Create, update or rename as appropriate. The dialog's single Save path."""
    known = list_domain_names(conn)
    if original_name and original_name != spec.name:
        if original_name not in known:
            raise DomainError('No domain named "{}".'.format(original_name))
        rename_domain(conn, original_name, spec, gpkg_path)
    elif spec.name in known:
        update_domain(conn, spec, gpkg_path)
    else:
        create_domain(conn, spec)


# ------------------------------------------------------- the GeoPackage tables

def _delete_domain_rows(db, name):
    """Every constraint row for `name`, including GDAL's description row.

    GDAL stores a domain's description in a companion row whose
    `constraint_name` is `_<name>_domain_description`, so deleting only the
    rows named `<name>` leaves that behind and the description reappears on a
    domain that no longer has one.
    """
    db.execute('DELETE FROM gpkg_data_column_constraints '
               'WHERE constraint_name IN (?, ?)',
               (name, _description_row_name(name)))


def _insert_domain_rows(db, spec):
    description = spec.description or None

    if spec.domain_type == CODED:
        if description:
            # Mirrors GDAL's own layout: an "enum" row with an empty value that
            # exists only to carry the description.
            db.execute(
                'INSERT INTO gpkg_data_column_constraints '
                '(constraint_name, constraint_type, value, description) '
                'VALUES (?, ?, ?, ?)',
                (_description_row_name(spec.name), 'enum', '', description))
        for code, label in spec.values:
            db.execute(
                'INSERT INTO gpkg_data_column_constraints '
                '(constraint_name, constraint_type, value, description) '
                'VALUES (?, ?, ?, ?)',
                (spec.name, 'enum', _text(code), label or _text(code)))

    elif spec.domain_type == RANGE:
        db.execute(
            'INSERT INTO gpkg_data_column_constraints '
            '(constraint_name, constraint_type, min, min_is_inclusive, '
            ' max, max_is_inclusive, description) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (spec.name, 'range', spec.min_value, 1 if spec.min_inclusive else 0,
             spec.max_value, 1 if spec.max_inclusive else 0, description))

    else:
        db.execute(
            'INSERT INTO gpkg_data_column_constraints '
            '(constraint_name, constraint_type, value, description) '
            'VALUES (?, ?, ?, ?)',
            (spec.name, 'glob', spec.glob_pattern or '*', description))


def _description_row_name(name):
    return '_{}_domain_description'.format(name)


@contextmanager
def _sqlite(path):
    """A GeoPackage open for writing, in one all-or-nothing transaction."""
    db = sqlite3.connect(path)
    try:
        db.execute('PRAGMA foreign_keys = ON')
        _ensure_constraint_tables(db)
        yield db
    except sqlite3.Error as exc:
        db.rollback()
        raise DomainError('The GeoPackage rejected the change: {}'.format(exc))
    except Exception:
        db.rollback()
        raise
    else:
        db.commit()
    finally:
        db.close()


def _ensure_constraint_tables(db):
    """Create the two standard tables when the GPKG has never held a domain.

    Both are defined by the GeoPackage Schema extension; GDAL creates them
    lazily, so a container with no domains yet simply does not have them.
    """
    db.execute("""
        CREATE TABLE IF NOT EXISTS gpkg_data_columns (
            table_name TEXT NOT NULL,
            column_name TEXT NOT NULL,
            name TEXT,
            title TEXT,
            description TEXT,
            mime_type TEXT,
            constraint_name TEXT,
            CONSTRAINT pk_gdc PRIMARY KEY (table_name, column_name))""")
    db.execute("""
        CREATE TABLE IF NOT EXISTS gpkg_data_column_constraints (
            constraint_name TEXT NOT NULL,
            constraint_type TEXT NOT NULL,
            value TEXT,
            min NUMERIC,
            min_is_inclusive BOOLEAN,
            max NUMERIC,
            max_is_inclusive BOOLEAN,
            description TEXT,
            CONSTRAINT gdcc_ntv UNIQUE (constraint_name, constraint_type, value))""")


def _require_gpkg(path, what):
    if not is_geopackage(path):
        raise DomainError(
            'Only a GeoPackage can {}. A File Geodatabase can be read and its '
            'domains applied elsewhere, but not edited.'.format(what))
    if not os.path.exists(path):
        raise DomainError('{} does not exist.'.format(path))


def guard_no_open_edits(path):
    """Refuse to write while a layer from this container is being edited.

    Two write handles on one GeoPackage will corrupt the edit buffer, and the
    user loses work that QGIS still believes it is holding. Better to stop.
    """
    try:
        layers = QgsProject.instance().mapLayers().values()
    except Exception:                       # pragma: no cover - no project
        return
    target = os.path.normcase(os.path.abspath(path))
    for layer in layers:
        if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
            continue
        source = layer.source().split('|')[0]
        if not source:
            continue
        if os.path.normcase(os.path.abspath(source)) != target:
            continue
        if layer.isEditable() and layer.isModified():
            raise DomainError(
                'The layer "{}" has unsaved edits and comes from this '
                'GeoPackage. Save or discard them first — writing now would '
                'corrupt the edit buffer.'.format(layer.name()))


def _validate(spec):
    if not spec.name:
        raise DomainError('A domain needs a name.')
    if spec.name.startswith('_'):
        raise DomainError(
            'A domain name cannot start with an underscore; GDAL reserves that '
            'prefix for its own description rows.')
    if spec.domain_type == CODED:
        if not spec.values:
            raise DomainError(
                'The coded domain "{}" has no values.'.format(spec.name))
        seen = set()
        for code, _label in spec.values:
            text = _text(code)
            if text in seen:
                raise DomainError(
                    'The code "{}" appears twice in "{}".'.format(text, spec.name))
            seen.add(text)
    elif spec.domain_type == RANGE:
        if spec.min_value is None and spec.max_value is None:
            raise DomainError(
                'The range domain "{}" needs a minimum, a maximum, or '
                'both.'.format(spec.name))
        if (spec.min_value is not None and spec.max_value is not None
                and spec.min_value > spec.max_value):
            raise DomainError(
                'The minimum is above the maximum in "{}".'.format(spec.name))
    elif not spec.glob_pattern:
        raise DomainError('The glob domain "{}" has no pattern.'.format(spec.name))


# -------------------------------------------------------------------- library

def export_library(conn, path, include_assignments=True):
    """The container's domains as a JSON-serialisable dict."""
    data = {
        'format': LIBRARY_FORMAT,
        'generator': 'KGA Toolbox',
        'domains': [spec.to_dict() for spec in list_domains(conn)],
    }
    if include_assignments:
        data['assignments'] = [
            {'table': table, 'field': field, 'domain': domain}
            for (table, field), domain in sorted(field_domain_map(path).items())
        ]
    return data


def load_library(path):
    """Read and sanity-check a library file."""
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise DomainError('Could not read {}: {}'.format(path, exc))
    if not isinstance(data, dict) or 'domains' not in data:
        raise DomainError('{} is not a KGA domain library.'.format(path))
    version = data.get('format', 1)
    if version > LIBRARY_FORMAT:
        raise DomainError(
            'That library was written by a newer KGA Toolbox (format {}, this '
            'build reads {}).'.format(version, LIBRARY_FORMAT))
    return data


def save_library(data, path):
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False, sort_keys=False)
    return path


def preview_library(conn, data, path=None, mode=MODE_MERGE,
                    attach_by_name=False):
    """What `import_library` would do, without doing it."""
    return _apply_library(conn, data, path, mode, attach_by_name, dry_run=True)


def import_library(conn, data, gpkg_path, mode=MODE_MERGE, attach_by_name=True,
                   dry_run=False):
    """Bring a library's domains into this container.

    `merge` adds and updates, leaving domains the library does not mention
    alone. `replace` additionally deletes those, which is what makes a
    deliverable match a template exactly.
    """
    return _apply_library(conn, data, gpkg_path, mode, attach_by_name, dry_run)


def _apply_library(conn, data, gpkg_path, mode, attach_by_name, dry_run):
    report = ImportReport()
    specs = []
    for entry in data.get('domains') or []:
        try:
            specs.append(DomainSpec.from_dict(entry))
        except DomainError as exc:
            report.errors.append(str(exc))
    if report.errors:
        return report

    existing = {}
    for name in list_domain_names(conn):
        try:
            existing[name] = read_domain(conn, name)
        except DomainError:
            existing[name] = None

    for spec in specs:
        current = existing.get(spec.name)
        if spec.name not in existing:
            report.created.append(spec.name)
            if not dry_run:
                try:
                    create_domain(conn, spec)
                except DomainError as exc:
                    report.errors.append(str(exc))
        elif current is not None and current.to_dict() == spec.to_dict():
            report.unchanged.append(spec.name)
        else:
            report.updated.append(spec.name)
            if not dry_run:
                try:
                    update_domain(conn, spec, gpkg_path)
                except DomainError as exc:
                    report.errors.append(str(exc))

    if mode == MODE_REPLACE:
        keep = {s.name for s in specs}
        for name in existing:
            if name in keep:
                continue
            report.deleted.append(name)
            if not dry_run:
                try:
                    delete_domain(conn, name, gpkg_path)
                except DomainError as exc:
                    report.errors.append(str(exc))

    _apply_assignments(conn, gpkg_path, data, specs, report, attach_by_name,
                       dry_run)
    return report


def _apply_assignments(conn, path, data, specs, report, attach_by_name, dry_run):
    current = field_domain_map(path) if path else {}
    known = {spec.name for spec in specs}
    wanted = []

    for entry in data.get('assignments') or []:
        table, field = entry.get('table'), entry.get('field')
        domain = entry.get('domain')
        if not (table and field and domain in known):
            continue
        wanted.append((table, field, domain))

    if attach_by_name:
        # The same field name recurs across layers in practice (status, owner,
        # material), so a library recorded on one layer is worth applying to
        # every layer that has the same column.
        by_field = {}
        for _table, field, domain in wanted:
            by_field.setdefault(field, domain)
        for table, fields in tables_and_fields(conn):
            for field in fields:
                domain = by_field.get(field)
                if domain is not None:
                    wanted.append((table, field, domain))

    seen = set()
    for table, field, domain in wanted:
        if (table, field) in seen:
            continue
        seen.add((table, field))
        if current.get((table, field)) == domain:
            continue
        report.attached.append((table, field, domain))
        if not dry_run:
            try:
                attach_domain(conn, table, field, domain)
            except DomainError as exc:
                report.errors.append(str(exc))


# ----------------------------------------------------------------- validation

def validate_layer(layer, specs_by_field, fail_on_null=False):
    """Every value in `layer` that its field's domain rejects.

    `specs_by_field` maps a field name to a `DomainSpec`. Returns a list of
    `Violation`, the same model the future Data Reviewer will consume.
    """
    violations = []
    if not specs_by_field:
        return violations

    indexes = []
    for name, spec in specs_by_field.items():
        index = layer.fields().indexOf(name)
        if index >= 0:
            indexes.append((index, name, spec))
    if not indexes:
        return violations

    for feature in layer.getFeatures():
        for index, name, spec in indexes:
            value = feature.attribute(index)
            if is_null(value):
                if fail_on_null:
                    violations.append(Violation(
                        layer.name(), feature.id(), name, None, spec.name,
                        'empty, and empty values are not allowed'))
                continue
            ok, reason = spec.accepts(value)
            if not ok:
                violations.append(Violation(
                    layer.name(), feature.id(), name, value, spec.name, reason))
    return violations


# ------------------------------------------------------------ layer refreshing

def refresh_layer_widgets(layer, specs_by_field):
    """Rewrite the editor widget config from the current domains.

    A layer already open in the project keeps whatever widget setup it read when
    it loaded, so an edited domain does not reach the attribute form until QGIS
    restarts. Rewriting the setup here is what makes an edit show up straight
    away, which is the difference between the tool feeling live and feeling
    broken. Called at the end of every write.
    """
    from qgis.core import QgsEditorWidgetSetup

    changed = 0
    for name, spec in (specs_by_field or {}).items():
        index = layer.fields().indexOf(name)
        if index < 0:
            continue

        if spec.domain_type == CODED:
            setup = QgsEditorWidgetSetup('ValueMap', {'map': spec.value_map()})
        elif spec.domain_type == RANGE:
            config = {'Style': 'SpinBox', 'AllowNull': True}
            if spec.min_value is not None:
                config['Min'] = spec.min_value
            if spec.max_value is not None:
                config['Max'] = spec.max_value
            if spec.field_type in (T_INT, T_LONGLONG):
                config['Precision'] = 0
            setup = QgsEditorWidgetSetup('Range', config)
        else:
            setup = QgsEditorWidgetSetup('TextEdit', {})

        layer.setEditorWidgetSetup(index, setup)
        changed += 1

    if changed:
        layer.triggerRepaint()
    return changed


def refresh_project_layers(gpkg_path):
    """Re-read domains for every loaded layer that comes from this container."""
    try:
        conn = connection_for(gpkg_path)
    except DomainError:
        return 0

    specs = {spec.name: spec for spec in list_domains(conn)}
    assignments = field_domain_map(gpkg_path)
    target = os.path.normcase(os.path.abspath(gpkg_path))
    touched = 0

    for layer in QgsProject.instance().mapLayers().values():
        if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
            continue
        source = layer.source()
        path = source.split('|')[0]
        if not path or os.path.normcase(os.path.abspath(path)) != target:
            continue
        table = _layer_name_from_uri(source) or layer.name()
        by_field = {}
        for (a_table, field), domain in assignments.items():
            if a_table == table and domain in specs:
                by_field[field] = specs[domain]
        if by_field and refresh_layer_widgets(layer, by_field):
            touched += 1
    return touched


def _layer_name_from_uri(uri):
    for part in uri.split('|')[1:]:
        if part.lower().startswith('layername='):
            return part.split('=', 1)[1]
    return None


# ------------------------------------------------------------------- plumbing

def _text(value):
    if value is None:
        return ''
    return value if isinstance(value, str) else str(value)


def _number_or_none(value):
    try:
        if value is None:
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number

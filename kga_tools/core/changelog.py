# -*- coding: utf-8 -*-
"""The change-report model shared by the append, Excel and domain tools.

Three tools need to say the same three things — "here is what I read", "here is
what I would change", "here is what went wrong" — and every one of them defaults
to a dry run, so the report *is* the product until the user opts in to writing.
Defining it once means the HTML looks the same whichever tool produced it, and
the violations table has one schema that the future Data Reviewer can reuse.

Nothing here imports Qt widgets, so it is safe to use from a head-less
Processing run.
"""

import html
import os
from collections import OrderedDict

from .compat import T_INT, T_STRING, make_field

#: Columns of the violations sink, in order. Reused by
#: `validate_against_domains` and anything else that reports per-feature
#: problems, so a user can union two reports without re-mapping fields.
VIOLATION_FIELDS = (
    ('layer', T_STRING, 254),
    ('feature_id', T_INT, 0),
    ('field', T_STRING, 254),
    ('value', T_STRING, 500),
    ('domain', T_STRING, 254),
    ('reason', T_STRING, 500),
)


def violation_fields():
    """QgsFields for the violations sink."""
    from qgis.core import QgsFields
    fields = QgsFields()
    for name, type_id, length in VIOLATION_FIELDS:
        fields.append(make_field(name, type_id, length=length))
    return fields


class Violation(object):
    """One rule broken by one attribute of one feature."""

    __slots__ = ('layer', 'feature_id', 'field', 'value', 'domain', 'reason')

    def __init__(self, layer, feature_id, field, value, domain, reason):
        self.layer = layer
        self.feature_id = feature_id
        self.field = field
        self.value = value
        self.domain = domain
        self.reason = reason

    def as_row(self):
        """Attribute list in `VIOLATION_FIELDS` order."""
        return [self.layer, int(self.feature_id), self.field,
                _as_text(self.value), self.domain, self.reason]

    def __repr__(self):
        return '<Violation {}.{} #{}: {}>'.format(
            self.layer, self.field, self.feature_id, self.reason)


class FieldChange(object):
    """One attribute moving from `old` to `new` on one feature."""

    __slots__ = ('key', 'feature_id', 'field', 'old', 'new', 'note')

    def __init__(self, key, feature_id, field, old, new, note=''):
        self.key = key
        self.feature_id = feature_id
        self.field = field
        self.old = old
        self.new = new
        self.note = note


class ChangeReport(object):
    """Accumulator for one run of one tool, and its HTML rendering.

    Counters are deliberately open-ended (`count(name)`) rather than fixed
    attributes: the append tool and the Excel tool count different things, and
    a shared fixed schema would only ever be a lowest common denominator.
    """

    #: How many example rows any one table shows before it is truncated.
    EXAMPLE_LIMIT = 25

    def __init__(self, title, subtitle='', dry_run=True):
        self.title = title
        self.subtitle = subtitle
        self.dry_run = dry_run
        self.counters = OrderedDict()
        self.changes = []
        self.violations = []
        self.warnings = []
        self.errors = []
        self.notes = []
        # field name -> {'count': n, 'examples': [(old, new, why), ...]}
        self.coercions = OrderedDict()
        self.sections = []      # (heading, [(col, ...)], [rows], note)

    # ---------------------------------------------------------- accumulation

    def count(self, name, delta=1):
        self.counters[name] = self.counters.get(name, 0) + delta

    def set_count(self, name, value):
        self.counters[name] = value

    def add_change(self, change):
        self.changes.append(change)

    def add_violation(self, violation):
        self.violations.append(violation)

    def warn(self, message):
        if message not in self.warnings:
            self.warnings.append(message)

    def error(self, message):
        self.errors.append(message)

    def note(self, message):
        self.notes.append(message)

    def add_coercion(self, field, old, new, why):
        """Record one value that changed shape on the way in.

        Every lossy conversion goes through here; the guide's rule is that no
        coercion is ever silent, and this is the one place that guarantees it.
        """
        entry = self.coercions.setdefault(field, {'count': 0, 'examples': []})
        entry['count'] += 1
        if len(entry['examples']) < self.EXAMPLE_LIMIT:
            entry['examples'].append((old, new, why))

    def add_table(self, heading, columns, rows, note=''):
        """A free-form table a tool wants in its report."""
        self.sections.append((heading, list(columns), list(rows), note))

    # ------------------------------------------------------------- rendering

    def summary_lines(self):
        """Short lines for `feedback.pushInfo`, in report order."""
        lines = ['{}: {}'.format(k, v) for k, v in self.counters.items()]
        for field, entry in self.coercions.items():
            lines.append('coerced {}: {}'.format(field, entry['count']))
        lines.extend('WARNING: ' + w for w in self.warnings)
        lines.extend('ERROR: ' + e for e in self.errors)
        return lines

    def to_html(self):
        parts = [_HTML_HEAD.format(title=html.escape(self.title))]
        parts.append('<h1>{}</h1>'.format(html.escape(self.title)))
        if self.subtitle:
            parts.append('<p class="sub">{}</p>'.format(html.escape(self.subtitle)))

        if self.dry_run:
            parts.append(
                '<p class="banner dry">Dry run - nothing was written. '
                'Turn off <b>Dry run</b> to apply these changes.</p>')
        else:
            parts.append('<p class="banner live">Changes were written.</p>')

        if self.errors:
            parts.append(_list_block('Errors', self.errors, 'err'))
        if self.warnings:
            parts.append(_list_block('Warnings', self.warnings, 'warn'))

        if self.counters:
            parts.append('<h2>Summary</h2>')
            parts.append(_table(
                ['Item', 'Count'],
                [[k, v] for k, v in self.counters.items()]))

        if self.coercions:
            parts.append('<h2>Type coercions</h2>')
            parts.append(
                '<p class="sub">Values whose shape changed on the way in. '
                'Anything listed here lost or gained information.</p>')
            rows = []
            for field, entry in self.coercions.items():
                examples = '; '.join(
                    '{} &rarr; {} <span class="why">({})</span>'.format(
                        _short(o), _short(n), html.escape(w))
                    for o, n, w in entry['examples'][:5])
                rows.append([field, entry['count'], examples])
            parts.append(_table(['Field', 'Count', 'Examples'], rows, raw_cols=(2,)))

        if self.changes:
            parts.append('<h2>Changed values</h2>')
            shown = self.changes[:self.EXAMPLE_LIMIT]
            rows = [[c.key, c.field, c.old, c.new, c.note] for c in shown]
            note = ''
            if len(self.changes) > len(shown):
                note = 'Showing the first {} of {} changes.'.format(
                    len(shown), len(self.changes))
            parts.append(_table(['Key', 'Field', 'Old', 'New', 'Note'], rows,
                                value_cols=(2, 3)))
            if note:
                parts.append('<p class="sub">{}</p>'.format(html.escape(note)))

        if self.violations:
            parts.append('<h2>Violations</h2>')
            shown = self.violations[:self.EXAMPLE_LIMIT]
            rows = [[v.layer, v.feature_id, v.field, v.value,
                     v.domain, v.reason] for v in shown]
            parts.append(_table(
                ['Layer', 'Feature', 'Field', 'Value', 'Domain', 'Reason'],
                rows, value_cols=(3,)))
            if len(self.violations) > len(shown):
                parts.append('<p class="sub">Showing the first {} of {}.</p>'.format(
                    len(shown), len(self.violations)))

        for heading, columns, rows, note in self.sections:
            parts.append('<h2>{}</h2>'.format(html.escape(heading)))
            if note:
                parts.append('<p class="sub">{}</p>'.format(html.escape(note)))
            parts.append(_table(columns, rows))

        if self.notes:
            parts.append(_list_block('Notes', self.notes, 'note'))

        parts.append('<p class="foot">KGA Toolbox</p></body></html>')
        return '\n'.join(parts)

    def write_html(self, path):
        """Write the report, returning the path, or '' when there is none."""
        if not path:
            return ''
        directory = os.path.dirname(path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write(self.to_html())
        return path

    def push_to(self, feedback):
        """Echo the summary into the Processing log."""
        for line in self.summary_lines():
            feedback.pushInfo(line)


# ------------------------------------------------------------------ helpers

def _as_text(value):
    if value is None:
        return ''
    try:
        from qgis.PyQt.QtCore import QVariant
        if isinstance(value, QVariant) and value.isNull():
            return ''
    except ImportError:                     # pragma: no cover
        pass
    return str(value)


def _short(value, limit=60, null_marker=True):
    """Escaped, length-capped cell text. Escapes exactly once — callers must
    hand `_table` raw values, never the output of this."""
    text = _as_text(value)
    if text == '':
        return '&lt;null&gt;' if null_marker else ''
    if len(text) > limit:
        text = text[:limit - 1] + '…'
    return html.escape(text)


def _table(columns, rows, raw_cols=(), value_cols=()):
    """Render a table.

    `raw_cols` are already-escaped HTML. `value_cols` are attribute values, so
    an empty one is shown as <null> rather than as a blank cell; every other
    column leaves a blank as a blank.
    """
    out = ['<table><thead><tr>']
    out.extend('<th>{}</th>'.format(html.escape(str(c))) for c in columns)
    out.append('</tr></thead><tbody>')
    for row in rows:
        out.append('<tr>')
        for index, cell in enumerate(row):
            if index in raw_cols:
                text = cell
            else:
                text = _short(cell, 200, null_marker=index in value_cols)
            out.append('<td>{}</td>'.format(text))
        out.append('</tr>')
    out.append('</tbody></table>')
    return ''.join(out)


def _list_block(heading, items, css_class):
    body = ''.join('<li>{}</li>'.format(html.escape(str(i))) for i in items)
    return '<h2>{}</h2><ul class="{}">{}</ul>'.format(
        html.escape(heading), css_class, body)


_HTML_HEAD = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>
 body {{ font-family: "Segoe UI", Arial, sans-serif; font-size: 13px;
         color: #10333B; margin: 18px; }}
 h1 {{ font-size: 19px; margin: 0 0 2px; color: #10333B; }}
 h2 {{ font-size: 15px; margin: 20px 0 6px; color: #0E7C86;
       border-bottom: 1px solid #cfe1e3; padding-bottom: 3px; }}
 p.sub {{ color: #5a6f75; margin: 2px 0 10px; }}
 p.foot {{ color: #92a4a8; margin-top: 24px; font-size: 11px; }}
 span.why {{ color: #92a4a8; }}
 .banner {{ padding: 7px 10px; border-radius: 3px; font-weight: bold; }}
 .banner.dry {{ background: #fdf3d8; border: 1px solid #e3c96b; }}
 .banner.live {{ background: #e2f2ea; border: 1px solid #7bbd9b; }}
 table {{ border-collapse: collapse; margin: 6px 0 4px; }}
 th, td {{ border: 1px solid #cfe1e3; padding: 3px 8px; text-align: left;
           vertical-align: top; }}
 th {{ background: #eef6f7; color: #10333B; }}
 ul.err li {{ color: #a3251b; }}
 ul.warn li {{ color: #9a6b00; }}
 ul.note li {{ color: #5a6f75; }}
</style></head><body>
"""

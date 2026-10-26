# -*- coding: utf-8 -*-
"""Shortcuts to a project's print layouts, kept in the workspace's Layouts folder.

QGIS stores print layouts inside the project file, so the Layouts folder of a
workspace is empty on disk. For each layout this module keeps a small
`<layout name>.kgalayout` file there (JSON: layout name and project file), which
the Catalog lists and opens like a shortcut. Files are only ever written for
layouts that exist and removed when the layout is gone; the shortcut files are
the only thing in the folder this module touches.
"""

import json
import os
import re

EXTENSION = '.kgalayout'
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def file_name(layout_name, taken=(), extension=EXTENSION, fallback='Layout'):
    """A safe shortcut file name for `layout_name`, unique among `taken`.

    `extension` and `fallback` let `map_shortcuts` share this code for maps.
    """
    base = _ILLEGAL.sub('_', layout_name).strip(' .') or fallback
    name, number = base + extension, 2
    used = {t.lower() for t in taken}
    while name.lower() in used:
        name = '{} ({}){}'.format(base, number, extension)
        number += 1
    return name


def read_data(path):
    """The JSON of a shortcut as a dict, or {} if unreadable."""
    try:
        with open(path, encoding='utf-8') as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def read(path, key='layout'):
    """(name, project file) from a shortcut, or ('', '') if unreadable."""
    data = read_data(path)
    return str(data.get(key, '')), str(data.get('project', ''))


def sync(folder, project_file, layout_names, extension=EXTENSION,
         key='layout', extras=None, fallback='Layout'):
    """Make `folder` hold exactly one shortcut per name; True if anything changed.

    Shortcuts that belong to another project are left alone. `extras` maps a
    name to more fields to store (a map's id).
    """
    extras = extras or {}
    if not folder or not os.path.isdir(folder):
        return False
    project_key = os.path.normcase(os.path.normpath(project_file))
    here = os.path.normcase(os.path.dirname(os.path.normpath(project_file)))
    existing = {}               # file name -> layout name, for this project
    others = []
    for entry in os.listdir(folder):
        if not entry.lower().endswith(extension):
            continue
        layout, project = read(os.path.join(folder, entry), key)
        # only a project saved in this same folder can share it; a shortcut naming one
        # elsewhere was copied here (a shared or imported workspace) and is adopted
        if project and os.path.normcase(os.path.normpath(project)) != project_key \
                and os.path.normcase(os.path.dirname(os.path.normpath(project))) == here:
            others.append(entry)
        else:
            existing[entry] = layout
    changed = False
    wanted = {}
    taken = list(others)
    for name in layout_names:
        # keep the file a layout already has so a rename does not shuffle names
        match = next((f for f, n in existing.items()
                      if n == name and f not in wanted), None)
        if match is None:
            match = file_name(name, taken + list(wanted), extension, fallback)
        wanted[match] = name
    for entry in existing:
        if entry not in wanted:
            try:
                os.remove(os.path.join(folder, entry))
                changed = True
            except OSError:
                pass
    for entry, name in wanted.items():
        path = os.path.join(folder, entry)
        record = dict({key: name, 'project': project_file}, **extras.get(name, {}))
        if (existing.get(entry) == name and os.path.isfile(path)
                and read_data(path) == record):
            continue
        try:
            with open(path, 'w', encoding='utf-8') as handle:
                json.dump(record, handle, indent=2)
            changed = True
        except OSError:
            pass
    return changed

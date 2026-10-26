# -*- coding: utf-8 -*-
"""The KGA workspace: the folder a project keeps everything in.

ArcGIS Pro makes a project folder holding the `.aprx`, a default geodatabase and
a default toolbox. A KGA workspace is the same idea for QGIS:

    <location>/<Name>/
        <Name>.qgz        the QGIS project
        Default.gpkg      the default database
        Toolbox/          script tools, read by Processing as a scripts folder
        Layouts/          somewhere to keep layout templates
        .kgaworkspace     marker + the folders / databases attached to it

Nothing here touches the GUI. The marker is what lets *Open Workspace* (and
opening a `.qgz` that lives in a workspace) recognise a folder, and it is where
the folders and databases a user attached are remembered per project.
"""

import json
import os
import re
import shutil
import sqlite3
import time
from contextlib import closing, suppress
from datetime import datetime

from qgis.PyQt.QtCore import QStandardPaths

from . import kga_settings

MARKER = '.kgaworkspace'
DEFAULT_DB = 'Default.gpkg'
TOOLBOX_DIR = 'Toolbox'
LAYOUTS_DIR = 'Layouts'
MAPS_DIR = 'Maps'
PROJECT_EXTENSION = '.qgz'
UNTITLED = 'Untitled'
LOCATION_NAME = 'KGA Toolbox Project'
MARKER_VERSION = 1
SCRATCH_DIR = '_scratch'        # under the standard location; holds Untitled work
PROMOTED = '.promoted'          # left in a scratch whose work moved to a workspace
SCRATCH_KEEP_DAYS = 7           # unrecovered scratch work is cleared after this
EMPTY_GRACE_MINUTES = 60        # an empty scratch this young may be another session's

_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {'con', 'prn', 'aux', 'nul'} | \
    {'com{}'.format(i) for i in range(1, 10)} | \
    {'lpt{}'.format(i) for i in range(1, 10)}


class WorkspaceError(Exception):
    """A workspace could not be created or read; the message is for the user."""


def documents_dir():
    """The user's Documents folder, falling back to the home folder."""
    found = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.DocumentsLocation)
    return found or os.path.expanduser('~')


def factory_location():
    """`Documents\\KGA Toolbox Project`, the standard home of new workspaces."""
    return os.path.join(documents_dir(), LOCATION_NAME)


def default_location():
    """Where workspaces go unless told otherwise: the Settings choice, else the standard."""
    return kga_settings.projects_folder() or factory_location()


def name_problem(name):
    """Why `name` cannot be a workspace / project name, or '' if it can."""
    text = (name or '').strip()
    if not text:
        return 'Enter a project name.'
    if _BAD_CHARS.search(text):
        return 'The name cannot contain any of  < > : " / \\ | ? *'
    if text.endswith('.'):
        return 'The name cannot end with a dot.'
    if text.lower() in _RESERVED:
        return '"{}" is a reserved name on Windows.'.format(text)
    if len(text) > 100:
        return 'The name is too long (100 characters at most).'
    return ''


def _norm(path):
    return os.path.normpath(os.path.abspath(path))


class Workspace(object):
    """One workspace folder. Cheap to build; reads / writes only the marker."""

    def __init__(self, root, name=None):
        self.root = _norm(root)
        self.name = name or os.path.basename(self.root)
        self.folders = []        # folders attached to the Project tab
        self.databases = []      # extra databases attached (besides Default)
        self.default_db = None   # the default database, when the user chose one
        self.default_dir = None  # the default folder, when the user chose one

    # ---------------------------------------------------------------- paths

    @property
    def project_file(self):
        return os.path.join(self.root, self.name + PROJECT_EXTENSION)

    @property
    def database(self):
        return os.path.join(self.root, DEFAULT_DB)

    @property
    def toolbox(self):
        return os.path.join(self.root, TOOLBOX_DIR)

    @property
    def layouts(self):
        return os.path.join(self.root, LAYOUTS_DIR)

    @property
    def maps(self):
        return os.path.join(self.root, MAPS_DIR)

    @property
    def marker(self):
        return os.path.join(self.root, MARKER)

    @property
    def is_untitled(self):
        """True for the scratch space (and the old permanent `Untitled` folder)."""
        if self.name != UNTITLED:
            return False
        parent = _norm(os.path.dirname(self.root))
        return parent == _norm(scratch_dir()) or self.is_legacy_untitled

    @property
    def is_legacy_untitled(self):
        """The permanent `Untitled` folder older versions kept in the projects folder."""
        return self.name == UNTITLED and \
            _norm(os.path.dirname(self.root)) == _norm(default_location())

    @property
    def display_name(self):
        return self.name + ' (unsaved)' if self.is_untitled else self.name

    def has_content(self):
        """True when anything beyond the empty scaffold has been put here."""
        for dirpath, _dirs, files in os.walk(self.root):
            at_root = _same(dirpath, self.root)
            for name in files:
                low = name.lower()
                if at_root and name in (MARKER, PROMOTED):
                    continue
                if at_root and low.startswith(DEFAULT_DB.lower()):
                    if low == DEFAULT_DB.lower() and \
                            _geopackage_has_data(os.path.join(dirpath, name)):
                        return True
                    continue            # Default.gpkg, its -wal / -shm
                return True
        return False

    def modified(self):
        """The newest modification time of anything in the folder."""
        newest = 0.0
        for dirpath, _dirs, files in os.walk(self.root):
            for name in files:
                with suppress(OSError):
                    newest = max(newest, os.path.getmtime(
                        os.path.join(dirpath, name)))
        if not newest:
            with suppress(OSError):
                newest = os.path.getmtime(self.root)
        return newest

    def __eq__(self, other):
        return isinstance(other, Workspace) and \
            os.path.normcase(self.root) == os.path.normcase(other.root)

    def __hash__(self):
        return hash(os.path.normcase(self.root))

    def __repr__(self):
        return 'Workspace({!r})'.format(self.root)

    # ---------------------------------------------------------- scaffolding

    def scaffold(self):
        """Create whatever is missing: folders, Default.gpkg, the marker."""
        try:
            for folder in (self.root, self.toolbox, self.layouts, self.maps):
                os.makedirs(folder, exist_ok=True)
            if not os.path.exists(self.database):
                create_empty_geopackage(self.database)
            if not os.path.exists(self.marker):
                self.save()
        except OSError as error:
            raise WorkspaceError(
                'Could not create the workspace in {}: {}'.format(
                    self.root, error))
        return self

    # --------------------------------------------------------------- marker

    def save(self):
        data = {'version': MARKER_VERSION, 'name': self.name,
                'folders': self.folders, 'databases': self.databases,
                'default_database': self.default_db,
                'default_folder': self.default_dir}
        try:
            with open(self.marker, 'w', encoding='utf-8') as handle:
                json.dump(data, handle, indent=2)
        except OSError as error:
            raise WorkspaceError(
                'Could not write {}: {}'.format(self.marker, error))

    def load(self):
        """Read the marker. A missing or damaged one just leaves the defaults."""
        try:
            with open(self.marker, encoding='utf-8') as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return self
        if not isinstance(data, dict):
            return self
        if isinstance(data.get('name'), str) and data['name'].strip():
            self.name = data['name'].strip()
        self.folders = _clean_paths(data.get('folders'))
        self.databases = _clean_paths(data.get('databases'))
        self.default_db = _first_path(data.get('default_database'))
        self.default_dir = _first_path(data.get('default_folder'))
        return self

    # ------------------------------------------------- attached folders / dbs

    def attach_folder(self, path):
        """Attach a folder. False if it is already there or is the workspace."""
        path = _norm(path)
        if not os.path.isdir(path) or _same(path, self.root) \
                or any(_same(path, known) for known in self.folders):
            return False
        self.folders.append(path)
        self.save()
        return True

    def detach_folder(self, path):
        kept = [known for known in self.folders if not _same(known, path)]
        changed = len(kept) != len(self.folders)
        self.folders = kept
        if self.default_dir and _same(self.default_dir, path):
            self.default_dir = None
            changed = True
        if changed:
            self.save()
        return changed

    def attach_database(self, path):
        path = _norm(path)
        if not os.path.isfile(path) or _same(path, self.database) \
                or any(_same(path, known) for known in self.databases):
            return False
        self.databases.append(path)
        self.save()
        return True

    def detach_database(self, path):
        kept = [known for known in self.databases if not _same(known, path)]
        changed = len(kept) != len(self.databases)
        self.databases = kept
        if self.default_db and _same(self.default_db, path):
            self.default_db = None
            changed = True
        if changed:
            self.save()
        return changed

    # ------------------------------------------------------------- listings

    DATABASE_SUFFIXES = ('.gpkg',)

    def project_databases(self):
        """The databases that live in the project folder itself, by name.

        They are listed under Databases without being attached, so a
        GeoPackage made in the project folder appears there on its own, the
        way a script dropped in Toolbox does.
        """
        try:
            names = sorted(os.listdir(self.root), key=str.lower)
        except OSError:
            return []
        return [os.path.join(self.root, n) for n in names
                if n.lower().endswith(self.DATABASE_SUFFIXES)
                and os.path.isfile(os.path.join(self.root, n))]

    def all_databases(self):
        """Project-folder databases, then the attached ones that still exist."""
        found = self.project_databases()
        for path in self.databases:
            if os.path.isfile(path) and not any(_same(path, p) for p in found):
                found.append(path)
        return found

    def all_folders(self):
        """The project folder, then the attached folders that still exist."""
        return [self.root] + [p for p in self.folders if os.path.isdir(p)]

    def is_project_database(self, path):
        """True for a database that sits in the project folder (not attached)."""
        return _same(os.path.dirname(os.path.abspath(path)), self.root)

    # ------------------------------------------------------------- defaults

    @property
    def default_database(self):
        """The default database: the one chosen, else Default.gpkg, else the first."""
        found = self.all_databases()
        if self.default_db and any(_same(self.default_db, p) for p in found):
            return self.default_db
        if os.path.isfile(self.database):
            return self.database
        return found[0] if found else ''

    @property
    def default_folder(self):
        """The default folder: the one chosen, else the project folder."""
        if self.default_dir and any(_same(self.default_dir, p)
                                    for p in self.all_folders()):
            return self.default_dir
        return self.root

    def set_default_database(self, path):
        """Make `path` the default database. It has to be one of the listed ones."""
        for known in self.all_databases():
            if _same(known, path):
                self.default_db = _norm(known)
                self.save()
                return True
        return False

    def set_default_folder(self, path):
        for known in self.all_folders():
            if _same(known, path):
                self.default_dir = _norm(known)
                self.save()
                return True
        return False


def _same(a, b):
    return os.path.normcase(_norm(a)) == os.path.normcase(_norm(b))


def _first_path(value):
    found = _clean_paths([value])
    return found[0] if found else None


def _clean_paths(value):
    if not isinstance(value, list):
        return []
    seen, out = set(), []
    for entry in value:
        if isinstance(entry, str) and entry.strip():
            path = _norm(entry)
            key = os.path.normcase(path)
            if key not in seen:
                seen.add(key)
                out.append(path)
    return out


def create_empty_geopackage(path):
    """Write a valid, empty GeoPackage at `path`."""
    from osgeo import gdal, ogr
    gdal.UseExceptions()
    driver = ogr.GetDriverByName('GPKG')
    source = driver.CreateDataSource(path)
    if source is None:
        raise WorkspaceError('Could not create the database {}.'.format(path))
    source = None       # closing writes the file


# --------------------------------------------------------------- operations

def create_workspace(location, name):
    """Make `<location>/<name>` and everything in it; return the Workspace.

    Refuses a name that is invalid or a folder that already holds something, so
    nothing a user already has is ever overwritten.
    """
    why = name_problem(name)
    if why:
        raise WorkspaceError(why)
    if not (location or '').strip():
        raise WorkspaceError('Choose a folder for the workspace.')
    name = name.strip()
    root = os.path.join(_norm(location), name)
    if os.path.exists(root) and (not os.path.isdir(root) or os.listdir(root)):
        raise WorkspaceError(
            '"{}" already exists and is not empty. Choose another name, or '
            'use Project > Open Workspace to open it.'.format(root))
    return Workspace(root, name).scaffold()


def _geopackage_has_data(path):
    """True when the GeoPackage holds a table (or cannot be read, to be safe)."""
    try:
        with closing(sqlite3.connect(path, timeout=1)) as db:
            row = db.execute('SELECT COUNT(*) FROM gpkg_contents').fetchone()
        return bool(row and row[0])
    except sqlite3.Error:
        return True


# ------------------------------------------------------------ scratch space
#
# An unsaved QGIS project has no home, so its tool output goes to a scratch
# workspace. It is made fresh at every start, never reused by name, and thrown
# away when it is empty. Set Workspace moves what is in it into the new
# workspace; whatever is left is offered back at the next start and cleared
# after SCRATCH_KEEP_DAYS.

def scratch_dir():
    """`Documents\\KGA Toolbox Project\\_scratch`, where Untitled work lives."""
    return os.path.join(factory_location(), SCRATCH_DIR)


def new_scratch_workspace(now=None):
    """A new, empty `Untitled` scratch workspace."""
    stamp = (now or datetime.now()).strftime('%Y%m%d-%H%M%S')
    base = os.path.join(scratch_dir(), '{}-{}'.format(UNTITLED, stamp))
    root, count = base, 1
    while os.path.exists(root):
        count += 1
        root = '{}-{}'.format(base, count)
    workspace = Workspace(root, UNTITLED).scaffold()
    _hide(scratch_dir())
    return workspace


def _hide(path):
    """Mark a folder hidden on Windows so it does not show among the workspaces."""
    if os.name != 'nt':
        return
    with suppress(Exception):
        import ctypes
        ctypes.windll.kernel32.SetFileAttributesW(path, 0x2)


def remove_tree(path):
    """Delete a folder as far as possible; True when it is gone."""
    shutil.rmtree(path, ignore_errors=True)
    return not os.path.exists(path)


def leftover_scratches(keep=None):
    """Scratch workspaces from earlier sessions (and the old Untitled), newest first."""
    roots = []
    with suppress(OSError):
        for entry in sorted(os.listdir(scratch_dir())):
            roots.append(os.path.join(scratch_dir(), entry))
    roots.append(os.path.join(default_location(), UNTITLED))
    found = []
    for root in roots:
        if not is_workspace(root) or (keep is not None and _same(root, keep.root)):
            continue
        found.append(Workspace(root, UNTITLED).load())
    found.sort(key=lambda w: w.modified(), reverse=True)
    return found


def cleanup_scratches(keep=None, now=None, grace_minutes=EMPTY_GRACE_MINUTES):
    """Delete scratch folders that are spent; return those worth recovering.

    Spent means empty, already moved to a workspace, or (new-style only) older
    than SCRATCH_KEEP_DAYS. The old permanent `Untitled` folder is never
    deleted here - it may hold someone's work - only offered for recovery.
    """
    now = now if now is not None else time.time()
    recover = []
    for workspace in leftover_scratches(keep):
        age = max(0.0, now - workspace.modified())
        moved = os.path.isfile(os.path.join(workspace.root, PROMOTED))
        has_content = workspace.has_content()
        if moved or (not has_content and age >= grace_minutes * 60):
            remove_tree(workspace.root)
        elif has_content and not workspace.is_legacy_untitled \
                and age > SCRATCH_KEEP_DAYS * 86400:
            remove_tree(workspace.root)
        elif has_content:
            recover.append(workspace)
    return recover


def discard_if_empty(workspace):
    """Remove a scratch that never got anything; True when it was removed."""
    if workspace is None or not workspace.is_untitled \
            or workspace.has_content():
        return False
    return remove_tree(workspace.root)


def _unique_path(path):
    base, ext = os.path.splitext(path)
    count = 1
    while os.path.exists(path):
        count += 1
        path = '{}_{}{}'.format(base, count, ext)
    return path


def _backup_geopackage(source, target):
    """Copy a GeoPackage through SQLite, which is safe while QGIS has it open."""
    with closing(sqlite3.connect(source, timeout=5)) as src, \
            closing(sqlite3.connect(target, timeout=5)) as dst:
        src.backup(dst)


def promote_files(source, target):
    """Copy everything in the `source` scratch into the `target` workspace.

    The target's empty Default.gpkg is replaced by the scratch's; any other
    name that is already taken is kept and the copy gets a numbered name.
    Nothing is removed from the scratch. Returns the number of files copied.
    """
    copied = 0
    skipped = ('-wal', '-shm', '-journal')
    try:
        for dirpath, _dirs, files in os.walk(source.root):
            rel = os.path.relpath(dirpath, source.root)
            folder = target.root if rel == '.' else os.path.join(target.root, rel)
            os.makedirs(folder, exist_ok=True)
            for name in files:
                low = name.lower()
                if rel == '.' and name in (MARKER, PROMOTED):
                    continue
                if low.endswith(skipped):
                    continue
                src, dst = os.path.join(dirpath, name), os.path.join(folder, name)
                if os.path.exists(dst) and not (
                        rel == '.' and low == DEFAULT_DB.lower()):
                    dst = _unique_path(dst)
                if low.endswith('.gpkg'):
                    _backup_geopackage(src, dst)
                else:
                    shutil.copy2(src, dst)
                copied += 1
    except (OSError, sqlite3.Error) as error:
        raise WorkspaceError(
            'Could not move the work in Untitled: {}'.format(error))
    for folder in source.folders:
        target.attach_folder(folder)
    for path in source.databases:
        target.attach_database(path)
    return copied


def repoint_layers(project, old_root, new_root):
    """Point every layer whose data is under `old_root` at the same file under `new_root`.

    Returns (moved, failed): layer names, and (name, reason) pairs for layers
    that could not be switched (being edited, or no longer valid).
    """
    from qgis.core import QgsDataProvider
    old = old_root.replace('\\', '/')
    new = new_root.replace('\\', '/')
    pattern = re.compile(re.escape(old) + r'(?=[/|?#]|$)', re.IGNORECASE)
    moved, failed = [], []
    for layer in list(project.mapLayers().values()):
        source = layer.source()
        flat = source.replace('\\', '/')
        if not pattern.search(flat):
            continue
        if layer.isEditable():
            failed.append((layer.name(), 'it is being edited'))
            continue
        fresh = pattern.sub(lambda _m: new, flat)
        options = QgsDataProvider.ProviderOptions()
        layer.setDataSource(fresh, layer.name(), layer.providerType(), options)
        if layer.isValid():
            moved.append(layer.name())
        else:
            layer.setDataSource(source, layer.name(), layer.providerType(), options)
            failed.append((layer.name(), 'the new location could not be read'))
    return moved, failed


def mark_promoted(workspace):
    """Flag a scratch as moved, so the next start deletes it if it could not be now."""
    with suppress(OSError):
        with open(os.path.join(workspace.root, PROMOTED), 'w') as handle:
            handle.write('moved')


def is_workspace(folder):
    return bool(folder) and os.path.isfile(os.path.join(folder, MARKER))


def open_workspace(path):
    """The Workspace for a workspace folder, its marker, or a `.qgz` inside one.

    Returns None when `path` is not (part of) a workspace.
    """
    if not path:
        return None
    path = _norm(path)
    folder = path if os.path.isdir(path) else os.path.dirname(path)
    if not is_workspace(folder):
        return None
    workspace = Workspace(folder).load()
    return workspace


def project_file_for(workspace):
    """The `.qgz` to open for `workspace`, or '' when it has none yet.

    The named one wins; otherwise the only `.qgz` in the folder.
    """
    if os.path.isfile(workspace.project_file):
        return workspace.project_file
    try:
        found = sorted(entry for entry in os.listdir(workspace.root)
                       if entry.lower().endswith(PROJECT_EXTENSION))
    except OSError:
        return ''
    return os.path.join(workspace.root, found[0]) if len(found) == 1 else ''

# -*- coding: utf-8 -*-
"""The `.kgapp` project package: format, reading, verified extraction, import.

A KGA project package is to a workspace what ArcGIS Pro's `.ppkx` is to a project.
It is a zip holding the workspace folder under `project/`, a `manifest.json`
describing it and an optional `thumbnail.png`:

    manifest.json
    thumbnail.png
    project/<Name>.qgz        the project, every data path relative
    project/.kgaworkspace     workspace marker
    project/Default.gpkg      project database
    project/Toolbox, Layouts, resources/, commondata/ ...

Everything under `project/` is unpacked as it is, so opening a package is one
extraction followed by a few repairs (project file name, workspace marker);
that is why the writer (`project_share.py`) makes every path relative. This
module is the reading half and the format definition both halves share. It has no
widgets.

Safety on the way in: the manifest is read first and a newer format is refused;
member names are checked so nothing can be written outside the destination
(zip-slip); every file's size and SHA-256 are compared with the manifest while
it is written; and a failed import removes what it created.
"""

import hashlib
import json
import os
import shutil
import zipfile

from . import workspace as ws

EXTENSION = '.kgapp'
MANIFEST = 'manifest.json'
THUMBNAIL = 'thumbnail.png'
ROOT = 'project/'
KIND = 'project'

#: Written into every package; a reader refuses anything newer than it knows.
PACKAGE_FORMAT = 1

MODE_EVERYTHING = 'everything'
MODE_MAP = 'map'
MODES = (MODE_EVERYTHING, MODE_MAP)

# Per-layer statuses recorded in the manifest.
STATUS_PACKAGED = 'packaged'            # data is inside the package
STATUS_SERVICE = 'service'              # kept as a web / remote source
STATUS_NOT_SHARABLE = 'not_sharable'    # kept in the project, data not shared
STATUS_EXCLUDED = 'excluded'            # taken out of the packaged project

CHUNK = 1024 * 1024


class ProjectPackageError(Exception):
    """Anything that stops a package being read or imported, phrased for a user."""


# -------------------------------------------------------------- the manifest

class PackageInfo(object):
    """The descriptive fields Pro's share dialog asks for."""

    FIELDS = ('name', 'summary', 'description', 'author', 'credits',
              'use_limitations')

    def __init__(self, **values):
        for field in self.FIELDS:
            setattr(self, field, values.get(field, '') or '')
        tags = values.get('tags') or []
        if isinstance(tags, str):
            tags = split_tags(tags)
        self.tags = [t for t in tags if t]

    def to_dict(self):
        data = {field: getattr(self, field) for field in self.FIELDS}
        data['tags'] = list(self.tags)
        return data


def split_tags(text):
    """'a, b;c' -> ['a', 'b', 'c'], without blanks or repeats."""
    seen, tags = set(), []
    for part in (text or '').replace(';', ',').split(','):
        tag = part.strip()
        if tag and tag.lower() not in seen:
            seen.add(tag.lower())
            tags.append(tag)
    return tags


class Manifest(object):
    """A read manifest, with the few accessors the dialogs use."""

    def __init__(self, data):
        self.data = data

    @property
    def info(self):
        return PackageInfo(**(self.data.get('info') or {}))

    @property
    def mode(self):
        return self.data.get('mode', MODE_EVERYTHING)

    @property
    def created(self):
        return self.data.get('created', '')

    @property
    def generator(self):
        return self.data.get('generator', '')

    @property
    def qgis(self):
        return self.data.get('qgis', '')

    @property
    def project(self):
        return self.data.get('project') or {}

    @property
    def project_file(self):
        """The `.qgz` inside the archive, as a name under `project/`."""
        return self.project.get('file', '')

    @property
    def layers(self):
        return list(self.data.get('layers') or [])

    @property
    def warnings(self):
        return list(self.data.get('warnings') or [])

    @property
    def files(self):
        return dict(self.data.get('files') or {})

    @property
    def defaults(self):
        return self.data.get('defaults') or {}

    @property
    def data_bytes(self):
        return int((self.data.get('stats') or {}).get('bytes', 0))

    def count(self, status):
        return sum(1 for layer in self.layers if layer.get('status') == status)


# ------------------------------------------------------------------- reading

def read_manifest(path):
    """The `Manifest` of the package at `path`, or `ProjectPackageError`."""
    if not os.path.isfile(path):
        raise ProjectPackageError('"{}" does not exist.'.format(path))
    try:
        with zipfile.ZipFile(path) as archive:
            try:
                raw = archive.read(MANIFEST)
            except KeyError:
                raise ProjectPackageError(
                    '"{}" is not a KGA project package (there is no {}). It '
                    'may be incomplete.'.format(os.path.basename(path), MANIFEST))
    except zipfile.BadZipFile:
        raise ProjectPackageError(
            '"{}" is not a valid package file; it may be damaged or only '
            'partly downloaded.'.format(os.path.basename(path)))
    except OSError as error:
        raise ProjectPackageError('Could not read "{}": {}'.format(path, error))
    try:
        data = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        raise ProjectPackageError('The package manifest is damaged.')
    if not isinstance(data, dict) or data.get('kind') != KIND:
        raise ProjectPackageError(
            '"{}" is not a project package (it may be a layer package; open '
            'those with Open Layer Package).'.format(os.path.basename(path)))
    if int(data.get('format', 0) or 0) > PACKAGE_FORMAT:
        raise ProjectPackageError(
            'This package was made by a newer KGA Toolbox (package format {}). '
            'Update KGA Toolbox to open it.'.format(data.get('format')))
    return Manifest(data)


def read_thumbnail(path):
    """The package's thumbnail as bytes, or None."""
    try:
        with zipfile.ZipFile(path) as archive:
            return archive.read(THUMBNAIL)
    except (KeyError, zipfile.BadZipFile, OSError):
        return None


def is_within(root, target):
    """True when `target` is `root` or somewhere below it."""
    root = os.path.normcase(os.path.abspath(root))
    target = os.path.normcase(os.path.abspath(target))
    return target == root or target.startswith(root + os.sep)


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(CHUNK), b''):
            digest.update(block)
    return digest.hexdigest()


def _member_target(destination, name):
    """Where member `name` (under `project/`) goes, or None to skip it.

    Raises when the name would land outside `destination`.
    """
    relative = name[len(ROOT):]
    if not relative or relative.endswith('/'):
        return None                                 # a directory entry
    parts = relative.replace('\\', '/').split('/')
    if relative.startswith('/') or ':' in parts[0] or '..' in parts:
        raise ProjectPackageError(
            'The package contains an unsafe path ("{}") and was not '
            'opened.'.format(name))
    target = os.path.join(destination, *parts)
    if not is_within(destination, target):
        raise ProjectPackageError(
            'The package contains an unsafe path ("{}") and was not '
            'opened.'.format(name))
    return target


def extract(path, destination, manifest=None, verify=True, feedback=None):
    """Unpack `project/` of the package into `destination`.

    Each file is hashed as it is written and compared with the manifest, so a
    damaged package is caught here rather than as a layer that will not load.
    Returns the list of relative file names written.
    """
    manifest = manifest or read_manifest(path)
    expected = manifest.files if verify else {}
    written = []
    try:
        with zipfile.ZipFile(path) as archive:
            members = [m for m in archive.infolist()
                       if m.filename.startswith(ROOT)]
            if not members:
                raise ProjectPackageError('The package holds no project.')
            total = sum(m.file_size for m in members) or 1
            done = 0
            for member in members:
                if feedback is not None and feedback.isCanceled():
                    raise ProjectPackageError('Canceled.')
                target = _member_target(destination, member.filename)
                if target is None:
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                digest = hashlib.sha256()
                size = 0
                with archive.open(member) as source, \
                        open(target, 'wb') as out:
                    for block in iter(lambda: source.read(CHUNK), b''):
                        if feedback is not None and feedback.isCanceled():
                            raise ProjectPackageError('Canceled.')
                        out.write(block)
                        digest.update(block)
                        size += len(block)
                        done += len(block)
                        if feedback is not None:
                            feedback.step(done * 100.0 / total,
                                          'Unpacking {}'.format(
                                              member.filename[len(ROOT):]))
                record = expected.get(member.filename)
                if record is not None and (
                        digest.hexdigest() != record.get('sha256')
                        or size != int(record.get('size', size))):
                    raise ProjectPackageError(
                        'The file "{}" in the package is damaged (it does not '
                        'match its checksum). Get a fresh copy of the '
                        'package.'.format(member.filename[len(ROOT):]))
                written.append(member.filename[len(ROOT):])
            missing = [n for n in expected
                       if n.startswith(ROOT)
                       and n[len(ROOT):] not in written]
            if missing:
                raise ProjectPackageError(
                    'The package is incomplete: {} is missing.'.format(
                        missing[0][len(ROOT):]))
    except zipfile.BadZipFile:
        raise ProjectPackageError('The package file is damaged.')
    return written


# -------------------------------------------------------------------- import

class ImportResult(object):

    def __init__(self, workspace, manifest):
        self.workspace = workspace
        self.manifest = manifest
        self.files = 0
        self.invalid_layers = []        # [(layer name, reason)]
        self.warnings = []

    @property
    def project_file(self):
        return self.workspace.project_file


def unique_folder_name(location, name):
    """`name`, or `name_2` ... when `location/name` is taken."""
    candidate, counter = name, 2
    while os.path.exists(os.path.join(location, candidate)):
        candidate = '{}_{}'.format(name, counter)
        counter += 1
    return candidate


def target_problem(location, name):
    """Why `location/name` cannot receive an import, or ''."""
    if not (location or '').strip():
        return 'Choose a folder to store the project in.'
    problem = ws.name_problem(name)
    if problem:
        return problem
    target = os.path.join(location.strip(), name.strip())
    if os.path.exists(target) and (not os.path.isdir(target)
                                   or os.listdir(target)):
        return '"{}" already exists and is not empty.'.format(target)
    return ''


def import_package(path, location, name=None, verify=True, feedback=None):
    """Unpack a package into `location/name` and make it a workspace.

    Nothing existing is overwritten: the target must be new or empty. A failed
    import removes the folder it created.
    """
    manifest = read_manifest(path)
    name = (name or manifest.info.name or manifest.project.get('name')
            or os.path.splitext(os.path.basename(path))[0]).strip()
    why = target_problem(location, name)
    if why:
        raise ProjectPackageError(why)
    target = os.path.normpath(os.path.join(location.strip(), name))
    created = not os.path.exists(target)
    try:
        os.makedirs(target, exist_ok=True)
        written = extract(path, target, manifest, verify, feedback)
        workspace = _adopt(target, name, manifest)
    except BaseException:
        if created:
            shutil.rmtree(target, ignore_errors=True)
        raise
    result = ImportResult(workspace, manifest)
    result.files = len(written)
    result.invalid_layers = check_project(workspace.project_file)
    if manifest.warnings:
        result.warnings = manifest.warnings
    if feedback is not None:
        feedback.step(100, 'Done')
    return result


def _adopt(target, name, manifest):
    """Make the unpacked folder a workspace called `name`."""
    original = manifest.project_file.replace('\\', '/')
    source = os.path.join(target, *original[len(ROOT):].split('/')) \
        if original.startswith(ROOT) else os.path.join(target, original)
    workspace = ws.Workspace(target, name)
    if os.path.isfile(source):
        if os.path.normcase(source) != os.path.normcase(workspace.project_file):
            if os.path.exists(workspace.project_file):
                os.remove(workspace.project_file)
            os.replace(source, workspace.project_file)
    # The sender's attached folders and databases mean nothing here; only the
    # choice of default survives, and as a path inside the package.
    workspace.folders, workspace.databases = [], []
    workspace.default_db = workspace.default_dir = None
    workspace.scaffold()
    workspace.save()
    defaults = manifest.defaults
    database = defaults.get('database')
    if database:
        workspace.set_default_database(os.path.join(target, database))
    folder = defaults.get('folder')
    if folder:
        workspace.set_default_folder(os.path.join(target, folder))
    return workspace


def check_project(project_file):
    """[(layer name, reason)] for the layers that do not load from `project_file`."""
    if not os.path.isfile(project_file):
        return [('(project)', 'the project file is missing')]
    from qgis.core import QgsProject
    project = QgsProject()
    if not project.read(project_file):
        return [('(project)', 'the project could not be read')]
    problems = []
    for layer in project.mapLayers().values():
        if not layer.isValid():
            problems.append((layer.name(), 'its data source cannot be opened'))
    return problems

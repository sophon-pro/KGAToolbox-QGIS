# -*- coding: utf-8 -*-
"""Project toolbox: a folder of Processing scripts that follows the workspace.

QGIS already knows how to read script tools: Processing's `SCRIPTS_FOLDERS`
setting is a list of folders it scans for `.py` files. The Catalog's Toolbox is
nothing more than the workspace's `Toolbox/` folder added to that list while the
workspace is open and taken out again when it is not, so a user "drops a script
in the toolbox folder" and it shows up in the Processing Toolbox as well.

`processing` ships inside QGIS's own plugins folder, so it is imported lazily and
every function degrades to a no-op if Processing is switched off.
"""

import os
import re
from contextlib import suppress

from qgis.core import QgsApplication, QgsSettings

# Remembers the folder this module added, so a crash cannot leave a stale
# entry in Processing's settings for ever.
SETTINGS_ADDED = 'KGA/catalog/registered_scripts_folder'
SEPARATOR = ';'


def _config():
    from processing.core.ProcessingConfig import ProcessingConfig
    from processing.script import ScriptUtils
    return ProcessingConfig, ScriptUtils


def _norm(path):
    return os.path.normcase(os.path.normpath(path))


def configured_folders():
    """The script folders Processing is configured with, as a list."""
    try:
        config, utils = _config()
        value = config.getSetting(utils.SCRIPTS_FOLDERS) or ''
    except Exception:
        return []
    return [part for part in str(value).split(SEPARATOR) if part]


def _write(folders):
    config, utils = _config()
    config.setSettingValue(utils.SCRIPTS_FOLDERS, SEPARATOR.join(folders))


def _refresh():
    provider = QgsApplication.processingRegistry().providerById('script')
    if provider is not None:
        provider.refreshAlgorithms()


def refresh():
    """Ask Processing to rescan its script folders (a script was added)."""
    try:
        _refresh()
    except Exception:
        return False
    return True


def unregister_previous():
    """Take out the folder an earlier session (or workspace) added."""
    settings = QgsSettings()
    previous = settings.value(SETTINGS_ADDED, '') or ''
    settings.remove(SETTINGS_ADDED)
    if not previous:
        return False
    try:
        kept = [f for f in configured_folders() if _norm(f) != _norm(previous)]
        if len(kept) == len(configured_folders()):
            return False
        _write(kept)
    except Exception:
        return False
    return True


def register(folder):
    """Make Processing scan `folder`. Replaces whatever this module added before."""
    changed = unregister_previous()
    if not folder or not os.path.isdir(folder):
        if changed:
            with suppress(Exception):
                _refresh()
        return False
    try:
        folders = configured_folders()
        if not any(_norm(f) == _norm(folder) for f in folders):
            _write(folders + [folder])
            # Only a folder this module added is ever removed again.
            QgsSettings().setValue(SETTINGS_ADDED, folder)
        _refresh()
    except Exception:
        return False
    return True


def unregister():
    """Stop Processing scanning the workspace toolbox (on unload)."""
    changed = unregister_previous()
    if changed:
        with suppress(Exception):
            _refresh()
    return changed


def list_scripts(folder):
    """Paths of the script files in `folder`, sorted by name.

    `__init__.py` and anything starting with an underscore is helper code, not
    a tool.
    """
    try:
        names = sorted(os.listdir(folder), key=str.lower)
    except OSError:
        return []
    return [os.path.join(folder, name) for name in names
            if name.lower().endswith('.py') and not name.startswith('_')
            and os.path.isfile(os.path.join(folder, name))]


def algorithm_id(path):
    """The Processing id of the script at `path`, or '' if it will not load."""
    try:
        _config_obj, utils = _config()
        stem = os.path.splitext(os.path.basename(path))[0]
        algorithm = utils.loadAlgorithm(stem, path)
    except Exception:
        return ''
    return 'script:{}'.format(algorithm.name()) if algorithm is not None else ''


SCRIPT_TEMPLATE = '''# -*- coding: utf-8 -*-
"""{title}: a QGIS script tool.

Save this file in the project's Toolbox folder and it appears in the Catalog
(Project > Toolboxes) and in the Processing Toolbox under Scripts.
"""

from qgis.core import (QgsProcessingAlgorithm, QgsProcessingParameterString)


class {cls}(QgsProcessingAlgorithm):

    TEXT = 'TEXT'

    def name(self):
        return '{ident}'

    def displayName(self):
        return '{title}'

    def group(self):
        return 'Project scripts'

    def groupId(self):
        return 'project_scripts'

    def createInstance(self):
        return {cls}()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterString(self.TEXT, 'Text'))

    def processAlgorithm(self, parameters, context, feedback):
        feedback.pushInfo(self.parameterAsString(parameters, self.TEXT, context))
        return {{}}
'''


def new_script_path(folder, title):
    """A free `.py` path in `folder` for a script called `title`."""
    stem = ''.join(ch if ch.isalnum() else '_' for ch in title.strip()) or 'script'
    if stem[0].isdigit():
        stem = '_' + stem
    path = os.path.join(folder, stem + '.py')
    number = 2
    while os.path.exists(path):
        path = os.path.join(folder, '{}_{}.py'.format(stem, number))
        number += 1
    return path


def write_new_script(folder, title):
    """Create a starter script and return its path."""
    path = new_script_path(folder, title)
    stem = os.path.splitext(os.path.basename(path))[0]
    cls = ''.join(part.capitalize() for part in stem.split('_') if part) or 'Script'
    if cls[0].isdigit():
        cls = 'Script' + cls
    # The title lands inside a string literal and a docstring.
    safe = re.sub(r'[\\\'"\r\n]', '', title).strip() or stem
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write(SCRIPT_TEMPLATE.format(
            title=safe, cls=cls, ident=stem.lower()))
    return path

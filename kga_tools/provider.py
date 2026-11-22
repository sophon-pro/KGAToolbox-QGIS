"""Processing provider for KGA Toolbox.

Algorithms are discovered automatically: every QgsProcessingAlgorithm
subclass defined inside a module in the `algorithms` package is registered.
Drop a script in there and it appears in the toolbox and on the toolbar.

Name a class with a leading underscore to keep it out of that sweep. A module
holding two related algorithms usually wants a shared base for the parameters
and helpers they have in common, and without the underscore it is registered
too - as an algorithm with no name, which shows up as a blank line in the
toolbar menu and the toolbox tree.
"""

import importlib
import inspect
import os
import pkgutil
import sys
import traceback

from qgis.core import (
    Qgis,
    QgsMessageLog,
    QgsProcessingAlgorithm,
    QgsProcessingProvider,
)
from .branding import icon as branding_icon

PLUGIN_DIR = os.path.dirname(__file__)
LOG_TAG = 'KGA Toolbox'


# Algorithm modules that are built but frozen: not registered, so they get no
# menu entry, toolbar button or Processing entry (and the KGA GeoAI toolbar,
# which only exists when it has tools, is not created). The code stays in the
# repo. To bring a tool back, remove its module name from this set.
FROZEN_MODULES = frozenset({
    'detect_roads_imagery_alg',
    'detect_paddy_plots_imagery_alg',
})


class KgaProvider(QgsProcessingProvider):

    def id(self):
        # Algorithm ids become "kga:<algorithm name()>"
        return 'kga'

    def name(self):
        return 'KGA Toolbox'

    def longName(self):
        return 'KGA Toolbox'

    def icon(self):
        found = branding_icon('branding/kga.png')
        return found if not found.isNull() else super().icon()

    def loadAlgorithms(self):
        from . import algorithms as alg_package

        package_dir = os.path.dirname(alg_package.__file__)
        for _finder, mod_name, _ispkg in pkgutil.iter_modules([package_dir]):
            if mod_name in FROZEN_MODULES:
                continue
            full_name = '{}.{}'.format(alg_package.__name__, mod_name)
            try:
                if full_name in sys.modules:
                    # Picks up edits without restarting QGIS.
                    module = importlib.reload(sys.modules[full_name])
                else:
                    module = importlib.import_module(full_name)
            except Exception:
                QgsMessageLog.logMessage(
                    'Failed to import {}:\n{}'.format(mod_name, traceback.format_exc()),
                    LOG_TAG,
                    Qgis.MessageLevel.Critical,
                )
                continue

            for _cls_name, obj in inspect.getmembers(module, inspect.isclass):
                if not issubclass(obj, QgsProcessingAlgorithm):
                    continue
                if obj is QgsProcessingAlgorithm:
                    continue
                # Shared base classes, by convention.
                if _cls_name.startswith('_'):
                    continue
                # Only classes actually defined in this module, not imports.
                if getattr(obj, '__module__', None) != full_name:
                    continue
                if inspect.isabstract(obj):
                    continue
                try:
                    self.addAlgorithm(obj())
                except Exception:
                    QgsMessageLog.logMessage(
                        'Failed to instantiate {}.{}:\n{}'.format(
                            mod_name, _cls_name, traceback.format_exc()),
                        LOG_TAG,
                        Qgis.MessageLevel.Critical,
                    )

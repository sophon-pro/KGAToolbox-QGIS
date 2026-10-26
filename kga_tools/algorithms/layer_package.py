# -*- coding: utf-8 -*-
"""Create Layer Package and Open Layer Package.

Both take no Processing parameters: running one opens its own window
(`gui/create_package_dialog.py`, `gui/open_package_dialog.py`), the same way
Add Layers to Layer Package does. `kga_plugin.run_algorithm` spots the empty
parameter list and runs the algorithm straight away. The work itself is in
`core/packaging.py`.
"""

from qgis.core import QgsProcessingAlgorithm
from qgis.PyQt.QtCore import QCoreApplication

from ..branding import docs_url
from ..core.compat import no_threading


class CreateLayerPackageAlgorithm(QgsProcessingAlgorithm):
    """Open the Create Layer Package window."""

    def tr(self, string):
        return QCoreApplication.translate('KgaLayerPackage', string)

    def createInstance(self):
        return CreateLayerPackageAlgorithm()

    def name(self):
        return 'create_layer_package'

    def displayName(self):
        return self.tr('Create Layer Package')

    def group(self):
        return self.tr('KGA Data Management')

    def groupId(self):
        return 'kgadatamanagement'

    def helpUrl(self):
        return docs_url('create_layer_package')

    def shortHelpString(self):
        return self.tr(
            'Package layers, their styles and everything those styles need '
            'into one <b>.kgalp</b> file that opens anywhere.\n\n'
            'A QLR points at data that has to travel beside it; this contains '
            'the data. Vector layers go into one GeoPackage — <b>field domains '
            'included</b> — rasters are copied as their own files with their '
            'sidecars, each style is saved as a QML, and every SVG symbol and '
            'raster marker image the symbology references is collected and '
            'de-duplicated. The group structure is recorded so the far side '
            'rebuilds it.\n\n'
            'Fonts are the one thing that cannot be packaged. If a layer uses a '
            'font marker you get a warning naming the font, so you can tell the '
            'recipient what to install.\n\n'
            'The manifest is written last, so a package interrupted halfway '
            'through has no manifest and is refused rather than opening '
            'half-loaded.'
        )

    def flags(self):
        return no_threading(super().flags())

    def initAlgorithm(self, config=None):
        pass

    def processAlgorithm(self, parameters, context, feedback):
        from ..gui.create_package_dialog import show_create_package

        show_create_package()
        feedback.pushInfo(self.tr('Create Layer Package opened.'))
        return {}


class OpenLayerPackageAlgorithm(QgsProcessingAlgorithm):
    """Open the Open Layer Package window."""

    def tr(self, string):
        return QCoreApplication.translate('KgaLayerPackage', string)

    def createInstance(self):
        return OpenLayerPackageAlgorithm()

    def name(self):
        return 'open_layer_package'

    def displayName(self):
        return self.tr('Open Layer Package')

    def group(self):
        return self.tr('KGA Data Management')

    def groupId(self):
        return 'kgadatamanagement'

    def helpUrl(self):
        return docs_url('open_layer_package')

    def shortHelpString(self):
        return self.tr(
            'Unpack a <b>.kgalp</b> and add its layers, styles and group '
            'structure to the current project.\n\n'
            'The package is extracted to a real folder, never to a temporary '
            'one — the layers point at the extracted files, so a temp folder '
            'would leave a project full of broken layers after the next '
            'restart. If you do not choose a folder, a <i>KGA Packages</i> '
            'folder is created beside the project file and the log says where '
            'everything went.\n\n'
            'SVG and image paths inside each style are rewritten to the '
            'extracted copies, so the symbology renders the same as it did on '
            'the machine that built the package.\n\n'
            'Archive entries that would be written outside the extraction '
            'folder are refused.'
        )

    def flags(self):
        return no_threading(super().flags())

    def initAlgorithm(self, config=None):
        pass

    def processAlgorithm(self, parameters, context, feedback):
        from ..gui.open_package_dialog import show_open_package

        show_open_package()
        feedback.pushInfo(self.tr('Open Layer Package opened.'))
        return {}


def close_layer_package_windows():
    """Shut both windows on plugin unload."""
    from ..gui.create_package_dialog import close_create_package
    from ..gui.open_package_dialog import close_open_package
    close_create_package()
    close_open_package()

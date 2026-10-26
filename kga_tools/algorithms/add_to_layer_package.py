# -*- coding: utf-8 -*-
"""Add Layers to Layer Package.

Puts more layers into a `.kgalp` that already exists, so a package does not
have to be rebuilt from scratch every time one more layer is wanted.

Takes no Processing parameters: running it opens the Add Layers to Layer
Package window (the layers to add, the selected-features switch and the target
package). `kga_plugin.run_algorithm` spots the empty parameter list and runs it
straight away.
"""

from qgis.core import QgsProcessingAlgorithm
from qgis.PyQt.QtCore import QCoreApplication

from ..branding import docs_url
from ..core.compat import no_threading


class AddToLayerPackageAlgorithm(QgsProcessingAlgorithm):
    """Open the Add Layers to Layer Package window."""

    def tr(self, string):
        return QCoreApplication.translate('KgaAddToLayerPackage', string)

    def createInstance(self):
        return AddToLayerPackageAlgorithm()

    def name(self):
        return 'add_to_layer_package'

    def displayName(self):
        return self.tr('Add Layers to Layer Package')

    def group(self):
        return self.tr('KGA Data Management')

    def groupId(self):
        return 'kgadatamanagement'

    def helpUrl(self):
        return docs_url('add_to_layer_package')

    def shortHelpString(self):
        return self.tr(
            '<p>Adds one or more layers to a <b>.kgalp</b> package that '
            'already exists, with the same treatment <i>Create Layer '
            'Package</i> gives them: vector data goes into the package '
            'GeoPackage (field domains included), rasters are copied with '
            'their sidecar files, each style is saved, and the SVG and image '
            'resources the symbology uses are collected.</p>'
            '<ul>'
            '<li><b>Input layers</b> lists the layers in the project, each '
            'with a tick box. <b>Browse...</b> adds layers from files on disk '
            '(a GeoPackage, shapefile, GeoJSON, raster...) that are not in the '
            'project.</li>'
            '<li><b>Use only the selected features</b>: for every ticked '
            'vector layer that has a selection, only those features go into '
            'the package. Layers without a selection are added whole.</li>'
            '<li><b>Layer package</b> is the existing .kgalp to add to.</li>'
            '</ul>'
            '<p>Layers already in the package are never changed or replaced: '
            'a new layer with the same name is stored beside the old one. The '
            'package is rebuilt in a temporary file and swapped in only when '
            'it is complete, so a failure leaves the original untouched.</p>'
            '<p>Fonts cannot be packaged; a layer using a font marker gives a '
            'warning naming the font.</p>'
        )

    def flags(self):
        return no_threading(super().flags())

    def initAlgorithm(self, config=None):
        pass

    def processAlgorithm(self, parameters, context, feedback):
        from ..gui.add_to_package_dialog import show_add_to_package

        show_add_to_package()
        feedback.pushInfo(self.tr('Add Layers to Layer Package opened.'))
        return {}


def close_add_to_layer_package():
    """Shut the window on plugin unload."""
    from ..gui.add_to_package_dialog import close_add_to_package
    close_add_to_package()

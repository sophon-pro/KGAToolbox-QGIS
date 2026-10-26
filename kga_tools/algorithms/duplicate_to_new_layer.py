# -*- coding: utf-8 -*-
"""Duplicate to New Layer.

QGIS's built-in Duplicate Layer makes a second layer pointing at the same data
source, so an edit to the copy is an edit to the original. This tool copies the
data too, and the copy is independent.

Takes no Processing parameters: running it opens the Duplicate to New Layer
window (two inputs: the original layer and where to save the copy).
`kga_plugin.run_algorithm` spots the empty parameter list and runs it straight
away. It is also on the right-click menu of layers, below QGIS's own Duplicate
Layer (see kga_plugin.py).
"""

from qgis.core import QgsProcessingAlgorithm
from qgis.PyQt.QtCore import QCoreApplication

from ..branding import docs_url
from ..core.compat import no_threading


class DuplicateToNewLayerAlgorithm(QgsProcessingAlgorithm):
    """Open the Duplicate to New Layer window."""

    def tr(self, string):
        return QCoreApplication.translate('KgaDuplicateToNewLayer', string)

    def createInstance(self):
        return DuplicateToNewLayerAlgorithm()

    def name(self):
        return 'duplicate_to_new_layer'

    def displayName(self):
        return self.tr('Duplicate to New Layer')

    def group(self):
        return self.tr('KGA Data Management')

    def groupId(self):
        return 'kgadatamanagement'

    def helpUrl(self):
        return docs_url('duplicate_to_new_layer')

    def shortHelpString(self):
        return self.tr(
            '<p>Makes a copy of a layer <b>and its data</b>. QGIS\'s own '
            '<i>Duplicate Layer</i> only adds a second layer pointing at the '
            'same file or table, so editing or deleting features in the copy '
            'changes the original. This copy is independent.</p>'
            '<ul>'
            '<li><b>Vector layers</b> are written to a GeoPackage, shapefile, File '
            'Geodatabase, GeoJSON, KML or KMZ, as you choose.</li>'
            '<li><b>Rasters</b> are written as GeoTIFF, Cloud Optimized '
            'GeoTIFF, Erdas IMAGINE, PNG, JPEG, JPEG 2000 or ASCII Grid, with '
            'the same values, no-data value and data type. Choose <b>Extent to '
            'copy</b> to take only part of the raster: the map extent, a '
            'layer\'s extent, a rectangle or polygon you draw, or the boundary '
            'of a polygon layer. Outside a polygon the pixels become NoData.</li>'
            '<li><b>Memory (scratch) layers</b> are copied in memory; no file '
            'is needed.</li>'
            '</ul>'
            '<p>The symbology, labels, aliases and form settings come across, '
            'and the copy is placed directly above the original in the Layers '
            'panel, named <i>&lt;layer&gt; (copy)</i>.</p>'
            '<p>The tool is also on the <b>right-click menu</b> of any vector '
            'or raster layer, just below <i>Duplicate Layer</i>.</p>'
            '<h3>Watch out for</h3>'
            '<ul>'
            '<li>If the layer has a <b>filter</b>, only the filtered features '
            'are copied, and the copy has no filter.</li>'
            '<li>If the layer is being edited, its <b>unsaved edits are '
            'included</b> in the copy; the original is left as it is.</li>'
            '<li>Rasters must be files on disk. Web services (WMS, XYZ tiles) '
            'cannot be copied.</li>'
            '<li>Leave <b>Output Location</b> (a folder) empty to save beside '
            'the original, as <code>&lt;layer&gt;_copy</code>. A copy never '
            'replaces an existing file.</li>'
            '<li>Tick <b>Selected features only</b> to copy just the features '
            'selected on the original layer. It is available when a vector '
            'layer has a selection.</li>'
            '<li>Shapefile field names are cut to 10 characters; KML and KMZ '
            'are written in WGS 84.</li>'
            '</ul>'
        )

    def flags(self):
        return no_threading(super().flags())

    def initAlgorithm(self, config=None):
        pass

    def processAlgorithm(self, parameters, context, feedback):
        from ..gui.duplicate_layer_dialog import show_duplicate_layer

        show_duplicate_layer()
        feedback.pushInfo(self.tr('Duplicate to New Layer opened.'))
        return {}


def close_duplicate_to_new_layer():
    """Shut the window on plugin unload."""
    from ..gui.duplicate_layer_dialog import close_duplicate_layer
    close_duplicate_layer()

from qgis.PyQt.QtCore import QCoreApplication, QVariant
from qgis.PyQt.QtWidgets import QMessageBox
from qgis.core import (
    QgsProcessingAlgorithm,
    QgsDistanceArea,
    QgsField,
    QgsWkbTypes,
    Qgis
)
import qgis.utils


class UpdateGeometryFields(QgsProcessingAlgorithm):
    def tr(self, string):
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return UpdateGeometryFields()

    def name(self):
        return 'updategeometryfields'

    def displayName(self):
        return self.tr('Update Geometry Fields')

    def group(self):
        return self.tr('KGA Geometry Utilities')

    def groupId(self):
        return 'kgageometryutilities'

    def helpUrl(self):
        return 'https://khmergrs.com/docs/qgis/updategeometryfields'

    def shortHelpString(self):
        return self.tr("Silently checks the active layer. Calculates Shape_Length (length for lines, perimeter for polygons) and Shape_Area. Creates fields if missing.")

    def flags(self):
        # FlagNoThreading forces the script to run in the main thread.
        # This is REQUIRED to safely grab the active layer and trigger QMessageBox pop-ups.
        return super().flags() | QgsProcessingAlgorithm.FlagNoThreading

    def initAlgorithm(self, config=None):
        pass

    def processAlgorithm(self, parameters, context, feedback):
        # 1. Grab the currently selected layer
        layer = qgis.utils.iface.activeLayer()
        
        if not layer:
            self.show_popup("Error", "No layer is currently selected in the Layers panel.", Qgis.Critical)
            return {}

        if not layer.isValid():
            self.show_popup("Error", "The selected layer is invalid.", Qgis.Critical)
            return {}

        geom_type = layer.geometryType()
        if geom_type not in [QgsWkbTypes.LineGeometry, QgsWkbTypes.PolygonGeometry]:
            self.show_popup("Failed", "Active layer must be a Line or Polygon.", Qgis.Warning)
            return {}

        # 2. Setup Distance Area for accurate calculation based on project CRS
        da = QgsDistanceArea()
        da.setSourceCrs(layer.crs(), context.transformContext())
        da.setEllipsoid(context.project().crs().ellipsoidAcronym())

        # 3. Determine required fields
        fields_to_check = ['Shape_Length']
        if geom_type == QgsWkbTypes.PolygonGeometry:
            fields_to_check.append('Shape_Area')

        created_fields = []
        
        # 4. Start Editing Session
        if not layer.startEditing():
            self.show_popup("Failed", f"Could not start editing on layer: {layer.name()}", Qgis.Critical)
            return {}

        fields = layer.fields()

        # 5. Check and Create Fields
        for fname in fields_to_check:
            if fields.indexOf(fname) == -1:
                layer.addAttribute(QgsField(fname, QVariant.Double))
                created_fields.append(fname)
        
        if created_fields:
            layer.updateFields()
            fields = layer.fields() # Refresh field indices

        idx_length = fields.indexOf('Shape_Length')
        idx_area = fields.indexOf('Shape_Area') if 'Shape_Area' in fields_to_check else -1

        # 6. Calculate Geometry
        for f in layer.getFeatures():
            geom = f.geometry()
            if geom.isNull() or geom.isEmpty():
                continue
            
            attrs = {}
            if idx_length != -1:
                if geom_type == QgsWkbTypes.PolygonGeometry:
                    attrs[idx_length] = da.measurePerimeter(geom)
                else:
                    attrs[idx_length] = da.measureLength(geom)
                    
            if idx_area != -1 and geom_type == QgsWkbTypes.PolygonGeometry:
                attrs[idx_area] = da.measureArea(geom)
            
            # Apply attribute changes directly
            layer.changeAttributeValues(f.id(), attrs)

        # 7. Commit changes to save
        if not layer.commitChanges():
            self.show_popup("Failed", "Could not commit changes to the layer.", Qgis.Critical)
            return {}

        # 8. Success Output
        msg = f"Successfully updated geometry fields for '{layer.name()}'."
        if created_fields:
            msg += f"\n\nNew fields created: {', '.join(created_fields)}"
        
        self.show_popup("Success", msg, Qgis.Success)

        return {}

    def show_popup(self, title, text, level):
        """Helper method to show a blocking pop-up message to the user."""
        parent = qgis.utils.iface.mainWindow()
        if level == Qgis.Success:
            QMessageBox.information(parent, title, text)
        elif level == Qgis.Critical:
            QMessageBox.critical(parent, title, text)
        else:
            QMessageBox.warning(parent, title, text)
# -*- coding: utf-8 -*-
from qgis.PyQt.QtCore import QCoreApplication, QVariant
from qgis.PyQt.QtWidgets import QMessageBox
from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterEnum,
    QgsProcessingParameterVectorLayer,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsField,
    Qgis
)
import qgis.utils
from ..branding import docs_url


class UpdateXYFields(QgsProcessingAlgorithm):
    def tr(self, string):
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return UpdateXYFields()

    def name(self):
        return 'updatexyfields'

    def displayName(self):
        return self.tr('Update X/Y or Lat/Lon Fields')

    def group(self):
        return self.tr('KGA Geometry Utilities')

    def groupId(self):
        return 'kgageometryutilities'

    def helpUrl(self):
        return docs_url('updatexyfields')

    def shortHelpString(self):
        return self.tr("Updates the selected layer directly. Calculates POINT_X / POINT_Y (Current CRS) or LONGITUDE / LATITUDE (WGS 84). For lines and polygons, it calculates the centroid. Creates fields if missing.")

    def flags(self):
        # FlagNoThreading forces the script to run in the main thread.
        # REQUIRED for safely editing the layer in place and triggering QMessageBox pop-ups.
        return super().flags() | QgsProcessingAlgorithm.FlagNoThreading

    def initAlgorithm(self, config=None):
        # 1. Input Layer Parameter
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                'INPUT',
                self.tr('Input Layer'),
                types=[QgsProcessing.TypeVectorAnyGeometry]
            )
        )

        # 2. Dropdown for coordinate format
        self.addParameter(
            QgsProcessingParameterEnum(
                'COORD_FORMAT',
                self.tr('Coordinate Format'),
                options=['X / Y (Layer CRS)', 'Longitude / Latitude (WGS 84 / EPSG:4326)'],
                defaultValue=0
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        # Retrieve the layer from the user's input selection
        layer = self.parameterAsVectorLayer(parameters, 'INPUT', context)
        
        if not layer or not layer.isValid():
            self.show_popup("Error", "The selected input layer is invalid or missing.", Qgis.Critical)
            return {}

        # Get user choice
        coord_choice = self.parameterAsEnum(parameters, 'COORD_FORMAT', context)
        is_latlon = (coord_choice == 1)

        # Determine field names based on choice
        if is_latlon:
            x_field = 'LONGITUDE'
            y_field = 'LATITUDE'
        else:
            x_field = 'POINT_X'
            y_field = 'POINT_Y'

        fields_to_check = [x_field, y_field]
        created_fields = []

        # Setup Coordinate Transformation (if Lat/Lon chosen)
        transform = None
        if is_latlon:
            source_crs = layer.crs()
            target_crs = QgsCoordinateReferenceSystem("EPSG:4326")
            transform = QgsCoordinateTransform(source_crs, target_crs, context.project())

        # Start Editing Session
        if not layer.startEditing():
            self.show_popup("Failed", f"Could not start editing on layer: {layer.name()}", Qgis.Critical)
            return {}

        fields = layer.fields()

        # Check and Create Fields
        for fname in fields_to_check:
            if fields.indexOf(fname) == -1:
                layer.addAttribute(QgsField(fname, QVariant.Double))
                created_fields.append(fname)
        
        if created_fields:
            layer.updateFields()
            fields = layer.fields() # Refresh field indices

        idx_x = fields.indexOf(x_field)
        idx_y = fields.indexOf(y_field)

        # Calculate Geometry
        for f in layer.getFeatures():
            geom = f.geometry()
            if geom.isNull() or geom.isEmpty():
                continue
            
            # Use centroid to safely handle Points, MultiPoints, Lines, and Polygons
            centroid_geom = geom.centroid()
            if centroid_geom.isNull():
                continue
                
            pt = centroid_geom.asPoint()

            # Transform coordinates if user chose Lat/Lon
            if is_latlon and transform:
                try:
                    pt = transform.transform(pt)
                except Exception as e:
                    feedback.reportError(f"Transformation failed for feature {f.id()}: {e}")
                    continue
            
            # Apply attribute changes directly
            attrs = {
                idx_x: pt.x(),
                idx_y: pt.y()
            }
            layer.changeAttributeValues(f.id(), attrs)

        # Commit changes to save
        if not layer.commitChanges():
            self.show_popup("Failed", "Could not commit changes to the layer.", Qgis.Critical)
            return {}

        # Success Output
        msg = f"Successfully updated coordinates for '{layer.name()}'."
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
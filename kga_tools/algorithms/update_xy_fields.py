# -*- coding: utf-8 -*-
from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtWidgets import QMessageBox
from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterEnum,
    QgsProcessingParameterVectorLayer,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsProject,
    Qgis
)
import qgis.utils
from ..branding import docs_url
from ..core.compat import T_DOUBLE, make_field


#: The window, kept alive between opens so it is not garbage collected.
DIALOG_INSTANCE = None


def _alive(dialog):
    try:
        return dialog is not None and not dialog._torn_down
    except RuntimeError:                    # the Qt object is already gone
        return False


def close_update_xy():
    """Close the window on plugin unload."""
    global DIALOG_INSTANCE
    dialog, DIALOG_INSTANCE = DIALOG_INSTANCE, None
    if dialog is None:
        return
    try:
        dialog.shutdown()
    except RuntimeError:                    # already destroyed by Qt
        pass


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
        return super().flags() | QgsProcessingAlgorithm.Flag.FlagNoThreading

    def initAlgorithm(self, config=None):
        # 1. Input Layer Parameter
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                'INPUT',
                self.tr('Input Layer'),
                types=[QgsProcessing.SourceType.TypeVectorAnyGeometry]
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

    @staticmethod
    def update_layer(layer, is_latlon, project=None, feedback=None):
        """Write the X/Y or Lon/Lat fields of *layer*; return (created, updated).

        Shared by the Processing run and the small window. Raises RuntimeError
        with a message fit to show the user when the layer cannot be edited.
        """
        x_field, y_field = ('LONGITUDE', 'LATITUDE') if is_latlon else ('POINT_X', 'POINT_Y')
        created_fields = []

        transform = None
        if is_latlon:
            transform = QgsCoordinateTransform(
                layer.crs(), QgsCoordinateReferenceSystem("EPSG:4326"),
                project or QgsProject.instance())

        if not layer.startEditing():
            raise RuntimeError("Could not start editing on layer: {}".format(layer.name()))

        fields = layer.fields()
        for fname in (x_field, y_field):
            if fields.indexOf(fname) == -1:
                layer.addAttribute(make_field(fname, T_DOUBLE))
                created_fields.append(fname)
        if created_fields:
            layer.updateFields()
            fields = layer.fields()

        idx_x = fields.indexOf(x_field)
        idx_y = fields.indexOf(y_field)
        updated = 0

        for f in layer.getFeatures():
            geom = f.geometry()
            if geom.isNull() or geom.isEmpty():
                continue
            # Centroid handles points, multipoints, lines and polygons alike.
            centroid_geom = geom.centroid()
            if centroid_geom.isNull():
                continue
            pt = centroid_geom.asPoint()
            if transform is not None:
                try:
                    pt = transform.transform(pt)
                except Exception as e:
                    if feedback is not None:
                        feedback.reportError(f"Transformation failed for feature {f.id()}: {e}")
                    continue
            layer.changeAttributeValues(f.id(), {idx_x: pt.x(), idx_y: pt.y()})
            updated += 1

        if not layer.commitChanges():
            errors = "; ".join(layer.commitErrors()[:3])
            layer.rollBack()
            raise RuntimeError("Could not commit changes to the layer. " + errors)
        return created_fields, updated

    def processAlgorithm(self, parameters, context, feedback):
        layer = self.parameterAsVectorLayer(parameters, 'INPUT', context)
        if not layer or not layer.isValid():
            self.show_popup("Error", "The selected input layer is invalid or missing.", Qgis.MessageLevel.Critical)
            return {}

        is_latlon = (self.parameterAsEnum(parameters, 'COORD_FORMAT', context) == 1)
        try:
            created_fields, _updated = self.update_layer(
                layer, is_latlon, context.project(), feedback)
        except RuntimeError as exc:
            self.show_popup("Failed", str(exc), Qgis.MessageLevel.Critical)
            return {}

        msg = f"Successfully updated coordinates for '{layer.name()}'."
        if created_fields:
            msg += f"\n\nNew fields created: {', '.join(created_fields)}"
        self.show_popup("Success", msg, Qgis.MessageLevel.Success)
        return {}

    def createCustomParametersWidget(self, parent=None):
        """Open the small Update X/Y window instead of the plain form.

        The parameters stay, so scripts and the modeller still work.
        """
        global DIALOG_INSTANCE
        if qgis.utils.iface is None:
            return None
        from ..gui.update_xy_dialog import UpdateXYDialog

        if not _alive(DIALOG_INSTANCE):
            DIALOG_INSTANCE = UpdateXYDialog(qgis.utils.iface.mainWindow())
        return DIALOG_INSTANCE

    def show_popup(self, title, text, level):
        """Helper method to show a blocking pop-up message to the user."""
        parent = qgis.utils.iface.mainWindow()
        if level == Qgis.MessageLevel.Success:
            QMessageBox.information(parent, title, text)
        elif level == Qgis.MessageLevel.Critical:
            QMessageBox.critical(parent, title, text)
        else:
            QMessageBox.warning(parent, title, text)
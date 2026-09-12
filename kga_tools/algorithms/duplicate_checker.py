# -*- coding: utf-8 -*-
from qgis.PyQt.QtCore import QCoreApplication, Qt
from qgis.PyQt.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QComboBox,
                                 QPushButton, QFormLayout, QMessageBox)
from qgis.core import (QgsProcessingAlgorithm, QgsProject, QgsMapLayerProxyModel,
                       QgsCategorizedSymbolRenderer, QgsRendererCategory, QgsSymbol)
from qgis.gui import QgsMapLayerComboBox, QgsFieldComboBox
from ..branding import docs_url, help_button
from ..core.compat import no_threading
# `qgis.PyQt.sip` is the name that works both where sip is a
# top-level module and where it is only PyQt5.sip; see modify_base.
from ..gui.modify_base import is_deleted as _is_deleted

try:
    from qgis.utils import iface
except ImportError:
    iface = None

DIALOG_INSTANCE = None

class DuplicateCheckerDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Duplicate Checker")
        self.setMinimumWidth(400)
        self.setWindowFlags(self.windowFlags() | Qt.Window)

        layout = QVBoxLayout(self)
        self.form = QFormLayout()

        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setFilters(QgsMapLayerProxyModel.VectorLayer)
        self.form.addRow("Input Layer:", self.layer_combo)

        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Check Duplicate Field Value", "Check Duplicate Geometry"])
        self.form.addRow("Mode:", self.mode_combo)

        self.field_combo = QgsFieldComboBox()
        self.form.addRow("Field:", self.field_combo)

        self.highlight_combo = QComboBox()
        self.highlight_combo.addItems(["Select Duplicate Features", "Symbolize (Categorize)"])
        self.form.addRow("Highlight Action:", self.highlight_combo)

        layout.addLayout(self.form)

        buttons = QHBoxLayout()
        self.run_btn = QPushButton("Detect and Highlight Duplicates")
        buttons.addWidget(self.run_btn)
        buttons.addWidget(help_button('duplicate_checker', self))
        layout.addLayout(buttons)

        self.layer_combo.layerChanged.connect(self.field_combo.setLayer)
        self.mode_combo.currentIndexChanged.connect(self.update_ui)
        self.run_btn.clicked.connect(self.run_detection)
        
        self.field_combo.setLayer(self.layer_combo.currentLayer())
        self.update_ui()

    def update_ui(self):
        is_field_mode = self.mode_combo.currentIndex() == 0
        self.field_combo.setEnabled(is_field_mode)

    def run_detection(self):
        layer = self.layer_combo.currentLayer()
        if not layer or not layer.isValid():
            QMessageBox.warning(self, "Error", "Select a valid vector layer.")
            return

        mode = self.mode_combo.currentIndex()
        action = self.highlight_combo.currentIndex()
        
        seen = {}
        duplicates = set()

        for feat in layer.getFeatures():
            if mode == 0:
                val = feat[self.field_combo.currentField()]
                key = str(val) if val is not None else "NULL"
            else:
                geom = feat.geometry()
                key = geom.asWkt() if not geom.isNull() else "NULL_GEOM"
            
            if key in seen:
                duplicates.add(feat.id())
                duplicates.add(seen[key])
            else:
                seen[key] = feat.id()

        if not duplicates:
            QMessageBox.information(self, "Result", "No duplicates found!")
            return

        if action == 0:
            layer.selectByIds(list(duplicates))
            QMessageBox.information(self, "Result", f"Selected {len(duplicates)} duplicate features.")
        else:
            self.apply_symbology(layer, duplicates)
            QMessageBox.information(self, "Result", f"Symbolized {len(duplicates)} duplicate features.")

    def apply_symbology(self, layer, duplicate_ids):
        field_name = "is_dup_temp"
        idx = layer.fields().indexOf(field_name)
        
        layer.startEditing()
        if idx == -1:
            from qgis.core import QgsField
            from qgis.PyQt.QtCore import QVariant
            layer.dataProvider().addAttributes([QgsField(field_name, QVariant.String)])
            layer.updateFields()
            idx = layer.fields().indexOf(field_name)

        for feat in layer.getFeatures():
            status = "Duplicate" if feat.id() in duplicate_ids else "Unique"
            layer.changeAttributeValue(feat.id(), idx, status)
        layer.commitChanges()

        cat_dup = QgsRendererCategory("Duplicate", QgsSymbol.defaultSymbol(layer.geometryType()), "Duplicate")
        cat_dup.symbol().setColor(Qt.red)
        cat_uniq = QgsRendererCategory("Unique", QgsSymbol.defaultSymbol(layer.geometryType()), "Unique")
        cat_uniq.symbol().setColor(Qt.gray)

        renderer = QgsCategorizedSymbolRenderer(field_name, [cat_dup, cat_uniq])
        layer.setRenderer(renderer)
        layer.triggerRepaint()
        if iface:
            iface.layerTreeView().refreshLayerSymbology(layer)


def close_duplicate_checker():
    """Close the dialog if one is open. Safe to call when none is.

    The dialog is parented to the QGIS main window and kept in a module global,
    so the plugin calls this on unload rather than leaving it behind.
    """
    global DIALOG_INSTANCE
    dialog, DIALOG_INSTANCE = DIALOG_INSTANCE, None
    if dialog is None or _is_deleted(dialog):
        return
    dialog.close()
    dialog.deleteLater()


class DuplicateCheckerAlgorithm(QgsProcessingAlgorithm):
    def createInstance(self):
        return DuplicateCheckerAlgorithm()

    def name(self): return 'duplicate_checker'
    def displayName(self): return 'Duplicate Checker'
    def group(self): return 'KGA Geometry Utilities'
    def groupId(self): return 'kgageometryutilities'
    def helpUrl(self): return docs_url('duplicate_checker')
    def shortHelpString(self): return "Detects and highlights duplicate attributes or geometries."

    def initAlgorithm(self, config=None): pass

    def flags(self):
        # Builds the dialog below in processAlgorithm, and a QWidget may only
        # be constructed on the GUI thread. Running from the KGA toolbar is
        # already on it, but the Processing Toolbox hands the algorithm to a
        # worker thread unless this says otherwise.
        return no_threading(super().flags())

    def processAlgorithm(self, parameters, context, feedback):
        global DIALOG_INSTANCE
        parent = iface.mainWindow() if iface else None

        if DIALOG_INSTANCE is not None and _is_deleted(DIALOG_INSTANCE):
            DIALOG_INSTANCE = None
        if DIALOG_INSTANCE is not None and not DIALOG_INSTANCE.isVisible():
            DIALOG_INSTANCE.deleteLater()
            DIALOG_INSTANCE = None

        if DIALOG_INSTANCE is None:
            DIALOG_INSTANCE = DuplicateCheckerDialog(parent)

        DIALOG_INSTANCE.setWindowModality(Qt.NonModal)
        DIALOG_INSTANCE.show()
        DIALOG_INSTANCE.raise_()
        DIALOG_INSTANCE.activateWindow()
        return {}
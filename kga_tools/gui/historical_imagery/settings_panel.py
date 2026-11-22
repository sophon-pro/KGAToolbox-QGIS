# -*- coding: utf-8 -*-
"""Settings tab: API keys, custom STAC catalog, safety limits."""

from qgis.gui import QgsPasswordLineEdit
from qgis.PyQt.QtCore import pyqtSignal
from qgis.PyQt.QtWidgets import (QCheckBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                                 QPushButton, QSpinBox, QVBoxLayout, QWidget)

from ...core.historical_imagery import settings


class SettingsPanel(QWidget):
    """Embedded in the dock's Settings tab. Emits ``saved`` after the user saves."""

    saved = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # --- Google Earth Engine
        gee = QGroupBox("Earth Engine")
        f = QFormLayout(gee)
        self.gee_project = QLineEdit(settings.get("gee_project"))
        self.gee_project.setPlaceholderText("e.g. my-ee-project-123456")
        self.gee_project.setToolTip(
            "Google Cloud project id registered for Earth Engine "
            "(free for research/education: https://code.earthengine.google.com/register).")
        f.addRow("Project id:", self.gee_project)
        hint = QLabel("Needs the <i>earthengine-api</i> package. Authenticate once in the QGIS "
                      "Python console: <code>import ee; ee.Authenticate()</code>")
        hint.setWordWrap(True)
        f.addRow(hint)
        layout.addWidget(gee)

        # --- Planetary Computer
        pc = QGroupBox("Planetary Computer")
        f = QFormLayout(pc)
        self.pc_key = QgsPasswordLineEdit()
        self.pc_key.setText(settings.get("pc_subscription_key"))
        self.pc_key.setToolTip("Optional. Anonymous access works; a free key raises rate limits.")
        f.addRow("Key (optional):", self.pc_key)
        layout.addWidget(pc)

        # --- Custom STAC
        stac = QGroupBox("Custom STAC catalog")
        f = QFormLayout(stac)
        self.stac_url = QLineEdit(settings.get("custom_stac_url"))
        self.stac_url.setToolTip("STAC API root, e.g. https://earth-search.aws.element84.com/v1 "
                                 "(the plugin POSTs to <root>/search).")
        self.stac_coll = QLineEdit(settings.get("custom_stac_collection"))
        self.stac_coll.setToolTip("Collection id, e.g. sentinel-2-l2a, landsat-c2-l2, naip, cop-dem-glo-30.")
        self.stac_bands = QLineEdit(settings.get("custom_stac_bands"))
        self.stac_bands.setToolTip("Comma-separated asset keys to stack, in R,G,B order "
                                   "(e.g. red,green,blue or image or visual).")
        self.stac_cloud = QLineEdit(settings.get("custom_stac_cloud_property"))
        self.stac_cloud.setToolTip("Item property holding cloud cover in %. Leave empty to disable "
                                   "the cloud filter for this catalog.")
        self.stac_sign = QCheckBox("Sign for Planetary Computer")
        self.stac_sign.setChecked(settings.get("custom_stac_sign_pc"))
        self.stac_sign.setToolTip("Enable when the catalog is https://planetarycomputer.microsoft.com/api/stac/v1")
        f.addRow("API URL:", self.stac_url)
        f.addRow("Collection:", self.stac_coll)
        f.addRow("Bands:", self.stac_bands)
        f.addRow("Cloud field:", self.stac_cloud)
        f.addRow(self.stac_sign)
        layout.addWidget(stac)

        # --- Limits
        lim = QGroupBox("Limits")
        f = QFormLayout(lim)
        self.max_mp = QSpinBox()
        self.max_mp.setRange(10, 20000)
        self.max_mp.setSuffix(" MP / band")
        self.max_mp.setValue(settings.get("max_output_megapixels"))
        self.max_mp.setToolTip("Safety limit on output size per band (1 MP = 1 million pixels).")
        self.timeout = QSpinBox()
        self.timeout.setRange(10, 600)
        self.timeout.setSuffix(" s")
        self.timeout.setValue(settings.get("http_timeout"))
        self.timeout.setToolTip("Timeout for catalog searches.")
        f.addRow("Max output:", self.max_mp)
        f.addRow("Timeout:", self.timeout)
        layout.addWidget(lim)

        row = QHBoxLayout()
        self.saved_label = QLabel()
        self.saved_label.setObjectName("hint")
        self.save_btn = QPushButton("Save")
        self.save_btn.setObjectName("primary")
        self.save_btn.setToolTip("Store these settings for future QGIS sessions.")
        self.save_btn.clicked.connect(self.save)
        self.revert_btn = QPushButton("Revert")
        self.revert_btn.setToolTip("Discard unsaved edits.")
        self.revert_btn.clicked.connect(self.reload)
        row.addWidget(self.saved_label, 1)
        row.addWidget(self.revert_btn)
        row.addWidget(self.save_btn)
        layout.addLayout(row)
        layout.addStretch(1)

    def reload(self):
        self.gee_project.setText(settings.get("gee_project"))
        self.pc_key.setText(settings.get("pc_subscription_key"))
        self.stac_url.setText(settings.get("custom_stac_url"))
        self.stac_coll.setText(settings.get("custom_stac_collection"))
        self.stac_bands.setText(settings.get("custom_stac_bands"))
        self.stac_cloud.setText(settings.get("custom_stac_cloud_property"))
        self.stac_sign.setChecked(settings.get("custom_stac_sign_pc"))
        self.max_mp.setValue(settings.get("max_output_megapixels"))
        self.timeout.setValue(settings.get("http_timeout"))
        self.saved_label.setText("")

    def save(self):
        settings.set("gee_project", self.gee_project.text().strip())
        settings.set("pc_subscription_key", self.pc_key.text().strip())
        settings.set("custom_stac_url", self.stac_url.text().strip())
        settings.set("custom_stac_collection", self.stac_coll.text().strip())
        settings.set("custom_stac_bands", self.stac_bands.text().strip())
        settings.set("custom_stac_cloud_property", self.stac_cloud.text().strip())
        settings.set("custom_stac_sign_pc", self.stac_sign.isChecked())
        settings.set("max_output_megapixels", self.max_mp.value())
        settings.set("http_timeout", self.timeout.value())
        self.saved_label.setText("✓ Saved")
        self.saved.emit()

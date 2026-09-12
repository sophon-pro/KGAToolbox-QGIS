# -*- coding: utf-8 -*-

import os

from qgis.PyQt.QtCore import QCoreApplication, Qt
from qgis.PyQt.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QComboBox,
                                 QCheckBox, QPushButton, QFormLayout,
                                 QMessageBox, QApplication, QProgressBar,
                                 QLineEdit)
from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProject,
    QgsMapLayerProxyModel,
    QgsVectorLayer,
    QgsWkbTypes,
    QgsFeature,
    QgsFeatureRequest,
    QgsGeometry,
    QgsLineString,
    QgsCurvePolygon,
    QgsCurve,
    QgsPoint,
    QgsFields,
    QgsField,
    QgsVectorFileWriter,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsCsException,
    QgsDistanceArea,
    QgsUnitTypes,
    QgsProviderRegistry,
)
from qgis.gui import QgsMapLayerComboBox, QgsFileWidget
from ..branding import docs_url, help_button
from ..core.compat import T_DOUBLE, T_LONGLONG, make_field
# `qgis.PyQt.sip` is the name that works both where sip is a
# top-level module and where it is only PyQt5.sip; see modify_base.
from ..gui.modify_base import is_deleted as _is_deleted

try:
    from qgis.utils import iface
except ImportError:
    iface = None

# Global reference keeps the modeless dialog alive after processAlgorithm() returns.
DIALOG_INSTANCE = None

# Conversion modes
MODE_LINES = 0
MODE_CENTRAL = 1
MODE_BOUNDARY = 2
MODE_VERTICES = 3

# Output destinations
DEST_TEMP = 0
DEST_FOLDER = 1
DEST_GPKG = 2
DEST_GDB = 3

# Default output name per conversion mode
MODE_SUFFIX = {
    MODE_LINES: 'lines',
    MODE_CENTRAL: 'points',
    MODE_BOUNDARY: 'boundary_pts',
    MODE_VERTICES: 'vertices',
}

# Characters Windows rejects in a file name
INVALID_FILENAME_CHARS = '<>:"/\\|?*'

# Column names each destination reserves for the primary key it maintains
# itself. A source field of the same name has to be renamed, or the insert
# fails on the UNIQUE constraint once a second feature reuses the value.
RESERVED_FIELDS = {
    DEST_GPKG: frozenset(['fid']),
    DEST_GDB: frozenset(['objectid', 'shape']),
}

CHUNK_SIZE = 10000


# ----------------------------------------------------------------- the dialog
class DynamicGeometryDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Geometry Conversion")
        self.setMinimumWidth(520)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.Window)

        layout = QVBoxLayout(self)
        self.form = QFormLayout()
        form = self.form

        # Input Layer
        self.layer_combo = QgsMapLayerComboBox()
        form.addRow("Input Layer:", self.layer_combo)

        # Conversion Modes
        self.mode_combo = QComboBox()
        self.mode_combo.addItems([
            "Polygon → Two-Point Lines",
            "Polygon → Central Point",
            "Polygon → Boundary Points",
            "Line → Vertices"
        ])
        form.addRow("Conversion Mode:", self.mode_combo)

        # Dynamic Option: Holes
        self.holes_cb = QCheckBox("Include holes")
        self.holes_cb.setChecked(True)
        form.addRow("", self.holes_cb)

        # Dynamic Option: Point Method
        self.point_method_combo = QComboBox()
        self.point_method_combo.addItems(["Point on Surface", "Centroid"])
        form.addRow("Point Method:", self.point_method_combo)

        # Dynamic Option: one central point per part instead of per feature
        self.per_part_cb = QCheckBox("One central point per part")
        self.per_part_cb.setChecked(False)
        form.addRow("", self.per_part_cb)

        # Selection handling
        self.selected_cb = QCheckBox("Selected features only")
        self.selected_cb.setChecked(False)
        form.addRow("", self.selected_cb)

        # Dynamic Option: Deduplication
        self.dedup_cb = QCheckBox("Remove duplicate geometries")
        self.dedup_cb.setChecked(False)
        form.addRow("", self.dedup_cb)

        # Dynamic Option: Add Geometry Fields
        self.geom_fields_cb = QCheckBox("Add geometry fields (XY/LatLon or Length)")
        self.geom_fields_cb.setChecked(True)
        form.addRow("", self.geom_fields_cb)

        # Where the result is stored
        self.dest_combo = QComboBox()
        self.dest_combo.addItems([
            "Temporary layer",
            "Folder (ESRI Shapefile)",
            "GeoPackage (.gpkg)",
            "File Geodatabase (.gdb)"
        ])
        form.addRow("Store Output In:", self.dest_combo)

        # Folder / database container
        self.output_location = QgsFileWidget()
        form.addRow("Folder / Database:", self.output_location)

        # Shapefile base name, or layer name inside the database
        self.output_name = QLineEdit()
        form.addRow("Output Name:", self.output_name)

        layout.addLayout(form)

        buttons = QHBoxLayout()
        self.run_btn = QPushButton("Run Conversion")
        self.run_btn.setMinimumHeight(30)
        buttons.addWidget(self.run_btn)
        buttons.addWidget(help_button('geometry_conversion_dynamic', self))
        layout.addLayout(buttons)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        # Connect signals for dynamic behavior
        self.mode_combo.currentIndexChanged.connect(self.update_ui)
        self.dest_combo.currentIndexChanged.connect(self.update_destination_ui)
        # Only refresh the suggested name here; update_ui() calls setFilters(),
        # which re-emits layerChanged and would recurse.
        self.layer_combo.layerChanged.connect(self.update_default_name)
        self.run_btn.clicked.connect(self.run_conversion)

        self.update_ui()

    # ---------------- UI helpers ----------------
    def _set_row_visible(self, widget, visible):
        """Hide a form row, label included."""
        widget.setVisible(visible)
        label = self.form.labelForField(widget)
        if label:
            label.setVisible(visible)

    def update_ui(self):
        mode = self.mode_combo.currentIndex()

        self._set_row_visible(self.holes_cb, mode in (MODE_LINES, MODE_BOUNDARY))
        self._set_row_visible(self.point_method_combo, mode == MODE_CENTRAL)
        self._set_row_visible(self.per_part_cb, mode == MODE_CENTRAL)

        # Restrict layer types based on mode
        if mode == MODE_VERTICES:
            self.layer_combo.setFilters(QgsMapLayerProxyModel.Filter.LineLayer)
        else:
            self.layer_combo.setFilters(QgsMapLayerProxyModel.Filter.PolygonLayer)

        self.update_default_name()
        self.update_destination_ui()

    def update_destination_ui(self):
        """Point the browse button at a folder or a database container."""
        dest = self.dest_combo.currentIndex()

        if dest == DEST_FOLDER:
            self.output_location.setStorageMode(QgsFileWidget.StorageMode.GetDirectory)
            self.output_location.setDialogTitle("Select Output Folder")
            self.output_location.setFilter("")
            self.output_location.lineEdit().setPlaceholderText(
                "Folder that will receive the Shapefile")
            self._set_row_label(self.output_location, "Output Folder:")
        elif dest == DEST_GPKG:
            # A .gpkg is a single file, so browse for one.
            self.output_location.setStorageMode(QgsFileWidget.StorageMode.GetFile)
            self.output_location.setDialogTitle("Select GeoPackage")
            self.output_location.setFilter("GeoPackage (*.gpkg);;All files (*.*)")
            self.output_location.lineEdit().setPlaceholderText(
                "Existing .gpkg, or a new path to create one")
            self._set_row_label(self.output_location, "GeoPackage:")
        elif dest == DEST_GDB:
            # A .gdb is a directory, so the directory browser is the right one.
            self.output_location.setStorageMode(QgsFileWidget.StorageMode.GetDirectory)
            self.output_location.setDialogTitle("Select File Geodatabase (.gdb)")
            self.output_location.setFilter("")
            self.output_location.lineEdit().setPlaceholderText(
                "Existing .gdb folder, or a new path to create one")
            self._set_row_label(self.output_location, "File Geodatabase:")

        is_file_out = dest != DEST_TEMP
        self._set_row_visible(self.output_location, is_file_out)
        self._set_row_visible(self.output_name, is_file_out)
        if is_file_out:
            self._set_row_label(
                self.output_name,
                "Shapefile Name:" if dest == DEST_FOLDER else "Output Layer Name:")

    def _set_row_label(self, widget, text):
        label = self.form.labelForField(widget)
        if label:
            label.setText(text)

    def default_output_name(self):
        layer = self.layer_combo.currentLayer()
        base = layer.name() if layer else "output"
        suffix = MODE_SUFFIX.get(self.mode_combo.currentIndex(), "converted")
        return "%s_%s" % (base, suffix)

    def update_default_name(self):
        """Show the suggested name as a placeholder, never overwriting typing."""
        self.output_name.setPlaceholderText(self.default_output_name())

    def resolved_output_name(self):
        return self.output_name.text().strip() or self.default_output_name()

    # ---------------- Destination resolution ----------------
    @staticmethod
    def _sanitize_filename(name):
        cleaned = ''.join('_' if (c in INVALID_FILENAME_CHARS or ord(c) < 32) else c
                          for c in name)
        return cleaned.strip().strip('.')

    @staticmethod
    def _sanitize_layer_name(name, for_gdb=False):
        # Unicode is fine in both GPKG and File GDB; only structural characters
        # have to go so the layer stays addressable via |layername=.
        cleaned = ''.join('_' if (c in '"\'`\\/|' or ord(c) < 32) else c
                          for c in name).strip()
        if for_gdb:
            # File GDB table names may not start with a digit.
            if cleaned and cleaned[0].isdigit():
                cleaned = '_' + cleaned
            cleaned = cleaned[:160]
        return cleaned

    @staticmethod
    def _write_cpg(shp_path):
        """Ensure a UTF-8 .cpg sidecar so Khmer attributes survive in the DBF."""
        cpg = os.path.splitext(shp_path)[0] + '.cpg'
        try:
            if not os.path.exists(cpg):
                with open(cpg, 'w') as fh:
                    fh.write('UTF-8')
        except OSError:
            pass

    @staticmethod
    def _existing_sublayers(path):
        """Layer names already present in a container, lowercased."""
        try:
            details = QgsProviderRegistry.instance().querySublayers(path)
            return set(d.name().lower() for d in details)
        except Exception:
            return set()

    def _confirm(self, text):
        return QMessageBox.question(
            self, "Overwrite?", text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes

    @staticmethod
    def _release_existing_layers(dest):
        """Drop project layers holding the output open, and report how many.

        Windows keeps a lock on an open dataset, so overwriting fails while the
        previous result is still loaded. The fresh version is added back to the
        project as soon as writing finishes.
        """
        target = os.path.normcase(os.path.normpath(dest['path']))
        doomed = []
        for layer_id, layer in QgsProject.instance().mapLayers().items():
            parts = layer.source().split('|')
            try:
                path = os.path.normcase(os.path.normpath(os.path.abspath(parts[0])))
            except (OSError, ValueError):
                continue
            if path != target:
                continue
            if dest['kind'] != DEST_FOLDER:
                # Release the layer being replaced, never its neighbours.
                names = [p.split('=', 1)[1] for p in parts[1:]
                         if p.lower().startswith('layername=')]
                if names and names[0] != dest['layer_name']:
                    continue
            doomed.append(layer_id)
        if doomed:
            QgsProject.instance().removeMapLayers(doomed)
        return len(doomed)

    def resolve_destination(self):
        """Turn the destination widgets into concrete writer settings.

        Returns a dict, or None when the settings are unusable or the user
        declined an overwrite (a message has already been shown).
        """
        dest = self.dest_combo.currentIndex()
        if dest == DEST_TEMP:
            return {'kind': DEST_TEMP, 'driver': None, 'path': '',
                    'layer_name': '', 'action': None}

        location = self.output_location.filePath().strip().strip('"')
        if not location:
            QMessageBox.warning(
                self, "Error",
                "Please choose an output folder."
                if dest == DEST_FOLDER else
                "Please choose the database that will receive the layer.")
            return None
        location = os.path.normpath(os.path.abspath(location))
        raw_name = self.resolved_output_name()

        if dest == DEST_FOLDER:
            if not os.path.isdir(location):
                QMessageBox.warning(self, "Error",
                                    "Output folder does not exist:\n%s" % location)
                return None
            base = self._sanitize_filename(os.path.splitext(raw_name)[0])
            if not base:
                QMessageBox.warning(self, "Error", "Please enter a Shapefile name.")
                return None
            path = os.path.join(location, base + '.shp')
            replacing = os.path.exists(path)
            if replacing and not self._confirm(
                    "This Shapefile already exists:\n%s\n\n"
                    "Replace it? Any copy currently loaded in the project will "
                    "be reloaded from the new file." % path):
                return None
            return {'kind': DEST_FOLDER, 'driver': 'ESRI Shapefile', 'path': path,
                    'layer_name': base, 'action': None, 'replacing': replacing}

        # --- database destinations ---
        if dest == DEST_GPKG:
            driver, suffix, label = 'GPKG', '.gpkg', 'GeoPackage'
            if os.path.isdir(location):
                QMessageBox.warning(
                    self, "Error",
                    "This is a folder, not a GeoPackage:\n%s\n\n"
                    "Pick a .gpkg file, or switch to the Folder destination."
                    % location)
                return None
        else:
            driver, suffix, label = 'OpenFileGDB', '.gdb', 'File Geodatabase'
            if driver not in [d.driverName
                              for d in QgsVectorFileWriter.ogrDriverList()]:
                QMessageBox.warning(
                    self, "Error",
                    "This QGIS build has no OpenFileGDB write driver, so File "
                    "Geodatabase output is not available.\nGDAL 3.6 or newer "
                    "is required.")
                return None

        if not location.lower().endswith(suffix):
            location += suffix

        parent = os.path.dirname(location)
        if parent and not os.path.isdir(parent):
            QMessageBox.warning(self, "Error",
                                "Folder does not exist:\n%s" % parent)
            return None

        # A .gpkg is a file, a .gdb is a directory.
        exists = os.path.isdir(location) if dest == DEST_GDB \
            else os.path.isfile(location)

        layer_name = self._sanitize_layer_name(raw_name, for_gdb=(dest == DEST_GDB))
        if not layer_name:
            QMessageBox.warning(self, "Error", "Please enter an output layer name.")
            return None

        replacing = False
        if exists:
            if layer_name.lower() in self._existing_sublayers(location):
                if not self._confirm(
                        "The layer '%s' already exists in this %s.\n\n"
                        "Replace it? A copy currently loaded in the project will "
                        "be reloaded from the new layer." % (layer_name, label)):
                    return None
                replacing = True
            # Only ever touch the one layer, never the whole container.
            action = QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer
        else:
            action = None  # default: create the container

        return {'kind': dest, 'driver': driver, 'path': location,
                'layer_name': layer_name, 'action': action, 'replacing': replacing}

    # ---------------- Field naming ----------------
    @staticmethod
    def _make_name_factory(existing_names, max_len):
        """Return a function producing unique (optionally truncated) field names."""
        taken = set(existing_names)

        def get_safe_field_name(base_name):
            name = base_name[:max_len]
            counter = 1
            while name.lower() in taken:
                suffix = "_%d" % counter
                name = base_name[:max(1, max_len - len(suffix))] + suffix
                counter += 1
            taken.add(name.lower())
            return name

        return get_safe_field_name

    # ---------------- Main entry point ----------------
    def run_conversion(self):
        source = self.layer_combo.currentLayer()
        if not source or not isinstance(source, QgsVectorLayer) or not source.isValid():
            QMessageBox.warning(self, "Error", "Please select a valid input layer.")
            return

        mode = self.mode_combo.currentIndex()

        # Guard against a stale selection that does not match the mode.
        want = (QgsWkbTypes.GeometryType.LineGeometry if mode == MODE_VERTICES
                else QgsWkbTypes.GeometryType.PolygonGeometry)
        if source.geometryType() != want:
            QMessageBox.warning(
                self, "Error",
                "'%s' is a %s layer.\nThe selected mode needs a %s layer."
                % (source.name(),
                   QgsWkbTypes.geometryDisplayString(source.geometryType()),
                   QgsWkbTypes.geometryDisplayString(want)))
            return

        include_holes = self.holes_cb.isChecked()
        point_method = self.point_method_combo.currentIndex()
        per_part = self.per_part_cb.isChecked()
        selected_only = self.selected_cb.isChecked()
        remove_duplicates = self.dedup_cb.isChecked()
        add_geom_fields = self.geom_fields_cb.isChecked()

        if selected_only and source.selectedFeatureCount() == 0:
            QMessageBox.warning(self, "Error",
                                "'Selected features only' is on but nothing is selected.")
            return

        dest = self.resolve_destination()
        if dest is None:
            return
        is_temp = dest['kind'] == DEST_TEMP
        is_shp = dest['kind'] == DEST_FOLDER

        # Build base output WKB type. Centroid / pointOnSurface always return a
        # flat 2D point, so Z and M must not be advertised for that mode.
        in_wkb = source.wkbType()
        keep_zm = (mode != MODE_CENTRAL)
        has_z = keep_zm and QgsWkbTypes.hasZ(in_wkb)
        has_m = keep_zm and QgsWkbTypes.hasM(in_wkb)

        out_wkb = QgsWkbTypes.Type.LineString if mode == MODE_LINES else QgsWkbTypes.Type.Point
        if has_z:
            out_wkb = QgsWkbTypes.addZ(out_wkb)
        if has_m:
            out_wkb = QgsWkbTypes.addM(out_wkb)

        # Prepare fields, avoiding collisions with the source attributes.
        #
        # A GeoPackage source exposes its primary key 'fid' as an ordinary
        # attribute. Every feature exploded out of one input row carries the
        # same value, so copying that column straight through makes the
        # destination reject all but the first insert on its UNIQUE primary
        # key. Rename it instead: the value is kept, the key stays the
        # destination's to assign.
        reserved = RESERVED_FIELDS.get(dest['kind'], frozenset())
        in_fields = source.fields()
        taken_names = set(f.name().lower() for f in in_fields)
        out_fields = QgsFields()
        renamed_fields = []
        for f in in_fields:
            field = QgsField(f)
            if field.name().lower() in reserved:
                base = "%s_src" % field.name()
                new_name = base
                counter = 1
                while new_name.lower() in taken_names:
                    counter += 1
                    new_name = "%s_%d" % (base, counter)
                taken_names.add(new_name.lower())
                renamed_fields.append((field.name(), new_name))
                field.setName(new_name)
            out_fields.append(field)

        # DBF caps names at 10 characters; shorten up-front so uniqueness still
        # holds after the driver truncates.
        max_len = 10 if is_shp else 63
        get_safe_field_name = self._make_name_factory(
            [f.name().lower()[:max_len] for f in out_fields] + list(reserved),
            max_len)

        ID_TYPE = T_LONGLONG
        DBL_TYPE = T_DOUBLE

        spec = [('source_id', 'source_id', ID_TYPE)]
        if mode in (MODE_LINES, MODE_BOUNDARY, MODE_VERTICES) or per_part:
            spec.append(('part_id', 'part_id', ID_TYPE))
        if mode in (MODE_LINES, MODE_BOUNDARY):
            spec.append(('ring_id', 'ring_id', ID_TYPE))
        if mode == MODE_LINES:
            spec.append(('segment_id', 'segment_id', ID_TYPE))
        if mode in (MODE_BOUNDARY, MODE_VERTICES):
            spec.append(('vertex_id', 'vertex_id', ID_TYPE))
            spec.append(('vertex_order', 'vertex_order', ID_TYPE))

        # Measurement or coordinate metadata fields
        is_geographic = source.crs().isGeographic()
        if add_geom_fields:
            if mode == MODE_LINES:
                spec.append(('length_m', 'length_m', DBL_TYPE))
            else:
                spec.append(('x', 'x', DBL_TYPE))
                spec.append(('y', 'y', DBL_TYPE))
                if not is_geographic:
                    spec.append(('latitude', 'latitude', DBL_TYPE))
                    spec.append(('longitude', 'longitude', DBL_TYPE))

        meta_fields = {}
        for key, base, ftype in spec:
            name = get_safe_field_name(base)
            meta_fields[key] = name
            out_fields.append(make_field(name, ftype))

        # Setup the output sink (memory or file)
        out_layer = None
        sink = None
        if is_temp:
            base_type = "LineString" if mode == MODE_LINES else "Point"
            if has_z:
                base_type += "Z"
            if has_m:
                base_type += "M"
            # authid() is empty for custom CRSs, so fall back to full WKT.
            crs_ref = source.crs().authid() or source.crs().toWkt()
            uri = "%s?crs=%s" % (base_type, crs_ref)

            out_layer = QgsVectorLayer(uri, "%s (Converted)" % source.name(), "memory")
            if not out_layer.isValid():
                QMessageBox.warning(self, "Error",
                                    "Could not create the temporary layer.")
                return
            sink = out_layer.dataProvider()
            sink.addAttributes(out_fields.toList())
            out_layer.updateFields()
        else:
            # An open dataset is locked on Windows, so let go of the previous
            # result before trying to replace it.
            if dest.get('replacing'):
                self._release_existing_layers(dest)
                if is_shp and os.path.exists(dest['path']) \
                        and not QgsVectorFileWriter.deleteShapeFile(dest['path']):
                    QMessageBox.warning(
                        self, "Error",
                        "Could not replace the existing Shapefile:\n%s\n\n"
                        "It may be open in another program." % dest['path'])
                    return

            save_options = QgsVectorFileWriter.SaveVectorOptions()
            save_options.driverName = dest['driver']
            save_options.fileEncoding = "UTF-8"
            if dest['kind'] != DEST_FOLDER:
                # Naming the layer keeps sibling layers in the container intact.
                save_options.layerName = dest['layer_name']
            if dest['action'] is not None:
                save_options.actionOnExistingFile = dest['action']

            try:
                sink = QgsVectorFileWriter.create(
                    dest['path'],
                    out_fields,
                    out_wkb,
                    source.crs(),
                    QgsProject.instance().transformContext(),
                    save_options
                )
            except Exception as exc:
                QMessageBox.warning(self, "Error",
                                    "Could not create output file:\n%s" % exc)
                return

            if sink is None or sink.hasError() != QgsVectorFileWriter.WriterError.NoError:
                msg = sink.errorMessage() if sink is not None else "unknown error"
                QMessageBox.warning(self, "Error",
                                    "Could not create output file:\n%s" % msg)
                return

        # Transformation for the lat/lon columns when the layer is projected
        transform = None
        if add_geom_fields and mode != MODE_LINES and not is_geographic:
            transform = QgsCoordinateTransform(
                source.crs(),
                QgsCoordinateReferenceSystem("EPSG:4326"),
                QgsProject.instance())

        # Ellipsoidal length measurement for MODE_LINES
        distance_area = None
        if add_geom_fields and mode == MODE_LINES:
            distance_area = QgsDistanceArea()
            distance_area.setSourceCrs(source.crs(),
                                       QgsProject.instance().transformContext())
            distance_area.setEllipsoid(QgsProject.instance().ellipsoid())

        # Execution setup
        total = (source.selectedFeatureCount() if selected_only
                 else source.featureCount())
        self.run_btn.setText("Processing...")
        self.run_btn.setEnabled(False)
        self.progress.setRange(0, max(0, total))
        self.progress.setValue(0)
        self.progress.setVisible(True)
        QApplication.processEvents()

        created_count = 0
        skipped_count = 0
        write_errors = []
        new_features = []
        seen_geometries = set() if remove_duplicates else None

        ctx = dict(fields=out_fields, meta=meta_fields, out=new_features,
                   dedup=seen_geometries, add_fields=add_geom_fields,
                   transform=transform)

        def flush():
            if not new_features:
                return
            if not sink.addFeatures(new_features):
                err = sink.errorMessage() if hasattr(sink, 'errorMessage') else ''
                if err and err not in write_errors:
                    write_errors.append(err)
            new_features.clear()

        try:
            request = QgsFeatureRequest()
            features = (source.getSelectedFeatures(request) if selected_only
                        else source.getFeatures(request))

            processed = 0
            for feature in features:
                processed += 1
                geom = feature.geometry()
                if geom.isNull() or geom.isEmpty():
                    skipped_count += 1
                    continue

                # Curved geometries expose no pointN()/exteriorRing() vertices to
                # walk, so approximate them with straight segments first.
                abstract = geom.constGet()
                if abstract is not None and abstract.hasCurvedSegments():
                    geom = QgsGeometry(abstract.segmentize())

                base_attrs = list(feature.attributes())

                if mode == MODE_LINES:
                    created_count += self.poly_to_lines(
                        geom, feature.id(), base_attrs, include_holes,
                        distance_area, ctx)
                elif mode == MODE_CENTRAL:
                    created_count += self.poly_to_central(
                        geom, feature.id(), base_attrs, point_method,
                        per_part, ctx)
                elif mode == MODE_BOUNDARY:
                    created_count += self.poly_to_boundary(
                        geom, feature.id(), base_attrs, include_holes, ctx)
                elif mode == MODE_VERTICES:
                    created_count += self.line_to_vertices(
                        geom, feature.id(), base_attrs, ctx)

                # Insert in chunks to save memory
                if len(new_features) >= CHUNK_SIZE:
                    flush()

                if processed % 200 == 0:
                    self.progress.setValue(processed)
                    QApplication.processEvents()

            flush()
        except Exception as exc:
            self.progress.setVisible(False)
            self.run_btn.setText("Run Conversion")
            self.run_btn.setEnabled(True)
            QMessageBox.critical(self, "Error",
                                 "Conversion failed:\n%s: %s"
                                 % (type(exc).__name__, exc))
            return
        finally:
            if not is_temp:
                # Dropping the writer flushes and finalises the dataset.
                sink = None

        if is_temp:
            out_layer.updateExtents()
            QgsProject.instance().addMapLayer(out_layer)
        else:
            if is_shp:
                self._write_cpg(dest['path'])
                uri = dest['path']
            else:
                uri = "%s|layername=%s" % (dest['path'], dest['layer_name'])
            out_layer = QgsVectorLayer(uri, dest['layer_name'], "ogr")
            if out_layer.isValid():
                QgsProject.instance().addMapLayer(out_layer)
            else:
                QMessageBox.warning(
                    self, "Warning",
                    "Features were written to:\n%s\n\nbut the result could not "
                    "be loaded back into the project." % dest['path'])

        self.progress.setVisible(False)
        self.run_btn.setText("Run Conversion")
        self.run_btn.setEnabled(True)

        # Count what actually landed, not what was generated, so a rejected
        # insert can never be reported as a success.
        written_count = out_layer.featureCount() if out_layer.isValid() else -1
        short = 0 <= written_count < created_count

        msg = "Conversion completed.\nCreated %d output features." % created_count
        if not is_temp:
            msg += ("\n\nWritten to:\n%s" % dest['path'] if is_shp else
                    "\n\nLayer '%s' written to:\n%s"
                    % (dest['layer_name'], dest['path']))
        if skipped_count:
            msg += "\nSkipped %d feature(s) with empty geometry." % skipped_count
        if renamed_fields:
            msg += "\n\nRenamed to avoid the destination's primary key:\n%s" % \
                "\n".join("  %s → %s" % pair for pair in renamed_fields)
        if short:
            msg += ("\n\nWARNING: only %d of %d features reached the output."
                    % (written_count, created_count))
        if write_errors:
            msg += "\n\nWriter reported:\n%s" % "\n".join(write_errors)
        if write_errors or short:
            QMessageBox.warning(self, "Completed with warnings", msg)
        else:
            QMessageBox.information(self, "Success", msg)

    # ---------------- Feature helpers ----------------
    @staticmethod
    def _new_feature(fields, base_attrs, source_id, meta_names):
        """Build an output feature with a correctly sized attribute vector.

        setAttributes() replaces the whole vector, so the source attributes are
        padded out to the output field count first; otherwise every following
        setAttribute() on a metadata column would be out of range.
        """
        feat = QgsFeature(fields)
        attrs = list(base_attrs)
        missing = fields.count() - len(attrs)
        if missing > 0:
            attrs.extend([None] * missing)
        elif missing < 0:
            attrs = attrs[:fields.count()]
        feat.setAttributes(attrs)
        feat.setAttribute(meta_names['source_id'], source_id)
        return feat

    @staticmethod
    def _set_coords(feat, meta_names, x, y, transform):
        feat.setAttribute(meta_names['x'], x)
        feat.setAttribute(meta_names['y'], y)
        if transform is not None:
            try:
                pt = QgsPoint(x, y)
                pt.transform(transform)
                feat.setAttribute(meta_names['longitude'], pt.x())
                feat.setAttribute(meta_names['latitude'], pt.y())
            except QgsCsException:
                pass

    @staticmethod
    def _polygon_rings(geom, include_holes):
        """Yield (part_index, ring_index, ring) for every polygon ring."""
        parts = geom.asGeometryCollection() if geom.isMultipart() else [geom]
        for part_idx, part_geom in enumerate(parts):
            abstract_part = part_geom.constGet()
            # QgsCurvePolygon is the base of QgsPolygon and of any curve polygon
            # that has already been segmentised.
            if not isinstance(abstract_part, QgsCurvePolygon):
                continue

            rings = [abstract_part.exteriorRing()]
            if include_holes:
                rings.extend(abstract_part.interiorRing(i)
                             for i in range(abstract_part.numInteriorRings()))

            for ring_idx, ring in enumerate(rings):
                if ring is None or ring.numPoints() < 2:
                    continue
                yield part_idx, ring_idx, ring

    # ---------------- Geometry Logic ----------------
    def poly_to_lines(self, geom, source_id, base_attrs, include_holes,
                      distance_area, ctx):
        fields, meta = ctx['fields'], ctx['meta']
        feature_list, dedup_set = ctx['out'], ctx['dedup']
        add_fields = ctx['add_fields']
        count = 0

        for part_idx, ring_idx, ring in self._polygon_rings(geom, include_holes):
            for v_idx in range(ring.numPoints() - 1):
                p1 = ring.pointN(v_idx)
                p2 = ring.pointN(v_idx + 1)

                if p1.x() == p2.x() and p1.y() == p2.y():
                    continue  # zero-length segment

                if dedup_set is not None:
                    hash_key = tuple(sorted([(p1.x(), p1.y()), (p2.x(), p2.y())]))
                    if hash_key in dedup_set:
                        continue
                    dedup_set.add(hash_key)

                line_geom = QgsGeometry(QgsLineString([p1, p2]))

                out_feat = self._new_feature(fields, base_attrs, source_id, meta)
                out_feat.setAttribute(meta['part_id'], part_idx)
                out_feat.setAttribute(meta['ring_id'], ring_idx)
                out_feat.setAttribute(meta['segment_id'], v_idx)

                if add_fields:
                    if distance_area is not None:
                        length = distance_area.measureLength(line_geom)
                        length = distance_area.convertLengthMeasurement(
                            length, QgsUnitTypes.DistanceUnit.DistanceMeters)
                    else:
                        length = line_geom.length()
                    out_feat.setAttribute(meta['length_m'], length)

                out_feat.setGeometry(line_geom)
                feature_list.append(out_feat)
                count += 1
        return count

    def poly_to_central(self, geom, source_id, base_attrs, point_method,
                        per_part, ctx):
        fields, meta = ctx['fields'], ctx['meta']
        feature_list, dedup_set = ctx['out'], ctx['dedup']
        add_fields, transform = ctx['add_fields'], ctx['transform']
        count = 0

        if per_part and geom.isMultipart():
            targets = list(enumerate(geom.asGeometryCollection()))
        else:
            targets = [(0, geom)]

        for part_idx, target in targets:
            out_geom = (target.pointOnSurface() if point_method == 0
                        else target.centroid())
            if out_geom.isNull() or out_geom.isEmpty():
                continue

            p = out_geom.asPoint()
            if dedup_set is not None:
                hash_key = (p.x(), p.y())
                if hash_key in dedup_set:
                    continue
                dedup_set.add(hash_key)

            out_feat = self._new_feature(fields, base_attrs, source_id, meta)
            if 'part_id' in meta:
                out_feat.setAttribute(meta['part_id'], part_idx)

            if add_fields:
                self._set_coords(out_feat, meta, p.x(), p.y(), transform)

            out_feat.setGeometry(out_geom)
            feature_list.append(out_feat)
            count += 1
        return count

    def poly_to_boundary(self, geom, source_id, base_attrs, include_holes, ctx):
        fields, meta = ctx['fields'], ctx['meta']
        feature_list, dedup_set = ctx['out'], ctx['dedup']
        add_fields, transform = ctx['add_fields'], ctx['transform']
        count = 0
        vertex_order = 0

        for part_idx, ring_idx, ring in self._polygon_rings(geom, include_holes):
            num_vertices = ring.numPoints()
            end_idx = num_vertices

            # Rings repeat the first vertex at the end; emit it only once.
            if num_vertices > 1 and ring.pointN(0) == ring.pointN(num_vertices - 1):
                end_idx = num_vertices - 1

            for v_idx in range(end_idx):
                p = ring.pointN(v_idx)

                current_v_order = vertex_order
                vertex_order += 1

                if dedup_set is not None:
                    hash_key = (p.x(), p.y())
                    if hash_key in dedup_set:
                        continue
                    dedup_set.add(hash_key)

                out_feat = self._new_feature(fields, base_attrs, source_id, meta)
                out_feat.setAttribute(meta['part_id'], part_idx)
                out_feat.setAttribute(meta['ring_id'], ring_idx)
                out_feat.setAttribute(meta['vertex_id'], v_idx)
                out_feat.setAttribute(meta['vertex_order'], current_v_order)

                if add_fields:
                    self._set_coords(out_feat, meta, p.x(), p.y(), transform)

                out_feat.setGeometry(QgsGeometry(p.clone()))
                feature_list.append(out_feat)
                count += 1
        return count

    def line_to_vertices(self, geom, source_id, base_attrs, ctx):
        fields, meta = ctx['fields'], ctx['meta']
        feature_list, dedup_set = ctx['out'], ctx['dedup']
        add_fields, transform = ctx['add_fields'], ctx['transform']
        count = 0
        vertex_order = 0

        parts = geom.asGeometryCollection() if geom.isMultipart() else [geom]
        for part_idx, part_geom in enumerate(parts):
            abstract_part = part_geom.constGet()
            # QgsCurve covers QgsLineString and any segmentised curve.
            if not isinstance(abstract_part, QgsCurve):
                continue

            for v_idx in range(abstract_part.numPoints()):
                p = abstract_part.pointN(v_idx)

                current_v_order = vertex_order
                vertex_order += 1

                if dedup_set is not None:
                    hash_key = (p.x(), p.y())
                    if hash_key in dedup_set:
                        continue
                    dedup_set.add(hash_key)

                out_feat = self._new_feature(fields, base_attrs, source_id, meta)
                out_feat.setAttribute(meta['part_id'], part_idx)
                out_feat.setAttribute(meta['vertex_id'], v_idx)
                out_feat.setAttribute(meta['vertex_order'], current_v_order)

                if add_fields:
                    self._set_coords(out_feat, meta, p.x(), p.y(), transform)

                out_feat.setGeometry(QgsGeometry(p.clone()))
                feature_list.append(out_feat)
                count += 1
        return count


# ---------------------------- launcher: the entry the toolbox and toolbar see
class GeometryConversionAlgorithm(QgsProcessingAlgorithm):
    def tr(self, string):
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return GeometryConversionAlgorithm()

    def name(self):
        return 'geometry_conversion_dynamic'

    def displayName(self):
        return self.tr('Geometry Conversion')

    def group(self):
        return self.tr('KGA Geometry Utilities')

    def groupId(self):
        return 'kgageometryutilities'

    def helpUrl(self):
        return docs_url('geometry_conversion_dynamic')

    def shortHelpString(self):
        return self.tr(
            "Opens a dialog that converts polygon or line layers into simpler "
            "geometries: boundary segments, central points, boundary vertices "
            "or line vertices.\n\n"
            "Output can be a temporary layer, a GeoPackage or a Shapefile.")

    def flags(self):
        base = super().flags()
        try:
            from qgis.core import Qgis
            return base | Qgis.ProcessingAlgorithmFlag.NoThreading
        except (ImportError, AttributeError):
            return base | QgsProcessingAlgorithm.Flag.FlagNoThreading

    def initAlgorithm(self, config=None):
        pass

    def processAlgorithm(self, parameters, context, feedback):
        global DIALOG_INSTANCE
        parent = iface.mainWindow() if iface else None

        # A dialog closed by the user may already be destroyed on the C++ side.
        if DIALOG_INSTANCE is not None and _is_deleted(DIALOG_INSTANCE):
            DIALOG_INSTANCE = None

        # Closing a QDialog only hides it, so a cached instance would come back
        # with the previous layer, mode and output path still filled in.
        # Re-running while the window is still open just brings it forward;
        # re-running after it was closed throws the old one away and starts clean.
        if DIALOG_INSTANCE is not None and not DIALOG_INSTANCE.isVisible():
            DIALOG_INSTANCE.deleteLater()
            DIALOG_INSTANCE = None

        if DIALOG_INSTANCE is None:
            DIALOG_INSTANCE = DynamicGeometryDialog(parent)

        DIALOG_INSTANCE.setWindowModality(Qt.WindowModality.NonModal)
        DIALOG_INSTANCE.show()
        DIALOG_INSTANCE.raise_()
        DIALOG_INSTANCE.activateWindow()

        feedback.pushInfo("Geometry Conversion dialog opened. "
                          "You can close this background log.")
        return {}

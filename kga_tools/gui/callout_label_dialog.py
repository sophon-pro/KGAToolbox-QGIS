# -*- coding: utf-8 -*-
"""Callout Label Studio window (KGA Mapping > Callout Label Studio)."""

from contextlib import suppress
import json

from qgis.PyQt.QtCore import Qt, QTimer, QPointF, QRectF
from qgis.PyQt.QtGui import (QColor, QFont, QFontMetricsF, QIcon, QPainter, QPainterPath,
                             QPen, QPixmap, QPolygonF)
from qgis.PyQt.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFileDialog, QFontComboBox, QFormLayout, QFrame, QGridLayout,
    QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox, QPushButton,
    QScrollArea, QSizePolicy, QSpinBox, QTabWidget, QToolButton, QVBoxLayout, QWidget,
)
from qgis.core import Qgis, QgsMapLayerProxyModel, QgsSettings
from qgis.gui import QgsColorButton, QgsFieldExpressionWidget, QgsMapLayerComboBox, QgsScaleWidget

from ..core.callout_studio import engine as E
from ..core.callout_studio import arrange as A
from ..core.callout_studio import numberkey as K
from ..core.callout_studio import layoutkey as LK
from ..core.compat import enum_value

SETTINGS_KEY = "kga_tools/callout_label_studio/user_presets"
LAST_KEY = "kga_tools/callout_label_studio/last_config"


# --------------------------------------------------------------------------
# Preset thumbnail renderer
# --------------------------------------------------------------------------

def preset_icon(cfg, w=84, h=54):
    cfg = E.merged({}, cfg)
    ratio = 2
    pm = QPixmap(w * ratio, h * ratio)
    pm.setDevicePixelRatio(ratio)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    # map-like background
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(cfg.get("icon_bg", "#ffe8edf0")))
    p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 6, 6)

    anchor = QPointF(16, h - 13)
    label_rect = QRectF(36, 8, 40, 16)
    start = QPointF(label_rect.left() + 2, label_rect.bottom() + 1)

    # the "feature"
    p.setBrush(QColor("#ff78909c"))
    p.drawEllipse(anchor, 3.5, 3.5)

    ctype = cfg["callout_type"]
    if ctype == "balloon":
        r = label_rect.adjusted(-3, -2, 3, 2)
        rad = min(6.0, float(cfg["corner_radius"]) * 2.5)
        body = QPainterPath()
        body.addRoundedRect(r, rad, rad)
        wedge = QPainterPath()
        wedge.addPolygon(QPolygonF([QPointF(r.left() + 6, r.bottom() - 1),
                                    QPointF(r.left() + 15, r.bottom() - 1),
                                    anchor + QPointF(2, -2)]))
        shape = body.united(wedge)
        if cfg["shadow"]:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 55))
            p.drawPath(shape.translated(1.5, 1.5))
        p.setBrush(QColor(cfg["fill_color"]))
        p.setPen(QPen(QColor(cfg["stroke_color"]), 1.0) if float(cfg["stroke_width"]) > 0 else Qt.PenStyle.NoPen)
        p.drawPath(shape)
    else:
        path = QPainterPath(start)
        if ctype == "simple":
            path.lineTo(anchor)
        elif ctype == "manhattan":
            path.lineTo(QPointF(anchor.x(), start.y()))
            path.lineTo(anchor)
        else:
            path.quadTo(QPointF(anchor.x() + 2, start.y() - 2), anchor)
        p.setBrush(Qt.BrushStyle.NoBrush)
        if cfg["casing"]:
            cp = QPen(QColor(cfg["casing_color"]), 4.0)
            cp.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(cp)
            p.drawPath(path)
        lp = QPen(QColor(cfg["line_color"]), 1.8)
        lp.setCapStyle(Qt.PenCapStyle.RoundCap)
        lp.setStyle({"dash": Qt.PenStyle.DashLine, "dot": Qt.PenStyle.DotLine}.get(cfg["line_style"], Qt.PenStyle.SolidLine))
        p.setPen(lp)
        p.drawPath(path)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(cfg["line_color"]))
        if cfg["end_marker"] == "dot":
            p.drawEllipse(anchor, 2.6, 2.6)
        elif cfg["end_marker"] == "arrow":
            # direction of the last segment
            prev = QPointF(anchor.x(), start.y()) if ctype == "manhattan" else (
                QPointF(anchor.x() + 2, start.y() - 2) if ctype == "curved" else start)
            d = anchor - prev
            length = max((d.x() ** 2 + d.y() ** 2) ** 0.5, 0.01)
            ux, uy = d.x() / length, d.y() / length
            tip = anchor
            base = QPointF(tip.x() - ux * 6, tip.y() - uy * 6)
            perp = QPointF(-uy * 3, ux * 3)
            p.drawPolygon(QPolygonF([tip, base + perp, base - perp]))

    # label text
    font = QFont()
    font.setPixelSize(11)
    font.setBold(bool(cfg["bold"]))
    font.setItalic(bool(cfg["italic"]))
    fm = QFontMetricsF(font)
    text = "Label"
    tw = fm.horizontalAdvance(text) if hasattr(fm, "horizontalAdvance") else fm.width(text)
    tx = label_rect.center().x() - tw / 2
    ty = label_rect.center().y() + fm.ascent() / 2 - 1
    tpath = QPainterPath()
    tpath.addText(QPointF(tx, ty), font, text)
    if cfg["buffer"]:
        bp = QPen(QColor(cfg["buffer_color"]), 3.0)
        bp.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.strokePath(tpath, bp)
    p.fillPath(tpath, QColor(cfg["text_color"]))
    p.end()
    return QIcon(pm)


# --------------------------------------------------------------------------
# Dialog
# --------------------------------------------------------------------------

class CalloutStudioDialog(QDialog):

    def __init__(self, iface, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self._loading = False
        self._backup = None          # (layer_id, labeling clone, labels enabled)
        self._last_sig = None        # signature of the last style pushed to the map
        self._preset_buttons = []

        self.setWindowTitle("Callout Label Studio")
        self.setMinimumWidth(600)
        self.resize(620, 900)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(350)
        self._timer.timeout.connect(self._live_apply)

        self._build_ui()
        self._rebuild_presets()
        with suppress(Exception):
            self.iface.mapCanvas().scaleChanged.connect(self._on_canvas_scale_changed)

        last = QgsSettings().value(LAST_KEY, "")
        try:
            start_cfg = E.merged(json.loads(last), {}) if last else dict(E.DEFAULTS)
        except (ValueError, TypeError):
            start_cfg = dict(E.DEFAULTS)
        self.set_cfg(start_cfg)
        self._on_layer_changed(self.layer_cb.currentLayer())

    # ------------------------------------------------------------------ UI
    def _dspin(self, lo, hi, step, dec=2, suffix=" mm"):
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setSingleStep(step)
        s.setDecimals(dec)
        s.setSuffix(suffix)
        s.setKeyboardTracking(False)
        s.valueChanged.connect(self._changed)
        return s

    def _combo(self, items):
        c = QComboBox()
        for key, text in items:
            c.addItem(text, key)
        c.currentIndexChanged.connect(self._changed_now)
        return c

    def _color(self, title, allow_opacity=True):
        b = QgsColorButton()
        b.setColorDialogTitle(title)
        b.setAllowOpacity(allow_opacity)
        b.setMinimumWidth(110)
        b.colorChanged.connect(self._changed)
        return b

    def _check(self, text):
        c = QCheckBox(text)
        c.toggled.connect(self._changed_now)
        return c

    @staticmethod
    def _hint(text):
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet("color: palette(mid); font-size: 11px;")
        return lbl

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)

        # Target
        tgt = QGroupBox("Layer && label text")
        tf = QFormLayout(tgt)
        self.layer_cb = QgsMapLayerComboBox()
        flt = 0
        for part in ("Point", "Line", "Polygon"):
            flt |= enum_value((Qgis, "LayerFilter.%sLayer" % part), (QgsMapLayerProxyModel, "Filter.%sLayer" % part),
                              (QgsMapLayerProxyModel, "%sLayer" % part))
        self.layer_cb.setFilters(flt)
        self.layer_cb.layerChanged.connect(self._on_layer_changed)
        tf.addRow("Layer", self.layer_cb)

        self.field_w = QgsFieldExpressionWidget()
        self.field_w.fieldChanged.connect(self._changed)
        tf.addRow("Label with", self.field_w)

        self.filter_w = QgsFieldExpressionWidget()
        self.filter_w.setAllowEmptyFieldName(True)
        self.filter_w.setExpressionDialogTitle("Only label features where…")
        self.filter_w.fieldChanged.connect(self._changed)
        self.filter_w.setToolTip("Optional. e.g.  \"population\" > 100000  — leave empty to label all features.")
        tf.addRow("Only where", self.filter_w)

        fit_row = QHBoxLayout()
        self.fit_cb = QComboBox()
        self.fit_cb.setToolTip("Which map the spacing (mm) is calculated for.\n"
                               "Choose your Print Layout map so columns and key boxes fit the printed scale.")
        self.fit_cb.currentIndexChanged.connect(self._on_fit_changed)
        fit_row.addWidget(self.fit_cb, 1)
        self.fit_refresh = QToolButton()
        self.fit_refresh.setText("⟳")
        self.fit_refresh.setToolTip("Refresh the list of Print Layouts")
        self.fit_refresh.clicked.connect(self._refresh_fit_targets)
        fit_row.addWidget(self.fit_refresh)
        tf.addRow("Fit for", fit_row)
        self._fit_item = None
        self.geom_lbl = self._hint("")
        tf.addRow("", self.geom_lbl)
        root.addWidget(tgt)

        # Tabs: Quick styles first, then the fine-tuning tabs
        self.tabs = QTabWidget()
        qs = QWidget()
        qv = QVBoxLayout(qs)
        self.preset_grid = QGridLayout()
        self.preset_grid.setSpacing(6)
        qv.addLayout(self.preset_grid)
        qv.addStretch(1)
        self.preset_group = QButtonGroup(self)
        self.preset_group.setExclusive(True)
        self.tabs.addTab(self._wrap_scroll(qs), "Quick styles")
        self.tabs.addTab(self._wrap_scroll(self._build_callout_tab()), "Callout")
        self.tabs.addTab(self._wrap_scroll(self._build_text_tab()), "Text")
        self.tabs.addTab(self._wrap_scroll(self._build_placement_tab()), "Placement")
        self.tabs.addTab(self._wrap_scroll(self._build_key_tab()), "Number key")
        root.addWidget(self.tabs, 1)

        # Tools row
        tools = QHBoxLayout()
        self.live_cb = QCheckBox("Live preview on map")
        self.live_cb.setChecked(True)
        self.live_cb.toggled.connect(self._on_live_toggled)
        tools.addWidget(self.live_cb)
        tools.addStretch(1)
        self.more_btn = QToolButton()
        self.more_btn.setText("More ▾")
        self.more_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.more_btn)
        menu.addAction("Save current style as my preset…", self._save_user_preset)
        menu.addAction("Copy equivalent PyQGIS code", self._copy_code)
        menu.addAction("Export label style (.qml)…", self._export_qml)
        menu.addSeparator()
        menu.addAction("Remove all labels from layer", self._remove_labels)
        self.more_btn.setMenu(menu)
        tools.addWidget(self.more_btn)
        root.addLayout(tools)

        self._sep_line = line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        root.addWidget(line)

        SB = QDialogButtonBox.StandardButton
        self.bbox = QDialogButtonBox(SB.RestoreDefaults | SB.Cancel | SB.Apply | SB.Ok)
        self.bbox.button(SB.RestoreDefaults).setText("Reset")
        self.bbox.button(SB.Ok).setText("Apply && Close")
        self.bbox.button(SB.RestoreDefaults).clicked.connect(self._reset)
        self.bbox.button(SB.Apply).clicked.connect(self._apply)
        self.bbox.button(SB.Ok).clicked.connect(self._ok)
        self.bbox.button(SB.Cancel).clicked.connect(self.reject)
        root.addWidget(self.bbox)

    @staticmethod
    def _wrap_scroll(w):
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.Shape.NoFrame)
        sa.setWidget(w)
        return sa

    def _build_callout_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)

        top = QFormLayout()
        self.type_cb = self._combo(E.CALLOUT_TYPES)
        top.addRow("Callout type", self.type_cb)
        v.addLayout(top)

        # Line group
        self.grp_line = QGroupBox("Leader line")
        lf = QFormLayout(self.grp_line)
        self.line_color = self._color("Leader line color")
        lf.addRow("Color", self.line_color)
        self.line_width = self._dspin(0.05, 5, 0.05)
        lf.addRow("Width", self.line_width)
        self.line_style = self._combo(E.LINE_STYLES)
        lf.addRow("Style", self.line_style)
        row = QHBoxLayout()
        self.casing = self._check("White casing / halo")
        self.casing.setToolTip("A wider line drawn underneath — keeps the leader readable over imagery and busy maps.")
        self.casing_color = self._color("Casing color")
        row.addWidget(self.casing)
        row.addWidget(self.casing_color)
        row.addStretch(1)
        lf.addRow("Casing", row)
        self.end_marker = self._combo(E.END_MARKERS)
        lf.addRow("Feature end", self.end_marker)
        self.marker_size = self._dspin(0.5, 8, 0.2)
        lf.addRow("Marker size", self.marker_size)
        v.addWidget(self.grp_line)

        # Curved
        self.grp_curved = QGroupBox("Curve")
        cf = QFormLayout(self.grp_curved)
        self.curvature = QSpinBox()
        self.curvature.setRange(0, 100)
        self.curvature.setSuffix(" %")
        self.curvature.valueChanged.connect(self._changed)
        cf.addRow("Curvature", self.curvature)
        self.orientation = self._combo(E.ORIENTATIONS)
        cf.addRow("Direction", self.orientation)
        v.addWidget(self.grp_curved)

        # Balloon
        self.grp_balloon = QGroupBox("Balloon")
        bf = QFormLayout(self.grp_balloon)
        self.fill_color = self._color("Balloon fill")
        bf.addRow("Fill", self.fill_color)
        self.stroke_color = self._color("Balloon outline")
        bf.addRow("Outline", self.stroke_color)
        self.stroke_width = self._dspin(0, 3, 0.05)
        bf.addRow("Outline width", self.stroke_width)
        self.corner_radius = self._dspin(0, 10, 0.25)
        bf.addRow("Corner radius", self.corner_radius)
        self.wedge_width = self._dspin(0.2, 10, 0.25)
        bf.addRow("Pointer width", self.wedge_width)
        self.margin = self._dspin(0, 10, 0.25)
        bf.addRow("Padding", self.margin)
        self.shadow = self._check("Drop shadow (slower to draw)")
        bf.addRow("", self.shadow)
        v.addWidget(self.grp_balloon)

        # Anchoring
        grp_anchor = QGroupBox("Anchoring")
        af = QFormLayout(grp_anchor)
        self.min_length = self._dspin(0, 50, 0.5)
        self.min_length.setToolTip("Callouts shorter than this are not drawn (avoids tiny stubs next to features).")
        self.lbl_min_length = QLabel("Min. length")
        af.addRow(self.lbl_min_length, self.min_length)
        self.offset_anchor = self._dspin(0, 20, 0.25)
        self.offset_anchor.setToolTip("Gap between the end of the callout and the feature.")
        af.addRow("Gap at feature", self.offset_anchor)
        self.feature_anchor = self._combo(E.FEATURE_ANCHORS)
        self.lbl_feature_anchor = QLabel("Polygon anchor")
        af.addRow(self.lbl_feature_anchor, self.feature_anchor)
        self.label_anchor = self._combo(E.LABEL_ANCHORS)
        self.lbl_label_anchor = QLabel("Label anchor")
        af.addRow(self.lbl_label_anchor, self.label_anchor)
        self.all_parts = self._check("Draw to every part of multi-part features")
        af.addRow("", self.all_parts)
        v.addWidget(grp_anchor)
        v.addStretch(1)
        return w

    def _build_text_tab(self):
        w = QWidget()
        f = QFormLayout(w)
        self.font_cb = QFontComboBox()
        self.font_cb.currentFontChanged.connect(self._changed_now)
        f.addRow("Font", self.font_cb)
        self.font_size = self._dspin(4, 72, 0.5, 1, " pt")
        f.addRow("Size", self.font_size)
        row = QHBoxLayout()
        self.bold = self._check("Bold")
        self.italic = self._check("Italic")
        row.addWidget(self.bold)
        row.addWidget(self.italic)
        row.addStretch(1)
        f.addRow("Style", row)
        self.text_color = self._color("Text color")
        f.addRow("Color", self.text_color)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        f.addRow(sep)
        self.buffer = self._check("Text halo (buffer)")
        f.addRow("", self.buffer)
        self.buffer_color = self._color("Halo color")
        f.addRow("Halo color", self.buffer_color)
        self.buffer_size = self._dspin(0.1, 5, 0.1)
        f.addRow("Halo size", self.buffer_size)
        f.addRow(self._hint("Tip: balloon styles usually look cleaner with the halo switched off; "
                            "on satellite imagery use a dark halo with white text."))
        return w

    def _build_placement_tab(self):
        w = QWidget()
        outer = QVBoxLayout(w)

        # Organized layout ----------------------------------------------
        org = QGroupBox("Organized layout (tidy columns / rows)")
        self.org_box = org
        of = QFormLayout(org)
        self.arrange = self._combo(A.ARRANGE_MODES)
        self.arrange.setToolTip("Stack labels in a neat column or row beside the data, sorted so leader lines don't cross.")
        of.addRow("Arrange", self.arrange)
        self.arrange_margin = self._dspin(0, 100, 0.5)
        self.arrange_margin.setToolTip("How far the column/row sits from the edge of the data.")
        self.lbl_arr_margin = QLabel("Distance from data")
        of.addRow(self.lbl_arr_margin, self.arrange_margin)
        self.arrange_gap = self._dspin(0, 20, 0.25)
        self.arrange_gap.setToolTip("Extra space between neighbouring labels.")
        self.lbl_arr_gap = QLabel("Space between labels")
        of.addRow(self.lbl_arr_gap, self.arrange_gap)
        self.arrange_even = self._check("Distribute evenly (index-list look)")
        self.arrange_even.setToolTip("Off: each label stays level with its feature and is only nudged to avoid overlaps "
                                     "(straightest leader lines).\nOn: labels are spaced evenly along the full side.")
        of.addRow("", self.arrange_even)
        self.arrange_rotate = self._check("Vertical text in rows (compact)")
        of.addRow("", self.arrange_rotate)
        self.arrange_hint = self._hint("Positions are calculated for the current map scale and refresh as you zoom "
                                       "while this window is open. Set the zoom you will print at, then Apply. "
                                       "Tip: Classic Leader or Survey Dashed look cleanest with columns.")
        of.addRow(self.arrange_hint)
        outer.addWidget(org)

        auto_box = QGroupBox("Automatic placement")
        self.auto_box = auto_box
        f = QFormLayout(auto_box)
        outer.addWidget(auto_box)
        self.placement = self._combo(E.PLACEMENTS)
        f.addRow("Mode", self.placement)
        self.distance = self._dspin(0, 100, 0.5)
        self.lbl_distance = QLabel("Distance")
        f.addRow(self.lbl_distance, self.distance)
        self.quadrant = self._combo(E.QUADRANTS)
        self.lbl_quadrant = QLabel("Quadrant")
        f.addRow(self.lbl_quadrant, self.quadrant)
        self.x_offset = self._dspin(-200, 200, 0.5)
        self.lbl_x = QLabel("Offset X")
        f.addRow(self.lbl_x, self.x_offset)
        self.y_offset = self._dspin(-200, 200, 0.5)
        self.lbl_y = QLabel("Offset Y")
        f.addRow(self.lbl_y, self.y_offset)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        f.addRow(sep)
        self.show_all = self._check("Show all labels (allow overlaps if needed)")
        f.addRow("", self.show_all)
        self.scale_limit = self._check("Only show labels when zoomed in beyond")
        f.addRow("", self.scale_limit)
        self.min_scale = QgsScaleWidget()
        self.min_scale.scaleChanged.connect(self._changed)
        f.addRow("Scale 1:", self.min_scale)
        f.addRow(self._hint(
            "Callouts appear whenever a label sits away from its feature. For hand-placed labels, "
            "use the Label toolbar's <b>Move Label</b> tool — the callout follows automatically — and "
            "<b>Move Callout</b> to drag the feature end of a leader line."))
        outer.addStretch(1)
        return w

    def _build_key_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        self.key_enable = self._check("Number the features and list their names in a key")
        f0 = self.key_enable.font()
        f0.setBold(True)
        self.key_enable.setFont(f0)
        v.addWidget(self.key_enable)
        v.addWidget(self._hint("Best for small polygons: each feature gets a short number or letter, "
                               "and the full names go into key boxes outside the data."))

        g1 = QGroupBox("Numbers on features")
        self.key_grp1 = g1
        f = QFormLayout(g1)
        self.key_style = self._combo(K.NUMBER_STYLES)
        f.addRow("Numbering", self.key_style)
        self.key_order = self._combo(K.NUMBER_ORDERS)
        f.addRow("Order", self.key_order)
        self.key_start = QSpinBox()
        self.key_start.setRange(1, 9999)
        self.key_start.valueChanged.connect(self._changed)
        f.addRow("Start at", self.key_start)
        self.key_look = self._combo(K.NUMBER_LOOKS)
        f.addRow("Look", self.key_look)
        self.key_badge_color = self._color("Badge / number color")
        f.addRow("Badge color", self.key_badge_color)
        self.key_number_color = self._color("Number text color")
        self.lbl_key_numcol = QLabel("Number color")
        f.addRow(self.lbl_key_numcol, self.key_number_color)
        self.key_number_size = self._dspin(4, 36, 0.5, 1, " pt")
        f.addRow("Size", self.key_number_size)
        self.key_number_pos = self._combo(K.NUMBER_POSITIONS)
        self.key_number_pos.setToolTip("Outside: numbers sit next to tiny features with a leader line "
                                       "drawn in the style chosen on the Callout tab.")
        f.addRow("Position", self.key_number_pos)
        v.addWidget(g1)

        g2 = QGroupBox("Key boxes")
        self.key_grp2 = g2
        f = QFormLayout(g2)
        self.key_position = self._combo(K.KEY_POSITIONS)
        f.addRow("Place key", self.key_position)
        self.key_columns = QSpinBox()
        self.key_columns.setRange(1, 12)
        self.key_columns.valueChanged.connect(self._changed)
        self.key_columns.setToolTip("Split the list into this many boxes, side by side.")
        f.addRow("Boxes / columns", self.key_columns)
        self.key_margin = self._dspin(0, 100, 0.5)
        f.addRow("Distance from data", self.key_margin)
        self.key_title = QLineEdit()
        self.key_title.setPlaceholderText("Optional, e.g. Villages")
        self.key_title.editingFinished.connect(self._changed)
        f.addRow("Title", self.key_title)
        self.key_box = self._check("Draw a frame around each box")
        f.addRow("", self.key_box)
        self.key_box_fill = self._color("Key box fill")
        f.addRow("Box fill", self.key_box_fill)
        self.key_box_stroke = self._color("Key box outline")
        f.addRow("Box outline", self.key_box_stroke)
        self.key_on_map = self._check("Show the key boxes on the map")
        self.key_on_map.setToolTip("Turn off when the key lives in a Print Layout instead.")
        f.addRow("", self.key_on_map)
        f.addRow(self._hint("Key text uses the font, size and color from the Text tab."))
        v.addWidget(g2)

        g3 = QGroupBox("Print layout (recommended for printed maps)")
        f3 = QVBoxLayout(g3)
        f3.addWidget(self._hint(
            "Numbers stay on the map; the key becomes text boxes beside the layout's map frame, "
            "so you can drag, resize and restyle it freely. Re-send any time — the old key is replaced."))
        self.send_key_btn = QPushButton("Put key in Print Layout")
        self.send_key_btn.clicked.connect(self._send_key_to_layout)
        f3.addWidget(self.send_key_btn)
        v.addWidget(g3)
        self.key_grp3 = g3
        v.addStretch(1)
        return w

    # ------------------------------------------------------------- presets
    def _user_presets(self):
        raw = QgsSettings().value(SETTINGS_KEY, "{}")
        try:
            data = json.loads(raw) if raw else {}
            return data if isinstance(data, dict) else {}
        except (ValueError, TypeError):
            return {}

    def _store_user_presets(self, data):
        QgsSettings().setValue(SETTINGS_KEY, json.dumps(data))

    def _rebuild_presets(self):
        for b in self._preset_buttons:
            self.preset_group.removeButton(b)
            b.deleteLater()
        self._preset_buttons = []

        items = [(n, p, False) for n, p in E.BUILTIN_PRESETS]
        items += [(n, p, True) for n, p in sorted(self._user_presets().items())]
        cols = 4
        for i, (name, preset, is_user) in enumerate(items):
            b = QToolButton()
            b.setText(("★ " if is_user else "") + name)
            b.setIcon(preset_icon(preset))
            b.setIconSize(preset_icon_size())
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            b.setCheckable(True)
            b.setAutoRaise(True)
            b.setMinimumWidth(128)
            b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            b.setToolTip("Apply the “{}” style{}".format(
                name, " (right-click to delete)" if is_user else ""))
            b.clicked.connect(lambda _=False, p=preset: self._apply_preset(p))
            if is_user:
                b.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                b.customContextMenuRequested.connect(
                    lambda pos, n=name, btn=b: self._user_preset_menu(n, btn, pos))
            self.preset_group.addButton(b)
            self.preset_grid.addWidget(b, i // cols, i % cols)
            self._preset_buttons.append(b)

    def _apply_preset(self, preset):
        current = self.get_cfg()
        new = E.merged({}, preset)
        for k in E.PRESERVED_ON_PRESET:
            new[k] = current[k]
        self.set_cfg(new)
        self._changed_now()

    def _user_preset_menu(self, name, btn, pos):
        m = QMenu(self)
        act = m.addAction("Delete preset “{}”".format(name))
        chosen = (getattr(m, "exec", None) or getattr(m, "exec_"))(btn.mapToGlobal(pos))
        if chosen == act:
            data = self._user_presets()
            data.pop(name, None)
            self._store_user_presets(data)
            self._rebuild_presets()

    def _save_user_preset(self):
        name, ok = QInputDialog.getText(self, "Save preset", "Preset name:")
        name = (name or "").strip()
        if not ok or not name:
            return
        data = self._user_presets()
        if name in data:
            SB = QMessageBox.StandardButton
            if QMessageBox.question(self, "Save preset", "Replace existing preset “{}”?".format(name),
                                    SB.Yes | SB.No) != SB.Yes:
                return
        cfg = self.get_cfg()
        cfg["icon_bg"] = "#ff3d4a3a" if QColor(cfg["text_color"]).lightness() > 200 else "#ffe8edf0"
        data[name] = cfg
        self._store_user_presets(data)
        self._rebuild_presets()
        self._msg("Preset saved", "“{}” is now in your Quick styles.".format(name), "success")

    # ------------------------------------------------------- cfg <-> widgets
    def get_cfg(self):
        def cd(c):
            return c.currentData()

        def col(b):
            return b.color().name(QColor.NameFormat.HexArgb)

        return {
            "callout_type": cd(self.type_cb),
            "line_color": col(self.line_color),
            "line_width": self.line_width.value(),
            "line_style": cd(self.line_style),
            "casing": self.casing.isChecked(),
            "casing_color": col(self.casing_color),
            "end_marker": cd(self.end_marker),
            "marker_size": self.marker_size.value(),
            "min_length": self.min_length.value(),
            "offset_anchor": self.offset_anchor.value(),
            "curvature": self.curvature.value(),
            "orientation": cd(self.orientation),
            "fill_color": col(self.fill_color),
            "stroke_color": col(self.stroke_color),
            "stroke_width": self.stroke_width.value(),
            "corner_radius": self.corner_radius.value(),
            "wedge_width": self.wedge_width.value(),
            "margin": self.margin.value(),
            "shadow": self.shadow.isChecked(),
            "feature_anchor": cd(self.feature_anchor),
            "label_anchor": cd(self.label_anchor),
            "all_parts": self.all_parts.isChecked(),
            "font_family": self.font_cb.currentFont().family(),
            "font_size": self.font_size.value(),
            "bold": self.bold.isChecked(),
            "italic": self.italic.isChecked(),
            "text_color": col(self.text_color),
            "buffer": self.buffer.isChecked(),
            "buffer_color": col(self.buffer_color),
            "buffer_size": self.buffer_size.value(),
            "placement": cd(self.placement),
            "distance": self.distance.value(),
            "x_offset": self.x_offset.value(),
            "y_offset": self.y_offset.value(),
            "quadrant": cd(self.quadrant),
            "show_all": self.show_all.isChecked(),
            "scale_limit": self.scale_limit.isChecked(),
            "min_scale": self.min_scale.scale(),
            "arrange": cd(self.arrange),
            "arrange_margin": self.arrange_margin.value(),
            "arrange_gap": self.arrange_gap.value(),
            "arrange_even": self.arrange_even.isChecked(),
            "arrange_rotate": self.arrange_rotate.isChecked(),
            "key_enable": self.key_enable.isChecked(),
            "key_style": cd(self.key_style),
            "key_order": cd(self.key_order),
            "key_start": self.key_start.value(),
            "key_look": cd(self.key_look),
            "key_badge_color": col(self.key_badge_color),
            "key_number_color": col(self.key_number_color),
            "key_number_size": self.key_number_size.value(),
            "key_number_pos": cd(self.key_number_pos),
            "key_position": cd(self.key_position),
            "key_columns": self.key_columns.value(),
            "key_margin": self.key_margin.value(),
            "key_title": self.key_title.text(),
            "key_box": self.key_box.isChecked(),
            "key_box_fill": col(self.key_box_fill),
            "key_box_stroke": col(self.key_box_stroke),
            "key_on_map": self.key_on_map.isChecked(),
        }

    def set_cfg(self, cfg):
        cfg = E.merged({}, cfg)
        self._loading = True
        try:
            def sc(combo, key):
                i = combo.findData(key)
                combo.setCurrentIndex(max(i, 0))

            sc(self.type_cb, cfg["callout_type"])
            self.line_color.setColor(QColor(cfg["line_color"]))
            self.line_width.setValue(cfg["line_width"])
            sc(self.line_style, cfg["line_style"])
            self.casing.setChecked(cfg["casing"])
            self.casing_color.setColor(QColor(cfg["casing_color"]))
            sc(self.end_marker, cfg["end_marker"])
            self.marker_size.setValue(cfg["marker_size"])
            self.min_length.setValue(cfg["min_length"])
            self.offset_anchor.setValue(cfg["offset_anchor"])
            self.curvature.setValue(int(cfg["curvature"]))
            sc(self.orientation, cfg["orientation"])
            self.fill_color.setColor(QColor(cfg["fill_color"]))
            self.stroke_color.setColor(QColor(cfg["stroke_color"]))
            self.stroke_width.setValue(cfg["stroke_width"])
            self.corner_radius.setValue(cfg["corner_radius"])
            self.wedge_width.setValue(cfg["wedge_width"])
            self.margin.setValue(cfg["margin"])
            self.shadow.setChecked(cfg["shadow"])
            sc(self.feature_anchor, cfg["feature_anchor"])
            sc(self.label_anchor, cfg["label_anchor"])
            self.all_parts.setChecked(cfg["all_parts"])
            if cfg["font_family"]:
                self.font_cb.setCurrentFont(QFont(cfg["font_family"]))
            self.font_size.setValue(cfg["font_size"])
            self.bold.setChecked(cfg["bold"])
            self.italic.setChecked(cfg["italic"])
            self.text_color.setColor(QColor(cfg["text_color"]))
            self.buffer.setChecked(cfg["buffer"])
            self.buffer_color.setColor(QColor(cfg["buffer_color"]))
            self.buffer_size.setValue(cfg["buffer_size"])
            sc(self.placement, cfg["placement"])
            self.distance.setValue(cfg["distance"])
            self.x_offset.setValue(cfg["x_offset"])
            self.y_offset.setValue(cfg["y_offset"])
            sc(self.quadrant, cfg["quadrant"])
            self.show_all.setChecked(cfg["show_all"])
            self.scale_limit.setChecked(cfg["scale_limit"])
            self.min_scale.setScale(cfg["min_scale"])
            sc(self.arrange, cfg["arrange"])
            self.arrange_margin.setValue(cfg["arrange_margin"])
            self.arrange_gap.setValue(cfg["arrange_gap"])
            self.arrange_even.setChecked(cfg["arrange_even"])
            self.arrange_rotate.setChecked(cfg["arrange_rotate"])
            self.key_enable.setChecked(cfg["key_enable"])
            sc(self.key_style, cfg["key_style"])
            sc(self.key_order, cfg["key_order"])
            self.key_start.setValue(int(cfg["key_start"]))
            sc(self.key_look, cfg["key_look"])
            self.key_badge_color.setColor(QColor(cfg["key_badge_color"]))
            self.key_number_color.setColor(QColor(cfg["key_number_color"]))
            self.key_number_size.setValue(cfg["key_number_size"])
            sc(self.key_number_pos, cfg["key_number_pos"])
            sc(self.key_position, cfg["key_position"])
            self.key_columns.setValue(int(cfg["key_columns"]))
            self.key_margin.setValue(cfg["key_margin"])
            self.key_title.setText(cfg["key_title"])
            self.key_box.setChecked(cfg["key_box"])
            self.key_box_fill.setColor(QColor(cfg["key_box_fill"]))
            self.key_box_stroke.setColor(QColor(cfg["key_box_stroke"]))
            self.key_on_map.setChecked(cfg["key_on_map"])
        finally:
            self._loading = False
        self._update_visibility()

    # ------------------------------------------------------------ behaviour
    def _update_visibility(self):
        ctype = self.type_cb.currentData()
        kind = E.geometry_kind(self.layer_cb.currentLayer())
        is_balloon = ctype == "balloon"
        self.grp_line.setVisible(not is_balloon)
        self.grp_balloon.setVisible(is_balloon)
        self.grp_curved.setVisible(ctype == "curved")
        for wdg in (self.min_length, self.lbl_min_length, self.label_anchor, self.lbl_label_anchor):
            wdg.setVisible(not is_balloon)
        self.all_parts.setVisible(not is_balloon)
        self.marker_size.setEnabled(self.end_marker.currentData() != "none")
        self.casing_color.setEnabled(self.casing.isChecked())

        poly = kind == "polygon"
        self.feature_anchor.setVisible(poly)
        self.lbl_feature_anchor.setVisible(poly)

        arranged = self.arrange.currentData() != "auto"
        for wdg in (self.arrange_margin, self.lbl_arr_margin, self.arrange_gap, self.lbl_arr_gap,
                    self.arrange_even, self.arrange_hint):
            wdg.setVisible(arranged)
        self.arrange_rotate.setVisible(self.arrange.currentData() in ("top", "bottom", "top_bottom"))
        # ring/offset settings only matter for automatic placement
        self.auto_box.setEnabled(not arranged)

        is_line = kind == "line"
        offset_mode = self.placement.currentData() == "offset" and not is_line
        self.placement.setEnabled(not is_line)
        for wdg in (self.distance, self.lbl_distance):
            wdg.setVisible(not offset_mode)
        for wdg in (self.quadrant, self.lbl_quadrant, self.x_offset, self.lbl_x, self.y_offset, self.lbl_y):
            wdg.setVisible(offset_mode)

        key_on = self.key_enable.isChecked()
        self.key_grp1.setEnabled(key_on)
        self.key_grp2.setEnabled(key_on)
        self.key_grp3.setEnabled(key_on)
        badge = self.key_look.currentData() == "badge"
        self.key_number_color.setVisible(badge)
        self.lbl_key_numcol.setVisible(badge)
        self.key_box_fill.setEnabled(self.key_box.isChecked())
        self.key_box_stroke.setEnabled(self.key_box.isChecked())
        self.org_box.setEnabled(not key_on)
        if key_on:
            self.auto_box.setEnabled(False)

        self.buffer_color.setEnabled(self.buffer.isChecked())
        self.buffer_size.setEnabled(self.buffer.isChecked())
        self.min_scale.setEnabled(self.scale_limit.isChecked())

    def _changed(self, *args):
        """Spin boxes / colour dragging: wait until the user pauses."""
        if self._loading:
            return
        self._update_visibility()
        self._timer.start()

    def _changed_now(self, *args):
        """Single deliberate actions (quick styles, drop-downs, ticks): apply at once."""
        if self._loading:
            return
        self._update_visibility()
        self._timer.stop()
        self._live_apply()

    def _on_layer_changed(self, layer):
        # discard un-applied preview on the previously selected layer
        self._restore_backup()
        self.field_w.setLayer(layer)
        self.filter_w.setLayer(layer)
        self.filter_w.setField("")
        if layer is not None:
            existing = E.read_existing_label_field(layer)
            if existing:
                self.field_w.setField(existing[0])
            else:
                disp = layer.displayField() if hasattr(layer, "displayField") else ""
                self.field_w.setField(disp or "")
            kind = E.geometry_kind(layer) or "?"
            hint = {
                "point": "Point layer — labels are placed around each point.",
                "line": "Line layer — labels float above/below lines; placement mode is fixed.",
                "polygon": "Polygon layer — labels sit around each polygon's centre with a callout pointing in.",
            }.get(kind, "")
            self.geom_lbl.setText(hint)
        else:
            self.geom_lbl.setText("Add a vector layer to the project to get started.")
        self._update_visibility()
        self._changed()

    # ------------------------------------------------------- fit target
    def _mm_units(self, layer):
        try:
            if self._fit_item is not None:
                v = LK.mm_units_for_map_item(self._fit_item, layer)
                if v:
                    return v
            return A.mm_to_layer_units(self.iface.mapCanvas(), layer)
        except Exception:
            return None

    def _refresh_fit_targets(self):
        cur = self.fit_cb.currentData()
        self.fit_cb.blockSignals(True)
        self.fit_cb.clear()
        self.fit_cb.addItem("Map canvas (screen zoom)", None)
        with suppress(Exception):
            for text, lname, uuid in LK.layout_maps():
                self.fit_cb.addItem("Layout: " + text, (lname, uuid))
        i = self.fit_cb.findData(cur) if cur else 0
        self.fit_cb.setCurrentIndex(max(i, 0))
        self.fit_cb.blockSignals(False)
        self._on_fit_changed()

    def _on_fit_changed(self, *_):
        if self._fit_item is not None:
            with suppress(Exception):
                self._fit_item.extentChanged.disconnect(self._on_scale_changed)
        self._fit_item = None
        data = self.fit_cb.currentData()
        if data:
            _, item = LK.find_map(*data)
            if item is not None:
                self._fit_item = item
                with suppress(Exception):
                    item.extentChanged.connect(self._on_scale_changed)
        self._changed()

    def _choose_layout_target(self, layer):
        """Return (layout, map item) to send the key to, creating a layout if none exist."""
        if self._fit_item is not None:
            return self._fit_item.layout(), self._fit_item
        targets = LK.layout_maps()
        if not targets:
            lay, m = LK.create_layout_for(layer)
            self._refresh_fit_targets()
            return lay, m
        if len(targets) == 1:
            return LK.find_map(targets[0][1], targets[0][2])
        names = [t[0] for t in targets]
        choice, ok = QInputDialog.getItem(self, "Put key in Print Layout", "Layout map:", names, 0, False)
        if not ok:
            return None, None
        t = targets[names.index(choice)]
        return LK.find_map(t[1], t[2])

    def _send_key_to_layout(self):
        layer = self.layer_cb.currentLayer()
        if layer is None:
            return
        expr, is_expr, flt = self._current_label_source()
        if not expr:
            self._msg("Nothing to label", "Choose a field or expression in “Label with”.", "warning")
            return
        lay, item = self._choose_layout_target(layer)
        if item is None:
            return
        try:
            n, overflow = LK.send_key_to_layout(layer, self.get_cfg(), expr, is_expr, flt, lay, item)
        except Exception as ex:
            self._msg("Could not create the key", str(ex), "critical")
            return
        if n == 0:
            self._msg("Nothing to list", "No features matched.", "warning")
            return
        # key now lives in the layout: keep only the numbers on the map
        self.key_on_map.setChecked(False)
        self._commit()
        i = self.fit_cb.findData((lay.name(), item.uuid()))
        if i >= 0:
            self.fit_cb.setCurrentIndex(i)
        text = "{} key box(es) added to “{}”. Drag the group to move it; double-click to edit text.".format(n, lay.name())
        if overflow:
            text += " It runs off the page — try fewer columns, a smaller font, or a narrower map frame."
        self._msg("Key added to layout", text, "warning" if overflow else "success")
        with suppress(Exception):
            self.iface.openLayoutDesigner(lay)

    def showEvent(self, event):
        super().showEvent(event)
        self._refresh_fit_targets()

    def _zoom_dependent(self):
        """True when label positions are computed from a map scale."""
        if self.key_enable.isChecked():
            return self.key_on_map.isChecked()
        return self.arrange.currentData() != "auto"

    def _on_scale_changed(self, *_):
        if self.isVisible() and self.live_cb.isChecked() and self._zoom_dependent():
            self._timer.start()

    def _on_canvas_scale_changed(self, *_):
        # when sizing for a layout map, zooming the screen canvas changes nothing
        if self._fit_item is None:
            self._on_scale_changed()

    def _disconnect_canvas(self):
        with suppress(Exception):
            self.iface.mapCanvas().scaleChanged.disconnect(self._on_canvas_scale_changed)
        if self._fit_item is not None:
            with suppress(Exception):
                self._fit_item.extentChanged.disconnect(self._on_scale_changed)
            self._fit_item = None

    def _on_live_toggled(self, on):
        if on:
            self._live_apply()
        else:
            self._restore_backup()

    # ---------------------------------------------------------- layer ops
    def _current_label_source(self):
        res = self.field_w.currentField()
        expr, is_expr = res[0], res[1]
        flt_res = self.filter_w.currentField()
        return expr, is_expr, flt_res[0] or ""

    def _ensure_backup(self, layer):
        if self._backup is None or self._backup[0] != layer.id():
            lab = layer.labeling()
            self._backup = (layer.id(), lab.clone() if lab else None, layer.labelsEnabled())

    def _restore_backup(self):
        if self._backup is None:
            return
        from qgis.core import QgsProject
        layer_id, lab, enabled = self._backup
        self._backup = None
        self._last_sig = None
        layer = QgsProject.instance().mapLayer(layer_id)
        if layer is None:
            return
        try:
            layer.setLabeling(lab.clone() if lab else None)
            layer.setLabelsEnabled(enabled)
            layer.triggerRepaint()
        except RuntimeError:
            pass

    def _apply_now(self, quiet=False, force=False):
        layer = self.layer_cb.currentLayer()
        if layer is None:
            if not quiet:
                self._msg("No layer", "Choose a point, line or polygon layer first.", "warning")
            return False
        expr, is_expr, flt = self._current_label_source()
        if not expr:
            if not quiet:
                self._msg("Nothing to label", "Choose a field or build an expression in “Label with”.", "warning")
            return False
        self._ensure_backup(layer)
        mm_units = None
        if self._zoom_dependent():
            mm_units = self._mm_units(layer)
        cfg = self.get_cfg()
        sig = json.dumps([layer.id(), cfg, expr, is_expr, flt,
                          None if mm_units is None else round(mm_units, 9)], sort_keys=True, default=str)
        if not force and sig == self._last_sig and self._backup is not None:
            return True              # nothing changed: skip a needless map redraw
        try:
            E.apply_to_layer(layer, cfg, expr, is_expr, flt, mm_units)
            self._last_sig = sig
        except Exception as ex:  # surface API problems instead of failing silently
            self._msg("Could not apply style", str(ex), "critical")
            return False
        return True

    def _live_apply(self):
        if self.live_cb.isChecked():
            self._apply_now(quiet=True)

    def _commit(self):
        if self._apply_now(force=True):
            self._backup = None   # nothing to roll back any more
            QgsSettings().setValue(LAST_KEY, json.dumps(self.get_cfg()))
            layer = self.layer_cb.currentLayer()
            with suppress(Exception):
                self.iface.layerTreeView().refreshLayerSymbology(layer.id())
            return True
        return False

    def _apply(self):
        if self._commit():
            self._msg("Callout labels applied", self.layer_cb.currentLayer().name(), "success")

    def _ok(self):
        if self._commit():
            self.accept()

    def _reset(self):
        self.set_cfg(dict(E.DEFAULTS))
        for b in self._preset_buttons:
            b.setChecked(False)
        self._changed()

    def _remove_labels(self):
        layer = self.layer_cb.currentLayer()
        if layer is None:
            return
        SB = QMessageBox.StandardButton
        if QMessageBox.question(self, "Remove labels",
                                "Turn off all labels on “{}”?".format(layer.name()),
                                SB.Yes | SB.No) != SB.Yes:
            return
        self._timer.stop()
        self.live_cb.blockSignals(True)
        self.live_cb.setChecked(False)   # stop live preview from re-labeling
        self.live_cb.blockSignals(False)
        layer.setLabelsEnabled(False)
        layer.triggerRepaint()
        self._backup = None              # committed change
        self._msg("Labels removed", "Tick “Live preview” or press Apply to label the layer again.", "info")

    def _copy_code(self):
        layer = self.layer_cb.currentLayer()
        if layer is None:
            return
        expr, is_expr, flt = self._current_label_source()
        code = E.generate_code(self.get_cfg(), layer.name(), expr, is_expr,
                               E.geometry_kind(layer), flt)
        QApplication.clipboard().setText(code)
        self._msg("Code copied", "Paste into the QGIS Python Console or an editor script.", "success")

    def _export_qml(self):
        layer = self.layer_cb.currentLayer()
        if layer is None or not self._apply_now():
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export label style", layer.name() + "_callouts.qml",
                                              "QGIS layer style (*.qml)")
        if not path:
            return
        if not path.lower().endswith(".qml"):
            path += ".qml"
        try:
            result = layer.saveNamedStyle(path, E.labeling_style_category())
        except TypeError:
            result = layer.saveNamedStyle(path)
        ok = result[1] if isinstance(result, tuple) else True
        if ok:
            self._msg("Style exported", path, "success")
        else:
            self._msg("Export failed", str(result[0]), "critical")

    # -------------------------------------------------------------- misc
    def _msg(self, title, text, level="info"):
        bar = self.iface.messageBar()
        fn = {"success": bar.pushSuccess, "warning": bar.pushWarning,
              "critical": bar.pushCritical}.get(level, bar.pushInfo)
        fn(title, text)

    def done(self, r):
        self._disconnect_canvas()
        super().done(r)

    def reject(self):
        self._timer.stop()
        self._restore_backup()
        super().reject()

    def closeEvent(self, event):
        self._timer.stop()
        self._restore_backup()
        super().closeEvent(event)


PANEL_PROP = "kga_tools/callout_studio"


class CalloutStudioPanel(CalloutStudioDialog):
    """The studio embedded in a page (Layer Properties > Labels) for one layer.

    Same widgets as the window; the layer is fixed, the window's own buttons
    are gone (the host dialog's Apply / OK / Cancel drive `commit` / `discard`)
    and the live preview starts off, like the other Labels pages."""

    def __init__(self, iface, layer, parent=None, direct=False):
        """direct: no Apply / OK in the host (Layer Styling panel), so every
        change is written to the layer at once instead of previewed."""
        super().__init__(iface, parent)
        self._direct = direct
        self.labels_removed = False
        self.setWindowFlags(Qt.WindowType.Widget)
        self.setMinimumWidth(0)
        self.setSizeGripEnabled(False)
        form = self.layer_cb.parentWidget().layout()
        label = form.labelForField(self.layer_cb)
        if label is not None:
            label.hide()
        self.layer_cb.hide()
        self.layer_cb.setLayer(layer)
        self.bbox.hide()
        self._sep_line.hide()
        self.live_cb.blockSignals(True)
        self.live_cb.setChecked(direct)
        self.live_cb.blockSignals(False)
        self.live_cb.setVisible(not direct)
        self._layer = layer

    def keyPressEvent(self, event):
        # Esc / Enter belong to the host dialog, not to this embedded "dialog"
        if event.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            event.ignore()
            return
        super().keyPressEvent(event)

    @staticmethod
    def layer_has_saved_style(layer):
        """True when the layer carries a style this studio wrote and still shows."""
        return layer is not None and bool(layer.customProperty(PANEL_PROP))             and layer.labelsEnabled() and layer.labeling() is not None

    def has_saved_style(self):
        return self.layer_has_saved_style(self._layer)

    def load_from_layer(self):
        """Show the settings stored with the layer by the last Apply, if any."""
        try:
            saved = json.loads(self._layer.customProperty(PANEL_PROP) or "")
        except (ValueError, TypeError):
            return
        if not isinstance(saved, dict):
            return
        self.set_cfg(E.merged({}, saved.get("cfg") or {}))
        self._loading = True
        try:
            if saved.get("expr"):
                self.field_w.setField(saved["expr"])
            self.filter_w.setField(saved.get("filter") or "")
        finally:
            self._loading = False
        self._update_visibility()

    def commit(self):
        """Write the style to the layer (Layer Properties Apply / OK)."""
        if not self._commit():
            return False
        self._store_style()
        return True

    def _store_style(self):
        expr, is_expr, flt = self._current_label_source()
        self._layer.setCustomProperty(PANEL_PROP, json.dumps(
            {"cfg": self.get_cfg(), "expr": expr, "is_expr": is_expr, "filter": flt}))

    def _remove_labels(self):
        super()._remove_labels()
        self.labels_removed = not self._layer.labelsEnabled()

    def _live_apply(self):
        self.labels_removed = False
        if not self._direct:
            super()._live_apply()
        else:
            # the property first: writing it makes the Layer Styling panel re-apply
            # its own page, which switches labels off for this mode
            self._store_style()
            if self._apply_now(quiet=True):
                self._backup = None      # nothing to roll back: it is the real style
                if not self._layer.labelsEnabled():
                    # the host re-applied its own page while the labeling was being set
                    self._layer.setLabelsEnabled(True)
                    self._layer.triggerRepaint()

    def discard(self):
        """Throw away an un-applied live preview."""
        self._timer.stop()
        self._restore_backup()
        self._disconnect_canvas()

    def _apply(self):
        self.commit()

    def done(self, r):
        pass                       # never closes: it is part of a page

    def reject(self):
        self.discard()


def preset_icon_size():
    from qgis.PyQt.QtCore import QSize
    return QSize(84, 54)

"""Cursor Style Manager dock panel (view only; state lives in CursorManager)."""

from qgis.PyQt.QtCore import Qt, QTimer, QSize
from qgis.PyQt.QtGui import QColor, QIcon, QFont
from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QToolButton,
    QCheckBox, QComboBox, QScrollArea, QFrame, QListWidgetItem, QMenu, QTabWidget,
    QInputDialog, QMessageBox, QButtonGroup, QSizePolicy, QGridLayout, QLineEdit)
from qgis.gui import QgsDockWidget, QgsColorButton

from .style import (CursorStyle, validated, BASE_CUSTOM, BASE_NATIVE,
                    BASE_SYSTEM_ARROW, BASE_SYSTEM_CROSS, LIMITS)
from .presets import builtin_names, builtin_description, is_builtin
from .renderer import build, hotspot_mode, ui_scale_factor
from .controller import CATEGORY_LABELS, ASSIGN_MAIN, ASSIGN_QGIS
from .presets import DEFAULT_PRESET
from .manager import ProfileError
from .preview import CursorPreview, BACKGROUNDS, system_arrow_pixmap
from .widgets import SliderSpin, Gallery, tile_icon, section_label

from ...branding import icon_path

ICON = icon_path("navigation/cursorStyleManager.png")

TYPE_CHOICES = [
    (BASE_CUSTOM, "Drawn cursor", "A cursor drawn from the shapes, colours and sizes below."),
    (BASE_SYSTEM_CROSS, "System crosshair", "The operating system's crosshair cursor."),
    (BASE_SYSTEM_ARROW, "System arrow", "The operating system's normal arrow pointer."),
    (BASE_NATIVE, "QGIS tool cursors", "Leave every tool's own QGIS cursor unchanged."),
]

SHAPE_BTN_QSS = (
    "QToolButton{border:1px solid rgba(127,127,127,0.45);border-radius:7px;"
    "padding:4px 2px;background:transparent;color:palette(window-text);}"
    "QToolButton:hover{border-color:palette(highlight);}"
    "QToolButton:checked{border:2px solid palette(highlight);"
    "background:rgba(31,111,235,0.20);font-weight:bold;}"
    "QToolButton:disabled{color:rgba(127,127,127,0.7);}")

SHAPES = [("crosshair", "Crosshair"), ("circle", "Circle"),
          ("inner_ring", "Inner ring"), ("dot", "Centre dot")]

_SHAPE_DEMO = {
    "crosshair": CursorStyle(crosshair=True, arm_length=8, main_color="#333333", outline=False),
    "circle": CursorStyle(crosshair=False, circle=True, circle_diameter=14, main_color="#333333", outline=False),
    "inner_ring": CursorStyle(crosshair=False, circle=True, inner_ring=True, circle_diameter=16,
                              inner_ring_diameter=7, main_color="#333333", outline=False),
    "dot": CursorStyle(crosshair=False, dot=True, dot_size=5, dot_color="#333333", outline=False),
}


def _luminance(hex_color):
    c = QColor(hex_color)
    return 0.2126 * c.redF() + 0.7152 * c.greenF() + 0.0722 * c.blueF()


class CursorStyleDock(QgsDockWidget):
    def __init__(self, manager, parent=None):
        super().__init__("Cursor Style Manager", parent)
        self.setObjectName("CursorStyleManagerDock")
        self.manager = manager
        self.draft = manager.style
        self.draft_name = manager.active_name
        self._loading = False
        self._icon_cache = {}
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(0)  # coalesce bursts of slider events
        self._preview_timer.timeout.connect(self._update_preview)
        self.preview_renders = 0

        self._build()
        self.manager.stateChanged.connect(self._on_manager_state)
        self.manager.profilesChanged.connect(self._fill_galleries)
        self.manager.controller.statusChanged.connect(self._set_status)
        self._fill_galleries()
        self._load_draft(self.draft, self.draft_name)
        self._sync_header()
        self._set_status(self.manager.controller.status)

    # ================================================================ build
    def _build(self):
        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(8)

        # ---- header
        # The dock's own title bar already says "Cursor Style Manager".
        self.enable_cb = QCheckBox("Use the custom map cursor")
        self.enable_cb.setToolTip("Use the custom cursor on the map canvas.\n"
                                  "Off = every QGIS tool shows its own cursor again.")
        self.enable_cb.toggled.connect(self.manager.set_enabled)
        outer.addWidget(self.enable_cb)

        self.status_box = QFrame()
        self.status_box.setObjectName("csmStatus")
        sl = QHBoxLayout(self.status_box)
        sl.setContentsMargins(8, 4, 4, 4)
        sl.setSpacing(6)
        self.status = QLabel()
        self.status.setWordWrap(True)
        sl.addWidget(self.status, 1)
        self.fix_btn = QPushButton()
        self.fix_btn.setToolTip("Give this kind of map tool the main style "
                                "(you can change it later under “Cursor for each kind of tool”)")
        self.fix_btn.clicked.connect(self._enable_blocked_category)
        self.fix_btn.hide()
        sl.addWidget(self.fix_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        outer.addWidget(self.status_box)

        # ---- live preview (kept visible while scrolling the controls)
        prow = QHBoxLayout()
        prow.setSpacing(4)
        self.name_label = QLabel()
        nf = QFont(self.name_label.font())
        nf.setBold(True)
        self.name_label.setFont(nf)
        prow.addWidget(self.name_label, 1)
        self.bg_group = QButtonGroup(self)
        for key, text in BACKGROUNDS:
            b = QToolButton()
            b.setText(text)
            b.setCheckable(True)
            b.setAutoRaise(True)
            b.setToolTip(f"Preview on a {text.lower()} background")
            b.setProperty("bg", key)
            self.bg_group.addButton(b)
            prow.addWidget(b)
            if key == "map":
                b.setChecked(True)
        self.bg_group.buttonClicked.connect(lambda b: self.preview.set_background(b.property("bg")))
        self.zoom_btn = QToolButton()
        self.zoom_btn.setText("Zoom")
        self.zoom_btn.setCheckable(True)
        self.zoom_btn.setChecked(True)
        self.zoom_btn.setAutoRaise(True)
        self.zoom_btn.setToolTip("Show a pixel-zoomed view with the hotspot marked")
        self.zoom_btn.toggled.connect(lambda on: self.preview.set_zoom(on))
        prow.addWidget(self.zoom_btn)
        outer.addLayout(prow)

        self.preview = CursorPreview()
        outer.addWidget(self.preview)

        self.msg = QLabel()
        self.msg.setWordWrap(True)
        self.msg.setStyleSheet("color:#b26a00;")
        self.msg.hide()
        outer.addWidget(self.msg)

        arow = QHBoxLayout()
        self.reset_btn = QPushButton("Reset")
        self.reset_btn.setToolTip("Discard edits and return to the selected style's saved values")
        self.reset_btn.clicked.connect(self._reset)
        self.apply_btn = QPushButton("Apply")
        self.apply_btn.setDefault(True)
        self.apply_btn.setToolTip("Use this cursor on the map canvas")
        self.apply_btn.clicked.connect(self._apply)
        self.live_cb = QCheckBox("Update map while editing")
        self.live_cb.setToolTip("Send every change straight to the map cursor.\n"
                                "Untick to try changes in the preview first and use Apply.")
        self.live_cb.setChecked(self.manager.store.get_flag("live_apply", True))
        self.live_cb.toggled.connect(self._live_toggled)
        self._live_timer = QTimer(self)
        self._live_timer.setSingleShot(True)
        self._live_timer.setInterval(120)  # debounce slider drags
        self._live_timer.timeout.connect(self._apply)
        arow.addWidget(self.reset_btn)
        arow.addWidget(self.live_cb)
        arow.addStretch(1)
        arow.addWidget(self.apply_btn)
        outer.addLayout(arow)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        outer.addWidget(line)

        # ---- tabs: Presets (with My styles), Customize, Advanced, Tools
        self.tabs = QTabWidget()

        presets_page = QWidget()
        bl = self._page_layout(presets_page)
        bl.addWidget(section_label("Presets"))
        self.gallery = Gallery()
        self.gallery.itemClicked.connect(self._gallery_clicked)
        bl.addWidget(self.gallery)

        mine_row = QHBoxLayout()
        mine_row.addWidget(section_label("My styles"), 1)
        self.save_btn = QToolButton()
        self.save_btn.setText("Save")
        self.save_btn.setToolTip("Overwrite the selected custom style with the current settings")
        self.save_btn.clicked.connect(self._save)
        self.saveas_btn = QToolButton()
        self.saveas_btn.setText("Save as…")
        self.saveas_btn.setToolTip("Save the current settings as a new custom style")
        self.saveas_btn.clicked.connect(self._save_as)
        self.more_btn = QToolButton()
        self.more_btn.setText("More")
        self.more_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.more_btn)
        self.rename_act = menu.addAction("Rename selected style…", self._rename)
        self.delete_act = menu.addAction("Delete selected style", self._delete)
        menu.addSeparator()
        menu.addAction("Restore default settings…", self._restore_defaults)
        self.more_btn.setMenu(menu)
        for b in (self.save_btn, self.saveas_btn, self.more_btn):
            mine_row.addWidget(b)
        bl.addLayout(mine_row)
        self.mine = Gallery()
        self.mine.itemClicked.connect(self._gallery_clicked)
        self.mine.itemDoubleClicked.connect(lambda _i: self._rename())
        bl.addWidget(self.mine)
        self.mine_hint = QLabel("Customise a cursor and choose “Save as…” to keep it here.")
        self.mine_hint.setWordWrap(True)
        self.mine_hint.setEnabled(False)
        bl.addWidget(self.mine_hint)

        bl.addStretch(1)
        self.tabs.addTab(self._scroll(presets_page), "Presets")
        self.tabs.setTabToolTip(0, "Ready-made cursors and your own saved styles")

        custom_page = QWidget()
        bl = self._page_layout(custom_page)
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        grid.addWidget(QLabel("Cursor type"), 0, 0)
        self.type_combo = QComboBox()
        for key, text, tip in TYPE_CHOICES:
            self.type_combo.addItem(text, key)
            self.type_combo.setItemData(self.type_combo.count() - 1, tip, Qt.ItemDataRole.ToolTipRole)
        self.type_combo.currentIndexChanged.connect(self._edited)
        grid.addWidget(self.type_combo, 0, 1)
        bl.addLayout(grid)

        self.custom_box = QWidget()
        cl = QVBoxLayout(self.custom_box)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(6)

        srow = QHBoxLayout()
        srow.setSpacing(4)
        self.shape_btns = {}
        for key, text in SHAPES:
            b = QToolButton()
            b.setText(text)
            b.setCheckable(True)
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            b.setIcon(self._shape_icon(key))
            b.setIconSize(QSize(22, 22))
            b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            b.setToolTip(f"Show or hide the {text.lower()} (combine freely)")
            b.setProperty("label", text)
            b.setStyleSheet(SHAPE_BTN_QSS)
            b.toggled.connect(lambda on, btn=b: self._shape_toggled(btn, on))
            b.toggled.connect(self._edited)
            self.shape_btns[key] = b
            srow.addWidget(b)
        cl.addLayout(srow)

        crow = QGridLayout()
        crow.setHorizontalSpacing(6)
        self.main_color = self._color_btn("Main colour of lines and rings")
        self.outline_color = self._color_btn("Outline (halo) colour – keeps the cursor visible on any background")
        self.dot_color = self._color_btn("Centre dot colour")
        self.outline_cb = QCheckBox("Outline")
        self.outline_cb.setToolTip("Draw a contrasting halo around every part")
        self.outline_cb.toggled.connect(self._edited)
        self.contrast_btn = QToolButton()
        self.contrast_btn.setText("Auto")
        self.contrast_btn.setAutoRaise(True)
        self.contrast_btn.setToolTip("Pick black or white outline for best contrast with the main colour")
        self.contrast_btn.clicked.connect(self._auto_contrast)
        crow.addWidget(QLabel("Main"), 0, 0)
        crow.addWidget(self.main_color, 1, 0)
        crow.addWidget(self.outline_cb, 0, 1)
        ol = QHBoxLayout()
        ol.setSpacing(2)
        ol.addWidget(self.outline_color, 1)
        ol.addWidget(self.contrast_btn)
        crow.addLayout(ol, 1, 1)
        crow.addWidget(QLabel("Centre dot"), 0, 2)
        crow.addWidget(self.dot_color, 1, 2)
        cl.addLayout(crow)

        self.s_scale = SliderSpin("Overall size", *LIMITS["scale"], suffix=" %",
                                  tooltip="Scales every part of the cursor together")
        self.s_arm = SliderSpin("Crosshair length", *LIMITS["arm_length"],
                                tooltip="Distance from the hotspot to the end of each arm")
        self.s_circle = SliderSpin("Circle diameter", *LIMITS["circle_diameter"])
        self.s_dot = SliderSpin("Centre dot size", *LIMITS["dot_size"])
        self.s_line = SliderSpin("Line thickness", *LIMITS["line_width"],
                                 tooltip="Rendered as an odd number of device pixels "
                                         "so lines stay centred on the hotspot")
        self.s_outline = SliderSpin("Outline thickness", *LIMITS["outline_width"])
        for w in (self.s_scale, self.s_arm, self.s_circle, self.s_dot, self.s_line, self.s_outline):
            w.valueChanged.connect(self._edited)
            cl.addWidget(w)

        bl.addWidget(self.custom_box)
        bl.addStretch(1)
        self.tabs.addTab(self._scroll(custom_page), "Customize")
        self.tabs.setTabToolTip(1, "Shape, colours and sizes of the cursor")

        self.adv_page = QWidget()  # enabled with the rest of the drawn-cursor controls
        al = self._page_layout(self.adv_page)
        adv_note = QLabel("These settings apply to the drawn cursor.")
        adv_note.setEnabled(False)
        al.addWidget(adv_note)
        self.s_gap = SliderSpin("Centre gap", *LIMITS["gap"],
                                tooltip="Leave the area around the hotspot open so you can see what is under it")
        self.s_inner = SliderSpin("Inner ring", *LIMITS["inner_ring_diameter"])
        self.s_opacity = SliderSpin("Opacity", *LIMITS["opacity"], suffix=" %")
        for w in (self.s_gap, self.s_inner, self.s_opacity):
            w.valueChanged.connect(self._edited)
            al.addWidget(w)
        self.dashed_cb = QCheckBox("Dashed lines")
        self.dashed_cb.toggled.connect(self._edited)
        self.uiscale_cb = QCheckBox("Scale with QGIS interface size")
        self.uiscale_cb.setToolTip("Grow the cursor with the QGIS font/UI size, the way QGIS sizes "
                                   "its own cursors (helps at 125–200 % Windows display scaling)")
        self.uiscale_cb.toggled.connect(self._edited)
        al.addWidget(self.dashed_cb)
        al.addWidget(self.uiscale_cb)
        al.addStretch(1)
        self.tabs.addTab(self._scroll(self.adv_page), "Advanced")
        self.tabs.setTabToolTip(2, "Centre gap, inner ring, opacity, dashes and interface scaling")

        tools_page = QWidget()
        xl = self._page_layout(tools_page)
        xl.addWidget(section_label("Cursor for each kind of tool"))
        intro = QLabel("“Main style” is the style selected on the Presets tab. Pick a preset or one of "
                       "your saved styles to give a kind of tool a cursor of its own.")
        intro.setWordWrap(True)
        intro.setEnabled(False)
        xl.addWidget(intro)
        ag = QGridLayout()
        ag.setHorizontalSpacing(8)
        ag.setVerticalSpacing(4)
        ag.setColumnStretch(1, 1)
        self.assign_combos = {}
        for row, (key, text, tip) in enumerate(CATEGORY_LABELS):
            lab = QLabel(text)  # no buddy, so '&' is shown as is
            lab.setToolTip(tip)
            combo = QComboBox()
            combo.setToolTip(tip)
            combo.setIconSize(QSize(20, 20))
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(10)
            combo.currentIndexChanged.connect(lambda _i, k=key: self._assignment_picked(k))
            self.assign_combos[key] = combo
            ag.addWidget(lab, row, 0)
            ag.addWidget(combo, row, 1)
        xl.addLayout(ag)
        note = QLabel("Short-lived QGIS feedback (closed hand while panning, wheel-zoom, "
                      "busy cursor) always stays native.")
        note.setWordWrap(True)
        note.setEnabled(False)
        xl.addWidget(note)
        xl.addStretch(1)
        self.tabs.addTab(self._scroll(tools_page), "Tools")
        self.tabs.setTabToolTip(3, "Give digitizing, editing, selection and other tools their own cursor")

        outer.addWidget(self.tabs, 1)
        self.setWidget(root)
        self.setMinimumWidth(300)

    @staticmethod
    def _page_layout(page):
        lay = QVBoxLayout(page)
        lay.setContentsMargins(4, 8, 4, 4)
        lay.setSpacing(6)
        return lay

    @staticmethod
    def _scroll(page):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(page)
        return scroll

    def _color_btn(self, tip):
        b = QgsColorButton()
        b.setAllowOpacity(False)
        b.setShowNoColor(False)
        b.setToolTip(tip)
        b.setColorDialogTitle(tip.split("–")[0].strip())
        b.setMinimumHeight(24)
        b.colorChanged.connect(self._edited)
        return b

    def _shape_icon(self, key):
        dpr = self.devicePixelRatioF()
        res = build(_SHAPE_DEMO[key].with_changes(match_ui_scale=False), dpr, 1.0, hotspot_mode())
        return QIcon(res.pixmap)

    # ============================================================ galleries
    def _icon_for(self, style):
        dpr = self.devicePixelRatioF()
        key = (style.key(), dpr)
        icon = self._icon_cache.get(key)
        if icon is None:
            if style.base == BASE_CUSTOM:
                pm = build(style.with_changes(match_ui_scale=False), dpr, 1.0, hotspot_mode()).pixmap
            elif style.base == BASE_SYSTEM_ARROW:
                pm = system_arrow_pixmap(26, dpr)
            elif style.base == BASE_SYSTEM_CROSS:
                pm = build(CursorStyle(arm_length=9, main_color="#000000"), dpr, 1.0, hotspot_mode()).pixmap
            else:
                pm = QIcon(ICON).pixmap(QSize(26, 26))
            icon = tile_icon(pm, 44, dpr)
            if len(self._icon_cache) > 64:
                self._icon_cache.clear()
            self._icon_cache[key] = icon
        return icon

    def _fill_galleries(self):
        for gal, names in ((self.gallery, builtin_names()),
                           (self.mine, list(self.manager.profiles.keys()))):
            gal.blockSignals(True)
            gal.clear()
            for name in names:
                st = self.manager.style_for(name)
                it = QListWidgetItem(self._icon_for(st), name)
                it.setData(Qt.ItemDataRole.UserRole, name)
                it.setToolTip(builtin_description(name) if is_builtin(name) else "Your saved style")
                it.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
                gal.addItem(it)
            gal.blockSignals(False)
            gal.fit_height()
        self.mine_hint.setVisible(not self.manager.profiles)
        self._mark_galleries()
        self._fill_assign_combos()

    # ======================================================= per-tool cursors
    def _fill_assign_combos(self):
        """(Re)build the drop-downs: QGIS cursor, Main style, presets, my styles."""
        presets = [n for n in builtin_names() if n != DEFAULT_PRESET]
        mine = list(self.manager.profiles.keys())
        for combo in self.assign_combos.values():
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(QIcon(ICON), "QGIS cursor", ASSIGN_QGIS)
            combo.setItemData(0, "The tool keeps its own QGIS cursor", Qt.ItemDataRole.ToolTipRole)
            combo.addItem(self._icon_for(self.manager.style), self.manager.main_label(), ASSIGN_MAIN)
            combo.setItemData(1, "The style selected in this panel", Qt.ItemDataRole.ToolTipRole)
            for group in (presets, mine):
                if group:
                    combo.insertSeparator(combo.count())
                for name in group:
                    combo.addItem(self._icon_for(self.manager.style_for(name)), name, name)
            combo.blockSignals(False)
        self._sync_assign_combos()

    def _sync_assign_combos(self):
        main_icon = self._icon_for(self.manager.style)
        for key, combo in self.assign_combos.items():
            combo.blockSignals(True)
            main = combo.findData(ASSIGN_MAIN)
            if main >= 0:
                combo.setItemText(main, self.manager.main_label())
                combo.setItemIcon(main, main_icon)
            idx = combo.findData(self.manager.assignments.get(key, ASSIGN_MAIN))
            combo.setCurrentIndex(idx if idx >= 0 else main)
            combo.blockSignals(False)

    def _assignment_picked(self, key):
        value = self.assign_combos[key].currentData()
        if value is not None:
            self.manager.set_assignment(key, value)

    def _mark_galleries(self):
        applied = self.manager.active_name
        for gal in (self.gallery, self.mine):
            for i in range(gal.count()):
                it = gal.item(i)
                name = it.data(Qt.ItemDataRole.UserRole)
                f = QFont(gal.font())
                f.setBold(name == applied)
                it.setFont(f)
                if name == self.draft_name:
                    gal.setCurrentItem(it)
                    it.setSelected(True)
                else:
                    it.setSelected(False)

    def _gallery_clicked(self, item):
        name = item.data(Qt.ItemDataRole.UserRole)
        style = self.manager.style_for(name)
        if style is None:
            return
        other = self.mine if item.listWidget() is self.gallery else self.gallery
        other.clearSelection()
        self._load_draft(style, name)
        self.manager.apply(name, style)  # one click applies a preset

    # =============================================================== editing
    def _load_draft(self, style, name):
        self._loading = True
        try:
            self.draft, self.draft_name = style, name
            self.type_combo.setCurrentIndex(max(0, self.type_combo.findData(style.base)))
            for key, b in self.shape_btns.items():
                b.setChecked(getattr(style, key))
            self.main_color.setColor(QColor(style.main_color))
            self.outline_color.setColor(QColor(style.outline_color))
            self.dot_color.setColor(QColor(style.dot_color))
            self.outline_cb.setChecked(style.outline)
            self.s_scale.setValue(style.scale)
            self.s_arm.setValue(style.arm_length)
            self.s_circle.setValue(style.circle_diameter)
            self.s_dot.setValue(style.dot_size)
            self.s_line.setValue(style.line_width)
            self.s_outline.setValue(style.outline_width)
            self.s_gap.setValue(style.gap)
            self.s_inner.setValue(style.inner_ring_diameter)
            self.s_opacity.setValue(style.opacity)
            self.dashed_cb.setChecked(style.dashed)
            self.uiscale_cb.setChecked(style.match_ui_scale)
        finally:
            self._loading = False
        self.msg.hide()
        self._sync_enabled_controls()
        self._mark_galleries()
        self._refresh_draft_ui()

    def _read_controls(self):
        def hexc(button):
            return button.color().name()
        return CursorStyle(
            base=self.type_combo.currentData(),
            crosshair=self.shape_btns["crosshair"].isChecked(),
            circle=self.shape_btns["circle"].isChecked(),
            inner_ring=self.shape_btns["inner_ring"].isChecked(),
            dot=self.shape_btns["dot"].isChecked(),
            scale=self.s_scale.value(), arm_length=self.s_arm.value(),
            gap=self.s_gap.value(), circle_diameter=self.s_circle.value(),
            inner_ring_diameter=self.s_inner.value(), dot_size=self.s_dot.value(),
            line_width=self.s_line.value(), outline=self.outline_cb.isChecked(),
            outline_width=self.s_outline.value(), dashed=self.dashed_cb.isChecked(),
            opacity=self.s_opacity.value(), main_color=hexc(self.main_color),
            outline_color=hexc(self.outline_color), dot_color=hexc(self.dot_color),
            match_ui_scale=self.uiscale_cb.isChecked())

    def _edited(self, *_args):
        if self._loading:
            return
        raw = self._read_controls()
        style, msgs = validated(raw)
        if style != raw:
            # reflect automatic corrections so controls never show invalid values
            self._load_draft(style, self.draft_name)
        self.draft = style
        self.msg.setText(" ".join(msgs))
        self.msg.setVisible(bool(msgs))
        self._sync_enabled_controls()
        self._refresh_draft_ui()
        if self.live_cb.isChecked():
            self._live_timer.start()

    def _sync_enabled_controls(self):
        custom = self.type_combo.currentData() == BASE_CUSTOM
        self.custom_box.setEnabled(custom)
        self.adv_page.setEnabled(custom)
        if not custom:
            return
        s = self.shape_btns
        cross, circ, ring, dot = (s[k].isChecked() for k in ("crosshair", "circle", "inner_ring", "dot"))
        self.s_arm.setEnabled(cross)
        self.s_gap.setEnabled(cross)
        self.s_circle.setEnabled(circ)
        self.s_inner.setEnabled(ring)
        self.s_dot.setEnabled(dot)
        self.dot_color.setEnabled(dot)
        self.s_line.setEnabled(cross or circ or ring)
        self.dashed_cb.setEnabled(cross or circ or ring)
        ol = self.outline_cb.isChecked()
        self.outline_color.setEnabled(ol)
        self.contrast_btn.setEnabled(ol)
        self.s_outline.setEnabled(ol)

    def _shape_toggled(self, btn, on):
        label = btn.property("label")
        btn.setText(("✓ " if on else "") + label)

    def _live_toggled(self, on):
        self.manager.store.set_flag("live_apply", on)
        if on:
            self._apply()
        self._refresh_draft_ui()

    def _enable_blocked_category(self):
        cat = self.manager.controller.blocked_category
        if cat:
            self.manager.set_assignment(cat, ASSIGN_MAIN)

    def _auto_contrast(self):
        lum = _luminance(self.main_color.color().name())
        self.outline_color.setColor(QColor("#000000" if lum > 0.45 else "#ffffff"))

    def _refresh_draft_ui(self):
        self._preview_timer.start()
        applied = self.manager.style
        dirty = self.draft != applied or self.draft_name != self.manager.active_name
        saved = self.manager.style_for(self.draft_name)
        modified = saved is not None and saved != self.draft
        name = self.draft_name if saved is not None else "Custom"
        self.name_label.setText(name + ("  · modified" if modified else ""))
        live = self.live_cb.isChecked()
        self.apply_btn.setVisible(not live)
        self.apply_btn.setEnabled(dirty)
        self.apply_btn.setText("Apply changes" if dirty else "Applied")
        self.reset_btn.setEnabled(modified or dirty)
        is_mine = self.draft_name in self.manager.profiles
        self.save_btn.setEnabled(is_mine and modified)
        self.rename_act.setEnabled(is_mine)
        self.delete_act.setEnabled(is_mine)

    def _update_preview(self):
        res = build(self.draft, self.preview.devicePixelRatioF(), ui_scale_factor(), hotspot_mode()) \
            if self.draft.base == BASE_CUSTOM else build(self.draft)
        self.preview_renders += 1
        self.preview.set_result(self.draft, res)

    # =============================================================== actions
    def _apply(self):
        name = self.draft_name if self.manager.style_for(self.draft_name) is not None else "Custom"
        self.manager.apply(name, self.draft)

    def _reset(self):
        saved = self.manager.style_for(self.draft_name)
        self._load_draft(saved if saved is not None else self.manager.style, self.draft_name)
        if self.live_cb.isChecked():
            self._apply()

    def _ask_name(self, title, label, text=""):
        name, ok = QInputDialog.getText(self, title, label, QLineEdit.EchoMode.Normal, text)
        return name if ok else None

    def _save(self):
        try:
            self.manager.save_profile(self.draft_name, self.draft, overwrite=True)
        except ProfileError as e:
            QMessageBox.warning(self, "Save style", str(e))
            return
        self.manager.apply(self.draft_name, self.draft)
        self._refresh_draft_ui()

    def _save_as(self):
        base = self.draft_name if self.draft_name not in (None, "Custom") else "My cursor"
        suggestion = base if base in self.manager.profiles else f"{base} (custom)"
        while True:
            name = self._ask_name("Save cursor style", "Name for this style:", suggestion)
            if name is None:
                return
            try:
                name = self.manager.save_profile(name, self.draft)
                break
            except ProfileError as e:
                QMessageBox.warning(self, "Save cursor style", str(e))
                suggestion = name
        self.draft_name = name
        self.manager.apply(name, self.draft)
        self._mark_galleries()
        self._refresh_draft_ui()

    def _rename(self):
        old = self.draft_name
        if old not in self.manager.profiles:
            return
        while True:
            name = self._ask_name("Rename style", "New name:", old)
            if name is None:
                return
            try:
                new = self.manager.rename_profile(old, name)
                break
            except ProfileError as e:
                QMessageBox.warning(self, "Rename style", str(e))
        self.draft_name = new
        self._fill_galleries()
        self._refresh_draft_ui()

    def _delete(self):
        name = self.draft_name
        if name not in self.manager.profiles:
            return
        if QMessageBox.question(self, "Delete style", f"Delete your style “{name}”?") \
                != QMessageBox.StandardButton.Yes:
            return
        moved = self.manager.delete_profile(name)
        self.draft_name = "Custom"
        self._refresh_draft_ui()
        if moved:
            labels = dict((k, l) for k, l, _ in CATEGORY_LABELS)
            self.msg.setText("{} now use{} the main style.".format(
                ", ".join(labels[c] for c in moved), "s" if len(moved) == 1 else ""))
            self.msg.show()

    def _restore_defaults(self):
        box = QMessageBox(QMessageBox.Icon.Question, "Restore default settings",
                          "Return to the QGIS Default cursor and default tool settings?",
                          parent=self)
        keep = box.addButton("Restore, keep my styles", QMessageBox.ButtonRole.AcceptRole)
        wipe = box.addButton("Restore and delete my styles", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(keep)
        (box.exec if hasattr(box, "exec") else box.exec_)()
        clicked = box.clickedButton()
        if clicked not in (keep, wipe):
            return
        self.manager.restore_defaults(delete_custom=clicked is wipe)
        self._load_draft(self.manager.style, self.manager.active_name)

    # ========================================================= manager sync
    def _on_manager_state(self):
        self._sync_header()
        self._mark_galleries()
        self._refresh_draft_ui()

    def _sync_header(self):
        self.enable_cb.blockSignals(True)
        self.enable_cb.setChecked(self.manager.enabled)
        self.enable_cb.blockSignals(False)
        self._sync_assign_combos()

    def _set_status(self, text):
        cat = self.manager.controller.blocked_category
        self.status.setText(text or "")
        self.status_box.setVisible(bool(text))
        if cat:
            label = dict((k, l) for k, l, _ in CATEGORY_LABELS)[cat]
            self.fix_btn.setText("Use here")
            self.fix_btn.setToolTip(f"Give “{label}” tools the main style")
            self.status_box.setStyleSheet("#csmStatus{border-radius:6px;"
                                          "background:rgba(255,170,0,0.22);"
                                          "border:1px solid rgba(255,170,0,0.7);}")
        else:
            self.status_box.setStyleSheet("#csmStatus{border-radius:6px;"
                                          "background:rgba(127,127,127,0.16);}")
        self.fix_btn.setVisible(bool(cat))

    def cleanup(self):
        """Disconnect from long-lived objects before deletion."""
        for sig, slot in ((self.manager.stateChanged, self._on_manager_state),
                          (self.manager.profilesChanged, self._fill_galleries),
                          (self.manager.controller.statusChanged, self._set_status)):
            try:
                sig.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self._preview_timer.stop()
        self._live_timer.stop()

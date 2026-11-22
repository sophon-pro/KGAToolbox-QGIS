# -*- coding: utf-8 -*-
"""Layout Guides - one small, simple panel inside the Layout Designer.

Step 1  Margin from page edge (Left / Right / Top / Bottom)
Step 2  Divide the area inside the margin into columns x rows
Guides on the current page update live while you type.
"""

from contextlib import suppress
import json
import os

from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QButtonGroup, QCheckBox, QDockWidget, QDoubleSpinBox, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QMainWindow, QLineEdit, QPushButton, QRadioButton, QSpinBox,
    QVBoxLayout, QWidget,
)
from qgis.core import QgsLayoutItemMap, QgsSettings

try:  # Qt6 moved QAction to QtGui
    from qgis.PyQt.QtGui import QAction
except ImportError:
    from qgis.PyQt.QtWidgets import QAction

from qgis.PyQt.QtCore import QPointF, QRectF, QSize
from qgis.PyQt.QtGui import QColor, QIcon, QPainter, QPen
from qgis.PyQt.QtWidgets import QSizePolicy

from .. import branding
from ..core.layout_guides import bridge as qb
from ..core.layout_guides import engine as eng
from . import map_tabs
from . import north_arrow_dock as na
from .panel_bubble import ToolbarReveal, attach_reveal, detach_reveal

SETTINGS_KEY = "KGA/layout_guides/"
ICON = os.path.join(os.path.dirname(os.path.dirname(__file__)), "icons", "mapping/layoutGuides.png")
UNDO_MERGE_ID = 0x4753  # consecutive live edits = one Ctrl+Z

CSS = """
QGroupBox { font-weight: 600; border: 1px solid palette(mid); border-radius: 6px;
            margin-top: 10px; padding: 10px 8px 8px 8px; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
QGroupBox QLabel, QGroupBox QCheckBox, QGroupBox QSpinBox,
QGroupBox QDoubleSpinBox { font-weight: normal; }
QLabel#muted { color: palette(dark); }
"""


def edge_spin(name):
    s = QDoubleSpinBox()
    s.setRange(0.0, 2000.0)
    s.setDecimals(1)
    s.setSingleStep(1.0)
    s.setPrefix(name + "  ")
    s.setSuffix(" mm")
    s.setAccelerated(True)
    s.setToolTip("Distance from the %s edge of the page" % name.upper())
    s.setMinimumWidth(140)
    s.setAlignment(Qt.AlignmentFlag.AlignCenter)
    return s


def dist_edit(tip):
    e = QLineEdit()
    e.setPlaceholderText("e.g. 50")
    e.setToolTip(tip + "\nOne value (50) or several (50, 120), in mm")
    e.setClearButtonEnabled(True)
    return e


def muted(text=""):
    lab = QLabel(text)
    lab.setObjectName("muted")
    lab.setWordWrap(True)
    return lab


def swatch(color, text):
    return '<span style="color:%s; font-size:14px">■</span> %s' % (color, text)


COLORS = {
    "edge": "#d6336c",
    "divide": "#1c7ed6",
    "gap": "#1c7ed6",
    "center": "#f76707",
}


class MiniPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.page_w, self.page_h = 297.0, 210.0
        self.result = None
        self.setMinimumSize(180, 130)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def sizeHint(self):
        return QSize(230, 160)

    def set_data(self, w, h, result):
        self.page_w, self.page_h = max(1.0, w), max(1.0, h)
        self.result = result
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pad = 6.0
        k = min((self.width() - 2 * pad) / self.page_w, (self.height() - 2 * pad) / self.page_h)
        w, h = self.page_w * k, self.page_h * k
        page = QRectF((self.width() - w) / 2.0, (self.height() - h) / 2.0, w, h)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 35))
        p.drawRect(page.translated(2, 2))
        p.setBrush(QColor("#ffffff"))
        p.setPen(QPen(QColor("#adb5bd"), 1))
        p.drawRect(page)

        res = self.result
        if res and res.get("area"):
            x, y, aw, ah = res["area"]
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(28, 126, 214, 22))
            p.drawRect(QRectF(page.left() + x * k, page.top() + y * k, aw * k, ah * k))
        if res:
            for o, pos, kind in res["guides"]:
                p.setPen(QPen(QColor(COLORS.get(kind, "#1c7ed6")), 2.0 if kind == "edge" else 1.6))
                if o == "v":
                    X = page.left() + pos * k
                    p.drawLine(QPointF(X, page.top()), QPointF(X, page.bottom()))
                else:
                    Y = page.top() + pos * k
                    p.drawLine(QPointF(page.left(), Y), QPointF(page.right(), Y))
        p.end()


class GuideStudioDock(ToolbarReveal, QDockWidget):

    def __init__(self, designer, parent=None):
        super().__init__("Guides", parent)
        self.designer = designer
        self.setObjectName("LayoutGuideStudioDock")
        self._loading = False
        self._connected_layout = None

        self._apply_timer = QTimer(self)
        self._apply_timer.setSingleShot(True)
        self._apply_timer.setInterval(300)
        self._apply_timer.timeout.connect(self._live_apply)

        root = QWidget()
        root.setStyleSheet(CSS)
        root.setMaximumWidth(420)
        v = QVBoxLayout(root)
        v.setContentsMargins(10, 4, 10, 8)
        v.setSpacing(8)

        # ---------------- Step 1: margin -------------------------------------
        box1 = QGroupBox("Step 1 · Margin from page edge")
        g = QGridLayout(box1)
        g.setSpacing(6)
        self.e_top = edge_spin("Top")
        self.e_bottom = edge_spin("Bottom")
        self.e_left = edge_spin("Left")
        self.e_right = edge_spin("Right")
        self.mini = MiniPage()
        g.addWidget(self.e_top, 0, 1, 1, 2, Qt.AlignmentFlag.AlignHCenter)
        g.addWidget(self.e_left, 1, 0, 1, 2, Qt.AlignmentFlag.AlignLeft)
        g.addWidget(self.e_right, 1, 2, 1, 2, Qt.AlignmentFlag.AlignRight)
        g.addWidget(self.mini, 2, 0, 1, 4)
        g.addWidget(self.e_bottom, 3, 1, 1, 2, Qt.AlignmentFlag.AlignHCenter)
        self.e_same = QCheckBox("Same margin on all sides")
        self.e_same.setToolTip("Change one box and the other three follow")
        g.addWidget(self.e_same, 4, 0, 1, 4)
        v.addWidget(box1)

        # ---------------- Step 2: divide -------------------------------------
        box2 = QGroupBox("Step 2 · Divide the area inside the margin")
        outer2 = QVBoxLayout(box2)
        outer2.setSpacing(6)

        mode_row = QHBoxLayout()
        self.mode_equal = QRadioButton("Equal parts")
        self.mode_equal.setToolTip("Split into columns / rows of the same size")
        self.mode_exact = QRadioButton("Exact distance")
        self.mode_exact.setToolTip("Put a guide a set distance from a margin line\n"
                                   "e.g. a 50 mm legend panel or a 25 mm title band")
        self.mode_group = QButtonGroup(self)
        self.mode_group.addButton(self.mode_equal)
        self.mode_group.addButton(self.mode_exact)
        self.mode_equal.setChecked(True)
        mode_row.addWidget(self.mode_equal)
        mode_row.addWidget(self.mode_exact)
        mode_row.addStretch(1)
        outer2.addLayout(mode_row)

        # --- exact distance page
        self.exact_page = QWidget()
        x = QGridLayout(self.exact_page)
        x.setContentsMargins(0, 0, 0, 0)
        x.setHorizontalSpacing(8)
        self.x_left = dist_edit("Distance to the right of the LEFT margin line")
        self.x_right = dist_edit("Distance to the left of the RIGHT margin line")
        self.x_top = dist_edit("Distance below the TOP margin line")
        self.x_bottom = dist_edit("Distance above the BOTTOM margin line")
        for row, (txt, ed) in enumerate((("From left margin  →", self.x_left),
                                         ("From right margin  ←", self.x_right),
                                         ("From top margin  ↓", self.x_top),
                                         ("From bottom margin  ↑", self.x_bottom))):
            x.addWidget(QLabel(txt), row, 0)
            x.addWidget(ed, row, 1)
        x.addWidget(muted("In mm. Several lines: 50, 120"), 4, 0, 1, 2)
        x.setRowMinimumHeight(4, 18)
        x.setColumnStretch(1, 1)
        outer2.addWidget(self.exact_page)

        # --- equal parts page
        self.equal_page = QWidget()
        d = QGridLayout(self.equal_page)
        d.setContentsMargins(0, 0, 0, 0)
        d.setHorizontalSpacing(8)
        self.d_cols = QSpinBox()
        self.d_cols.setRange(1, 50)
        self.d_cols.setSuffix("  across")
        self.d_cols.setToolTip("Parts from left to right (1 = no split)")
        self.d_rows = QSpinBox()
        self.d_rows.setRange(1, 50)
        self.d_rows.setSuffix("  down")
        self.d_rows.setToolTip("Parts from top to bottom (1 = no split)")
        d.addWidget(QLabel("Columns"), 0, 0)
        d.addWidget(self.d_cols, 0, 1)
        d.addWidget(QLabel("Rows"), 1, 0)
        d.addWidget(self.d_rows, 1, 1)
        d.setColumnStretch(1, 1)
        outer2.addWidget(self.equal_page)

        # --- shared by both
        shared = QGridLayout()
        shared.setHorizontalSpacing(8)
        self.gap_label = QLabel("Space between")
        self.d_gap = QDoubleSpinBox()
        self.d_gap.setRange(0.0, 500.0)
        self.d_gap.setDecimals(1)
        self.d_gap.setSuffix(" mm")
        self.d_gap.setToolTip("0 = one guide per split.\n"
                              "More = a second guide with this space, e.g. the gap\n"
                              "between the map and the legend panel.")
        shared.addWidget(self.gap_label, 0, 0)
        shared.addWidget(self.d_gap, 0, 1)
        self.d_center = QCheckBox("Add center lines")
        self.d_center.setToolTip("A cross through the middle of the area inside the margin")
        shared.addWidget(self.d_center, 1, 0, 1, 2)
        shared.setColumnStretch(1, 1)
        outer2.addLayout(shared)
        v.addWidget(box2)

        # ---------------- result ---------------------------------------------
        self.result_label = QLabel("")
        self.result_label.setWordWrap(True)
        v.addWidget(self.result_label)
        self.legend = muted()
        self.legend.setTextFormat(Qt.TextFormat.RichText)
        v.addWidget(self.legend)
        v.addWidget(muted("Guides on this page update as you type.  Ctrl+Z undoes."))

        # ---------------- buttons --------------------------------------------
        b = QHBoxLayout()
        self.btn_all = QPushButton("Apply to all pages")
        self.btn_all.setToolTip("Put the same margin and parts on every page "
                                "(each page uses its own size)")
        self.btn_all.clicked.connect(self.apply_all)
        self.btn_clear = QPushButton("Remove guides")
        self.btn_clear.setToolTip("Remove all guides from this page (Ctrl+Z to undo)")
        self.btn_clear.clicked.connect(self.clear_current)
        b.addWidget(self.btn_all, 1)
        b.addWidget(self.btn_clear, 1)
        v.addLayout(b)

        vb = QHBoxLayout()
        self.v_show = QCheckBox("Show guides")
        self.v_snap = QCheckBox("Snap to guides")
        self.v_show.clicked.connect(lambda on: self._set_view("mActionShowGuides", on))
        self.v_snap.clicked.connect(lambda on: self._set_view("mActionSnapGuides", on))
        vb.addWidget(self.v_show)
        vb.addWidget(self.v_snap)
        vb.addStretch(1)
        v.addLayout(vb)
        v.addStretch(1)

        holder = QWidget()
        hl = QHBoxLayout(holder)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.addWidget(root)
        hl.addStretch(0)
        self.setWidget(holder)

        # ---------------- wiring ---------------------------------------------
        self.edges = (self.e_left, self.e_right, self.e_top, self.e_bottom)
        for s in self.edges:
            s.valueChanged.connect(self._edge_changed)
        self.d_cols.valueChanged.connect(self._changed)
        self.d_rows.valueChanged.connect(self._changed)
        self.d_gap.valueChanged.connect(self._changed)
        self.d_center.toggled.connect(self._changed)
        self.mode_exact.toggled.connect(self._changed)
        for e in (self.x_left, self.x_right, self.x_top, self.x_bottom):
            e.textChanged.connect(self._changed)
        self.e_same.toggled.connect(self._same_toggled)

        self._load_settings()
        self._connect_layout()
        self._sync_view()
        self.visibilityChanged.connect(lambda vis: vis and self.refresh())
        QTimer.singleShot(0, self.refresh)

    # =================================================================== state
    def layout_(self):
        try:
            return self.designer.layout()
        except Exception:
            return None

    def page_index(self):
        lay = self.layout_()
        n = qb.page_count(lay)
        if n == 0:
            return -1
        try:
            p = self.designer.view().currentPage()
        except Exception:
            p = 0
        return min(max(0, p), n - 1)

    def get_settings(self):
        return {
            "left": self.e_left.value(), "right": self.e_right.value(),
            "top": self.e_top.value(), "bottom": self.e_bottom.value(),
            "same": self.e_same.isChecked(),
            "cols": self.d_cols.value(), "rows": self.d_rows.value(),
            "gap": self.d_gap.value(), "center": self.d_center.isChecked(),
            "mode": "exact" if self.mode_exact.isChecked() else "equal",
            "from_left": self.x_left.text(), "from_right": self.x_right.text(),
            "from_top": self.x_top.text(), "from_bottom": self.x_bottom.text(),
        }

    def set_settings(self, s):
        s = eng.clean_settings(s)
        self._loading = True
        try:
            self.e_left.setValue(float(s["left"]))
            self.e_right.setValue(float(s["right"]))
            self.e_top.setValue(float(s["top"]))
            self.e_bottom.setValue(float(s["bottom"]))
            self.e_same.setChecked(bool(s["same"]))
            self.d_cols.setValue(int(s["cols"]))
            self.d_rows.setValue(int(s["rows"]))
            self.d_gap.setValue(float(s["gap"]))
            self.d_center.setChecked(bool(s["center"]))
            (self.mode_exact if s["mode"] == "exact" else self.mode_equal).setChecked(True)
            self.x_left.setText(str(s["from_left"]))
            self.x_right.setText(str(s["from_right"]))
            self.x_top.setText(str(s["from_top"]))
            self.x_bottom.setText(str(s["from_bottom"]))
        finally:
            self._loading = False
        self.refresh()

    def _load_settings(self):
        raw = QgsSettings().value(SETTINGS_KEY + "simple", "")
        try:
            s = json.loads(raw) if raw else eng.default_settings()
        except Exception:
            s = eng.default_settings()
        # opening the panel never changes the layout; only an edit does
        self.set_settings(s)

    def _save_settings(self):
        QgsSettings().setValue(SETTINGS_KEY + "simple", json.dumps(self.get_settings()))

    # ================================================================= changes
    def _edge_changed(self, value):
        if self._loading:
            return
        if self.e_same.isChecked():
            src = self.sender()
            for s in self.edges:
                if s is not src and s.value() != value:
                    s.blockSignals(True)
                    s.setValue(value)
                    s.blockSignals(False)
        self._changed()

    def _same_toggled(self, on):
        if self._loading:
            return
        if on:
            val = self.e_left.value()
            for s in self.edges:
                s.blockSignals(True)
                s.setValue(val)
                s.blockSignals(False)
        self._changed()

    def _changed(self, *args):
        if self._loading:
            return
        self._save_settings()
        self.refresh_preview()
        self._apply_timer.start()

    # ================================================================ preview
    def refresh(self):
        self._connect_layout()
        self.refresh_preview()
        self._sync_view()

    def refresh_preview(self, *args):
        exact = self.mode_exact.isChecked()
        self.exact_page.setVisible(exact)
        self.equal_page.setVisible(not exact)
        if exact:
            divided = any(e.text().strip() for e in
                          (self.x_left, self.x_right, self.x_top, self.x_bottom))
        else:
            divided = self.d_cols.value() > 1 or self.d_rows.value() > 1
        self.gap_label.setVisible(divided)
        self.d_gap.setVisible(divided)

        lay = self.layout_()
        page = self.page_index()
        if page < 0:
            self.mini.set_data(297, 210, None)
            self.result_label.setText("This layout has no pages.")
            self.legend.setText("")
            return None
        w, h = qb.page_size_mm(lay, page)
        res = eng.compute(w, h, self.get_settings())
        self.mini.set_data(w, h, res)

        n = qb.page_count(lay)
        where = "page %d of %d" % (page + 1, n) if n > 1 else "this page"
        lines = []
        if res["warning"]:
            lines.append("<span style='color:#e8590c'>⚠ %s</span>" % res["warning"])
        if exact:
            # show every resulting width / height, left→right and top→bottom
            if res.get("across") and len(res["across"]) > 1:
                lines.append("Widths across: <b>%s mm</b>" % "  |  ".join(_f(a) for a in res["across"]))
            if res.get("down") and len(res["down"]) > 1:
                lines.append("Heights down: <b>%s mm</b>" % "  |  ".join(_f(a) for a in res["down"]))
            if not divided:
                lines.append("Type a distance above, e.g. 50.")
        elif res["cell_w"] is not None and res["cell_h"] is not None:
            txt = "Each part: <b>%s × %s mm</b>" % (_f(res["cell_w"]), _f(res["cell_h"]))
            if divided:
                txt += "  (%d × %d)" % (self.d_cols.value(), self.d_rows.value())
            lines.append(txt)
        lines.append("%d guides on %s" % (len(res["guides"]), where))
        self.result_label.setText("<br>".join(lines))

        kinds = {k for _, _, k in res["guides"]}
        parts = [swatch(COLORS["edge"], "Margin")]
        if kinds & {"divide", "gap"}:
            parts.append(swatch(COLORS["divide"], "Divide"))
        if "center" in kinds:
            parts.append(swatch(COLORS["center"], "Center"))
        self.legend.setText("&nbsp;&nbsp;&nbsp;".join(parts))
        return res

    # ================================================================ actions
    def _apply_to(self, pages, text, merge_id=0):
        lay = self.layout_()
        if lay is None or not pages:
            return 0
        settings = self.get_settings()
        added = 0
        with qb.undo_command(lay, text, merge_id):
            for i in pages:
                w, h = qb.page_size_mm(lay, i)
                res = eng.compute(w, h, settings)
                if res["warning"] and not res["guides"]:
                    continue
                qb.clear_page(lay, i)
                added += qb.add_guides(lay, i, res["guides"])
        if not qb.guides_visible(lay):
            self._set_view("mActionShowGuides", True)
        return added

    def _live_apply(self):
        page = self.page_index()
        if page >= 0:
            self._apply_to([page], "Guides (margin & parts)", UNDO_MERGE_ID)

    def apply_all(self):
        lay = self.layout_()
        n_pages = qb.page_count(lay)
        if n_pages == 0:
            return
        self._apply_to(list(range(n_pages)), "Apply guides to all pages")
        self.result_label.setText("✔ Guides applied to all %d page(s).  Ctrl+Z to undo." % n_pages)

    def clear_current(self):
        lay = self.layout_()
        page = self.page_index()
        if page < 0:
            return
        self._apply_timer.stop()
        with qb.undo_command(lay, "Remove guides"):
            qb.clear_page(lay, page)
        self.result_label.setText("Guides removed from this page.  Change any value to draw them again.")

    # ============================================================ view toggles
    def _designer_action(self, name):
        try:
            return self.designer.window().findChild(QAction, name)
        except Exception:
            return None

    def _set_view(self, action_name, on):
        act = self._designer_action(action_name)
        if act is not None and act.isCheckable():
            if act.isChecked() != on:
                act.trigger()
        else:
            lay = self.layout_()
            if lay is None:
                return
            if action_name == "mActionShowGuides":
                qb.set_guides_visible(lay, on)
            else:
                lay.snapper().setSnapToGuides(on)
        self._sync_view()

    def _sync_view(self):
        lay = self.layout_()
        if lay is None:
            return
        try:
            states = ((self.v_show, qb.guides_visible(lay)),
                      (self.v_snap, lay.snapper().snapToGuides()))
        except Exception:
            return
        for cb, on in states:
            cb.blockSignals(True)
            cb.setChecked(bool(on))
            cb.blockSignals(False)
        for name in ("mActionShowGuides", "mActionSnapGuides"):
            act = self._designer_action(name)
            if act is not None and not act.property("lgs_hooked"):
                act.toggled.connect(lambda *_: QTimer.singleShot(0, self._sync_view))
                act.setProperty("lgs_hooked", True)

    # ======================================================== layout signals
    def _connect_layout(self):
        lay = self.layout_()
        if lay is None or lay is self._connected_layout:
            return
        self._disconnect_layout()
        with suppress(Exception):
            lay.pageCollection().changed.connect(self.refresh_preview)
        with suppress(Exception):
            self.designer.view().pageChanged.connect(self.refresh_preview)
        self._connected_layout = lay

    def _disconnect_layout(self):
        lay, self._connected_layout = self._connected_layout, None
        if lay is None:
            return
        with suppress(Exception):
            lay.pageCollection().changed.disconnect(self.refresh_preview)

    def closeEvent(self, event):
        QgsSettings().setValue(SETTINGS_KEY + "visible", False)
        super().closeEvent(event)

    def cleanup(self):
        self._apply_timer.stop()
        self._disconnect_layout()


def _f(v):
    return ("%.1f" % v).rstrip("0").rstrip(".")


class LayoutGuideManager(object):
    """Adds the KGA Layout toolbar (Guides panel, Add Grid) to every Layout Designer."""

    def __init__(self, iface, run_algorithm=None):
        self.iface = iface
        self.run_algorithm = run_algorithm      # callable(alg_id), opens a KGA tool
        self.docks = {}  # id(designer) -> (designer, dock, action, toolbar)
        self.arrow_docks = {}  # id(designer) -> north arrow dock
        self.arrow_catalog = None

    def install(self):
        try:
            na.register_svg_path()
            self.arrow_catalog = na.load_catalog()
        except Exception:
            self.arrow_catalog = None
        self.iface.layoutDesignerOpened.connect(self.designer_opened)
        self.iface.layoutDesignerWillBeClosed.connect(self.designer_closing)
        # designers that were already open when the plugin was loaded
        for designer in self.iface.openLayoutDesigners():
            self.designer_opened(designer)

    def uninstall(self):
        for sig, slot in ((self.iface.layoutDesignerOpened, self.designer_opened),
                          (self.iface.layoutDesignerWillBeClosed, self.designer_closing)):
            try:
                sig.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        for key in list(self.docks):
            self.designer_closing(self.docks[key][0])
        na.unregister_svg_path()

    def designer_opened(self, designer):
        key = id(designer)
        if key in self.docks:
            return
        dock = GuideStudioDock(designer, designer.window())
        designer.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)

        action = dock.toggleViewAction()
        action.setIcon(branding.themed_icon(ICON))
        action.setText("Layout Guides")
        action.setToolTip("Layout Guides - guides from page edges, divided into parts")
        action.triggered.connect(self._remember_visibility)
        toolbar = None
        window = designer.window()
        if isinstance(window, QMainWindow):
            toolbar = window.addToolBar("KGA Layout")
            toolbar.setObjectName("KgaLayoutToolbar")
            toolbar.setToolTip("KGA Layout")
            toolbar.addAction(action)
            if self.run_algorithm is not None:
                grid = toolbar.addAction(
                    branding.icon(branding.ALG_ICONS.get("add_grid")),
                    "Add Grid to Layout")
                grid.setToolTip("Add Grid to Layout - coordinate grid on the selected map")
                grid.triggered.connect(self._add_grid)
        if toolbar is not None:
            refresh = toolbar.addAction(
                QIcon(":/images/themes/default/mActionRefresh.svg"), "Refresh Layout")
            refresh.setToolTip("Refresh Layout - redraw the map frames and the layer panels "
                               "with the latest layer symbology")
            refresh.triggered.connect(lambda *_: self._refresh_layout(designer))
        if self.arrow_catalog and toolbar is not None:
            arrows = na.NorthArrowDock(designer, self.arrow_catalog, window)
            designer.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, arrows)
            arrows.hide()
            arrow_action = arrows.toggleViewAction()
            arrow_action.setIcon(branding.icon("mapping/northArrows.png"))
            arrow_action.setText("Khmer North Arrows")
            arrow_action.setToolTip("Khmer North Arrows - insert a Khmer-style north arrow")
            toolbar.addAction(arrow_action)
            self.arrow_docks[key] = arrows
        with suppress(Exception):
            designer.viewMenu().addAction(action)

        show = QgsSettings().value(SETTINGS_KEY + "visible", False, type=bool)
        dock.setVisible(show)
        if show:
            dock.raise_()
        if isinstance(window, QMainWindow):         # the docks open out of their buttons in the designer
            attach_reveal(dock, action, 'layout_guides', window=window)
            if self.arrow_docks.get(key) is not None:
                attach_reveal(self.arrow_docks[key], arrow_action, 'north_arrows', window=window)
        self.docks[key] = (designer, dock, action, toolbar)

    def designer_closing(self, designer):
        entry = self.docks.pop(id(designer), None)
        if not entry:
            return
        _, dock, action, toolbar = entry
        detach_reveal(dock)
        detach_reveal(self.arrow_docks.get(id(designer)))
        dock.cleanup()
        arrows = self.arrow_docks.pop(id(designer), None)
        if arrows is not None:
            with suppress(Exception):
                designer.removeDockWidget(arrows)
            arrows.deleteLater()
        with suppress(Exception):
            _safe_view_menu(designer).removeAction(action)
        if toolbar is not None:
            with suppress(Exception):
                designer.window().removeToolBar(toolbar)
            toolbar.deleteLater()
        with suppress(Exception):
            designer.removeDockWidget(dock)
        dock.deleteLater()

    @staticmethod
    def _refresh_layout(designer):
        tabs = map_tabs.active()
        if tabs is not None:
            tabs.refresh_layouts()
        layout = designer.layout()
        if layout is not None:
            for item in layout.items():
                if isinstance(item, QgsLayoutItemMap):
                    item.invalidateCache()
                    item.refresh()

    def _add_grid(self, *_):
        self.run_algorithm("kga:add_grid")

    @staticmethod
    def _remember_visibility(checked):
        QgsSettings().setValue(SETTINGS_KEY + "visible", bool(checked))


def _safe_view_menu(designer):
    try:
        return designer.viewMenu()
    except Exception:
        return None

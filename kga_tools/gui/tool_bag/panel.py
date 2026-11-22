"""The Tool Bag: a cute white bag of tools that floats above the QGIS window."""

import html
import json
import os
from contextlib import suppress

from qgis.PyQt.QtCore import (QByteArray, QEvent, QMimeData, QObject, QPoint, QPointF, QRect, QRectF, QSize, Qt,
                              QTimer)
from qgis.PyQt.QtGui import (QBrush, QColor, QDrag, QFont, QFontMetrics, QGuiApplication, QIcon,
                             QLinearGradient, QPainter, QPainterPath, QPen)
from qgis.PyQt.QtWidgets import (QApplication, QFrame, QGraphicsDropShadowEffect, QGridLayout, QHBoxLayout,
                                 QLabel, QMenu, QScrollArea, QSizeGrip, QSizePolicy,
                                 QToolButton, QToolTip, QVBoxLayout, QWidget)

from .compat import OWN_PREFIX, alive, event_global_pos, event_pos, qexec
from .themes import FIXTURE_SCALE, HANDLE_STYLES, StylePicker, scrollbar_stylesheet, stylesheet, theme, tinted_icon

MIME = "application/x-toolbag-tool"
ICON_DIR = os.path.join(os.path.dirname(__file__), "icons")
PANEL_NAME = OWN_PREFIX + "_panel"
VIEW_MODES = {
    "list": "List",
    "grid_icons": "Grid \u2013 icons only",
    "grid_labels": "Grid \u2013 icons with names",
}
MAX_COLUMNS = 3
MAX_BAG_HEIGHT = 520     # tallest the bag itself grows (px); more tools than fit -> it scrolls

def icon(name):
    return QIcon(os.path.join(ICON_DIR, name))


def _supports_translucency():
    # Rounded corners, shadow and the handle need a compositing window system.
    # Classic X11 may not have one, so it gets the square (still styled) bag.
    return QGuiApplication.platformName() in ("windows", "cocoa", "wayland", "offscreen")


def _alpha(color, a):
    c = QColor(color)
    c.setAlphaF(max(0.0, min(1.0, a)))
    return c


class BagFrame(QFrame):
    """Paints the bag itself: fabric body, glossy header, zipper and stitching.

    The fabric can be made see-through (Background transparency) while the
    tools on it stay solid and readable.
    """

    def __init__(self, panel):
        super().__init__(panel)
        self.panel = panel
        self.setObjectName("ctwBag")
        self.grip = None

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.grip is not None:
            self.grip.move(self.width() - self.grip.width() - 3, self.height() - self.grip.height() - 3)
            self.grip.raise_()

    def paintEvent(self, event):
        panel, t = self.panel, self.panel.t
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rad = float(panel.radius)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        a = panel.fabric_alpha()

        # fabric body with a gentle top-to-bottom shade
        g = QLinearGradient(0, r.top(), 0, r.bottom())
        g.setColorAt(0, _alpha(QColor(t["body"]).lighter(102), a))
        g.setColorAt(1, _alpha(QColor(t["body"]).darker(104), a))
        p.setPen(QPen(_alpha(t["border"], max(0.55, a)), 1))
        p.setBrush(QBrush(g))
        p.drawRoundedRect(r, rad, rad)

        # header with a glossy sheen
        hh = float(panel.header.geometry().bottom() + 1)
        full = QPainterPath()
        full.addRoundedRect(r, rad, rad)
        top_rect = QPainterPath()
        top_rect.addRect(QRectF(r.left(), r.top(), r.width(), hh))
        header = full.intersected(top_rect)
        ha = max(0.78, a)
        hg = QLinearGradient(0, r.top(), 0, hh)
        hg.setColorAt(0, _alpha(t["top"], ha))
        hg.setColorAt(1, _alpha(t["bottom"], ha))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(hg))
        p.drawPath(header)
        gloss_rect = QPainterPath()
        gloss_rect.addRect(QRectF(r.left(), r.top(), r.width(), hh * 0.55))
        sheen = QLinearGradient(0, r.top(), 0, r.top() + hh * 0.55)
        sheen.setColorAt(0, QColor(255, 255, 255, 95))
        sheen.setColorAt(1, QColor(255, 255, 255, 0))
        p.setBrush(QBrush(sheen))
        p.drawPath(full.intersected(gloss_rect))

        # zipper: tape, interlocking teeth and a pull with a ring
        zy = hh + 2
        p.setBrush(_alpha(t["zip_band"], max(0.85, a)))
        p.drawRect(QRectF(r.left() + 1, zy, r.width() - 2, 7))
        p.setBrush(QColor(t["zip_teeth"]))
        x, up = r.left() + 7, True
        end = r.right() - 30
        while x < end:
            p.drawRoundedRect(QRectF(x, zy + (0.5 if up else 3.0), 3.2, 3.5), 1, 1)
            x += 3.6
            up = not up
        px = r.right() - 26
        p.setBrush(QColor(t["pull"]))
        p.setPen(QPen(QColor(t["pull_border"]), 1))
        p.drawRoundedRect(QRectF(px, zy - 1, 14, 9), 3.5, 3.5)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QRectF(px + 15, zy + 1, 6, 6))

        # stitched seam around the pocket
        pen = QPen(_alpha(t["handle"], 0.45), 1.1)
        pen.setStyle(Qt.PenStyle.CustomDashLine)
        pen.setDashPattern([3.0, 3.0])
        p.setPen(pen)
        seam = QRectF(r.left() + 4, zy + 11, r.width() - 8, r.bottom() - zy - 15)
        if seam.height() > 10:
            p.drawRoundedRect(seam, max(2.0, rad - 4), max(2.0, rad - 4))
        p.end()


class ToolChip(QToolButton):
    """A tool in the bag: soft card, pastel bubble behind the icon, gentle lift on hover."""

    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self.panel = panel
        self.accent = "#8ecae6"
        self.missing = False
        self.view = "list"          # "list" | "grid_icon" | "grid_label"
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def _metrics(self):
        compact = self.panel.ctrl.compact
        s = self.iconSize().width()
        chip = s + (8 if compact else 12)
        pad = 4 if compact else 6
        return s, chip, pad

    def _label(self):
        return self.text().replace("&&", "\x00").replace("&", "").replace("\x00", "&")

    def _name_font(self):
        f = QFont(self.font())
        if f.pointSizeF() > 8.5:
            f.setPointSizeF(f.pointSizeF() - 1.0)
        f.setBold(self.isChecked())
        return f

    def _wrap(self, text, font, width, max_lines=2):
        """Break a name into at most two centred lines, eliding what does not fit."""
        fm = QFontMetrics(font)
        lines, cur = [], ""
        for word in text.split():
            trial = (cur + " " + word).strip()
            if cur and fm.horizontalAdvance(trial) > width:
                lines.append(cur)
                cur = word
            else:
                cur = trial
        if cur:
            lines.append(cur)
        if len(lines) > max_lines:
            lines = lines[:max_lines - 1] + [" ".join(lines[max_lines - 1:])]
        return [fm.elidedText(line, Qt.TextElideMode.ElideRight, max(10, int(width))) for line in lines]

    def sizeHint(self):
        s, chip, pad = self._metrics()
        h = chip + 2 * pad
        if self.view == "grid_label":
            return QSize(chip + 2 * pad + 28, h + 2 * QFontMetrics(self._name_font()).height() + 4)
        if self.view == "grid_icon":
            return QSize(chip + 2 * pad + 6, h + 4)
        if self.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly:
            return QSize(chip + 2 * pad + 6, h)
        return QSize(chip + 3 * pad + self.fontMetrics().horizontalAdvance(self._label()) + 22, h)

    def minimumSizeHint(self):
        s, chip, pad = self._metrics()
        if self.view == "grid_label":
            return QSize(chip + 2 * pad + 6, self.sizeHint().height())
        if self.view == "grid_icon":
            return QSize(chip + 2 * pad + 6, chip + 2 * pad + 4)
        return QSize(chip + 2 * pad + 6, chip + 2 * pad)

    def event(self, ev):
        if ev.type() in (QEvent.Type.HoverEnter, QEvent.Type.HoverLeave, QEvent.Type.Enter, QEvent.Type.Leave):
            self.update()
        return super().event(ev)

    def paintEvent(self, event):
        t = self.panel.t
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        s, chip, pad = self._metrics()
        enabled, checked, down = self.isEnabled(), self.isChecked(), self.isDown()
        hover = enabled and self.underMouse()
        rad = 8.0 if self.panel.ctrl.compact else 10.0
        r = QRectF(self.rect()).adjusted(1.5, 1.0, -1.5, -2.5)

        # soft shadow, card lifts a little on hover
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(15, 23, 42, 30 if hover else 16))
        p.drawRoundedRect(r.translated(0, 0.6 if down else 1.8), rad, rad)
        if hover and not down:
            r.translate(0, -0.7)

        if self.missing:
            bg, border = QColor("#fff7e6"), QColor("#e0b25a")
        elif checked:
            bg, border = QColor(t["checked"]), QColor(t["checked_border"])
        elif hover:
            bg, border = QColor(t["hover"]), QColor(t["hover_border"])
        else:
            bg, border = QColor(t["btn"]), QColor(t["btn_border"])
        if not enabled and not self.missing:
            bg = QColor(t["disabled_bg"])
        cg = QLinearGradient(0, r.top(), 0, r.bottom())
        cg.setColorAt(0, bg.lighter(103))
        cg.setColorAt(1, bg)
        p.setBrush(QBrush(cg))
        p.setPen(QPen(border, 1.3 if checked else 1.0))
        p.drawRoundedRect(r, rad, rad)

        grid_label = self.view == "grid_label"
        icons_only = (self.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly
                      or self.view == "grid_icon") and not grid_label
        if grid_label:                                   # icon on top, name underneath
            chip_r = QRectF(r.center().x() - chip / 2, r.top() + pad, chip, chip)
        else:
            cx = r.center().x() - chip / 2 if icons_only else r.left() + pad
            chip_r = QRectF(cx, r.center().y() - chip / 2, chip, chip)
        accent = QColor("#f5c36b" if self.missing else self.accent)
        bubble = QLinearGradient(0, chip_r.top(), 0, chip_r.bottom())
        strength = 0.95 if checked else (0.55 if hover else 0.40)
        if not enabled:
            strength = 0.18
        bubble.setColorAt(0, _alpha(accent.lighter(112), strength))
        bubble.setColorAt(1, _alpha(accent, strength))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(bubble))
        p.drawRoundedRect(chip_r, chip * 0.34, chip * 0.34)

        mode = QIcon.Mode.Normal if enabled else QIcon.Mode.Disabled
        icon_rect = QRect(int(round(chip_r.center().x() - s / 2)), int(round(chip_r.center().y() - s / 2)), s, s)
        self.icon().paint(p, icon_rect, Qt.AlignmentFlag.AlignCenter, mode)

        if grid_label:
            f = self._name_font()
            p.setFont(f)
            if self.missing:
                p.setPen(QColor("#8a6116"))
            elif checked:
                p.setPen(QColor(t["checked_ink"]))
            else:
                p.setPen(QColor(t["ink"] if enabled else t["disabled"]))
            lines = self._wrap(self._label(), f, r.width() - 8)
            lh = QFontMetrics(f).height()
            y = chip_r.bottom() + 3
            for line in lines:
                p.drawText(QRectF(r.left() + 4, y, r.width() - 8, lh),
                           int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter), line)
                y += lh
        elif not icons_only:
            tx = chip_r.right() + pad + 2
            tr = QRectF(tx, r.top(), r.right() - tx - (16 if checked else 8), r.height())
            f = QFont(self.font())
            f.setBold(checked)
            p.setFont(f)
            if self.missing:
                ink = QColor("#8a6116")
            elif checked:
                ink = QColor(t["checked_ink"])
            else:
                ink = QColor(t["ink"] if enabled else t["disabled"])
            p.setPen(ink)
            text = QFontMetrics(f).elidedText(self._label(), Qt.TextElideMode.ElideRight, max(10, int(tr.width())))
            p.drawText(tr, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), text)

        if checked:  # small "in use" dot
            d = 6.0
            if icons_only or grid_label:
                dot = QRectF(r.right() - d - 3, r.top() + 3, d, d)
            else:
                dot = QRectF(r.right() - pad - d, r.center().y() - d / 2, d, d)
            p.setPen(QPen(QColor("#ffffff"), 1.2))
            p.setBrush(QColor(t["checked_border"]))
            p.drawEllipse(dot)
        p.end()


class _ButtonFilter(QObject):
    """Drag-to-reorder and friendly tooltips for the tools in the bag."""

    def __init__(self, panel):
        super().__init__(panel)
        self.panel = panel
        self._press = None

    def eventFilter(self, obj, event):
        t = event.type()
        if t == QEvent.Type.ToolTip:
            return self.panel.show_tooltip(obj, event)
        if t == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            self._press = (obj, event_pos(event))
        elif t == QEvent.Type.MouseMove and self._press is not None and self._press[0] is obj:
            if not (event.buttons() & Qt.MouseButton.LeftButton):
                self._press = None
            elif (event_pos(event) - self._press[1]).manhattanLength() >= 2 * QApplication.startDragDistance():
                self._press = None
                self.panel.start_drag(obj)
                return True
        elif t == QEvent.Type.MouseButtonRelease:
            self._press = None
        return False


class _Body(QWidget):
    """Column of tools; accepts drops to reorder."""

    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.setObjectName("ctwBody")
        self.setAcceptDrops(True)
        self.indicator = QFrame(self)
        self.indicator.setObjectName("ctwIndicator")
        self.indicator.hide()

    def drop_index(self, pos):
        """Where a dragged tool would land: (index, rectangle for the insertion marker)."""
        buttons = self.panel.buttons
        cols = self.panel.columns()
        if cols <= 1:                                   # list: marker is a line above a row
            for i, b in enumerate(buttons):
                g = b.geometry()
                if pos.y() < g.center().y():
                    return i, QRect(8, max(0, g.top() - 4), self.width() - 16, 3)
            y = buttons[-1].geometry().bottom() + 3 if buttons else 4
            return len(buttons), QRect(8, max(0, y), self.width() - 16, 3)
        for i, b in enumerate(buttons):                 # grid: marker is a bar left of a tile
            g = b.geometry()
            if pos.y() < g.top() or (pos.y() <= g.bottom() and pos.x() < g.center().x()):
                return i, QRect(max(0, g.left() - 4), g.top(), 3, g.height())
        if buttons:
            g = buttons[-1].geometry()
            return len(buttons), QRect(g.right() + 2, g.top(), 3, g.height())
        return 0, QRect(8, 4, 3, 20)

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(MIME):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if not event.mimeData().hasFormat(MIME):
            event.ignore()
            return
        _, rect = self.drop_index(event_pos(event))
        self.indicator.setGeometry(rect)
        self.indicator.raise_()
        self.indicator.show()
        event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        self.indicator.hide()

    def dropEvent(self, event):
        self.indicator.hide()
        if not event.mimeData().hasFormat(MIME):
            event.ignore()
            return
        try:
            src = json.loads(bytes(event.mimeData().data(MIME)).decode("utf-8"))["index"]
        except Exception:
            event.ignore()
            return
        dst, _ = self.drop_index(event_pos(event))
        self.panel.ctrl.defer(lambda: self.panel.ctrl.move_to(src, dst))
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()

    def contextMenuEvent(self, event):
        child = self.childAt(event.pos())
        while child is not None and child is not self and not isinstance(child, QToolButton):
            child = child.parentWidget()
        index = child.property("ctw_index") if isinstance(child, QToolButton) else None
        self.panel.show_menu(event.globalPos(), index)
        event.accept()


class ToolPanel(QWidget):
    def __init__(self, controller, main_window):
        # Qt.Tool + parent: floats above the QGIS window and hides with it.
        # WindowDoesNotAcceptFocus: clicking the bag never steals keyboard focus
        # from the map, so digitizing keys (Esc, Backspace, Enter) keep working.
        flags = (Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint |
                 Qt.WindowType.WindowDoesNotAcceptFocus)
        super().__init__(main_window, flags)
        self.ctrl = controller
        self.setObjectName(PANEL_NAME)
        self.setWindowTitle("Tool Bag")
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.translucent = _supports_translucency()
        self.radius = 14 if self.translucent else 0
        if self.translucent:
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.t = theme(controller.theme)
        self.margins = self._margins()
        self.buttons = []
        self.header_buttons = []
        self._filter = _ButtonFilter(self)
        self._drag_offset = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(*self.margins)
        self.bag = BagFrame(self)
        self.shadow = None
        if self.translucent:
            self.shadow = QGraphicsDropShadowEffect(self.bag)
            self.shadow.setBlurRadius(24)
            self.shadow.setOffset(0, 5)
            self.shadow.setColor(QColor(36, 50, 74, 70))
            self.bag.setGraphicsEffect(self.shadow)
        outer.addWidget(self.bag)

        root = QVBoxLayout(self.bag)
        root.setContentsMargins(1, 1, 1, 1)
        root.setSpacing(0)

        # header: bag icon + title (drag to move) + bubble buttons
        self.header = QWidget(self.bag)
        self.header.setObjectName("ctwHeader")
        self.header.setCursor(Qt.CursorShape.OpenHandCursor)
        self.header.setToolTip("Drag to move your Tool Bag")
        h = QHBoxLayout(self.header)
        h.setContentsMargins(8, 6, 6, 6)
        h.setSpacing(4)
        self.logo = QLabel(self.header)
        self.logo.setFixedSize(QSize(20, 20))
        self.logo.setStyleSheet("background: transparent; border: none;")
        h.addWidget(self.logo)
        self.title = QLabel("Tool Bag", self.header)
        self.title.setObjectName("ctwTitle")
        h.addWidget(self.title, 1)
        self.pick_btn = self._hdr_btn("add.svg", "Put a tool in the bag: click this, then click any QGIS "
                                                 "button or menu item")
        self.pick_btn.setCheckable(True)
        self.pick_btn.setProperty("ctw_pick_button", True)
        self.pick_btn.clicked.connect(lambda on: self.ctrl.toggle_pick(on))
        h.addWidget(self.pick_btn)
        self.more_btn = self._hdr_btn("settings.svg", "Options")
        self.more_btn.clicked.connect(
            lambda: self.show_menu(self.more_btn.mapToGlobal(QPoint(0, self.more_btn.height())), None))
        h.addWidget(self.more_btn)
        self.min_btn = self._hdr_btn("minimize.svg", "Minimize")
        self.min_btn.clicked.connect(lambda: self.ctrl.minimize_bag())
        h.addWidget(self.min_btn)
        close = self._hdr_btn("close.svg", "Hide the Tool Bag (bring it back with the bag icon "
                                           "on the Plugins toolbar)")
        close.clicked.connect(lambda: self.ctrl.set_panel_visible(False))
        h.addWidget(close)
        for w in (self.header, self.title, self.logo):
            w.installEventFilter(self)
        root.addWidget(self.header)
        self.zipper = QWidget(self.bag)        # space for the painted zipper
        self.zipper.setFixedHeight(12)
        self.zipper.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        root.addWidget(self.zipper)

        # body: scrollable column of tools
        self.scroll = QScrollArea(self.bag)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.viewport().setAutoFillBackground(False)
        self.body = _Body(self)
        self.col = QGridLayout(self.body)
        self._rows = 0
        self.col.setContentsMargins(10, 8, 10, 16)   # bottom: room for the resize grip
        self.col.setSpacing(6)
        self.scroll.setWidget(self.body)
        root.addWidget(self.scroll, 1)

        # the resize grip floats over the bottom-right corner instead of taking a row of its own,
        # so the tool list can use the whole height of the bag
        self.grip = QSizeGrip(self.bag)
        self.grip.setFixedSize(16, 16)
        self.grip.setToolTip("Drag to resize")
        self.grip.raise_()
        self.bag.grip = self.grip
        self.apply_style()

    def _margins(self):
        if not self.translucent:
            return (0, 0, 0, 0)
        top = HANDLE_STYLES.get(self.ctrl.handle_style, HANDLE_STYLES["handle"])[1]   # room for the top fixture + shadow
        return (10, top, 10, 12)

    def apply_style(self):
        """(Re)apply the chosen bag design, size and transparency."""
        self.t = t = theme(self.ctrl.theme)
        self.margins = self._margins()
        self.layout().setContentsMargins(*self.margins)
        self.setStyleSheet(stylesheet(t, self.radius, self.ctrl.compact))
        # the scrolling list keeps its own style rules, so no global theme can override them
        bar = self.scroll.verticalScrollBar()
        bar.setStyleSheet(scrollbar_stylesheet(t))
        bar.setFixedWidth(10)
        self.scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self.scroll.viewport().setStyleSheet("QWidget#qt_scrollarea_viewport { background: transparent; }")
        self.body.setStyleSheet("QWidget#ctwBody { background: transparent; }")
        self.logo.setPixmap(tinted_icon("bag_white.svg", t["title"]).pixmap(QSize(20, 20)))
        for b in self.header_buttons:
            b.setIcon(tinted_icon(b.property("ctw_icon"), t["bubble_icon"]))
        self.col.setSpacing(6 if self.columns() > 1 else (2 if self.ctrl.compact else 5))
        for i, b in enumerate(self.buttons):
            if not b.missing:
                b.accent = t["accents"][i % len(t["accents"])]
            b.updateGeometry()
            b.update()
        if self.shadow is not None:
            a = self.fabric_alpha()
            self.shadow.setColor(QColor(36, 50, 74, int(8 + 72 * a * a * a)))
        self.min_btn.setToolTip("Minimize to a bubble (click the bubble to open the bag again)"
                                if self.ctrl.bubble_visible else
                                "Minimize: hides the bag (bring it back with the Tool Bag toolbar icon)")
        self.setWindowOpacity(max(0.4, min(1.0, self.ctrl.opacity)))
        self.zipper.update()
        self.update()
        self.fit_height()

    def fit_height(self):
        """The bag is exactly as tall as its tools need, up to MAX_BAG_HEIGHT; beyond that the
        tool list scrolls. (Width is still resizable with the corner grip.)"""
        if not hasattr(self, "grip"):
            return
        root = self.bag.layout()
        for w in [self.header, self.body] + list(self.buttons):
            w.ensurePolished()
        cm = root.contentsMargins()
        chrome = cm.top() + cm.bottom() + self.header.sizeHint().height() + self.zipper.height()
        lay = self.col
        width = max(120, self.width() - self.margins[0] - self.margins[2] - 2)
        body_h = lay.heightForWidth(width) if lay.hasHeightForWidth() else lay.sizeHint().height()
        visible = min(MAX_BAG_HEIGHT, chrome + max(body_h, lay.sizeHint().height()))
        total = int(visible + self.margins[1] + self.margins[3])
        if total == self.height() and self.minimumHeight() == total:
            return
        self.setFixedHeight(total)
        if self.isVisible():                      # never let the growing bag run off the screen
            g = self.geometry()
            screen = QGuiApplication.screenAt(g.center()) or QGuiApplication.primaryScreen()
            area = screen.availableGeometry()
            if g.bottom() > area.bottom():
                self.move(g.x(), max(area.top(), area.bottom() - g.height()))

    def showEvent(self, event):
        super().showEvent(event)
        self.fit_height()

    def fabric_alpha(self):
        if not self.translucent:
            return 1.0
        return max(0.08, 1.0 - self.ctrl.bg_transparency)

    def open_style_picker(self, global_pos=None):
        picker = StylePicker(self.ctrl, self)
        picker.adjustSize()
        if global_pos is None:
            global_pos = self.mapToGlobal(QPoint(self.width(), self.margins[1]))
        screen = QGuiApplication.screenAt(global_pos) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        x = min(max(area.left(), global_pos.x()), area.right() - picker.width())
        y = min(max(area.top(), global_pos.y()), area.bottom() - picker.height())
        picker.move(x, y)
        picker.show()
        return picker

    def _hdr_btn(self, icon_name, tip):
        b = QToolButton(self.header)
        b.setObjectName("ctwHdrBtn")
        b.setProperty("ctw_icon", icon_name)
        self.header_buttons.append(b)
        b.setIcon(icon(icon_name))
        b.setIconSize(QSize(15, 15))
        b.setFixedSize(QSize(23, 23))
        b.setToolTip(tip)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        return b

    # ------------------------------------------------------------ painting: the bag handle
    def paintEvent(self, event):
        style = self.ctrl.handle_style
        if not self.translucent or style == "none":
            return super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        top = self.margins[1]
        w = self.width()
        k = FIXTURE_SCALE   # top fixtures are drawn a bit smaller than their design size
        p.translate(w / 2, top)
        p.scale(k, k)
        p.translate(-w / 2, -top)
        if style != "handle":
            getattr(self, "_paint_" + style, lambda *_: None)(p, w, top)
            p.end()
            return
        hw = min(90.0, max(40.0, (w - 40) * 0.42))
        x1, x2 = w / 2 - hw / 2, w / 2 + hw / 2
        path = QPainterPath(QPointF(x1, top + 8))
        path.cubicTo(QPointF(x1, 2), QPointF(x2, 2), QPointF(x2, top + 8))
        base = QColor(self.t["handle"])
        pen = QPen(base.darker(115), 8)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawPath(path)
        pen = QPen(base, 6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawPath(path)
        pen = QPen(QColor(255, 255, 255, 120), 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawPath(path)
        # little loops where the handle is sewn on
        p.setPen(QPen(base.darker(125), 1))
        p.setBrush(base.lighter(110))
        for x in (x1, x2):
            p.drawRoundedRect(QRectF(x - 5, top + 2, 10, 9), 3, 3)
        p.end()

    # other top fixtures (each draws into the room above the bag)
    def _pen(self, color, width, cap=Qt.PenCapStyle.RoundCap):
        pen = QPen(color, width)
        pen.setCapStyle(cap)
        return pen

    def _paint_strap(self, p, w, top):
        base = QColor(self.t["handle"])
        hw = min(80.0, max(36.0, (w - 40) * 0.38))
        x1, x2 = w / 2 - hw / 2, w / 2 + hw / 2
        path = QPainterPath(QPointF(x1, top + 6))
        path.cubicTo(QPointF(x1, 7), QPointF(x2, 7), QPointF(x2, top + 6))
        p.setPen(self._pen(base.darker(115), 10, Qt.PenCapStyle.FlatCap))
        p.drawPath(path)
        p.setPen(self._pen(base, 7.5, Qt.PenCapStyle.FlatCap))
        p.drawPath(path)
        stitch = QPen(QColor(255, 255, 255, 170), 1)
        stitch.setStyle(Qt.PenStyle.DashLine)
        p.setPen(stitch)
        p.drawPath(path)

    def _paint_pin(self, p, w, top):
        base = QColor(self.t["handle"])
        cx = w / 2
        cy = top - 11
        p.setPen(self._pen(QColor(120, 120, 130), 2.2))
        p.drawLine(QPointF(cx, cy + 4), QPointF(cx, top + 6))        # needle
        p.setPen(QPen(base.darker(125), 1))
        p.setBrush(base)
        p.drawEllipse(QPointF(cx, cy), 9, 9)                         # head
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 150))
        p.drawEllipse(QPointF(cx - 3, cy - 3), 3, 3)                 # shine

    def _paint_clip(self, p, w, top):
        base = QColor(self.t["handle"]).darker(135)
        cx = w / 2
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(self._pen(QColor(160, 165, 175), 2))
        for sx in (-1, 1):                                           # folding wire arms
            path = QPainterPath(QPointF(cx + sx * 11, top - 11))
            path.cubicTo(QPointF(cx + sx * 15, 1), QPointF(cx - sx * 15, 1), QPointF(cx - sx * 11, top - 11))
            p.drawPath(path)
        p.setPen(QPen(base.darker(120), 1))
        p.setBrush(base)
        body = QPainterPath()
        body.moveTo(cx - 22, top + 4)
        body.lineTo(cx - 16, top - 12)
        body.lineTo(cx + 16, top - 12)
        body.lineTo(cx + 22, top + 4)
        body.closeSubpath()
        p.drawPath(body)

    def _paint_hook(self, p, w, top):
        base = QColor(self.t["handle"])
        cx = w / 2
        path = QPainterPath(QPointF(cx, top + 6))
        path.lineTo(cx, 15)
        path.cubicTo(QPointF(cx, 6), QPointF(cx + 14, 0), QPointF(cx + 14, 8))
        path.cubicTo(QPointF(cx + 14, 15), QPointF(cx + 6, 14), QPointF(cx + 6, 11))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(self._pen(base.darker(115), 7))
        p.drawPath(path)
        p.setPen(self._pen(base, 4.5))
        p.drawPath(path)

    def _paint_ring(self, p, w, top):
        base = QColor(self.t["handle"])
        cx = w / 2
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(self._pen(base.darker(115), 5))
        p.drawEllipse(QPointF(cx, 11), 9, 9)
        p.setPen(self._pen(base, 3))
        p.drawEllipse(QPointF(cx, 11), 9, 9)
        p.setPen(QPen(base.darker(125), 1))
        p.setBrush(base.lighter(110))
        p.drawRoundedRect(QRectF(cx - 5, top - 2, 10, 10), 3, 3)     # little tab

    def _paint_tape(self, p, w, top):
        base = QColor(self.t["handle"])
        base.setAlpha(205)
        p.save()
        p.translate(w / 2, top + 2)
        p.rotate(-3)
        rect = QRectF(-30, -7, 60, 14)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(base)
        p.drawRect(rect)
        p.setPen(QPen(QColor(255, 255, 255, 110), 2))
        for x in range(-24, 30, 9):                                   # diagonal stripes
            p.drawLine(QPointF(x, 7), QPointF(x + 7, -7))
        p.restore()

    # ------------------------------------------------------------ build
    def columns(self):
        """How many columns of tools (1 to MAX_COLUMNS), in the list as well as in the grid."""
        return max(1, min(MAX_COLUMNS, int(self.ctrl.columns)))

    def preferred_width(self):
        """A comfortable bag width for the current view (list keeps its own rules)."""
        mode = self.ctrl.view_mode
        cols = self.columns()
        s = self.ctrl.icon_size
        if mode == "list":
            if cols == 1:
                return None
            cell = 140 if self.ctrl.labels else s + 30
        else:
            cell = s + 30 if mode == "grid_icons" else max(s + 30, 78)
        inner = cols * cell + (cols - 1) * 6 + 20 + 2 + 10        # tiles + gaps + margins + scroll bar
        return int(inner + self.margins[0] + self.margins[2])

    def rebuild(self, tools, labels, icon_size):
        while self.col.count():
            item = self.col.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()
                w.deleteLater()
        for r in range(self._rows + 2):
            self.col.setRowStretch(r, 0)
        for c in range(MAX_COLUMNS):
            self.col.setColumnStretch(c, 0)
        self.buttons = []
        mode = self.ctrl.view_mode
        cols = self.columns()
        view = {"list": "list", "grid_icons": "grid_icon", "grid_labels": "grid_label"}.get(mode, "list")
        if mode == "list":
            style = (Qt.ToolButtonStyle.ToolButtonTextBesideIcon if labels
                     else Qt.ToolButtonStyle.ToolButtonIconOnly)
        else:
            style = Qt.ToolButtonStyle.ToolButtonIconOnly
        self.col.setSpacing(6 if cols > 1 else (2 if self.ctrl.compact else 5))
        for c in range(cols):
            self.col.setColumnStretch(c, 1)
        reg = self.ctrl.registry
        for index, tool in enumerate(tools):
            b = ToolChip(self, self.body)
            b.view = view
            b.setIconSize(QSize(icon_size, icon_size))
            b.setToolButtonStyle(style)
            b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            action = reg.action_for(tool["id"])
            if action is not None:
                b.setObjectName("ctwTool")
                b.accent = self.t["accents"][index % len(self.t["accents"])]
                b.setDefaultAction(action)          # the original QGIS action
                b.clicked.connect(lambda *_, a=action: self.ctrl.defer(lambda: self.ctrl.after_tool_click(a)))
            else:
                b.setObjectName("ctwMissing")
                b.missing = True
                b.setIcon(icon("missing.svg"))
                b.setText(tool.get("text") or tool["id"])
                b.clicked.connect(lambda *_, t=tool: self.ctrl.report_missing(t))
            b.setProperty("ctw_index", index)
            b.setProperty("ctw_id", tool["id"])
            b.installEventFilter(self._filter)
            self.col.addWidget(b, index // cols, index % cols)
            self.buttons.append(b)
        rows = (len(tools) + cols - 1) // cols
        if not tools:
            pic = QLabel(self.body)
            pic.setObjectName("ctwHint")
            pic.setPixmap(icon("plugin.svg").pixmap(QSize(56, 56)))
            pic.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.col.setRowMinimumHeight(0, 12)
            self.col.addWidget(pic, 1, 0, 1, cols)
            hint = QLabel("<b>Your bag is empty</b><br><br>Tap <b>\uff0b</b> above, then click<br>"
                          "any QGIS tool to pop it in!", self.body)
            hint.setObjectName("ctwHint")
            hint.setWordWrap(True)
            hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.col.addWidget(hint, 2, 0, 1, cols)
            rows = 3
        else:
            self.col.setRowMinimumHeight(0, 0)
        self.col.setRowStretch(rows, 1)                 # pushes the tools to the top
        self._rows = rows
        pw = self.preferred_width()
        show_title = (labels if mode == "list" else True) and (pw is None or pw >= 190)
        self.title.setVisible(show_title)
        self.logo.setVisible(show_title)
        self.fit_height()
        QTimer.singleShot(0, self.fit_height)   # again once the new tools are styled

    def set_picking(self, on):
        self.pick_btn.setChecked(on)

    # ------------------------------------------------------------ move the bag by its header
    def eventFilter(self, obj, event):
        t = event.type()
        if t == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            handle = self.windowHandle()
            if handle is not None and hasattr(handle, "startSystemMove"):
                with suppress(Exception):
                    if handle.startSystemMove():   # X11, Wayland, Windows, macOS
                        return True
            self._drag_offset = event_global_pos(event) - self.frameGeometry().topLeft()
            return True
        if t == QEvent.Type.MouseMove and self._drag_offset is not None:
            self.move(event_global_pos(event) - self._drag_offset)
            return True
        if t == QEvent.Type.MouseButtonRelease and self._drag_offset is not None:
            self._drag_offset = None
            self.ctrl.save_geometry()
            return True
        if t == QEvent.Type.MouseButtonDblClick:
            return True
        return False

    def mousePressEvent(self, event):
        # the handle area above the bag is also a grip
        if (event.button() == Qt.MouseButton.LeftButton and self.ctrl.handle_style != "none"
                and event_pos(event).y() < self.margins[1] + 4):
            self.eventFilter(self, event)
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None:
            self.eventFilter(self, event)
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag_offset is not None:
            self.eventFilter(self, event)
            return
        super().mouseReleaseEvent(event)

    def moveEvent(self, event):
        super().moveEvent(event)
        self.ctrl.geometry_changed()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if event.size().width() != event.oldSize().width():
            self.fit_height()   # wrapped text (empty-bag hint) changes the height
        self.ctrl.geometry_changed()

    # ------------------------------------------------------------ tooltips
    def show_tooltip(self, button, event):
        action = button.defaultAction()
        if action is None or not alive(action):
            return False
        name = action.text().replace("&", "").rstrip(".…")
        info = self.ctrl.registry.info(button.property("ctw_id") or "")
        lines = [f"<b>{html.escape(name)}</b>"]
        desc = (info.description if info else "") or action.statusTip()
        if desc and desc.strip().lower() != name.lower():
            lines.append(html.escape(desc))
        if info and info.where:
            lines.append("<span style='color:#6b7785'>" + html.escape(info.where) + "</span>")
        if not action.isEnabled():
            lines.append("<i style='color:#b45309'>Not available right now – it needs the right "
                         "context (e.g. a layer in edit mode, or a selection).</i>")
        lines.append("<span style='color:#94a3b8'>Right-click to remove or move · drag to reorder</span>")
        QToolTip.showText(event.globalPos(), "<br>".join(lines), button)
        return True

    # ------------------------------------------------------------ drag to reorder
    def start_drag(self, button):
        index = button.property("ctw_index")
        if index is None:
            return
        button.setDown(False)
        mime = QMimeData()
        mime.setData(MIME, QByteArray(json.dumps({"index": int(index)}).encode("utf-8")))
        drag = QDrag(self)
        drag.setMimeData(mime)
        pix = button.grab()
        drag.setPixmap(pix)
        drag.setHotSpot(QPoint(pix.width() // 2, pix.height() // 2))
        qexec(drag, Qt.DropAction.MoveAction)
        self.body.indicator.hide()

    # ------------------------------------------------------------ menus
    def contextMenuEvent(self, event):
        self.show_menu(event.globalPos(), None)
        event.accept()

    def show_menu(self, global_pos, index):
        ctrl = self.ctrl
        menu = QMenu(self)
        menu.setObjectName(OWN_PREFIX + "_panelmenu")
        if index is not None:
            n = len(ctrl.tools)
            t = menu.addAction(ctrl.tool_name(index))
            t.setEnabled(False)
            menu.addAction(icon("remove.svg"), "Take Out of the Bag",
                           lambda: ctrl.defer(lambda: ctrl.remove(index)))
            up = menu.addAction(icon("up.svg"), "Move Up", lambda: ctrl.defer(lambda: ctrl.move_to(index, index - 1)))
            down = menu.addAction(icon("down.svg"), "Move Down",
                                  lambda: ctrl.defer(lambda: ctrl.move_to(index, index + 2)))
            up.setEnabled(index > 0)
            down.setEnabled(index < n - 1)
            menu.addSeparator()
        menu.addAction(icon("pick.svg"), "Pick a Tool from QGIS", lambda: ctrl.defer(lambda: ctrl.toggle_pick(True)))
        menu.addAction(icon("manager.svg"), "Add Tool from List…", ctrl.open_add_dialog)
        menu.addSeparator()
        menu.addAction(icon("palette.svg"), "Bag Style\u2026",
                       lambda: ctrl.defer(lambda: self.open_style_picker(global_pos)))
        if ctrl.group is not None:               # the same entries in all three bags
            from ..bag_common import fill_common_menu
            fill_common_menu(menu, ctrl.group, ctrl.style_key, ctrl.set_style_key, key="tool")
        else:
            ctrl.add_bubble_menu(menu.addMenu("Bubble"))
        views = menu.addMenu("Show Tools As")
        for key, label in VIEW_MODES.items():
            a = views.addAction(label, lambda k=key: ctrl.defer(lambda: ctrl.set_view(mode=k)))
            a.setCheckable(True)
            a.setChecked(ctrl.view_mode == key)
        cols_menu = menu.addMenu("Columns")
        for n in range(1, MAX_COLUMNS + 1):
            a = cols_menu.addAction(f"{n} column" + ("s" if n > 1 else ""),
                                    lambda v=n: ctrl.defer(lambda: ctrl.set_view(columns=v)))
            a.setCheckable(True)
            a.setChecked(ctrl.columns == n)
        labels = menu.addAction("Show Tool Names (list)")
        labels.setCheckable(True)
        labels.setChecked(ctrl.labels)
        labels.setEnabled(ctrl.view_mode == "list")
        labels.toggled.connect(lambda on: ctrl.defer(lambda: ctrl.set_labels(on)))
        sizes = menu.addMenu("Icon Size")
        for s in (16, 20, 24, 32, 40):
            a = sizes.addAction(f"{s} px", lambda v=s: ctrl.defer(lambda: ctrl.set_icon_size(v)))
            a.setCheckable(True)
            a.setChecked(ctrl.icon_size == s)
        menu.addSeparator()
        clear = menu.addAction(icon("clear.svg"), "Empty the Bag…", ctrl.clear_all)
        clear.setEnabled(bool(ctrl.tools))
        menu.addAction(icon("help.svg"), "Help", ctrl.show_help)
        menu.addAction(icon("minimize.svg"), "Minimize to Bubble", lambda: ctrl.defer(ctrl.minimize_bag))
        menu.addAction(icon("close.svg"), "Hide Tool Bag", lambda: ctrl.set_panel_visible(False))
        qexec(menu, global_pos)

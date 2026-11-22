# -*- coding: utf-8 -*-
"""One row per panel or toolbar.

Layout (normal mode)
    Panel:    [snapshot] Title              (switch)
                         Right dock
    Toolbar:  Title                         (switch)
              20 tools · Top
              [icon][icon][icon] … +9

Compact mode collapses every row to a single line with one icon.
A click anywhere on the row toggles the widget. Hovering a panel snapshot shows it large.
"""

from qgis.PyQt.QtCore import QSize, Qt, QUrl, pyqtSignal
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout

from ..core.docs import doc_url
from ..core.registry import PANEL, TOOLBAR, clean_text
from .widgets import IconStrip, InfoButton, ToggleSwitch

THUMB = QSize(78, 48)
LEAD_ICON = QSize(20, 20)
TITLE_ICON = QSize(16, 16)


def _plural(n, word):
    return "{} {}{}".format(n, word, "" if n == 1 else "s")


class ItemCard(QFrame):
    stateChanged = pyqtSignal()

    def __init__(self, item, thumbs, compact=False, parent=None):
        super().__init__(parent)
        self.item = item
        self.thumbs = thumbs
        self.compact = compact
        self._tools = item.tool_actions()
        self._action_names = [clean_text(a.text()) for a in self._tools if a.text()]

        self.setObjectName("uiRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

        row = QHBoxLayout(self)
        row.setContentsMargins(10, 8, 10, 8)
        row.setSpacing(10)

        # Leading visual: snapshot (panels) or icon (compact mode) ---------------
        self.thumb = QLabel()
        self.thumb.setObjectName("uiThumb")
        self.thumb.setFixedSize(THUMB)
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(self.thumb, 0, Qt.AlignmentFlag.AlignTop)

        self.lead_icon = QLabel()
        self.lead_icon.setFixedSize(LEAD_ICON)
        self.lead_icon.setPixmap(item.icon().pixmap(LEAD_ICON))
        row.addWidget(self.lead_icon, 0, Qt.AlignmentFlag.AlignVCenter)

        # Text column -----------------------------------------------------------
        col = QVBoxLayout()
        col.setSpacing(3)
        title_row = QHBoxLayout()
        title_row.setSpacing(6)
        self.title_icon = QLabel()
        own = item.own_icon()
        if item.kind == PANEL and not own.isNull():
            self.title_icon.setPixmap(own.pixmap(TITLE_ICON))
        else:
            self.title_icon.hide()
        title_row.addWidget(self.title_icon)
        title = QLabel(item.title)
        title.setObjectName("uiTitle")
        title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        title_row.addWidget(title, 1)
        col.addLayout(title_row)

        self.meta = QLabel()
        self.meta.setObjectName("uiMeta")
        small = self.meta.font()
        small.setPointSizeF(max(7.0, small.pointSizeF() * 0.9))
        self.meta.setFont(small)
        col.addWidget(self.meta)

        self.strip = None
        self.snap_strip = None
        if item.kind == TOOLBAR:
            if self._tools:
                self.strip = IconStrip(self._tools)
                col.addWidget(self.strip)
            else:
                self.snap_strip = QLabel()
                self.snap_strip.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
                self.snap_strip.setObjectName("uiMeta")
                self.snap_strip.setFont(small)
                col.addWidget(self.snap_strip)

        self.hint = QLabel()
        self.hint.setObjectName("uiHint")
        self.hint.setFont(small)
        self.hint.setWordWrap(True)
        self.hint.hide()
        col.addWidget(self.hint)
        row.addLayout(col, 1)

        # Right column: info button in the top-right corner, switch below ----------
        right = QVBoxLayout()
        right.setSpacing(4)
        self.doc_url = doc_url(item)
        self.info = InfoButton()
        self.info.setObjectName("uiInfo")
        self.info.setToolTip("Open the QGIS documentation for {}".format(item.title))
        self.info.clicked.connect(self._open_docs)
        retain = self.info.sizePolicy()
        retain.setRetainSizeWhenHidden(True)  # keep every switch on the same line
        self.info.setSizePolicy(retain)
        if self.doc_url is None:
            self.info.hide()  # never show() here: it has no parent yet and would flash as a window
        right.addWidget(self.info, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)

        self.switch = ToggleSwitch()
        self.switch.setToolTip("Show / hide")
        self.switch.clicked.connect(self._set)
        right.addWidget(self.switch, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        right.addStretch(1)
        row.addLayout(right)

        self.search_blob = " ".join([item.title, item.widget.objectName()] + self._action_names).lower()
        self._update_meta()
        self.update_thumbnail()
        self.set_compact(compact)

        item.widget.toggleViewAction().toggled.connect(self.sync)
        self.sync()

    # Visuals -------------------------------------------------------------------
    def _update_meta(self):
        parts = []
        if self.item.kind == TOOLBAR and self._tools:
            parts.append(_plural(len(self._tools), "tool"))
        if self.item.location:
            loc = self.item.location
            parts.append(loc if loc == "Floating" else "{} {}".format(loc, "dock" if self.item.kind == PANEL else "area"))
        self.meta.setText("  ·  ".join(parts))

    def update_thumbnail(self):
        pix = self.thumbs.get(self.item.key)
        dpr = self.devicePixelRatioF() if hasattr(self, "devicePixelRatioF") else 1.0
        if self.item.kind == PANEL:
            if pix is not None:
                target = QSize(int((THUMB.width() - 2) * dpr), int((THUMB.height() - 2) * dpr))
                scaled = pix.scaled(target, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                    Qt.TransformationMode.SmoothTransformation)
                scaled = scaled.copy(0, 0, target.width(), target.height())
                scaled.setDevicePixelRatio(dpr)
                self.thumb.setPixmap(scaled)
                path = self.thumbs.path(self.item.key).replace("\\", "/")
                self.thumb.setToolTip('<b>{}</b><br><img src="file:///{}" width="300">'.format(
                    self.item.title, path.lstrip("/")))
            else:
                self.thumb.setPixmap(self.item.icon().pixmap(QSize(22, 22)))
                self.thumb.setToolTip("No preview yet. Open this panel once and a snapshot is saved.")
        elif self.snap_strip is not None:
            if pix is not None:
                h = IconStrip.ICON + 4
                scaled = pix.scaledToHeight(int(h * dpr), Qt.TransformationMode.SmoothTransformation)
                scaled.setDevicePixelRatio(dpr)
                self.snap_strip.setPixmap(scaled)
                self.snap_strip.setFixedHeight(h)
            else:
                self.snap_strip.setText("Widget toolbar · open it once to capture a preview")
                self.snap_strip.setFixedHeight(IconStrip.ICON + 2)

    def set_compact(self, compact):
        self.compact = compact
        is_panel = self.item.kind == PANEL
        self.thumb.setVisible(is_panel and not compact)
        self.lead_icon.setVisible(compact)
        self.meta.setVisible(not compact)
        for w in (self.strip, self.snap_strip):
            if w is not None:
                w.setVisible(not compact)
        self.layout().setContentsMargins(10, 5 if compact else 8, 10, 5 if compact else 8)
        self.layout().activate()
        self.updateGeometry()

    def set_search_hint(self, text):
        """Explain a match that came from a tool name rather than the toolbar title."""
        if self.item.kind == TOOLBAR and text and text not in self.item.title.lower():
            hits = [n for n in self._action_names if text in n.lower()]
            if hits:
                self.hint.setText("Contains: " + ", ".join(hits[:4]) + (" …" if len(hits) > 4 else ""))
                self.hint.show()
                return
        self.hint.hide()

    # State ---------------------------------------------------------------------
    def sync(self, *_):
        try:
            on = self.item.is_visible()
        except RuntimeError:  # the widget was deleted (plugin unloaded)
            return
        self.switch.setChecked(on)
        if self.property("active") != on:
            self.setProperty("active", on)
            self.style().unpolish(self)
            self.style().polish(self)
        self.stateChanged.emit()

    def _open_docs(self):
        if self.doc_url:
            QDesktopServices.openUrl(QUrl(self.doc_url))

    def toggle(self):
        try:
            self._set(not self.item.is_visible())
        except RuntimeError:
            pass

    def _set(self, visible):
        try:
            self.item.set_visible(bool(visible))
        except RuntimeError:
            return
        self.sync()

    def mouseReleaseEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
                event.position().toPoint() if hasattr(event, "position") else event.pos()):
            self.toggle()
        super().mouseReleaseEvent(event)

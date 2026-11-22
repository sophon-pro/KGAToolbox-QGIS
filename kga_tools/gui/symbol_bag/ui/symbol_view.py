# -*- coding: utf-8 -*-
"""Symbol Bag – card grid: preview cache, model, delegate and view."""

import json
import math
import os
from collections import OrderedDict

from qgis.PyQt.QtCore import (QAbstractListModel, QElapsedTimer, QMimeData, QModelIndex, QPoint, QPointF, QRect,
                              QRectF, QSize, Qt, QTimer, pyqtSignal)
from qgis.PyQt.QtGui import QColor, QDrag, QFont, QFontMetrics, QPainter, QPainterPath, QPen, QPixmap
from qgis.PyQt.QtWidgets import QAbstractItemView, QListView, QStyle, QStyledItemDelegate

from ..core.compat import event_pos
from ..core.log import log_warning
from ..core.storage import KIND_RASTER
from ..core.symbols import (broken_preview, image_to_png, missing_resources, png_to_pixmap,
                            raster_preview_image, symbol_from_xml, vector_preview)
from .motion import ease_out, pulse, spring

MIME_SYMBOLS = "application/x-symbolbag-symbols"
DROP_EXTENSIONS = (".qml", ".xml", ".sld", ".svg", ".db", ".sbag")
RASTER_PREVIEW_SIZE = 160

ROLE_RECORD = Qt.ItemDataRole.UserRole + 1
ROLE_UID = Qt.ItemDataRole.UserRole + 2
ROLE_MISSING = Qt.ItemDataRole.UserRole + 3


def encode_uids(uids):
    md = QMimeData()
    md.setData(MIME_SYMBOLS, json.dumps(list(uids)).encode("utf-8"))
    md.setText("\n".join(uids))
    return md


def decode_uids(mime):
    if mime is None or not mime.hasFormat(MIME_SYMBOLS):
        return []
    try:
        return json.loads(bytes(mime.data(MIME_SYMBOLS)).decode("utf-8"))
    except ValueError:
        return []


# ----------------------------------------------------------------------
class PreviewCache(object):
    """LRU cache of preview pixmaps.

    Raster previews are expensive (a map render), so they are also persisted
    as PNG in the library database and only regenerated when a style changes.
    """

    def __init__(self, store, limit=600):
        self.store = store
        self.limit = limit
        self._cache = OrderedDict()
        self._missing = {}

    def clear(self):
        self._cache.clear()
        self._missing.clear()

    def invalidate(self, uid):
        for key in [k for k in self._cache if k[0] == uid]:
            del self._cache[key]
        self._missing.pop(uid, None)

    def missing(self, uid):
        return self._missing.get(uid, [])

    def get(self, rec, size):
        key = (rec.uid, rec.modified, size)
        pm = self._cache.get(key)
        if pm is not None:
            self._cache.move_to_end(key)
            return pm
        try:
            pm = self._render(rec, size)
        except Exception as e:
            log_warning("Preview failed for '{}': {}".format(rec.name, e))
            pm = broken_preview(size)
        self._cache[key] = pm
        while len(self._cache) > self.limit:
            self._cache.popitem(last=False)
        return pm

    def _render(self, rec, size):
        xml = self.store.symbol_xml(rec.uid)
        if rec.kind == KIND_RASTER:
            data = self.store.preview(rec.uid)
            if data is None:
                img = raster_preview_image(xml, RASTER_PREVIEW_SIZE)
                data = image_to_png(img)
                self.store.set_preview(rec.uid, data)
            pm = png_to_pixmap(data)
            return pm.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
        sym = symbol_from_xml(xml)
        if sym is None:
            return broken_preview(size)
        self._missing[rec.uid] = missing_resources(sym)
        return vector_preview(sym, size)


# ----------------------------------------------------------------------
class SymbolModel(QAbstractListModel):
    def __init__(self, cache, parent=None):
        super().__init__(parent)
        self.cache = cache
        self.records = []
        self.preview_size = 64
        self.collection_names = {}

    def set_records(self, records):
        self.beginResetModel()
        self.records = list(records)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.records)

    def record(self, index):
        if not index.isValid() or index.row() >= len(self.records):
            return None
        return self.records[index.row()]

    def index_of(self, uid):
        for i, r in enumerate(self.records):
            if r.uid == uid:
                return self.index(i, 0)
        return QModelIndex()

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        rec = self.record(index)
        if rec is None:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return rec.name
        if role == Qt.ItemDataRole.DecorationRole:
            return self.cache.get(rec, self.preview_size)
        if role == ROLE_RECORD:
            return rec
        if role == ROLE_UID:
            return rec.uid
        if role == ROLE_MISSING:
            return self.cache.missing(rec.uid)
        if role == Qt.ItemDataRole.ToolTipRole:
            lines = ["<b>{}</b>".format(rec.name.replace("<", "&lt;")),
                     "{}{}".format(rec.kind_label, " · " + rec.subtype if rec.subtype and rec.is_raster else "")]
            coll = self.collection_names.get(rec.collection_id)
            if coll:
                lines.append("Collection: {}".format(coll))
            if rec.tags:
                lines.append("Tags: {}".format(rec.tags))
            if rec.description:
                lines.append("<i>{}</i>".format(rec.description.replace("<", "&lt;")))
            miss = self.cache.missing(rec.uid)
            if miss:
                lines.append("<span style='color:#c0392b'>Missing file: {}</span>".format(
                    ", ".join(os.path.basename(m) for m in miss)))
            lines.append("<span style='color:gray'>Double-click to apply · drag onto a layer</span>")
            return "<br>".join(lines)
        return None

    def flags(self, index):
        f = super().flags(index)
        if index.isValid():
            f |= Qt.ItemFlag.ItemIsDragEnabled
        return f

    def mimeTypes(self):
        return [MIME_SYMBOLS]

    def mimeData(self, indexes):
        uids = []
        for i in indexes:
            rec = self.record(i)
            if rec is not None and rec.uid not in uids:
                uids.append(rec.uid)
        return encode_uids(uids)

    def supportedDragActions(self):
        return Qt.DropAction.CopyAction | Qt.DropAction.MoveAction


# ----------------------------------------------------------------------
class CardMotion(object):
    """Per-card motion state, advanced by the view's ticker.

    - cascade: cards rise, scale and fade in one after another
    - hover:   the card under the mouse lifts (eased in and out)
    - pulse:   press-and-release bump + ring when a symbol is applied
    - star:    the favourite star pops
    """

    CASCADE_DUR = 300.0   # ms per card
    STAGGER = 22.0        # ms between consecutive cards
    MAX_STAGGER = 12      # cards after this all start together
    PULSE_DUR = 480.0
    STAR_DUR = 420.0

    def __init__(self):
        self.clock = QElapsedTimer()
        self.clock.start()
        self.enabled = True
        self.speed = 1.0
        self.hover_target = -1
        self.hover = {}            # row -> 0..1
        self.pulses = {}           # uid -> start ms
        self.stars = {}            # uid -> start ms
        self.cascade_start = None  # ms, or None
        self.cascade_hold = False  # True: every card hidden until start_cascade()
        self.cascade_base = 0      # first visible row when the cascade started

    def now(self):
        return float(self.clock.elapsed())

    def _dur(self, ms):
        return ms * max(0.3, self.speed)

    # --- state for painting -------------------------------------------
    def entrance(self, row):
        """(opacity, dy, scale) of a card during the cascade."""
        if not self.enabled:
            return 1.0, 0.0, 1.0
        if self.cascade_hold:
            return 0.0, 14.0, 0.88
        if self.cascade_start is None:
            return 1.0, 0.0, 1.0
        order = min(max(0, row - self.cascade_base), self.MAX_STAGGER)
        t = (self.now() - self.cascade_start - order * self._dur(self.STAGGER)) / self._dur(self.CASCADE_DUR)
        if t <= 0:
            return 0.0, 14.0, 0.88
        if t >= 1:
            return 1.0, 0.0, 1.0
        k = spring(t, 0.62)
        return ease_out(t * 2.2, 2), 14.0 * (1.0 - k), 0.88 + 0.12 * k

    def hover_of(self, row, hovered):
        if not self.enabled:
            return 1.0 if hovered else 0.0
        return self.hover.get(row, 0.0)

    def pulse_of(self, uid):
        st = self.pulses.get(uid)
        if st is None or not self.enabled:
            return None
        t = (self.now() - st) / self._dur(self.PULSE_DUR)
        return t if t < 1.0 else None

    def star_of(self, uid):
        st = self.stars.get(uid)
        if st is None or not self.enabled:
            return None
        t = (self.now() - st) / self._dur(self.STAR_DUR)
        return t if t < 1.0 else None

    # --- triggers -------------------------------------------------------
    def hold(self):
        if self.enabled:
            self.cascade_hold = True
            self.cascade_start = None

    def start_cascade(self, base_row=0):
        self.cascade_hold = False
        if self.enabled:
            self.cascade_base = base_row
            self.cascade_start = self.now()

    # --- ticker ---------------------------------------------------------
    def tick(self):
        """Advance; returns (still_active, cascade_active, rows_to_repaint, uids_to_repaint)."""
        rows, uids = set(), set()
        for row in set(self.hover) | ({self.hover_target} if self.hover_target >= 0 else set()):
            cur = self.hover.get(row, 0.0)
            target = 1.0 if row == self.hover_target else 0.0
            nxt = cur + (target - cur) * 0.32
            if abs(nxt - target) < 0.01:
                nxt = target
            if nxt == 0.0:
                self.hover.pop(row, None)
            else:
                self.hover[row] = nxt
            if nxt != cur:
                rows.add(row)
        now = self.now()
        for table, dur in ((self.pulses, self.PULSE_DUR), (self.stars, self.STAR_DUR)):
            for uid, st in list(table.items()):
                uids.add(uid)
                if now - st > self._dur(dur):
                    del table[uid]
        cascade = False
        if self.cascade_start is not None:
            end = self._dur(self.CASCADE_DUR + self.STAGGER * self.MAX_STAGGER)
            if now - self.cascade_start > end:
                self.cascade_start = None
            cascade = True
        active = bool(rows or uids or cascade or
                      any(v not in (0.0, 1.0) for v in self.hover.values()))
        return active, cascade, rows, uids


# ----------------------------------------------------------------------
class CardDelegate(QStyledItemDelegate):
    """Paints symbols as rounded cards (grid) or rows (list)."""

    def __init__(self, view):
        super().__init__(view)
        self.view = view
        self.theme = None
        self.list_mode = False
        self.show_names = True
        self.preview_size = 64
        self.motion = CardMotion()
        self.preview_override = {}   # uid -> QPixmap (live colour-picker preview)

    # geometry ----------------------------------------------------------
    TILE_PAD = 7

    cell = QSize()     # set by the view: every card fills exactly one cell

    def card_size(self):
        """Minimum card size for the current mode."""
        tile = self.preview_size + 2 * self.TILE_PAD
        if self.list_mode:
            return QSize(200, tile + 12)
        if self.show_names:
            return QSize(max(tile + 24, 92), tile + 44)
        return QSize(tile + 16, tile + 16)

    def sizeHint(self, option, index):
        return self.cell if self.cell.isValid() else self.card_size()

    def star_rect(self, rect):
        if self.list_mode:
            return QRect(rect.right() - 54, rect.center().y() - 10, 20, 20)
        return QRect(rect.right() - 25, rect.top() + 8, 18, 18)

    def color_rect(self, rect):
        """Palette (recolour) button: bottom-left corner of the preview tile."""
        if self.list_mode:
            return QRect(rect.right() - 78, rect.center().y() - 10, 20, 20)
        tile = self.preview_size + 2 * self.TILE_PAD
        if self.show_names:
            top = rect.top() + 4 + 8
        else:
            top = rect.top() + (rect.height() - tile) // 2
        return QRect(rect.left() + 8, top + tile - 18, 18, 18)

    def more_rect(self, rect):
        if self.list_mode:
            return QRect(rect.right() - 30, rect.center().y() - 10, 20, 20)
        return QRect(rect.left() + 8, rect.top() + 8, 18, 18)

    # painting ----------------------------------------------------------
    def paint(self, painter, option, index):
        t = self.theme
        rec = index.data(ROLE_RECORD)
        if t is None or rec is None:
            return super().paint(painter, option, index)
        m = self.motion
        opacity, dy, scale = m.entrance(index.row())
        if opacity <= 0.0:
            return
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        hp = m.hover_of(index.row(), hover)          # 0..1 hover lift
        pt = m.pulse_of(rec.uid)                     # apply feedback
        if pt is not None:
            scale *= pulse(pt)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setOpacity(opacity)
        r = QRectF(option.rect).adjusted(4, 4, -4, -4)
        lift = 2.0 * hp
        if dy or scale != 1.0 or lift:
            c = r.center()
            painter.translate(c.x(), c.y() + dy - lift)
            painter.scale(scale, scale)
            painter.translate(-c.x(), -c.y())
        radius = 12 if not t.compact else 8

        # soft shadow that grows as the card lifts (cards float over the map)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, int(22 + 26 * hp)))
        painter.drawRoundedRect(r.translated(0, 1.5 + 2.5 * hp), radius, radius)

        card = t.color("card")
        if selected:
            card = _blend(card, t.accent, 0.16)
        elif hp:
            card = _blend(card, t.accent, 0.06 * hp)
        painter.setBrush(card)
        if selected:
            border = QColor(t.accent)
        else:
            rest = QColor(0, 0, 0, 22) if not t.dark else QColor(255, 255, 255, 26)
            border = _blend_a(rest, t.accent_soft(120), hp)
        painter.setPen(QPen(border, 1.6 if selected else 1))
        painter.drawRoundedRect(r, radius, radius)

        ps = self.preview_size
        pad = self.TILE_PAD
        if self.list_mode:
            tile = QRectF(r.left() + 6, r.center().y() - ps / 2.0 - pad, ps + 2 * pad, ps + 2 * pad)
        elif self.show_names:
            tile = QRectF(r.center().x() - ps / 2.0 - pad, r.top() + 8, ps + 2 * pad, ps + 2 * pad)
        else:
            tile = QRectF(r.center().x() - ps / 2.0 - pad, r.center().y() - ps / 2.0 - pad, ps + 2 * pad, ps + 2 * pad)
        tile_path = QPainterPath()
        tile_path.addRoundedRect(tile, 10, 10)
        painter.setPen(Qt.PenStyle.NoPen)
        tile_bg = t.tile(rec.kind)
        if t.dark and not rec.is_raster:
            tile_bg = QColor("#f4f6f9")   # symbols are designed for light maps
        painter.setBrush(tile_bg)
        painter.drawPath(tile_path)

        pm = self.preview_override.get(rec.uid) or index.data(Qt.ItemDataRole.DecorationRole)
        if isinstance(pm, QPixmap) and not pm.isNull():
            painter.save()
            painter.setClipPath(tile_path)
            if rec.is_raster:
                # raster previews fill the whole tile
                painter.drawPixmap(tile.toRect(), pm)
            else:
                x = tile.center().x() - pm.width() / pm.devicePixelRatio() / 2.0
                y = tile.center().y() - pm.height() / pm.devicePixelRatio() / 2.0
                painter.drawPixmap(int(x), int(y), pm)
            painter.restore()

        # text
        name_color = QColor(t.accent).darker(115) if selected and not t.dark else (
            QColor(t.accent) if selected else t.color("text"))
        if self.list_mode:
            font = QFont(option.font)
            font.setBold(selected)
            painter.setFont(font)
            fm = QFontMetrics(font)
            tx = int(tile.right()) + 10
            tw = int(r.right()) - tx - 52
            painter.setPen(name_color)
            painter.drawText(QRect(tx, int(r.top()) + 3, tw, int(r.height() / 2) - 1),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
                             fm.elidedText(rec.name, Qt.TextElideMode.ElideRight, tw))
            small = QFont(option.font)
            small.setPointSizeF(max(7.0, small.pointSizeF() - 1))
            painter.setFont(small)
            painter.setPen(t.color("sub"))
            sub = rec.kind_label + ("  ·  " + rec.tags if rec.tags else "")
            painter.drawText(QRect(tx, int(r.center().y()) + 2, tw, int(r.height() / 2) - 4),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                             QFontMetrics(small).elidedText(sub, Qt.TextElideMode.ElideRight, tw))
        elif self.show_names:
            small = QFont(option.font)
            small.setPointSizeF(max(7.0, small.pointSizeF() - 0.5))
            small.setBold(selected)
            painter.setFont(small)
            painter.setPen(name_color)
            fm = QFontMetrics(small)
            text_rect = QRect(int(r.left()) + 4, int(tile.bottom()) + 4, int(r.width()) - 8, 22)
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                             fm.elidedText(rec.name, Qt.TextElideMode.ElideRight, text_rect.width()))

        # missing resource warning
        if index.data(ROLE_MISSING):
            wr = QRect(int(tile.right()) - 15, int(tile.bottom()) - 15, 14, 14)
            t.icon("warning", "#d35400", 14).paint(painter, wr)

        # hover actions / favourite (fade in with the hover lift)
        st = m.star_of(rec.uid)
        if rec.favorite or hp > 0.02 or st is not None:
            color = "#f2b01e" if rec.favorite else t.p["sub"]
            sr = QRectF(self.star_rect(option.rect))
            painter.save()
            if not rec.favorite and st is None:
                painter.setOpacity(opacity * hp)
            if st is not None:                       # pop: 0.4 → 1.2 → 1
                k = 0.4 + 0.6 * spring(st, 0.4)
                c = sr.center()
                painter.translate(c)
                painter.scale(k, k)
                painter.translate(-c)
                if rec.favorite:                     # burst of dots
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(QColor(242, 176, 30, int(200 * (1 - st))))
                    rad = 6 + 10 * ease_out(st)
                    for i in range(6):
                        a = math.radians(i * 60 - 90)
                        painter.drawEllipse(QPointF(c.x() + rad * math.cos(a), c.y() + rad * math.sin(a)),
                                            1.6 * (1 - st) + 0.4, 1.6 * (1 - st) + 0.4)
            t.icon("star_on" if rec.favorite else "star", color, 16).paint(painter, sr.toRect())
            painter.restore()
        if hp > 0.02:
            mr = self.more_rect(option.rect)
            painter.save()
            painter.setOpacity(opacity * hp)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(t.color("card"))
            painter.drawEllipse(QRectF(mr).adjusted(-1, -1, 1, 1))
            t.icon("menu", t.p["sub"], 16).paint(painter, mr)
            painter.restore()
        show_color = max(hp, 1.0 if selected else 0.0)
        if show_color > 0.02 or rec.uid in self.preview_override:
            cr = QRectF(self.color_rect(option.rect))
            painter.save()
            painter.setOpacity(opacity * (1.0 if rec.uid in self.preview_override else show_color))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(t.accent if rec.uid in self.preview_override else t.color("card"))
            painter.drawEllipse(cr.adjusted(-2, -2, 2, 2))
            t.icon("palette", "#ffffff" if rec.uid in self.preview_override else t.p["sub"], 16).paint(
                painter, cr.toRect())
            painter.restore()
        if selected:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(t.accent)
            painter.drawEllipse(QPointF(r.right() - 7, r.bottom() - 7), 3, 3)
        if pt is not None:                           # "applied" ring expanding outward
            ring = QColor(t.accent)
            ring.setAlphaF(0.75 * (1.0 - ease_out(pt, 2)))
            grow = 5.0 * ease_out(pt)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(ring, 2.2))
            painter.drawRoundedRect(r.adjusted(-grow, -grow, grow, grow), radius + grow, radius + grow)
        painter.restore()


def _blend(a, b, f):
    return QColor(int(a.red() * (1 - f) + b.red() * f), int(a.green() * (1 - f) + b.green() * f),
                  int(a.blue() * (1 - f) + b.blue() * f))


def _blend_a(a, b, f):
    """Blend including alpha."""
    return QColor(int(a.red() * (1 - f) + b.red() * f), int(a.green() * (1 - f) + b.green() * f),
                  int(a.blue() * (1 - f) + b.blue() * f), int(a.alpha() * (1 - f) + b.alpha() * f))


# ----------------------------------------------------------------------
class SymbolView(QListView):
    """Card grid with drag & drop:
    - drag cards onto the Layers panel to apply,
    - drag cards onto a collection in the sidebar to move,
    - drag cards between cards to reorder (manual sort),
    - drop style files from the file manager to import.
    """

    reorderRequested = pyqtSignal(list, str)      # uids, target uid ('' = end)
    filesDropped = pyqtSignal(list)
    favoriteToggled = pyqtSignal(str)
    recolorRequested = pyqtSignal(str, QPoint)
    menuRequested = pyqtSignal(str, QPoint)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("SBView")
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setUniformItemSizes(False)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setMouseTracking(True)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setWordWrap(False)
        self.setSpacing(0)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.reorder_enabled = True
        self.columns = 0
        self.card_delegate = CardDelegate(self)
        self.setItemDelegate(self.card_delegate)
        self.motion = self.card_delegate.motion
        self._drop_row = None
        # one ticker drives every card animation and stops itself when idle
        self._ticker = QTimer(self)
        self._ticker.setInterval(16)
        self._ticker.timeout.connect(self._tick)

    # layout --------------------------------------------------------------
    def apply_layout(self, list_mode, preview_size, show_names, columns):
        d = self.card_delegate
        d.list_mode = list_mode
        d.preview_size = preview_size
        d.show_names = show_names
        self.columns = columns
        if self.model() is not None:
            self.model().preview_size = preview_size
        self.setViewMode(QListView.ViewMode.ListMode if list_mode else QListView.ViewMode.IconMode)
        self.setMovement(QListView.Movement.Static)
        self.setFlow(QListView.Flow.TopToBottom if list_mode else QListView.Flow.LeftToRight)
        self.setWrapping(not list_mode)
        self.setSpacing(0)
        d.cell = QSize()
        self._update_grid(force=True)

    def available_width(self):
        """Width the cards may use: the view minus frame and a reserved
        scrollbar lane, so the layout never jumps or slides under the scrollbar."""
        # Qt's own icon-mode layout reserves the *style's* scrollbar extent,
        # which can be wider than our slim styled scrollbar – use the larger.
        sb = max(10, self.verticalScrollBar().sizeHint().width(),
                 self.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent))
        return max(80, self.width() - 2 * self.frameWidth() - sb - 4)

    def column_count(self):
        d = self.card_delegate
        if d.list_mode:
            return 1
        avail = self.available_width()
        if self.columns and self.columns > 0:
            return self.columns
        return max(1, avail // d.card_size().width())

    def _update_grid(self, force=False):
        d = self.card_delegate
        base = d.card_size()
        avail = self.available_width()
        if d.list_mode:
            cell = QSize(avail, base.height())
        else:
            n = self.column_count()
            cell = QSize(max(base.width() if not self.columns else 40, avail // n), base.height())
        if force or cell != d.cell:
            d.cell = cell
            self.setGridSize(QSize() if d.list_mode else cell)
            self.doItemsLayout()
            self.viewport().update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_grid()

    # motion ----------------------------------------------------------------
    def set_motion(self, enabled, speed=1.0):
        self.motion.enabled = enabled
        self.motion.speed = speed
        if not enabled:
            self.motion.hover.clear()
            self.motion.pulses.clear()
            self.motion.stars.clear()
            self.motion.cascade_start = None
            self.motion.cascade_hold = False
            self._ticker.stop()
            self.viewport().update()

    def _kick(self):
        if self.motion.enabled and not self._ticker.isActive():
            self._ticker.start()

    def first_visible_row(self):
        idx = self.indexAt(QPoint(8, 8))
        return idx.row() if idx.isValid() else 0

    def hold_cards(self):
        """Hide every card until start_cascade() (used while the bag flies in)."""
        self.motion.hold()
        self.viewport().update()

    def start_cascade(self):
        self.motion.start_cascade(self.first_visible_row())
        self.viewport().update()
        self._kick()

    def pulse(self, uid):
        if self.motion.enabled:
            self.motion.pulses[uid] = self.motion.now()
            self._kick()

    def star_pop(self, uid):
        if self.motion.enabled:
            self.motion.stars[uid] = self.motion.now()
            self._kick()

    def _row_rect(self, row):
        return self.visualRect(self.model().index(row, 0)).adjusted(-8, -8, 8, 8)

    def _tick(self):
        active, cascade, rows, uids = self.motion.tick()
        if cascade:
            self.viewport().update()
        else:
            model = self.model()
            for row in rows:
                if model is not None and 0 <= row < model.rowCount():
                    self.viewport().update(self._row_rect(row))
            for uid in uids:
                idx = model.index_of(uid) if model is not None else None
                if idx is not None and idx.isValid():
                    self.viewport().update(self._row_rect(idx.row()))
        if not active:
            self._ticker.stop()

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        idx = self.indexAt(event_pos(event))
        row = idx.row() if idx.isValid() else -1
        if row != self.motion.hover_target:
            self.motion.hover_target = row
            self._kick()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        if self.motion.hover_target != -1:
            self.motion.hover_target = -1
            self._kick()

    # hover actions ---------------------------------------------------------
    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            idx = self.indexAt(event_pos(event))
            if idx.isValid():
                rect = self.visualRect(idx)
                uid = idx.data(ROLE_UID)
                if self.card_delegate.star_rect(rect).contains(event_pos(event)):
                    self.favoriteToggled.emit(uid)
                    return
                if self.card_delegate.color_rect(rect).adjusted(-3, -3, 3, 3).contains(event_pos(event)):
                    self.setCurrentIndex(idx)
                    self.recolorRequested.emit(uid, self.viewport().mapToGlobal(
                        self.card_delegate.color_rect(rect).center()))
                    return
                if self.card_delegate.more_rect(rect).contains(event_pos(event)):
                    if not self.selectionModel().isSelected(idx):
                        self.setCurrentIndex(idx)
                    self.menuRequested.emit(uid, self.viewport().mapToGlobal(event_pos(event)))
                    return
        super().mouseReleaseEvent(event)

    # dragging ---------------------------------------------------------------
    def startDrag(self, supported):
        indexes = self.selectedIndexes()
        if not indexes:
            return
        md = self.model().mimeData(indexes)
        drag = QDrag(self)
        drag.setMimeData(md)
        pm = indexes[0].data(Qt.ItemDataRole.DecorationRole)
        if isinstance(pm, QPixmap) and not pm.isNull():
            pm = pm.scaled(48, 48, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            drag.setPixmap(pm)
            drag.setHotSpot(QPoint(pm.width() // 2, pm.height() // 2))
        drag.exec(Qt.DropAction.CopyAction | Qt.DropAction.MoveAction, Qt.DropAction.CopyAction)

    def _file_paths(self, mime):
        out = []
        if mime.hasUrls():
            for url in mime.urls():
                path = url.toLocalFile()
                if path and os.path.splitext(path)[1].lower() in DROP_EXTENSIONS:
                    out.append(path)
        return out

    def dragEnterEvent(self, event):
        mime = event.mimeData()
        if (mime.hasFormat(MIME_SYMBOLS) and event.source() is self and self.reorder_enabled) \
                or self._file_paths(mime):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        mime = event.mimeData()
        if mime.hasFormat(MIME_SYMBOLS) and event.source() is self and self.reorder_enabled:
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
            idx = self.indexAt(event_pos(event))
            row = idx.row() if idx.isValid() else None
            if row != self._drop_row:
                self._drop_row = row
                self.viewport().update()
        elif self._file_paths(mime):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self._drop_row = None
        self.viewport().update()
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        mime = event.mimeData()
        self._drop_row = None
        files = self._file_paths(mime)
        if files:
            event.acceptProposedAction()
            self.filesDropped.emit(files)
            return
        uids = decode_uids(mime)
        if uids and event.source() is self and self.reorder_enabled:
            idx = self.indexAt(event_pos(event))
            target = idx.data(ROLE_UID) if idx.isValid() else ""
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
            if target not in uids:
                self.reorderRequested.emit(uids, target or "")
            return
        event.ignore()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._drop_row is not None and self.card_delegate.theme is not None and self.model():
            idx = self.model().index(self._drop_row, 0)
            rect = self.visualRect(idx)
            p = QPainter(self.viewport())
            p.setPen(QPen(self.card_delegate.theme.accent, 3))
            if self.card_delegate.list_mode:
                p.drawLine(rect.topLeft(), rect.topRight())
            else:
                p.drawLine(rect.topLeft() + QPoint(1, 6), rect.bottomLeft() + QPoint(1, -6))
            p.end()

"""
Layers tab of the Attributes pane (as in Pro): choose a layer, step through its features with the
arrow buttons, or pick one in the list of every feature in the layer. Nothing here changes the map
selection; the pane shows (and outlines) whichever feature is chosen.
"""

import os
from contextlib import suppress

from qgis.core import QgsFeatureRequest, QgsVectorLayerUtils
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QAbstractListModel, QModelIndex, Qt, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QFont, QIcon
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QListView,
    QMenu,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

try:  # Qt6 / QGIS 4
    from qgis.PyQt.QtGui import QActionGroup
except ImportError:  # Qt5 / QGIS 3.44
    from qgis.PyQt.QtWidgets import QActionGroup

ICON_DIR = os.path.join(os.path.dirname(__file__), "icons")
ROLE_FID = Qt.ItemDataRole.UserRole + 2
PLAY_INTERVAL_MS = 1200
CACHE_LIMIT = 4000
PLACEHOLDER = "Choose a layer"
MAP_ACTIONS = (("none", "Do nothing on the map"), ("flash", "Flash"), ("pan", "Pan To"), ("zoom", "Zoom To"))


def feature_label(layer, feature):
    """What a feature is called in the lists: the layer's display expression, else its id."""
    with suppress(Exception):
        text = QgsVectorLayerUtils.getFeatureDisplayString(layer, feature)
        if text:
            return str(text)
    return str(feature.id())


def feature_labeler(layer):
    """A function feature -> name for many features of one layer.

    `feature_label` builds the layer's expression and its whole context again for every feature
    (about 0.4 ms each); listing a selection of a thousand features took over a second. Here the
    display expression is prepared once and only the feature changes."""
    try:
        from qgis.core import QgsExpression, QgsExpressionContext, QgsExpressionContextUtils
        text = layer.displayExpression()
        if not text:
            raise ValueError('no display expression')
        expression = QgsExpression(text)
        context = QgsExpressionContext(QgsExpressionContextUtils.globalProjectLayerScopes(layer))
        if expression.hasParserError() or not expression.prepare(context):
            raise ValueError('expression not usable')
    except Exception:
        return lambda feature: feature_label(layer, feature)

    def label(feature):
        with suppress(Exception):
            context.setFeature(feature)
            value = expression.evaluate(context)
            if not expression.hasEvalError() and value is not None:
                text = str(value)
                if text and text != 'NULL':
                    return text
        return feature_label(layer, feature)
    return label


class FeatureListModel(QAbstractListModel):
    """Every feature id of a layer; names are looked up only for the rows that are drawn."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.layer = None
        self.ids = []
        self._labels = {}

    def set_layer(self, layer):
        self.beginResetModel()
        self.layer = layer
        self._labels = {}
        self.ids = sorted(layer.allFeatureIds()) if layer is not None else []
        self.endResetModel()

    def clear_cache(self):
        self._labels = {}
        if self.ids:
            self.dataChanged.emit(self.index(0), self.index(len(self.ids) - 1))

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.ids)

    def row_of(self, fid):
        with suppress(ValueError):
            return self.ids.index(fid)
        return -1

    def _label(self, fid):
        text = self._labels.get(fid)
        if text is None:
            feature = next(self.layer.getFeatures(QgsFeatureRequest(fid).setFlags(QgsFeatureRequest.Flag.NoGeometry)),
                           None)
            text = feature_label(self.layer, feature) if feature is not None else str(fid)
            if len(self._labels) > CACHE_LIMIT:
                self._labels.clear()
            self._labels[fid] = text
        return text

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or self.layer is None:
            return None
        fid = self.ids[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return self._label(fid)
        if role == ROLE_FID:
            return fid
        return None


class LayerBrowser(QWidget):

    currentChanged = pyqtSignal()           # another layer or feature is chosen
    featureStepped = pyqtSignal()           # chosen by the arrows / play (the map action applies)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._layers = {}
        self._building = False
        self.map_action = "none"
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        self.combo = QComboBox(self)
        self.combo.setObjectName("KgaAttrLayerCombo")
        popup = QListView(self.combo)                  # own popup list so the pane's styling cannot garble its rows
        popup.setObjectName("KgaAttrComboView")
        self.combo.setView(popup)
        self.combo.currentIndexChanged.connect(self._layer_chosen)
        lay.addWidget(self.combo)

        nav = QHBoxLayout()
        nav.setSpacing(2)
        self.prev_button = self._nav_button("prev.svg", "Previous feature")
        self.play_button = self._nav_button("play.svg", "Step through the features automatically")
        self.next_button = self._nav_button("next.svg", "Next feature")
        self.prev_button.clicked.connect(lambda: self.step(-1))
        self.next_button.clicked.connect(lambda: self.step(1))
        self.play_button.clicked.connect(self._toggle_play)
        self.options_button = QToolButton(self)
        self.options_button.setObjectName("KgaAttrNavMenu")
        self.options_button.setIcon(QIcon(os.path.join(ICON_DIR, "chevron_down.svg")))
        self.options_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.options_button.setToolTip("What happens on the map when you move to a feature")
        menu = QMenu(self.options_button)
        self.action_group = QActionGroup(self)
        self.map_actions = {}
        for key, text in MAP_ACTIONS:
            a = menu.addAction(text)
            a.setCheckable(True)
            a.setChecked(key == self.map_action)
            self.action_group.addAction(a)
            a.triggered.connect(lambda _c=False, k=key: setattr(self, "map_action", k))
            self.map_actions[key] = a
        self.options_button.setMenu(menu)
        nav.addWidget(self.prev_button)
        nav.addWidget(self.play_button)
        nav.addWidget(self.options_button)
        nav.addWidget(self.next_button)
        nav.addStretch(1)
        lay.addLayout(nav)

        self.model = FeatureListModel(self)
        self.list = QListView(self)
        self.list.setObjectName("KgaAttrFeatureList")
        self.list.setModel(self.model)
        self.list.setUniformItemSizes(True)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.selectionModel().currentChanged.connect(self._feature_chosen)
        lay.addWidget(self.list, 1)

        self._timer = QTimer(self)
        self._timer.setInterval(PLAY_INTERVAL_MS)
        self._timer.timeout.connect(self._play_tick)
        self._reload_timer = QTimer(self)
        self._reload_timer.setSingleShot(True)
        self._reload_timer.setInterval(100)
        self._reload_timer.timeout.connect(self.reload)
        self._fill_combo([])
        self._update_buttons()

    def _nav_button(self, icon, tip):
        b = QToolButton(self)
        b.setObjectName("KgaAttrNav")
        b.setIcon(QIcon(os.path.join(ICON_DIR, icon)))
        b.setToolTip(tip)
        b.setAutoRaise(True)
        return b

    # ------------------------------------------------------------------ layers
    def set_layers(self, layers):
        """Offer these layers. Keeps the chosen layer (and feature) when it is still there."""
        ids = [layer.id() for layer in layers]
        if ids == list(self._layers):
            return
        chosen = self.layer()
        self._layers = {layer.id(): layer for layer in layers}
        self._fill_combo(layers, chosen.id() if chosen is not None else None)

    def _fill_combo(self, layers, keep=None):
        self._building = True
        self.combo.clear()
        self.combo.addItem(PLACEHOLDER, None)
        italic = QFont(self.combo.font())
        italic.setItalic(True)
        self.combo.setItemData(0, italic, Qt.ItemDataRole.FontRole)
        pick = 0
        for n, layer in enumerate(layers, start=1):
            self.combo.addItem(layer.name(), layer.id())
            if layer.id() == keep:
                pick = n
        self.combo.setCurrentIndex(pick)
        self._building = False
        if pick == 0 and self.model.layer is not None:
            self.model.set_layer(None)
            self._update_buttons()
            self.currentChanged.emit()

    def layer(self):
        lid = self.combo.currentData()
        layer = self._layers.get(lid) if lid else None
        return None if layer is None or sip.isdeleted(layer) else layer

    def current(self):
        """(layer, fid) chosen, fid None when no feature is picked."""
        layer = self.layer()
        if layer is None:
            return None, None
        index = self.list.currentIndex()
        return layer, (index.data(ROLE_FID) if index.isValid() else None)

    def _layer_chosen(self, _index):
        if self._building:
            return
        self._stop_play()
        layer = self.layer()
        self.model.set_layer(layer)
        self._update_buttons()
        self.currentChanged.emit()

    def choose_layer(self, layer):
        index = self.combo.findData(layer.id())
        if index >= 0:
            self.combo.setCurrentIndex(index)

    def choose_feature(self, fid):
        row = self.model.row_of(fid)
        if row >= 0:
            self.list.setCurrentIndex(self.model.index(row))
            self.list.scrollTo(self.model.index(row))

    def _feature_chosen(self, _current, _previous):
        self._update_buttons()
        self.currentChanged.emit()

    # ------------------------------------------------------------------ stepping
    def step(self, delta):
        count = self.model.rowCount()
        if not count:
            return False
        row = self.list.currentIndex().row() if self.list.currentIndex().isValid() else (-1 if delta > 0 else count)
        row += delta
        if not 0 <= row < count:
            return False
        self.list.setCurrentIndex(self.model.index(row))
        self.list.scrollTo(self.model.index(row))
        self.featureStepped.emit()
        return True

    def _toggle_play(self):
        if self._timer.isActive():
            self._stop_play()
        elif self.model.rowCount():
            self.play_button.setIcon(QIcon(os.path.join(ICON_DIR, "pause.svg")))
            self.play_button.setToolTip("Stop")
            self._timer.start()
            self._play_tick()

    def _play_tick(self):
        if not self.step(1):
            self._stop_play()

    def _stop_play(self):
        self._timer.stop()
        self.play_button.setIcon(QIcon(os.path.join(ICON_DIR, "play.svg")))
        self.play_button.setToolTip("Step through the features automatically")

    def _update_buttons(self):
        count = self.model.rowCount()
        row = self.list.currentIndex().row() if self.list.currentIndex().isValid() else -1
        self.prev_button.setEnabled(count > 0 and row > 0)
        self.next_button.setEnabled(count > 0 and row < count - 1)
        self.play_button.setEnabled(count > 0)
        self.options_button.setEnabled(count > 0)

    # ------------------------------------------------------------------ the layer changed
    def schedule_reload(self, *_args):
        self._reload_timer.start()

    def reload(self):
        """Features were added, deleted or committed: read the id list again, keeping the chosen one."""
        layer = self.layer()
        if layer is None:
            return
        _l, fid = self.current()
        self._building = True
        self.model.set_layer(layer)
        self._building = False
        if fid is not None:
            self.choose_feature(fid)
        self._update_buttons()
        self.currentChanged.emit()

    def clear_cache(self):
        self.model.clear_cache()

    def shutdown(self):
        self._stop_play()
        self._reload_timer.stop()

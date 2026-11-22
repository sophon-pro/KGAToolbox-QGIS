"""The bubbles of the floating bags (Tool Bag, Symbol Bag, Layer Colors) sit on the map as one group.

Every bag that is minimized to a bubble reports itself here. The group stacks the bubbles that are
on the map in one column, in a fixed order, at the place the Tool Bag's bubble position setting
names (``ToolBag/bubble_position``) and in the size of its bubble size setting. When a bubble
appears or goes away the others glide to their new places.

A bubble is any widget with ``group_center()`` (its circle's centre, screen coordinates),
``group_move(QPointF)`` (put the circle's centre there) and, optionally, ``group_set_size(px)``.
"""

from contextlib import suppress

from qgis.PyQt.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QVariantAnimation, pyqtSignal

from ..core.kga_settings import shared as _settings     # one QgsSettings: these are read once a second

ORDER = ("tool", "symbol", "colors", "catalog", "explorer", "attributes", "geoprocessing", "ui_manager",
         "layer_symbol", "create_features", "modify_features", "attributes_pane", "dem_dsm_inspector",
         "satellite_imagery_archive", "layers_panel", "layer_mini_panel", "label_bag")
#: Bubbles a panel can switch off on its own in Settings > Bubbles (the bags follow the shared switch).
PANEL_BUBBLES = (("catalog", "Catalog"), ("explorer", "Data Explorer"), ("attributes", "Attribute tables"),
                 ("geoprocessing", "KGA Geoprocessing"), ("ui_manager", "Panels and Toolbars Manager"),
                 ("layer_symbol", "KGA Layer Symbol"), ("create_features", "Create Features"),
                 ("modify_features", "Modify Features"), ("attributes_pane", "Open Feature Attribute"),
                 ("dem_dsm_inspector", "DEM / DSM Inspector"),
                 ("satellite_imagery_archive", "Satellite Imagery Archive"), ("layers_panel", "Layers panel"),
                 ("layer_mini_panel", "Layer Bag"), ("label_bag", "Label Bag"))
POSITIONS = {                      # key -> (horizontal 0/1/2, vertical 0/1/2)
    "top_left": (0, 0), "top_center": (1, 0), "top_right": (2, 0),
    "middle_left": (0, 1), "center": (1, 1), "middle_right": (2, 1),
    "bottom_left": (0, 2), "bottom_center": (1, 2), "bottom_right": (2, 2),
}
DEFAULT_POSITION = "bottom_right"
DEFAULT_SIZE = 40
EDGE = 14        # gap between the column and the edge of the map
GAP = 10         # gap between two bubbles
GLIDE_MS = 220
SIDE_GAP = 12    # gap between the column and a window revealed beside it


def animation_style():
    """'genie' or 'classic' (KGA settings, Appearance page)."""
    from ..core import kga_settings as ks
    return ks.bag_animation()


# Settings the three bags share (QgsSettings "KGA/bags/<key>"); changing one tells every bag.
COMMON_DEFAULTS = {"style": "mint", "bubble": True, "motion": "normal",
                   "sound": False, "sound_style": "whoosh", "sound_volume": 70,
                   "size": DEFAULT_SIZE, "position": DEFAULT_POSITION, "gap": GAP, "edge": EDGE,
                   "disabled": ""}
#: allowed range of the numeric settings (px)
LIMITS = {"size": (28, 64), "gap": (2, 30), "edge": (4, 40), "sound_volume": (0, 100)}
#: the settings that move or resize the bubbles (they relayout the column)
GEOMETRY_KEYS = ("size", "position", "gap", "edge")
COMMON_PREFIX = "KGA/bags/"
MOTIONS = ("off", "snappy", "normal", "relaxed")
SPEED = {"off": 0.0, "snappy": 0.75, "normal": 1.0, "relaxed": 1.35}


def read(key):
    """The shared bubble setting *key* (QgsSettings "KGA/bags/<key>"), typed and clamped; works without
    a live BubbleGroup (the Settings window uses it). Position and size fall back to the values the Tool
    Bag used to keep, so nobody loses their placement."""
    settings = _settings()
    default = COMMON_DEFAULTS[key]
    value = settings.value(COMMON_PREFIX + key, None)
    if value is None:
        if key == "style":                     # first run: take over the Tool Bag's design
            old = str(settings.value("ToolBag/theme", "") or "")
            return "white" if old == "classic" else (old or default)
        if key == "bubble":
            return str(settings.value("ToolBag/bubble_visible", True)).lower() in ("true", "1")
        if key in ("size", "position"):
            value = settings.value("ToolBag/bubble_" + key, None)
        if value is None:
            return default
    if isinstance(default, bool):
        return str(value).lower() in ("true", "1")
    if isinstance(default, int):
        try:
            number = int(value)
        except (TypeError, ValueError):
            return default
        lo, hi = LIMITS.get(key, (number, number))
        return max(lo, min(hi, number))
    value = str(value)
    if key == "position" and value not in POSITIONS:
        return default
    return value


def write(key, value):
    if isinstance(COMMON_DEFAULTS[key], int) and not isinstance(COMMON_DEFAULTS[key], bool):
        lo, hi = LIMITS.get(key, (int(value), int(value)))
        value = max(lo, min(hi, int(value)))
    _settings().setValue(COMMON_PREFIX + key, value)


OVERRIDES = ("style", "position", "size")       # what a single bubble can keep for itself


def _override_key(key, what):
    return "{}bubble/{}/{}".format(COMMON_PREFIX, key, what)


def override(key, what):
    """What bubble *key* keeps for itself ('style', 'position' or 'size'), or None: it follows the
    shared setting."""
    value = _settings().value(_override_key(key, what), None)
    if value in (None, ""):
        return None
    if what == "size":
        try:
            lo, hi = LIMITS["size"]
            return max(lo, min(hi, int(value)))
        except (TypeError, ValueError):
            return None
    value = str(value)
    if what == "position" and value not in POSITIONS:
        return None
    return value


def set_override(key, what, value):
    """Give bubble *key* its own style / position / size (value None: follow the shared one again)
    and tell the live bubbles."""
    if override(key, what) == value:
        return False
    settings = _settings()
    if value is None:
        settings.remove(_override_key(key, what))
    else:
        settings.setValue(_override_key(key, what), value)
    group = BubbleGroup.instance
    if group is not None:
        if what == "style":
            group.settingsChanged.emit("style")
        else:
            group.relayout(True)
            group.settingsChanged.emit("bubble_geometry")
    return True


def clear_overrides(key=None):
    """Every bubble (or just *key*) follows the shared settings again."""
    keys = [key] if key else [k for k in ORDER]
    changed = False
    for k in keys:
        for what in OVERRIDES:
            if override(k, what) is not None:
                changed = set_override(k, what, None) or changed
    return changed


def apply_settings(values):
    """Save several shared settings at once and tell the live bubbles (when there are any).

    Geometry keys relayout the column once, whatever number of them changed."""
    changed = []
    for key, value in values.items():
        if read(key) != value:
            write(key, value)
            changed.append(key)
    group = BubbleGroup.instance
    if group is None or not changed:
        return changed
    if "style" in changed:
        group.apply_style(read("style"))
    if any(k in GEOMETRY_KEYS for k in changed):
        group.relayout(True)
        group.settingsChanged.emit("bubble_geometry")
    for key in changed:
        if key not in GEOMETRY_KEYS:
            group.settingsChanged.emit(key)
    return changed


def reset_settings():
    """Back to the defaults (the style is left to the bags: it is applied through them)."""
    return apply_settings(dict(COMMON_DEFAULTS))


class BubbleGroup(QObject):
    settingsChanged = pyqtSignal(str)     # which shared setting changed
    instance = None                       # the live group (one per plugin), for the Settings window

    def __init__(self, iface):
        super().__init__()
        self.iface = iface
        self.bubbles = {}          # key -> bubble widget
        self.present = set()       # keys of the bubbles that are (or are about to be) on the map
        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setDuration(GLIDE_MS)
        self._anim.valueChanged.connect(self._on_glide)
        self._from = {}
        self._to = {}
        self._watched = []
        self.style_setters = {}    # key -> (getter, setter): the bags' own colour
        BubbleGroup.instance = self
        canvas = iface.mapCanvas()
        for w in (canvas, iface.mainWindow()):
            w.installEventFilter(self)
            self._watched.append(w)

    # ------------------------------------------------------------ shared settings
    def get(self, key):
        """A shared bubble setting: style, bubble, motion, sound, sound_style, sound_volume, size,
        position, gap, edge, disabled, animation ('genie' | 'classic')."""
        if key == "animation":
            return animation_style()
        return read(key)

    def set(self, key, value):
        if key == "animation":
            from ..core import kga_settings as ks
            if value != animation_style():
                ks.set_value(ks.KEY_BAG_ANIMATION, value)
                self.settingsChanged.emit(key)
            return
        apply_settings({key: value})

    # ------------------------------------------------------------ colour (each bag keeps its own)
    def register_style(self, key, getter, setter):
        """A bag tells how to read / set its colour so one choice can recolour them all."""
        self.style_setters[key] = (getter, setter)

    def apply_style(self, style):
        for getter, setter in list(self.style_setters.values()):
            with suppress(Exception):  # a bag that is gone
                if getter() != style:
                    setter(style)

    # ------------------------------------------------------------ which panels get a bubble
    def disabled_keys(self):
        return {k for k in str(self.get("disabled")).split(",") if k}

    def bubble_enabled(self, key):
        """The shared switch is on and this panel's own bubble has not been turned off."""
        return bool(self.get("bubble")) and key not in self.disabled_keys()

    def speed(self):
        return SPEED.get(self.get("motion"), 1.0)

    def play_sound(self, event):
        """The Genie sound ('in' = into the bubble, 'out' = back out), if sound is on."""
        if self.get("sound") and self.speed() > 0:
            from .symbol_bag.ui import sound
            sound.play_event(event, self.get("sound_style"), self.get("sound_volume") / 100.0)

    # ------------------------------------------------------------ registration
    def register(self, key, bubble):
        self.bubbles[key] = bubble

    def unregister(self, key):
        self.bubbles.pop(key, None)
        self.style_setters.pop(key, None)
        self.present.discard(key)

    def unload(self):
        self._anim.stop()
        for w in self._watched:
            try:
                w.removeEventFilter(self)
            except RuntimeError:
                pass
        self._watched = []
        self.bubbles.clear()
        self.present.clear()
        self.style_setters.clear()
        if BubbleGroup.instance is self:
            BubbleGroup.instance = None

    # ------------------------------------------------------------ settings
    def diameter(self):
        """The shared size (a bubble with its own size: `size_of`)."""
        return self.get("size")

    def position(self):
        return self.get("position")

    def size_of(self, key):
        return override(key, "size") or self.get("size")

    def position_of(self, key):
        return override(key, "position") or self.get("position")

    def style_of(self, key):
        return override(key, "style") or self.get("style")

    def has_override(self, key):
        return any(override(key, what) is not None for what in OVERRIDES)

    def gap(self):
        return self.get("gap")

    def edge(self):
        return self.get("edge")

    # ------------------------------------------------------------ layout
    def _area(self):
        canvas = self.iface.mapCanvas()
        return QRect(canvas.mapToGlobal(QPoint(0, 0)), canvas.size())

    def targets(self, extra=None):
        """key -> global centre for every present bubble (plus *extra*, a bubble about to appear).

        Bubbles that share a position stack in one column there, each in its own size; a bubble with a
        position of its own starts a column of its own."""
        keys = [k for k in ORDER if k in self.present or k == extra]
        if not keys:
            return {}
        area, gap, edge = self._area(), self.gap(), self.edge()
        columns = {}
        for k in keys:
            columns.setdefault(self.position_of(k), []).append(k)
        out = {}
        for position, members in columns.items():
            h, v = POSITIONS[position]
            sizes = [self.size_of(k) for k in members]
            height = sum(sizes) + (len(members) - 1) * gap
            top = (area.top() + edge, area.center().y() - height // 2, area.bottom() + 1 - edge - height)[v]
            y = max(area.top() + 4, top)
            for k, d in zip(members, sizes):
                left = (area.left() + edge, area.center().x() - d // 2, area.right() + 1 - edge - d)[h]
                left = max(area.left() + 4, left)
                out[k] = QPointF(left + d / 2.0, y + d / 2.0)
                y += d + gap
        return out

    def beside_x(self, key, center_x, radius, w):
        """Left edge for a window revealed beside the column, when other bubbles are in it (the window
        must not cover them): left of a right-hand column, right of a left-hand or middle one.
        None when *key* is alone on the map (the window then opens right on its spot)."""
        position = self.position_of(key)
        if not [k for k in self.present if k != key and self.position_of(k) == position]:
            return None
        hx, _vy = POSITIONS[position]
        area = self._area()
        right = int(round(center_x + radius + SIDE_GAP))
        left = int(round(center_x - radius - SIDE_GAP - w))
        if hx == 2:
            return left
        if hx == 0:
            return right
        return right if right + w <= area.right() - 3 else left

    def anchor_topleft(self, center, radius, w, h, key=None):
        """Top-left (screen) of a w x h window revealed at a bubble: aligned to the bubble the way the
        bubble is aligned to the map (bottom bubble -> bottoms meet, ...), on its spot when alone, beside
        the column when other bubbles are there; kept on the map."""
        hx, vy = POSITIONS[self.position_of(key) if key else self.position()]
        x = (center.x() - radius, center.x() - w / 2.0, center.x() + radius - w)[hx]
        side = self.beside_x(key, center.x(), radius, w) if key else None
        if side is not None:
            x = side
        y = (center.y() - radius, center.y() - h / 2.0, center.y() + radius - h)[vy]
        area = self._area()
        x = max(area.left() + 4, min(area.right() - w - 3, int(round(x))))
        y = max(area.top() + 4, min(area.bottom() - h - 3, int(round(y))))
        return QPoint(x, y)

    def slot_rect(self, key):
        """The circle (screen coordinates) where bubble *key* sits, or would sit, in the column."""
        from qgis.PyQt.QtCore import QRectF
        c = self.targets(extra=key).get(key)
        if c is None:
            return None
        d = float(self.size_of(key))
        return QRectF(c.x() - d / 2.0, c.y() - d / 2.0, d, d)

    def target_for(self, key):
        """Where the bubble *key* will sit once it is on the map (reserves its place first)."""
        self.set_present(key, True)
        return self.targets().get(key)

    def set_present(self, key, on, animate=True):
        if on == (key in self.present):
            return
        if on:
            self.present.add(key)
        else:
            self.present.discard(key)
        self.relayout(animate)

    def relayout(self, animate=False):
        targets = self.targets()
        self._anim.stop()
        self._from, self._to = {}, {}
        for key, tgt in targets.items():
            b = self.bubbles.get(key)
            if b is None:
                continue
            if hasattr(b, "group_set_size"):
                b.group_set_size(self.size_of(key))
            if animate and b.isVisible():
                self._from[key] = b.group_center()
                self._to[key] = tgt
            else:
                b.group_move(tgt)
        if self._to:
            self._anim.start()

    def _on_glide(self, value):
        t = float(value)
        t = 1.0 - (1.0 - t) ** 3
        for key, tgt in self._to.items():
            b = self.bubbles.get(key)
            src = self._from.get(key)
            if b is None or src is None:
                continue
            try:
                b.group_move(QPointF(src.x() + (tgt.x() - src.x()) * t, src.y() + (tgt.y() - src.y()) * t))
            except RuntimeError:
                pass

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Move, QEvent.Type.Show) and self.present:
            self.relayout(False)
        return False

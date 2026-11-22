"""The menu entries the Tool Bag, Symbol Bag and Layer Colors share.

All three show the same four submenus, in the same order, backed by the same settings
(``BubbleGroup.get / set``): Bag Style (colourway), Bubble, Animation and Sound Effects.
"""

from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QColor, QIcon, QPixmap
from qgis.PyQt.QtWidgets import QHBoxLayout, QLabel, QSlider, QWidget, QWidgetAction

try:
    from qgis.PyQt.QtWidgets import QActionGroup
except ImportError:  # Qt6
    from qgis.PyQt.QtGui import QActionGroup

from .bag_group import MOTIONS, POSITIONS, apply_settings, clear_overrides, override, set_override
from .symbol_bag.ui.sound import STYLES as SOUND_STYLES
from .symbol_bag.ui.theme import BAG_STYLES, STYLE_ORDER

POSITION_NAMES = {"top_left": "Top left", "top_center": "Top center", "top_right": "Top right",
                  "middle_left": "Middle left", "center": "Center", "middle_right": "Middle right",
                  "bottom_left": "Bottom left", "bottom_center": "Bottom center",
                  "bottom_right": "Bottom right"}
SIZES = (("Tiny", 28), ("Small", 34), ("Medium", 40), ("Large", 52), ("Extra large", 64))
MOTION_NAMES = {"off": "Off", "snappy": "Snappy", "normal": "Normal", "relaxed": "Relaxed"}
ANIMATIONS = (("genie", "Genie: pours into the bubble"), ("classic", "Classic: shrinks into the bubble"))


def _swatch(style):
    pix = QPixmap(14, 14)
    pix.fill(Qt.GlobalColor.transparent)
    from qgis.PyQt.QtGui import QPainter
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(BAG_STYLES[style]["h2"]))
    p.setPen(QColor(0, 0, 0, 70))
    p.drawEllipse(1, 1, 12, 12)
    p.end()
    return QIcon(pix)


def _later(fn):
    QTimer.singleShot(0, fn)          # never change things while the menu that asked is still closing


def _radio(menu, items, current, apply):
    group = QActionGroup(menu)
    for key, label, *icon in items:
        act = menu.addAction(label)
        if icon:
            act.setIcon(icon[0])
        act.setCheckable(True)
        act.setChecked(current == key)
        act.setActionGroup(group)
        act.triggered.connect(lambda _c=False, k=key: _later(lambda: apply(k)))


def _volume_action(menu, group):
    act = QWidgetAction(menu)
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(12, 4, 12, 4)
    lay.setSpacing(8)
    slider = QSlider(Qt.Orientation.Horizontal)
    slider.setRange(0, 100)
    slider.setSingleStep(10)
    slider.setPageStep(10)
    slider.setValue(group.get("sound_volume"))
    slider.setMinimumWidth(120)
    value = QLabel("{} %".format(slider.value()))
    value.setMinimumWidth(36)
    slider.valueChanged.connect(lambda v: (value.setText("{} %".format(v)), group.set("sound_volume", v)))
    slider.sliderReleased.connect(lambda: group.play_sound("in"))
    lay.addWidget(QLabel("Volume"))
    lay.addWidget(slider, 1)
    lay.addWidget(value)
    act.setDefaultWidget(w)
    return act


def fill_override_menus(menu, group, key, with_style=False):
    """Size, Position (and Style) of the single bubble *key*: each starts with "same as the other
    bubbles" (the shared setting of Settings > Bubbles) and can be set to this bubble alone."""
    if with_style:
        style = menu.addMenu("Style (colour)")
        shared = BAG_STYLES.get(group.get("style"), {}).get("label", group.get("style"))
        _radio(style, [(None, "Same as other bubbles ({})".format(shared))]
               + [(k, BAG_STYLES[k]["label"], _swatch(k)) for k in STYLE_ORDER],
               override(key, "style"), lambda v: set_override(key, "style", v))
    size = menu.addMenu("Size")
    _radio(size, [(None, "Same as other bubbles ({} px)".format(group.diameter()))]
           + [(px, "{} ({} px)".format(label, px)) for label, px in SIZES],
           override(key, "size"), lambda v: set_override(key, "size", v))
    pos = menu.addMenu("Position")
    _radio(pos, [(None, "Same as other bubbles ({})".format(POSITION_NAMES[group.position()]))]
           + [(k, POSITION_NAMES[k]) for k in POSITIONS],
           override(key, "position"), lambda v: set_override(key, "position", v))
    reset = menu.addAction("Reset This Bubble to the Shared Settings")
    reset.setEnabled(group.has_override(key))
    reset.triggered.connect(lambda _c=False: _later(lambda: clear_overrides(key)))
    opener = getattr(group, "open_settings", None)
    if opener is not None:
        menu.addAction("All Bubble Settings...", lambda: _later(opener))


def fill_common_menu(menu, group, get_style, set_style, key=None, with_style=True):
    """Add Bag Style, Bubble, Animation and Sound Effects submenus to *menu*.

    The colour is each bag's own (get_style() -> key, set_style(key)); the rest is shared. With *key*,
    the bubble's size and position are that bubble's own (shared by default). *with_style* False leaves
    the Bag Style menu out (a panel with a colour menu of its own, e.g. the Layer Bag)."""
    if with_style:
        style = menu.addMenu("Bag Style (colour)")
        _radio(style, [(k, BAG_STYLES[k]["label"], _swatch(k)) for k in STYLE_ORDER],
               get_style(), set_style)

    bubble = menu.addMenu("Bubble")
    on = bubble.addAction("Minimize to a Bubble")
    on.setCheckable(True)
    on.setChecked(group.get("bubble"))
    on.toggled.connect(lambda c: _later(lambda: group.set("bubble", bool(c))))
    if key:                                    # this bubble's own size and position
        fill_override_menus(bubble, group, key)
    else:
        size = bubble.addMenu("Bubble Size")
        _radio(size, [(px, "{} ({} px)".format(label, px)) for label, px in SIZES], group.diameter(),
               lambda px: _set_geometry(group, size=px))
        pos = bubble.addMenu("Bubble Position")
        _radio(pos, [(k, POSITION_NAMES[k]) for k in POSITIONS], group.position(),
               lambda k: _set_geometry(group, position=k))
        note = bubble.addAction("Shared: change it for all bubbles in Settings > Bubbles")
        note.setEnabled(False)

    anim = menu.addMenu("Animation")
    _radio(anim, [(k, label) for k, label in ANIMATIONS], group.get("animation"),
           lambda k: group.set("animation", k))
    anim.addSection("Speed")
    _radio(anim, [(k, MOTION_NAMES[k]) for k in MOTIONS], group.get("motion"),
           lambda k: group.set("motion", k))

    sound = menu.addMenu("Sound Effects")
    on = sound.addAction("Sound On")
    on.setCheckable(True)
    on.setChecked(group.get("sound"))
    on.toggled.connect(lambda c: _later(lambda: group.set("sound", bool(c))))
    sound.addAction(_volume_action(sound, group))
    sound.addSection("Style")
    _radio(sound, [(k, "{}  -  {}".format(label, desc)) for k, label, desc in SOUND_STYLES],
           group.get("sound_style"), lambda k: (group.set("sound_style", k), group.set("sound", True),
                                                group.play_sound("in")))


def _set_geometry(group, size=None, position=None):
    values = {}
    if size is not None:
        values["size"] = int(size)
    if position is not None:
        values["position"] = position
    apply_settings(values)

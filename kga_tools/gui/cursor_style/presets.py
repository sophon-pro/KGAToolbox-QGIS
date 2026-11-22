"""Built-in cursor presets. Pure data; kept apart from application logic."""

from collections import OrderedDict

from .style import CursorStyle, BASE_NATIVE, BASE_SYSTEM_ARROW

DEFAULT_PRESET = "QGIS Default"

_P = OrderedDict()

_P["QGIS Default"] = (
    "Keep each QGIS tool's own cursor (plugin passive).",
    CursorStyle(base=BASE_NATIVE))

_P["Standard Pointer"] = (
    "Your operating system's normal arrow pointer.",
    CursorStyle(base=BASE_SYSTEM_ARROW))

_P["Crosshair"] = (
    "Thin full crosshair with a contrasting outline.",
    CursorStyle(crosshair=True, arm_length=12, main_color="#111111",
                outline_color="#ffffff"))

_P["Precision Crosshair"] = (
    "Crosshair with an open centre and a single-pixel hotspot dot.",
    CursorStyle(crosshair=True, arm_length=16, gap=5, dot=True, dot_size=1,
                main_color="#e91e63", dot_color="#e91e63",
                outline_color="#ffffff"))

_P["Circle"] = (
    "Ring with a one-pixel centre mark.",
    CursorStyle(crosshair=False, circle=True, circle_diameter=20, dot=True,
                dot_size=1, main_color="#2962ff", dot_color="#2962ff",
                outline_color="#ffffff"))

_P["Crosshair + Circle"] = (
    "Crosshair with a small circle around the hotspot.",
    CursorStyle(crosshair=True, circle=True, arm_length=14,
                circle_diameter=10, main_color="#d50000",
                outline_color="#ffffff"))

_P["Target"] = (
    "Outer circle, inner ring, open crosshair and centre dot.",
    CursorStyle(crosshair=True, circle=True, inner_ring=True, dot=True,
                arm_length=16, gap=6, circle_diameter=26,
                inner_ring_diameter=10, dot_size=1,
                main_color="#ff6d00", dot_color="#ff6d00",
                outline_color="#000000"))

_P["Dot + Ring"] = (
    "Solid centre dot surrounded by a ring.",
    CursorStyle(crosshair=False, circle=True, dot=True, circle_diameter=16,
                dot_size=4, main_color="#00c853", dot_color="#00c853",
                outline_color="#000000"))

_P["Precision Digitizing"] = (
    "Long open crosshair and small ring for snapping to vertices.",
    CursorStyle(crosshair=True, circle=True, dot=True, arm_length=22, gap=7,
                circle_diameter=8, dot_size=1, main_color="#ffd600",
                dot_color="#ff1744", outline_color="#000000"))

_P["High Visibility"] = (
    "Thick, large yellow cursor with a black outline.",
    CursorStyle(crosshair=True, circle=True, dot=True, arm_length=18, gap=7,
                circle_diameter=24, line_width=3, outline_width=2,
                dot_size=1, dot_color="#ff1744",
                scale=125, main_color="#ffea00", outline_color="#000000"))

_P["Dark Background"] = (
    "Bright cyan cursor for imagery and dark basemaps.",
    CursorStyle(crosshair=True, circle=True, arm_length=14, gap=3,
                circle_diameter=12, main_color="#18ffff",
                outline_color="#000000"))

_P["Light Background"] = (
    "Deep navy cursor for light basemaps and blank canvases.",
    CursorStyle(crosshair=True, circle=True, arm_length=14, gap=3,
                circle_diameter=12, main_color="#0d1b6e",
                outline_color="#ffffff"))

BUILTIN_PRESETS = _P


def builtin_names():
    return list(_P.keys())


def builtin_style(name):
    entry = _P.get(name)
    return entry[1] if entry else None


def builtin_description(name):
    entry = _P.get(name)
    return entry[0] if entry else ""


def is_builtin(name):
    return name in _P

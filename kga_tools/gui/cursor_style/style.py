"""Cursor style model: a plain, validated, serialisable description of a cursor.

This module has no Qt dependency so it can be tested and reused anywhere.
"""

from dataclasses import dataclass, fields, asdict, replace
import json
import re

# How the cursor is produced.
BASE_NATIVE = "native"            # leave QGIS tool cursors untouched
BASE_SYSTEM_ARROW = "system_arrow"  # Qt::ArrowCursor (OS pointer)
BASE_SYSTEM_CROSS = "system_cross"  # Qt::CrossCursor (OS crosshair)
BASE_CUSTOM = "custom"            # pixmap drawn by the renderer
BASES = (BASE_NATIVE, BASE_SYSTEM_ARROW, BASE_SYSTEM_CROSS, BASE_CUSTOM)

# Value limits (logical pixels, before scale / display factor).
LIMITS = {
    "scale": (50, 300),
    "arm_length": (3, 48),
    "gap": (0, 20),
    "circle_diameter": (6, 96),
    "inner_ring_diameter": (4, 64),
    "dot_size": (1, 16),
    "line_width": (1, 5),
    "outline_width": (1, 4),
    "opacity": (25, 100),
}

# Largest logical half-extent of a generated cursor. 64 px half extent ->
# up to 129 px wide; very large OS cursors are slow to create and some
# platforms clip them.
MAX_HALF_EXTENT = 64

_HEX = re.compile(r"^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$")


@dataclass(frozen=True)
class CursorStyle:
    base: str = BASE_CUSTOM
    crosshair: bool = True
    circle: bool = False
    dot: bool = False
    inner_ring: bool = False
    scale: int = 100            # overall size, percent
    arm_length: int = 12        # centre -> end of each crosshair arm
    gap: int = 0                # empty radius around the hotspot
    circle_diameter: int = 18
    inner_ring_diameter: int = 8
    dot_size: int = 3
    line_width: int = 1
    outline: bool = True
    outline_width: int = 1
    dashed: bool = False
    opacity: int = 100          # percent
    main_color: str = "#ff1744"
    outline_color: str = "#ffffff"
    dot_color: str = "#ff1744"
    match_ui_scale: bool = True

    # ------------------------------------------------------------------ helpers
    @property
    def is_custom(self):
        return self.base == BASE_CUSTOM

    @property
    def overrides_native(self):
        return self.base != BASE_NATIVE

    def has_visible_component(self):
        return self.crosshair or self.circle or self.dot or self.inner_ring

    def key(self):
        """Hashable key covering every property that affects rendering."""
        return tuple(getattr(self, f.name) for f in fields(self))

    def to_dict(self):
        return asdict(self)

    def to_json(self):
        return json.dumps(self.to_dict(), sort_keys=True)

    def with_changes(self, **kw):
        return validated(replace(self, **kw))[0]

    @classmethod
    def from_dict(cls, data):
        """Build from an untrusted dict (settings / user file). Unknown keys are
        ignored, wrong types fall back to defaults, ranges are clamped."""
        default = cls()
        kw = {}
        if isinstance(data, dict):
            for f in fields(cls):
                if f.name not in data:
                    continue
                val = data[f.name]
                ref = getattr(default, f.name)
                try:
                    if isinstance(ref, bool):
                        if isinstance(val, str):
                            val = val.strip().lower() in ("1", "true", "yes")
                        else:
                            val = bool(val)
                    elif isinstance(ref, int):
                        val = int(round(float(val)))
                    elif isinstance(ref, str):
                        val = str(val)
                except (TypeError, ValueError):
                    continue
                kw[f.name] = val
        return validated(cls(**kw))[0]

    @classmethod
    def from_json(cls, text):
        try:
            return cls.from_dict(json.loads(text))
        except (TypeError, ValueError):
            return cls()


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def validated(style):
    """Return (clean_style, [messages]). Never raises; every returned style is
    renderable. Messages describe adjustments a UI may show to the user."""
    msgs = []
    kw = {}
    if style.base not in BASES:
        kw["base"] = BASE_CUSTOM
        msgs.append("Unknown cursor type; using a custom drawn cursor.")
    for name, (lo, hi) in LIMITS.items():
        v = getattr(style, name)
        c = _clamp(v, lo, hi)
        if c != v:
            kw[name] = c
    for name in ("main_color", "outline_color", "dot_color"):
        v = getattr(style, name)
        if not isinstance(v, str) or not _HEX.match(v):
            kw[name] = getattr(CursorStyle, name)
            msgs.append(f"Invalid colour for {name.replace('_', ' ')}; reset.")
    s = replace(style, **kw) if kw else style

    if s.base == BASE_CUSTOM and not s.has_visible_component():
        s = replace(s, crosshair=True)
        msgs.append("A cursor needs at least one visible part; crosshair enabled.")
    if s.crosshair and s.gap >= s.arm_length:
        s = replace(s, gap=max(0, s.arm_length - 2))
        msgs.append("Centre gap must be shorter than the crosshair arms.")
    if s.circle and s.inner_ring and s.inner_ring_diameter >= s.circle_diameter:
        s = replace(s, inner_ring_diameter=max(4, s.circle_diameter - 4))
        msgs.append("Inner ring must be smaller than the outer circle.")
    return s, msgs

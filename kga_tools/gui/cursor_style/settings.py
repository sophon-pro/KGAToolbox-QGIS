"""Persistent settings (QgsSettings, current QGIS user profile, under KGA/cursor_style/).

Values are stored as JSON strings so they round-trip identically on every
platform's settings backend (INI / registry / plist).
"""

import json
from collections import OrderedDict

from qgis.core import QgsSettings

from .style import CursorStyle
from .presets import DEFAULT_PRESET, builtin_style, is_builtin
from .controller import DEFAULT_ASSIGNMENTS, ASSIGN_MAIN, ASSIGN_QGIS

PREFIX = "KGA/cursor_style/"
SCHEMA = 2  # 2: a cursor per kind of tool ("assignments") instead of on/off "contexts"
MAX_PROFILES = 100
MAX_NAME = 48


class SettingsStore:
    def __init__(self, settings=None):
        self._s = settings or QgsSettings()

    def _get(self, key, default=None):
        v = self._s.value(PREFIX + key, default)
        return default if v is None else v

    def _set(self, key, value):
        self._s.setValue(PREFIX + key, value)

    def _load_json(self, key, default):
        raw = self._get(key, None)
        if not raw:
            return default
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return default

    # ---------------------------------------------------------- state
    def load_state(self):
        """Return (active_name, style, assignments, enabled, profiles)."""
        profiles = self.load_profiles()
        active = str(self._get("active_profile", DEFAULT_PRESET))
        style_data = self._load_json("current_style", None)
        if style_data is not None:
            style = CursorStyle.from_dict(style_data)
        elif is_builtin(active):
            style = builtin_style(active)
        elif active in profiles:
            style = profiles[active]
        else:
            active, style = DEFAULT_PRESET, builtin_style(DEFAULT_PRESET)
        assignments = dict(DEFAULT_ASSIGNMENTS)
        saved = self._load_json("assignments", None)
        if isinstance(saved, dict):
            for k in assignments:
                v = saved.get(k)
                if isinstance(v, str) and v:
                    assignments[k] = clean_name(v) if v not in (ASSIGN_MAIN, ASSIGN_QGIS) else v
        else:
            # Schema 1 had one style plus an on/off box per kind of tool:
            # on -> the main style, off -> the QGIS cursor.
            ctx = self._load_json("contexts", None)
            if isinstance(ctx, dict):
                for k in assignments:
                    if k in ctx:
                        assignments[k] = ASSIGN_MAIN if bool(ctx[k]) else ASSIGN_QGIS
        enabled = str(self._get("enabled", "true")).lower() in ("1", "true")
        return active, style, assignments, enabled, profiles

    def save_state(self, active, style, assignments, enabled):
        self._set("schema", SCHEMA)
        self._set("active_profile", active)
        self._set("current_style", style.to_json())
        self._set("assignments", json.dumps(assignments, sort_keys=True))
        self._s.remove(PREFIX + "contexts")
        self._set("enabled", "true" if enabled else "false")

    # ------------------------------------------------------- profiles
    def load_profiles(self):
        data = self._load_json("profiles", {})
        out = OrderedDict()
        if isinstance(data, dict):
            for name, sd in list(data.items())[:MAX_PROFILES]:
                name = clean_name(name)
                if name and not is_builtin(name):
                    out[name] = CursorStyle.from_dict(sd)
        return out

    def save_profiles(self, profiles):
        self._set("profiles", json.dumps(
            OrderedDict((n, s.to_dict()) for n, s in profiles.items())))

    def get_flag(self, key, default):
        return str(self._get("ui/" + key, "true" if default else "false")).lower() in ("1", "true")

    def set_flag(self, key, value):
        self._set("ui/" + key, "true" if value else "false")

    def reset_all(self):
        self._s.remove(PREFIX.rstrip("/"))


def clean_name(name):
    name = " ".join(str(name or "").split())
    return name[:MAX_NAME]

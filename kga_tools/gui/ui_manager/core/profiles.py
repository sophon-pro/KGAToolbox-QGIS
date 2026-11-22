# -*- coding: utf-8 -*-
"""Layout profiles: named snapshots of which panels/toolbars are open and where they sit.

A profile stores two things:
* ``state``   - QMainWindow.saveState(), which restores positions, docking and tab order.
* ``visible`` - an explicit {key: bool} map, applied afterwards so visibility is exact even
                for widgets created by plugins that load after the profile was saved.

Profiles live in QgsSettings and can be exported/imported as JSON, which makes it easy to
give a whole class the same workspace.
"""
import json

from qgis.PyQt.QtCore import QByteArray
from qgis.core import QgsSettings

SETTINGS_KEY = "KGA/ui_manager/profiles"
FILE_FORMAT = "kga_tools.ui_manager.profiles"
FILE_VERSION = 1


class ProfileStore:
    def __init__(self):
        self.settings = QgsSettings()

    # Storage -------------------------------------------------------------
    def all(self):
        raw = self.settings.value(SETTINGS_KEY, "{}")
        try:
            data = json.loads(raw) if isinstance(raw, str) else {}
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    def names(self):
        return sorted(self.all(), key=str.lower)

    def _write(self, data):
        self.settings.setValue(SETTINGS_KEY, json.dumps(data))

    # Operations ----------------------------------------------------------
    def save(self, name, main_window, items):
        data = self.all()
        data[name] = {
            "state": bytes(main_window.saveState().toBase64()).decode("ascii"),
            "visible": {item.key: item.is_visible() for item in items},
        }
        self._write(data)

    def delete(self, name):
        data = self.all()
        data.pop(name, None)
        self._write(data)

    def apply(self, name, main_window, items):
        profile = self.all().get(name)
        if not profile:
            return False
        state = profile.get("state")
        if state:
            main_window.restoreState(QByteArray.fromBase64(state.encode("ascii")))
        visible = profile.get("visible", {})
        for item in items:
            if item.key in visible:
                try:
                    item.set_visible(bool(visible[item.key]))
                except RuntimeError:  # widget deleted by another plugin meanwhile
                    pass
        return True

    # Exchange ------------------------------------------------------------
    def export_to(self, path, names=None):
        data = self.all()
        if names:
            data = {n: data[n] for n in names if n in data}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"format": FILE_FORMAT, "version": FILE_VERSION, "profiles": data}, fh, indent=2)
        return len(data)

    def import_from(self, path):
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        if not isinstance(payload, dict) or payload.get("format") != FILE_FORMAT:
            raise ValueError("This file is not a Panels and Toolbars Manager layout file.")
        incoming = payload.get("profiles", {})
        data = self.all()
        data.update({str(k): v for k, v in incoming.items() if isinstance(v, dict)})
        self._write(data)
        return len(incoming)

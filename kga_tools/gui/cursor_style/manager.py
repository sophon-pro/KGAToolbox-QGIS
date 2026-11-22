"""Application service: owns the applied state, profiles and persistence and
feeds the controller. The UI only talks to this class.

Two things decide the cursor of a map tool:

* the **main style** (``style`` / ``active_name``): the one edited in the panel
  and set by clicking a preset;
* the **assignment** of the tool's kind (``assignments[category]``): the QGIS
  cursor, the main style, or the name of a preset / saved style.
"""

from collections import OrderedDict

from qgis.PyQt.QtCore import QObject, pyqtSignal

from .presets import DEFAULT_PRESET, builtin_style, is_builtin, builtin_names
from .controller import (CursorController, DEFAULT_ASSIGNMENTS, ASSIGN_MAIN,
                         ASSIGN_QGIS)
from .settings import SettingsStore, clean_name, MAX_PROFILES


class ProfileError(ValueError):
    pass


class CursorManager(QObject):
    stateChanged = pyqtSignal()      # main style / enabled / assignments
    profilesChanged = pyqtSignal()

    def __init__(self, iface, store=None, parent=None):
        super().__init__(parent)
        self.store = store or SettingsStore()
        self.controller = CursorController(iface, parent=self)
        (self.active_name, self.style, self.assignments, self.enabled,
         self.profiles) = self.store.load_state()

    # ------------------------------------------------------------ lifecycle
    def start(self):
        self.controller.set_config(self.resolved_styles(), self.enabled)
        self.controller.start()

    def stop(self):
        self.controller.stop()

    # -------------------------------------------------------------- applying
    def apply(self, name, style):
        """Make a style the main style and persist it."""
        self.active_name = name
        self.style = style
        self._push()

    def set_enabled(self, enabled):
        if bool(enabled) != self.enabled:
            self.enabled = bool(enabled)
            self._push()

    def set_assignment(self, category, value):
        """value: ASSIGN_QGIS, ASSIGN_MAIN or a preset / saved style name."""
        if category in self.assignments and self.assignments[category] != value:
            self.assignments[category] = value
            self._push()

    def resolved_styles(self):
        """{category: (display name, CursorStyle) or None for the QGIS cursor}."""
        return {cat: self.resolve(value) for cat, value in self.assignments.items()}

    def resolve(self, value):
        if value == ASSIGN_QGIS:
            return None
        if value != ASSIGN_MAIN:
            if value == self.active_name and self.style_for(value) is not None:
                # the style being edited: follow the live edits, not the saved copy
                return value, self.style
            style = self.style_for(value)
            if style is not None:
                return value, style
            # an unknown name (deleted elsewhere) falls back to the main style
        return self.main_label(), self.style

    def main_label(self):
        name = self.active_name if self.style_for(self.active_name) is not None else "Custom"
        return f"Main style ({name})"

    def _push(self):
        self.controller.set_config(self.resolved_styles(), self.enabled)
        self._save_state()
        self.stateChanged.emit()

    def _save_state(self):
        self.store.save_state(self.active_name, self.style, self.assignments, self.enabled)

    # -------------------------------------------------------------- profiles
    def all_names(self):
        return builtin_names() + list(self.profiles.keys())

    def style_for(self, name):
        if is_builtin(name):
            return builtin_style(name)
        return self.profiles.get(name)

    def check_new_name(self, name, allow=None):
        name = clean_name(name)
        if not name:
            raise ProfileError("Please enter a name.")
        if name in (ASSIGN_MAIN, ASSIGN_QGIS, "Custom"):
            raise ProfileError(f"“{name}” is reserved. Choose another name.")
        if is_builtin(name):
            raise ProfileError(f"“{name}” is a built-in preset name. Choose another name.")
        if name in self.profiles and name != allow:
            raise ProfileError(f"A style named “{name}” already exists.")
        if name not in self.profiles and len(self.profiles) >= MAX_PROFILES:
            raise ProfileError(f"You can keep up to {MAX_PROFILES} custom styles.")
        return name

    def categories_using(self, name):
        return [cat for cat, value in self.assignments.items() if value == name]

    def save_profile(self, name, style, overwrite=False):
        name = clean_name(name) if overwrite else self.check_new_name(name)
        if is_builtin(name):
            raise ProfileError("Built-in presets cannot be overwritten.")
        self.profiles[name] = style
        self.store.save_profiles(self.profiles)
        self.profilesChanged.emit()
        if self.categories_using(name):
            self._push()  # kinds of tool using this style get the saved version
        return name

    def rename_profile(self, old, new):
        if old not in self.profiles:
            raise ProfileError("Only your own styles can be renamed.")
        new = self.check_new_name(new, allow=old)
        if new == old:
            return old
        self.profiles = OrderedDict((new if k == old else k, v) for k, v in self.profiles.items())
        self.store.save_profiles(self.profiles)
        if self.active_name == old:
            self.active_name = new
        for cat in self.categories_using(old):
            self.assignments[cat] = new
        self.profilesChanged.emit()
        self._push()
        return new

    def delete_profile(self, name):
        """Delete a saved style. Kinds of tool that used it fall back to the
        main style; returns those categories so the UI can say so."""
        if name not in self.profiles:
            raise ProfileError("Built-in presets cannot be deleted.")
        del self.profiles[name]
        self.store.save_profiles(self.profiles)
        moved = self.categories_using(name)
        for cat in moved:
            self.assignments[cat] = ASSIGN_MAIN
        if self.active_name == name:
            # keep the cursor the user is looking at, but mark it unsaved
            self.active_name = "Custom"
        self.profilesChanged.emit()
        self._push()
        return moved

    def restore_defaults(self, delete_custom=False):
        """Back to the passive QGIS Default preset and default assignments."""
        self.assignments = dict(DEFAULT_ASSIGNMENTS)
        self.enabled = True
        if delete_custom:
            self.profiles = OrderedDict()
            self.store.save_profiles(self.profiles)
            self.profilesChanged.emit()
        self.apply(DEFAULT_PRESET, builtin_style(DEFAULT_PRESET))

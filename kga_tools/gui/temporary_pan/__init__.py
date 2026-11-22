# -*- coding: utf-8 -*-
"""Temporary pan while digitizing: hold a key, drag with the left button.

Ported from the standalone Temporary Pan While Digitizing plugin. The map tool
is never switched, so a half-drawn line or polygon survives the pan. Switched on
and off (and the key chosen) in Settings > Appearance > Map navigation.
"""

from contextlib import suppress

from ...core import kga_settings as settings
from .controller import TemporaryPanController
from .keys import DEFAULT_KEY, check_key, key_name

__all__ = ['TemporaryPan', 'DEFAULT_KEY', 'check_key', 'key_name']


class TemporaryPan:
    """Switch the temporary pan on or off to follow the Settings window."""

    def __init__(self, iface):
        self.iface = iface
        self.controller = TemporaryPanController(self._key())

    @staticmethod
    def _key():
        key = settings.temp_pan_key()
        return key if check_key(key)[0] else DEFAULT_KEY

    def _canvases(self):
        canvases = [self.iface.mapCanvas()]
        with suppress(Exception):
            canvases += [c for c in self.iface.mapCanvases()
                         if all(c is not k for k in canvases)]
        return [c for c in canvases if c is not None]

    @property
    def enabled(self):
        return self.controller is not None and self.controller.enabled

    def refresh(self):
        """Follow Settings: on/off and the pan key."""
        if self.controller is None:
            return
        key = self._key()
        if key != self.controller.key:
            self.controller.set_key(key)
        if settings.temp_pan_enabled():
            self.controller.enable(self._canvases())
            for canvas in self._canvases():     # extra map views opened since
                self.controller.attach(canvas)
        else:
            self.controller.disable()

    def uninstall(self):
        if self.controller is None:
            return
        with suppress(Exception):
            self.controller.disable()
        self.controller.deleteLater()
        self.controller = None

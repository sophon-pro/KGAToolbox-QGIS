# -*- coding: utf-8 -*-
"""Pre-fill a dialog's output field with the project's default location.

A KGA dialog that asks "Save to" / "Output folder" uses a `FollowingField` on
that `QgsFileWidget`: when the dialog opens, and whenever the thing being made
changes, the field is filled with a new, unused path in the project's default
folder (or the folder itself), the way ArcGIS Pro proposes an output in the
project geodatabase. A path the user typed or browsed to is never replaced; one
the dialog proposed itself is, so a second run does not offer the file the first
run just wrote.

Without a project (see `core.output_defaults`) the field is left as it was.
"""

import os

from ..core import output_defaults as od


def _same(a, b):
    return os.path.normcase(os.path.normpath(a)) == \
        os.path.normcase(os.path.normpath(b))


class FollowingField(object):
    """Keeps a `QgsFileWidget` (or a line edit) at the project until the user says no."""

    def __init__(self, widget, extension=''):
        self.widget = widget
        self.extension = extension
        self.last = ''

    def _path(self):
        read = getattr(self.widget, 'filePath', None) or self.widget.text
        return (read() or '').strip()

    def _write(self, path):
        write = getattr(self.widget, 'setFilePath', None) or self.widget.setText
        write(path)

    def is_ours(self):
        """True while the field is empty or still holds what we proposed."""
        current = self._path()
        return not current or (bool(self.last) and _same(current, self.last))

    def _put(self, path):
        self._write(path)
        self.last = self._path()

    def suggest(self, stem, extension=None):
        """Propose `<project folder>/<stem>.<ext>` unless the user has chosen."""
        if not self.is_ours():
            return False
        path = od.default_file(stem, self.extension if extension is None
                               else extension)
        if not path:
            return False
        self._put(path)
        return True

    def suggest_path(self, path):
        """Put an exact `path` (e.g. the default database) there if still ours."""
        if not path or not self.is_ours():
            return False
        self._put(path)
        return True

    def suggest_folder(self):
        """Propose the project's default folder unless the user has chosen."""
        if not self.is_ours():
            return False
        folder = od.project_folder()
        if not folder:
            return False
        self._put(folder)
        return True

    def retarget(self, stem, extension=None):
        """The project may have changed: move our own proposal to the new one."""
        if self.is_ours():
            self._write('')
            self.last = ''
            self.suggest(stem, extension)

    def retarget_folder(self):
        """As `retarget`, for a field that holds a folder."""
        if self.is_ours():
            self._write('')
            self.last = ''
            self.suggest_folder()

    def renew(self, stem, extension=None):
        """Before a run: replace our own proposal if the file now exists."""
        current = self._path()
        if current and not (self.is_ours() and os.path.exists(current)):
            return
        self._write('')
        self.last = ''
        self.suggest(stem, extension)

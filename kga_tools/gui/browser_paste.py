# -*- coding: utf-8 -*-
"""Copy and Paste on the QGIS Browser's right-click menu.

QGIS lets a plugin add to the Browser's context menu through a
`QgsDataItemGuiProvider`. The Browser panel and the Data Source Manager's Browser
tab share that registry, so one provider covers both.

* Right-click one or more layers (or a whole GeoPackage / File Geodatabase) and
  choose **Copy**.
* Right-click a folder, a GeoPackage or a File Geodatabase and choose **Paste**.

The work itself is in `core/data_paste.py`.
"""

import traceback
from contextlib import suppress
from functools import partial

from qgis.core import (
    Qgis,
    QgsDirectoryItem,
    QgsLayerItem,
    QgsMessageLog,
    QgsProject,
    QgsRasterLayer,
    QgsVectorLayer,
)
from qgis.gui import QgsBrowserTreeView, QgsDataItemGuiProvider, QgsGui
from qgis.PyQt.QtCore import QEvent, QObject, Qt, QTimer
from qgis.PyQt.QtGui import QKeySequence
from qgis.PyQt.QtWidgets import (
    QAction,
    QApplication,
    QMessageBox,
    QProgressDialog,
)

from ..branding import LOG_TAG
from ..branding import icon as _icon
from ..core import data_paste as dp
from ..core import gdb_convert as gc
from .paste_dialog import PasteDialog, needs_dialog, remembered_options
from ..core.compat import enum_value


def _is_vector_type(layer_type):
    """True for a vector map-layer type, whichever enum this QGIS spells it in."""
    from qgis import core
    return layer_type == enum_value((Qgis, 'LayerType.Vector'), (core, 'QgsMapLayerType.VectorLayer'))


def _log(message, level=Qgis.MessageLevel.Warning):
    QgsMessageLog.logMessage(message, LOG_TAG, level)


def sources_from_items(items):
    """`Source` objects for whatever can be copied among the Browser `items`.

    A layer is copied as itself; a GeoPackage or File Geodatabase is copied as
    every layer it holds. Anything else in the selection is left out.
    """
    sources = []
    for item in items:
        if isinstance(item, QgsLayerItem):
            provider = item.providerKey()
            if provider == 'gdal':
                kind = dp.KIND_RASTER
            elif _is_vector_type(item.mapLayerType()):
                kind = dp.KIND_VECTOR
            else:
                continue
            sources.append(dp.Source(item.name(), item.uri(), provider, kind))
            continue
        path = dp.container_path(item.path())
        if path:
            sources.extend(_container_sources(path))
    return sources


def other_layer_uris(items):
    """Mime URIs of the selected layers that cannot be copied or pasted.

    Web services (WMS, XYZ, vector tiles, ...), meshes, point clouds and the
    like are not data a paste can write, but they can still go on the map.
    """
    uris = []
    for item in items:
        if not isinstance(item, QgsLayerItem):
            continue
        provider = item.providerKey()
        if provider == 'gdal' or _is_vector_type(item.mapLayerType()):
            continue
        with suppress(Exception):
            uris.extend(u for u in item.mimeUris() if u.isValid())
    return uris


def layer_from_uri(uri):
    """The map layer a mime URI describes, or None when its type is unknown."""
    from qgis import core
    classes = {'raster': 'QgsRasterLayer', 'vector': 'QgsVectorLayer',
               'mesh': 'QgsMeshLayer', 'vector-tile': 'QgsVectorTileLayer',
               'pointcloud': 'QgsPointCloudLayer',
               'tiled-scene': 'QgsTiledSceneLayer'}
    cls = getattr(core, classes.get(uri.layerType, ''), None)
    if cls is None:
        return None
    if uri.layerType == 'vector-tile':
        return cls(uri.uri, uri.name)
    return cls(uri.uri, uri.name, uri.providerKey)


def _container_sources(path):
    try:
        layers, _domains = gc.describe(path, path.lower().endswith(gc.GDB_SUFFIX))
    except Exception as error:
        _log('Could not list the layers in {}: {}'.format(path, error))
        return []
    return [dp.Source(info.name, '{}|layername={}'.format(path, info.name),
                      'ogr') for info in layers]


class BrowserCopyPaste(QgsDataItemGuiProvider):

    def __init__(self, iface):
        super().__init__()
        self.iface = iface

    def name(self):
        return 'kga_copy_paste'

    # ------------------------------------------------------------- menu

    def populateContextMenu(self, item, menu, selected_items, context):
        try:
            self._populate(item, menu, selected_items)
        except Exception:
            # A menu that fails to build is far worse than one without our
            # entries, so log and carry on.
            _log('Could not add Copy / Paste to the Browser menu:\n{}'.format(
                traceback.format_exc()))

    def _populate(self, item, menu, selected_items):
        selected = list(selected_items or [])
        if not any(sel is item for sel in selected):
            selected = [item]

        # Ours go above QGIS's own entries, which are already in the menu.
        existing = menu.actions()
        first = existing[0] if existing else None
        added = False
        last = None

        sources = sources_from_items(selected)
        extras = other_layer_uris(selected)
        if sources or extras:
            add = self._add(menu, first, 'addToMap.png', 'Add to Map')
            last = add
            add.setToolTip('Add {} layer(s) to the map'.format(
                len(sources) + len(extras)))
            add.triggered.connect(partial(self._add_later, sources, extras))
            added = True

        if sources:
            copy = self._add(menu, first, 'copyInBrowser.png', 'Copy')
            last = copy
            copy.setToolTip('Copy {} item(s) so they can be pasted into a '
                            'folder, GeoPackage or File Geodatabase'.format(
                                len(sources)))
            copy.triggered.connect(partial(self.copy, sources))
            added = True

        held = dp.CLIPBOARD.sources()
        target = self.paste_target(selected)
        if target is not None:
            text = 'Paste' if len(held) <= 1 else 'Paste ({})'.format(len(held))
            paste = self._add(menu, first, 'pasteInBrowser.png', text)
            paste.setEnabled(bool(held))
            paste.setToolTip('Paste {} into {}'.format(
                ', '.join(s.name for s in held[:3]) +
                (' ...' if len(held) > 3 else ''), target.label)
                if held else 'Nothing copied yet: right-click layers and '
                'choose Copy first')
            # Deferred, so the menu has gone before a window comes up.
            paste.triggered.connect(partial(
                self._paste_later, target.path, item.path(), None))
            last = paste
            added = True

            if target.kind == dp.TARGET_FOLDER:
                # The common case, one click: no format window.
                as_shp = self._add(menu, first, 'pasteInBrowser.png',
                                   'Paste as Shapefile')
                as_shp.setEnabled(bool(held))
                as_shp.setToolTip('Paste into {} as shapefiles'.format(
                    target.label))
                as_shp.triggered.connect(partial(
                    self._paste_later, target.path, item.path(),
                    dp.FMT_SHAPEFILE))
                last = as_shp

        if added:
            # Other providers add their entries after this one returns, so the
            # divider is placed as the menu opens, when the list is complete.
            menu.aboutToShow.connect(partial(self._separate_after, menu, last))

    @staticmethod
    def paste_target(selected):
        """The `Target` a paste would go into for this selection, or None."""
        if len(selected) != 1:
            return None
        item = selected[0]
        # A layer inside a GeoPackage reports the .gpkg as its path, but it is
        # the container that takes a paste, not one of its layers.
        if isinstance(item, QgsLayerItem):
            return None
        # A folder knows its real directory; `path()` can be a Browser-internal
        # address (Favorites, Project Home) rather than a place on disk.
        candidates = []
        if isinstance(item, QgsDirectoryItem):
            candidates.append(item.dirPath())
        candidates.append(item.path())
        for path in candidates:
            target = dp.target_for_path(path)
            if target is not None:
                return target
        if isinstance(item, QgsDirectoryItem):
            _log('Paste: could not use the folder item {!r} (paths tried: '
                 '{}).'.format(item.name(), candidates))
        return None

    def copy_selection(self, selected):
        """Ctrl+C: copy what is selected. False when nothing there can be."""
        sources = sources_from_items(selected)
        if not sources:
            return False
        self.copy(sources)
        return True

    def paste_selection(self, selected):
        """Ctrl+V: paste into the selected item. False when it cannot take one."""
        target = self.paste_target(selected)
        if target is None or not dp.CLIPBOARD.sources():
            return False
        self._paste_later(target.path, selected[0].path(), None)
        return True

    @staticmethod
    def _separate_after(menu, action):
        """A separator straight after `action`, unless one is already there.

        Nothing is added when `action` is last: a divider with nothing below
        it is just a stray line.
        """
        actions = menu.actions()
        if action not in actions:
            return
        position = actions.index(action) + 1
        if position < len(actions) and not actions[position].isSeparator():
            menu.insertSeparator(actions[position])

    @staticmethod
    def _add(menu, before, icon_file, text):
        action = QAction(_icon(icon_file), text, menu)
        if before is None:
            menu.addAction(action)
        else:
            menu.insertAction(before, action)
        return action

    # ------------------------------------------------------------ actions

    def copy(self, sources, _checked=False):
        dp.CLIPBOARD.set(sources)
        count = len(dp.CLIPBOARD)
        self.iface.messageBar().pushInfo(
            'KGA Toolbox',
            'Copied {} item{}. Right-click a folder, GeoPackage or File '
            'Geodatabase and choose Paste.'.format(count, '' if count == 1
                                                   else 's'),
        )

    def _add_later(self, sources, extras=(), _checked=False):
        QTimer.singleShot(0, partial(self.add_to_map, sources, extras))

    def add_to_map(self, sources, extras=()):
        """Add each source to the project as a layer; report what failed."""
        project = QgsProject.instance()
        added, failed = 0, []
        for uri in extras:
            try:
                layer = layer_from_uri(uri)
                if layer is None or not layer.isValid():
                    failed.append(uri.name)
                    continue
                project.addMapLayer(layer)
                added += 1
            except Exception:
                _log('Add to Map failed for {}: {}'.format(
                    uri.name, traceback.format_exc()))
                failed.append(uri.name)
        for src in sources:
            try:
                if src.kind == dp.KIND_RASTER:
                    layer = QgsRasterLayer(src.uri, src.name, src.provider)
                else:
                    layer = QgsVectorLayer(src.uri, src.name, src.provider)
                if not layer.isValid():
                    failed.append(src.name)
                    continue
                project.addMapLayer(layer)
                added += 1
            except Exception:
                _log('Add to Map failed for {}:\n{}'.format(
                    src.name, traceback.format_exc()))
                failed.append(src.name)
        bar = self.iface.messageBar()
        if failed:
            bar.pushWarning('KGA Toolbox', 'Could not add: {}'.format(
                ', '.join(failed[:5]) + (' ...' if len(failed) > 5 else '')))
        elif added > 1:
            bar.pushSuccess('KGA Toolbox', 'Added {} layers to the map.'
                            .format(added))

    def _paste_later(self, target_path, browser_path, fmt, _checked=False):
        QTimer.singleShot(
            0, partial(self.paste, target_path, browser_path, fmt))

    def paste(self, target_path, browser_path, fmt=None):
        """Paste the clipboard. `fmt` skips the format question for a folder."""
        sources = dp.CLIPBOARD.sources()
        target = dp.target_for_path(target_path)
        window = self.iface.mainWindow()
        try:
            options = remembered_options()
            if fmt:
                options.format = fmt
            why = dp.problem(sources, target, options)
            if why:
                self.iface.messageBar().pushWarning('KGA Toolbox', why)
                return
            if needs_dialog(sources, target, options, direct=bool(fmt)):
                dialog = PasteDialog(sources, target, window, fmt=fmt)
                if dialog.exec() != PasteDialog.DialogCode.Accepted:
                    return
                options = dialog.options()
            result = self._run(sources, target, options, window)
        except dp.PasteError as error:
            self.iface.messageBar().pushWarning('KGA Toolbox', str(error))
            return
        except Exception as error:
            _log('Paste failed:\n{}'.format(traceback.format_exc()),
                 Qgis.MessageLevel.Critical)
            self.iface.messageBar().pushCritical(
                'KGA Toolbox', 'Paste failed: {}'.format(error))
            return
        self._refresh_browser(browser_path, target.path)
        self._report(result, target, window)

    def _run(self, sources, target, options, window):
        progress = QProgressDialog('Pasting...', 'Cancel', 0, 100, window)
        progress.setWindowTitle('Paste')
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setMinimumWidth(380)

        def on_progress(percent, text):
            progress.setValue(int(percent))
            if text:
                progress.setLabelText(text)
            QApplication.processEvents()

        feedback = dp.Feedback(on_progress=on_progress,
                               on_tick=QApplication.processEvents)
        progress.canceled.connect(feedback.cancel)
        progress.show()
        try:
            return dp.paste(sources, target, options, feedback)
        finally:
            progress.close()
            progress.deleteLater()

    def _refresh_browser(self, *paths):
        """Make the Browser show what was written."""
        try:
            model = self.iface.browserModel()
        except Exception:
            return
        for path in paths:
            with suppress(Exception):
                model.refresh(path)

    def _report(self, result, target, window):
        where = result.container or target.path
        label = dp.Target(target.kind, where).label
        bar = self.iface.messageBar()
        plural = '' if result.count == 1 else 's'

        renamed = ['{}: "{}" -> "{}"'.format(*row)
                   for row in result.renamed_fields]
        if renamed:
            _log('Field names changed to fit the target format:\n  {}'.format(
                '\n  '.join(renamed)), Qgis.MessageLevel.Info)

        if result.canceled:
            bar.pushWarning(
                'KGA Toolbox',
                'Paste canceled. {} item{} written to {} before it stopped.'
                .format(result.count, plural, label))
        elif not (result.skipped or result.failed):
            if not result.count:
                bar.pushInfo('KGA Toolbox', 'Nothing was pasted.')
                return
            # Shapefile's 10-character limit makes this routine, so it is a
            # note on the success message rather than a window to dismiss.
            note = '' if not renamed else ' {} field name{} shortened to fit; ' \
                'see the KGA Toolbox log.'.format(
                    len(renamed), '' if len(renamed) == 1 else 's')
            bar.pushSuccess('KGA Toolbox', 'Pasted {} item{} into {}.{}'.format(
                result.count, plural, label, note))
            return
        if not (result.skipped or result.failed):
            return

        lines = []
        if result.count:
            lines.append('Pasted {} into {}.'.format(result.count, label))
        for name, reason in result.skipped:
            lines.append('Skipped {}: {}'.format(name, reason))
        for name, message in result.failed:
            lines.append('FAILED {}: {}'.format(name, message))
        if renamed:
            lines.append('')
            lines.append('Field names changed to fit the target format:')
            lines.extend('  ' + row for row in renamed)
        box = QMessageBox(
            QMessageBox.Icon.Warning if (result.failed or not result.count)
            else QMessageBox.Icon.Information,
            'Paste', 'Paste finished with notes.', parent=window)
        box.setDetailedText('\n'.join(lines))
        box.setInformativeText('Pasted {}, skipped {}, failed {}.'.format(
            result.count, len(result.skipped), len(result.failed)))
        box.exec()


def selected_items(tree):
    """The data items selected in a Browser tree view."""
    model = tree.model()
    items = []
    for index in tree.selectionModel().selectedRows() \
            if tree.selectionModel() is not None else []:
        item = model.dataItem(index)
        if item is not None:
            items.append(item)
    return items


class BrowserShortcuts(QObject):
    """Ctrl+C and Ctrl+V in any Browser tree: the panel and the Data Source Manager.

    One application-wide filter rather than one per tree, because the Data
    Source Manager builds its Browser tab only when it is first opened.
    """

    def __init__(self, provider, parent=None):
        super().__init__(parent)
        self.provider = provider

    def eventFilter(self, obj, event):          # noqa: N802 - Qt naming
        if event.type() != QEvent.Type.KeyPress \
                or not isinstance(obj, QgsBrowserTreeView):
            return False
        try:
            if event.matches(QKeySequence.StandardKey.Copy):
                return self.provider.copy_selection(selected_items(obj))
            if event.matches(QKeySequence.StandardKey.Paste):
                return self.provider.paste_selection(selected_items(obj))
        except Exception:
            _log('Browser shortcut failed:\n{}'.format(traceback.format_exc()))
        return False


class BrowserPasteInstaller(object):
    """Registers the provider on load and takes it out again on unload."""

    def __init__(self, iface):
        self.iface = iface
        self.provider = None
        self.shortcuts = None

    def install(self):
        if self.provider is not None:
            return
        self.provider = BrowserCopyPaste(self.iface)
        QgsGui.dataItemGuiProviderRegistry().addProvider(self.provider)
        self.shortcuts = BrowserShortcuts(self.provider)
        QApplication.instance().installEventFilter(self.shortcuts)

    def uninstall(self):
        if self.provider is None:
            return
        try:
            if self.shortcuts is not None:
                app = QApplication.instance()
                if app is not None:
                    app.removeEventFilter(self.shortcuts)
                self.shortcuts.deleteLater()
            QgsGui.dataItemGuiProviderRegistry().removeProvider(self.provider)
        finally:
            self.provider = None
            self.shortcuts = None
            dp.CLIPBOARD.clear()

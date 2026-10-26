# -*- coding: utf-8 -*-
"""A Pro-style start page in place of QGIS's own welcome page.

QGIS shows `QgsWelcomePage` (recent projects, news, templates) in the main
window's central stack whenever no project is open. `StartPageInstaller` keeps
that widget - QGIS still owns it and switches to it - but moves its contents
into a hidden holder and puts `StartPage` in their place: a left rail (New /
Open / Settings / Help / About / Exit) and two pages, *New* (blank map,
workspace, package, templates) and *Open* (recent projects as cards, with find,
sort, grid / list and a "..." menu on each card).

Recent projects are read from, and pin / remove written back to, the
`UI/recentProjects/<n>/{path,title,previewImage,crs,pin}` settings QGIS itself
uses, so the Open Recent menu and this page stay one list. Colours come from the
application palette, so the Night Mapping theme works without any special case.
"""

import os
import traceback
from contextlib import contextmanager, suppress

from qgis.core import Qgis, QgsApplication, QgsMessageLog, QgsProject, QgsSettings
from qgis.PyQt.QtCore import QDateTime, QLocale, QObject, QSize, Qt, QTimer, QUrl
from qgis.PyQt.QtGui import QDesktopServices, QKeySequence, QPixmap
from qgis.PyQt.QtWidgets import (
    QMenuBar,
    QAction,
    QButtonGroup,
    QDockWidget,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QToolBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..branding import KGA_WEBSITE, LOG_TAG
from ..core import kga_settings as ks
from ..branding import icon as _icon

RECENT_GROUP = 'UI/recentProjects'
PROJECT_SUFFIXES = ('.qgz', '.qgs')

SORT_RECENT = 'recent'
SORT_NAME = 'name'
SORT_MODIFIED = 'modified'
SETTING_SORT = 'KGA/startpage/sort'
SETTING_VIEW = 'KGA/startpage/view'
#: Panels hidden for the start page; kept so a restart (QGIS saves the layout
#: with them closed) still brings them back with the first project.
SETTING_HIDDEN = 'KGA/startpage/hidden_docks'
VIEW_GRID = 'grid'
VIEW_LIST = 'list'

CARD_WIDTH = 214
THUMB_HEIGHT = 112
GAP = 14
RECENT_ON_NEW = 4
#: How long the blank start-up project is kept clean (after QGIS says it is up / at most).
START_UP_SETTLE_MS = 6000
START_UP_LIMIT_MS = 12000


def _log(message, level=Qgis.MessageLevel.Warning):
    QgsMessageLog.logMessage(message, LOG_TAG, level)


def _native(path):
    return os.path.normpath(path) if path else path


def theme_icon(name):
    return QgsApplication.getThemeIcon(name)


def settle_blank_project():
    """Forget 'unsaved changes' on an untitled project with no layers.

    QGIS marks the blank start-up project changed (settings, scratch workspace),
    which would otherwise ask "save the current project?" before anything the
    start page opens.
    """
    project = QgsProject.instance()
    if not project.fileName() and not project.mapLayers():
        project.setDirty(False)


# --------------------------------------------------------------------- recents

class RecentProject:
    """One row of the recent-projects settings."""

    def __init__(self, key, path, title, preview, crs, pinned, order):
        self.key = key
        self.path = path
        self.title = title or os.path.splitext(os.path.basename(path))[0]
        self.preview = preview
        self.crs = crs
        self.pinned = pinned
        self.order = order

    @property
    def is_file(self):
        return not ('://' in self.path or self.path.startswith('postgresql'))

    @property
    def exists(self):
        return (not self.is_file) or os.path.isfile(self.path)

    @property
    def modified(self):
        """QDateTime of the file, or None (database projects, missing files)."""
        if not self.is_file or not os.path.isfile(self.path):
            return None
        with suppress(OSError):
            return QDateTime.fromSecsSinceEpoch(int(os.path.getmtime(self.path)))
        return None


def recent_projects():
    """Recent projects in QGIS's own order (most recent first)."""
    settings = QgsSettings()
    settings.beginGroup(RECENT_GROUP)
    found = []
    for key in settings.childGroups():
        settings.beginGroup(key)
        path = str(settings.value('path', '') or '')
        if path:
            try:
                order = int(key)
            except ValueError:
                order = 10 ** 6
            found.append(RecentProject(
                key, path, str(settings.value('title', '') or ''),
                str(settings.value('previewImage', '') or ''),
                str(settings.value('crs', '') or ''),
                settings.value('pin', False, type=bool), order))
        settings.endGroup()
    settings.endGroup()
    found.sort(key=lambda r: r.order)
    return found


def set_pinned(key, pinned):
    settings = QgsSettings()
    path = '{}/{}/pin'.format(RECENT_GROUP, key)
    if pinned:
        settings.setValue(path, True)
    else:
        settings.remove(path)


def forget(key):
    QgsSettings().remove('{}/{}'.format(RECENT_GROUP, key))


def forget_path(path):
    """Drop every entry for `path` (a template opened as a new project)."""
    norm = os.path.normcase(os.path.normpath(path))
    for item in recent_projects():
        if os.path.normcase(os.path.normpath(item.path)) == norm:
            forget(item.key)


def sorted_projects(items, mode, text=''):
    text = text.strip().lower()
    if text:
        items = [r for r in items
                 if text in r.title.lower() or text in r.path.lower()]
    if mode == SORT_NAME:
        items = sorted(items, key=lambda r: r.title.lower())
    elif mode == SORT_MODIFIED:
        items = sorted(items, key=lambda r: (r.modified or QDateTime()).toSecsSinceEpoch(),
                       reverse=True)
    return sorted(items, key=lambda r: not r.pinned)


def template_files():
    """(name, path) of every project template QGIS would offer."""
    folders = [os.path.join(QgsApplication.qgisSettingsDirPath(),
                            'project_templates'),
               os.path.join(QgsApplication.pkgDataPath(), 'project_templates'),
               os.path.join(QgsApplication.pkgDataPath(), 'resources',
                            'project_templates')]
    seen, found = set(), []
    for folder in folders:
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            stem, suffix = os.path.splitext(name)
            path = os.path.join(folder, name)
            if suffix.lower() in PROJECT_SUFFIXES and path not in seen:
                seen.add(path)
                found.append((stem, path))
    return found


# --------------------------------------------------------------------- widgets

class Card(QFrame):
    """A recent project: thumbnail, name, modified date and path."""

    def __init__(self, item, mode, page):
        super().__init__()
        self.item = item
        self.page = page
        self.setObjectName('card')
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(_native(item.path))
        listing = mode == VIEW_LIST

        thumb = QLabel()
        thumb.setObjectName('thumb')
        thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        size = QSize(112, 64) if listing else QSize(CARD_WIDTH - 2, THUMB_HEIGHT)
        thumb.setFixedSize(size)
        pixmap = QPixmap(item.preview) if item.preview else QPixmap()
        if pixmap.isNull():
            pixmap = theme_icon('/mIconQgsProjectFile.svg').pixmap(
                QSize(44, 44) if listing else QSize(72, 72))
        else:
            pixmap = pixmap.scaled(size, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                   Qt.TransformationMode.SmoothTransformation)
        thumb.setPixmap(pixmap)

        if item.pinned:
            badge = QLabel('★ Pinned', thumb)
            badge.setObjectName('badge')
            badge.move(6, 6)
            metrics = badge.fontMetrics()
            badge.setFixedSize(metrics.horizontalAdvance(badge.text()) + 28,
                               metrics.height() + 6)
        title = QLabel(item.title)
        title.setObjectName('cardTitle')
        title.setProperty('missing', not item.exists)
        modified = item.modified
        if not item.exists:
            when = 'Not found'
        elif modified is not None:
            when = QLocale.system().toString(modified, QLocale.FormatType.ShortFormat)
        else:
            when = ''
        meta = QLabel('<b>Modified:</b> {}'.format(when))
        meta.setObjectName('cardMeta')
        where = QLabel('<b>Path:</b> {}'.format(_native(item.path)))
        where.setObjectName('cardMeta')
        where.setMinimumWidth(10)
        where.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

        more = QToolButton()
        more.setObjectName('more')
        more.setText('•••')
        more.setCursor(Qt.CursorShape.ArrowCursor)
        more.setToolTip('More')
        more.clicked.connect(self._show_menu)
        self._more = more

        text = QVBoxLayout()
        text.setContentsMargins(10, 8, 6, 4)
        text.setSpacing(3)
        text.addWidget(title)
        text.addWidget(meta)
        text.addWidget(where)

        if listing:
            self.setFixedHeight(76)
            row = QHBoxLayout(self)
            row.setContentsMargins(6, 5, 6, 5)
            row.addWidget(thumb)
            row.addLayout(text, 1)
            row.addWidget(more, 0, Qt.AlignmentFlag.AlignVCenter)
        else:
            self.setFixedWidth(CARD_WIDTH)
            col = QVBoxLayout(self)
            col.setContentsMargins(1, 1, 1, 4)
            col.setSpacing(0)
            col.addWidget(thumb)
            col.addLayout(text)
            foot = QHBoxLayout()
            foot.setContentsMargins(0, 0, 6, 0)
            foot.addStretch(1)
            foot.addWidget(more)
            col.addLayout(foot)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
                event.pos()):
            self.page.open_recent(self.item)
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):
        self._menu().exec(event.globalPos())

    def _show_menu(self):
        self._menu().exec(self._more.mapToGlobal(self._more.rect().bottomLeft()))

    def _menu(self):
        item = self.item
        menu = QMenu(self)
        open_action = menu.addAction('Open')
        open_action.setEnabled(item.exists)
        open_action.triggered.connect(lambda: self.page.open_recent(item))
        pin = menu.addAction('Unpin' if item.pinned else 'Pin to top')
        pin.triggered.connect(lambda: self.page.pin(item))
        menu.addSeparator()
        folder = menu.addAction('Show in folder')
        folder.setEnabled(item.is_file and os.path.isdir(os.path.dirname(item.path)))
        folder.triggered.connect(lambda: self.page.show_in_folder(item))
        copy = menu.addAction('Copy path')
        copy.triggered.connect(lambda: QgsApplication.clipboard().setText(
            _native(item.path)))
        menu.addSeparator()
        remove = menu.addAction('Remove from list')
        remove.triggered.connect(lambda: self.page.remove(item))
        return menu


class Tile(QToolButton):
    """A big icon-over-text button, as on Pro's New page."""

    def __init__(self, icon, text, tip='', parent=None):
        super().__init__(parent)
        self.setObjectName('tile')
        self.setIcon(icon)
        self.setIconSize(QSize(56, 56))
        self.setText(text)
        self.setToolTip(tip)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(132, 112)


STYLE = """
#startPage, #pages, #page {{ background: palette(window); }}
#nav {{ background: palette(midlight); }}
#navBrand {{ font-size: 15px; font-weight: 600; color: {accent}; }}
#navButton {{ text-align: left; border: none; border-left: 3px solid transparent;
    padding: 11px 18px; font-size: 13px; background: transparent; }}
#navButton:hover {{ background: palette(window); }}
#navButton:checked {{ border-left: 3px solid {accent}; color: {accent};
    background: palette(window); }}
#navLine {{ background: palette(mid); max-height: 1px; min-height: 1px; }}
#heading {{ font-size: 30px; color: {accent}; padding-top: 4px; }}
#section {{ font-size: 19px; }}
#panel {{ background: palette(base); border-radius: 6px; }}
#tile {{ border: 1px solid transparent; border-radius: 4px; background: transparent;
    font-size: 12px; }}
#tile:hover {{ background: palette(window); border-color: {accent}; }}
#vline {{ background: palette(mid); max-width: 1px; min-width: 1px; }}
#card {{ background: palette(base); border: 1px solid palette(midlight);
    border-radius: 4px; }}
#card:hover {{ border: 1px solid {accent}; }}
#thumb {{ background: palette(midlight); border-top-left-radius: 3px;
    border-top-right-radius: 3px; }}
#cardTitle {{ color: {accent}; font-size: 14px; font-weight: 600; }}
#cardTitle[missing="true"] {{ color: palette(mid); text-decoration: line-through; }}
#badge {{ background: {accent}; color: white; border-radius: 3px; padding: 1px 6px;
    font-size: 11px; }}
#cardMeta {{ font-size: 11px; color: palette(text); }}
#more {{ border: none; background: transparent; color: palette(text); padding: 0 6px;
    font-size: 12px; }}
#more:hover {{ background: palette(midlight); border-radius: 3px; }}
#link {{ color: {accent}; border: none; background: transparent; text-align: left;
    padding: 0; font-size: 13px; }}
#link:hover {{ text-decoration: underline; }}
#find {{ padding: 3px 6px; border: 1px solid palette(mid); background: palette(base); }}
#flat {{ background: palette(base); border: 1px solid palette(midlight);
    border-radius: 3px; padding: 4px 10px; }}
#flat:hover {{ border-color: {accent}; }}
#flat:checked {{ background: palette(midlight); }}
#empty {{ font-size: 13px; color: palette(mid); }}
"""


def _accent(widget):
    dark = widget.palette().window().color().lightness() < 128
    return '#4da3e0' if dark else '#0079c1'


def _hline():
    line = QFrame()
    line.setObjectName('navLine')
    return line


# ------------------------------------------------------------------- the pages

class OpenPage(QWidget):
    """Recent projects as cards."""

    def __init__(self, page):
        super().__init__()
        self.setObjectName('page')
        self.page = page
        settings = QgsSettings()
        self.sort = settings.value(SETTING_SORT, SORT_RECENT, type=str)
        self.view = settings.value(SETTING_VIEW, VIEW_GRID, type=str)
        self._columns = 0

        head = QLabel('Open')
        head.setObjectName('heading')
        sub = QLabel('Recent Projects')
        sub.setObjectName('section')

        self.find = QLineEdit()
        self.find.setObjectName('find')
        self.find.setPlaceholderText('Find')
        self.find.setClearButtonEnabled(True)
        self.find.setFixedWidth(280)
        self.find.textChanged.connect(self.rebuild)

        self.sort_button = QToolButton()
        self.sort_button.setObjectName('flat')
        self.sort_button.setText('Sort')
        self.sort_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.sort_button)
        for mode, label in ((SORT_RECENT, 'Recently opened'),
                            (SORT_NAME, 'Name'),
                            (SORT_MODIFIED, 'Date modified')):
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(mode == self.sort)
            action.triggered.connect(lambda _c=False, m=mode: self.set_sort(m))
        self.sort_button.setMenu(menu)
        self._sort_menu = menu

        another = QPushButton(theme_icon('/mActionFileOpen.svg'),
                              ' Open another project')
        another.setObjectName('flat')
        another.setCursor(Qt.CursorShape.PointingHandCursor)
        another.clicked.connect(self.page.browse_project)
        workspace = QPushButton(_icon('openWorkspace.png'), ' Open workspace')
        workspace.setObjectName('flat')
        workspace.setCursor(Qt.CursorShape.PointingHandCursor)
        workspace.clicked.connect(lambda: self.page.call('open_workspace'))
        workspace.setVisible(self.page.has('open_workspace'))

        self.grid_button = QToolButton()
        self.grid_button.setObjectName('flat')
        self.grid_button.setText('Grid')
        self.list_button = QToolButton()
        self.list_button.setObjectName('flat')
        self.list_button.setText('List')
        group = QButtonGroup(self)
        for mode, button in ((VIEW_GRID, self.grid_button),
                             (VIEW_LIST, self.list_button)):
            button.setCheckable(True)
            button.setChecked(mode == self.view)
            group.addButton(button)
            button.clicked.connect(lambda _c=False, m=mode: self.set_view(m))

        bar = QHBoxLayout()
        bar.setSpacing(10)
        bar.addWidget(sub)
        bar.addSpacing(14)
        bar.addWidget(self.find)
        bar.addWidget(self.sort_button)
        bar.addStretch(1)
        bar.addWidget(workspace)
        bar.addWidget(another)
        bar.addWidget(self.grid_button)
        bar.addWidget(self.list_button)

        self.body = QWidget()
        self.body.setObjectName('page')
        self.grid = QGridLayout(self.body)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(GAP)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.empty = QLabel('')
        self.empty.setObjectName('empty')
        self.empty.setWordWrap(True)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        holder = QWidget()
        holder.setObjectName('page')
        hl = QVBoxLayout(holder)
        hl.setContentsMargins(0, 4, 8, 12)
        hl.addWidget(self.body)
        hl.addWidget(self.empty)
        hl.addStretch(1)
        self.scroll.setWidget(holder)
        self.scroll.viewport().setObjectName('page')

        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 14, 26, 0)
        lay.setSpacing(14)
        lay.addWidget(head)
        lay.addLayout(bar)
        lay.addWidget(self.scroll, 1)

    # -- state -------------------------------------------------------------

    def set_sort(self, mode):
        self.sort = mode
        QgsSettings().setValue(SETTING_SORT, mode)
        for action in self._sort_menu.actions():
            action.setChecked(action.text() == {SORT_RECENT: 'Recently opened',
                                                SORT_NAME: 'Name',
                                                SORT_MODIFIED: 'Date modified'}[mode])
        self.rebuild()

    def set_view(self, mode):
        self.view = mode
        QgsSettings().setValue(SETTING_VIEW, mode)
        self._columns = 0
        self.rebuild()

    def refresh(self):
        self._columns = 0
        self.rebuild()

    # -- layout ------------------------------------------------------------

    def _column_count(self):
        if self.view == VIEW_LIST:
            return 1
        width = self.scroll.viewport().width() - 8
        return max(1, (width + GAP) // (CARD_WIDTH + GAP))

    def rebuild(self, *_args):
        while self.grid.count():
            widget = self.grid.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()
        everything = recent_projects()
        items = sorted_projects(everything, self.sort, self.find.text())
        columns = self._columns = self._column_count()
        for index, item in enumerate(items):
            self.grid.addWidget(Card(item, self.view, self.page),
                                index // columns, index % columns)
        for column in range(columns):
            self.grid.setColumnStretch(column, 1 if self.view == VIEW_LIST else 0)
        if items:
            self.empty.setText('')
        elif everything:
            self.empty.setText('No project matches "{}".'.format(self.find.text()))
        else:
            self.empty.setText(
                'Nothing here yet. Projects you open or save will be listed so '
                'you can come back to them.')
        self.empty.setVisible(not items)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._column_count() != self._columns:
            self.rebuild()


class NewPage(QWidget):
    """New project tiles and project templates."""

    def __init__(self, page):
        super().__init__()
        self.setObjectName('page')
        self.page = page

        head = QLabel('New')
        head.setObjectName('heading')
        sub = QLabel('New Project')
        sub.setObjectName('section')

        panel = QFrame()
        panel.setObjectName('panel')
        row = QHBoxLayout(panel)
        row.setContentsMargins(22, 18, 22, 18)
        row.setSpacing(8)

        def add(tile, key=None, call=None):
            if key is not None and not page.has(key):
                return
            tile.clicked.connect(call or (lambda _c=False, k=key: page.call(k)))
            row.addWidget(tile)

        add(Tile(theme_icon('/mActionNewMap.svg'), 'Map',
                 'A new empty project (with the basemap chosen in Settings)'),
            call=page.new_map)
        add(Tile(_icon('setWorkspace.png'), 'Workspace',
                 'A new map whose data lives in its own workspace folder'),
            'new_workspace')
        add(Tile(_icon('importProjectPackage.png'), 'Import Package',
                 'Unpack a .kgapp project package and open it'),
            'import_package')
        divider = QFrame()
        divider.setObjectName('vline')
        row.addSpacing(8)
        row.addWidget(divider)
        row.addSpacing(8)
        add(Tile(theme_icon('/mActionFileOpen.svg'), 'Start from a template',
                 'Choose a .qgz / .qgs file to start a new project from'),
            call=page.browse_template)
        row.addStretch(1)

        recent_head = QLabel('Recent Projects')
        recent_head.setObjectName('section')
        see_all = QPushButton('See all')
        see_all.setObjectName('link')
        see_all.setCursor(Qt.CursorShape.PointingHandCursor)
        see_all.clicked.connect(lambda: page.show_page(1))
        recent_bar = QHBoxLayout()
        recent_bar.addWidget(recent_head)
        recent_bar.addSpacing(10)
        recent_bar.addWidget(see_all)
        recent_bar.addStretch(1)
        another = QPushButton(theme_icon('/mActionFileOpen.svg'),
                              ' Start with another template')
        another.setObjectName('flat')
        another.setCursor(Qt.CursorShape.PointingHandCursor)
        another.clicked.connect(page.browse_template)
        recent_bar.addWidget(another)
        self.recent = QHBoxLayout()
        self.recent.setSpacing(GAP)
        self.recent.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self.recent_box = QWidget()
        self.recent_box.setObjectName('page')
        box_lay = QVBoxLayout(self.recent_box)
        box_lay.setContentsMargins(0, 0, 0, 0)
        box_lay.setSpacing(14)
        box_lay.addLayout(recent_bar)
        box_lay.addLayout(self.recent)

        self.templates = QHBoxLayout()
        self.templates.setSpacing(10)
        self.templates.setAlignment(Qt.AlignmentFlag.AlignLeft)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 14, 26, 0)
        lay.setSpacing(14)
        lay.addWidget(head)
        lay.addSpacing(6)
        lay.addWidget(sub)
        lay.addWidget(panel)
        lay.addSpacing(10)
        lay.addWidget(self.recent_box)
        lay.addLayout(self.templates)
        lay.addStretch(1)

    def refresh(self):
        while self.recent.count():
            widget = self.recent.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()
        items = sorted_projects(recent_projects(), SORT_RECENT)[:RECENT_ON_NEW]
        for item in items:
            self.recent.addWidget(Card(item, VIEW_GRID, self.page))
        self.recent_box.setVisible(bool(items))
        while self.templates.count():
            widget = self.templates.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()
        found = template_files()
        for name, path in found[:6]:
            tile = Tile(theme_icon('/mIconQgsProjectFile.svg'), name, path)
            tile.setIconSize(QSize(40, 40))
            tile.clicked.connect(lambda _c=False, p=path: self.page.new_from_template(p))
            self.templates.addWidget(tile)


class StartPage(QWidget):
    """The whole page: rail on the left, New / Open on the right."""

    def __init__(self, iface, handlers=None, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.handlers = handlers or {}
        self.setObjectName('startPage')
        self.setStyleSheet(STYLE.format(accent=_accent(self)))

        nav = QFrame()
        nav.setObjectName('nav')
        nav.setFixedWidth(208)
        nav_lay = QVBoxLayout(nav)
        nav_lay.setContentsMargins(0, 14, 0, 14)
        nav_lay.setSpacing(0)

        self.back_button = QPushButton('Back to Map')
        self.back_button.setObjectName('navButton')
        self.back_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.back_button.setVisible(False)
        self.back_button.clicked.connect(lambda: self.call('back'))
        nav_lay.addWidget(self.back_button)

        brand = QHBoxLayout()
        brand.setContentsMargins(16, 0, 10, 14)
        logo = QLabel()
        logo.setPixmap(_icon('kga.png').pixmap(QSize(30, 30)))
        name = QLabel('KGA Toolbox')
        name.setObjectName('navBrand')
        brand.addWidget(logo)
        brand.addSpacing(6)
        brand.addWidget(name)
        brand.addStretch(1)
        nav_lay.addLayout(brand)

        self.pages = QStackedWidget()
        self.pages.setObjectName('pages')
        self.new_page = NewPage(self)
        self.open_page = OpenPage(self)
        self.pages.addWidget(self.new_page)
        self.pages.addWidget(self.open_page)

        self.buttons = QButtonGroup(self)
        self.buttons.setExclusive(True)

        def page_button(text, index):
            button = QPushButton(text)
            button.setObjectName('navButton')
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.buttons.addButton(button, index)
            button.clicked.connect(lambda _c=False, i=index: self.show_page(i))
            nav_lay.addWidget(button)
            return button

        def action_button(text, key, call=None):
            if call is None and not self.has(key):
                return
            button = QPushButton(text)
            button.setObjectName('navButton')
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(call or (lambda _c=False, k=key: self.call(k)))
            nav_lay.addWidget(button)

        page_button('New', 0)
        page_button('Open', 1)
        nav_lay.addSpacing(10)
        nav_lay.addWidget(_hline())
        nav_lay.addSpacing(6)
        action_button('Settings', 'settings')
        action_button('Help', 'help', call=self.open_help)
        action_button('About', 'about')
        nav_lay.addSpacing(6)
        nav_lay.addWidget(_hline())
        nav_lay.addSpacing(6)
        action_button('Exit', 'exit', call=self.exit_qgis)
        nav_lay.addStretch(1)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(nav)
        lay.addWidget(self.pages, 1)

        # Opens on New; the recent projects are a click away (and shown there too).
        self.show_page(0)

    # -- plumbing ------------------------------------------------------------

    def has(self, key):
        return callable(self.handlers.get(key))

    def call(self, key):
        handler = self.handlers.get(key)
        if handler is None:
            return
        try:
            handler()
        except Exception as exc:
            _log('Start page "{}" failed:\n{}'.format(key, traceback.format_exc()))
            self.iface.messageBar().pushWarning('KGA Toolbox', str(exc))

    def set_overlay(self, overlay):
        """True when shown on top of an open project, so there is a way back."""
        self.back_button.setVisible(bool(overlay))

    def show_page(self, index):
        self.pages.setCurrentIndex(index)
        button = self.buttons.button(index)
        if button is not None:
            button.setChecked(True)
        self.refresh()

    def refresh(self):
        self.new_page.refresh()
        self.open_page.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        # QGIS shows the page again after a project is closed; the list changed.
        QTimer.singleShot(0, self.refresh)

    # -- actions -------------------------------------------------------------

    def open_help(self):
        QDesktopServices.openUrl(QUrl(KGA_WEBSITE))

    def exit_qgis(self):
        self.iface.mainWindow().close()

    def open_path(self, path):
        try:
            settle_blank_project()
            self.iface.addProject(path)
        except Exception as exc:
            _log('Could not open {}:\n{}'.format(path, traceback.format_exc()))
            self.iface.messageBar().pushWarning(
                'KGA Toolbox', 'Could not open {}: {}'.format(path, exc))

    def open_recent(self, item):
        if not item.exists:
            self.missing(item)
            return
        self.open_path(item.path)

    def missing(self, item):
        answer = QMessageBox.question(
            self, 'Project not found',
            '"{}" is no longer there.\n\nRemove it from the list?'.format(
                _native(item.path)))
        if answer == QMessageBox.StandardButton.Yes:
            self.remove(item)

    def browse_project(self):
        path, _filter = QFileDialog.getOpenFileName(
            self, 'Open Project', self._start_folder(),
            'QGIS projects (*.qgz *.qgs);;All files (*)')
        if path:
            self.open_path(path)

    def browse_template(self):
        path, _filter = QFileDialog.getOpenFileName(
            self, 'Start from a template',
            os.path.join(QgsApplication.qgisSettingsDirPath(), 'project_templates'),
            'QGIS projects (*.qgz *.qgs);;All files (*)')
        if path:
            self.new_from_template(path)

    def _start_folder(self):
        for item in recent_projects():
            folder = os.path.dirname(item.path)
            if os.path.isdir(folder):
                return folder
        return ''

    def new_map(self):
        settle_blank_project()
        self.iface.newProject()

    def new_from_template(self, path):
        """Open `path` as an unsaved new project, like Project > New from Template."""
        try:
            settle_blank_project()
            self.iface.addProject(path)
            project = QgsProject.instance()
            if os.path.normcase(os.path.normpath(project.fileName())) == \
                    os.path.normcase(os.path.normpath(path)):
                project.setFileName('')
                forget_path(path)
        except Exception as exc:
            _log('Could not start from {}:\n{}'.format(path, traceback.format_exc()))
            self.iface.messageBar().pushWarning(
                'KGA Toolbox', 'Could not start from {}: {}'.format(path, exc))

    def pin(self, item):
        set_pinned(item.key, not item.pinned)
        self.refresh()

    def remove(self, item):
        forget(item.key)
        self.refresh()

    def show_in_folder(self, item):
        QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(item.path)))


# ------------------------------------------------------------------- installer

class StartPageInstaller(QObject):
    """Swap the contents of QGIS's welcome page for `StartPage` (and back)."""

    def __init__(self, iface, handlers=None, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.handlers = handlers or {}
        self.page = None
        self.start = None
        self._holder = None
        self._moved = []
        self._margins = None
        self._tries = 0
        self._stack = None
        self._hidden = []
        self._overlay = False
        self._starting = False
        self._previous = 0
        self._open_action = None
        self._moved_open = []

    def welcome_page(self):
        for widget in self.iface.mainWindow().findChildren(QWidget):
            if widget.metaObject().className() == 'QgsWelcomePage':
                return widget
        return None

    @property
    def installed(self):
        return self.start is not None

    def install(self):
        if self.installed:
            return True
        page = self.welcome_page()
        layout = page.layout() if page is not None else None
        if layout is None:
            # The main window is still being built; look again shortly.
            if self._tries < 20:
                self._tries += 1
                QTimer.singleShot(250, self.install)
            return False
        self.page = page
        self._holder = QWidget()
        self._holder.hide()
        self._moved = []
        for index in range(layout.count()):
            widget = layout.itemAt(index).widget()
            if widget is not None:
                self._moved.append((index, widget))
        for _index, widget in self._moved:
            layout.removeWidget(widget)
            widget.setParent(self._holder)
        self._margins = layout.contentsMargins()
        layout.setContentsMargins(0, 0, 0, 0)
        self.handlers = dict(self.handlers, back=self.leave)
        self.start = StartPage(self.iface, self.handlers)
        layout.addWidget(self.start)
        self._take_over_open()
        self._keep_blank_project_clean()
        for signal in (self.iface.projectRead, self.iface.newProjectCreated):
            signal.connect(self.leave_after_project)
        self._stack = page.parent()
        if isinstance(self._stack, QStackedWidget):
            self._stack.currentChanged.connect(self._page_changed)
            self._adopt_saved_docks()
            self._page_changed(self._stack.currentIndex())
            # QGIS restores the saved window layout after plugins load, which
            # would show the panels again; look once more when it has finished.
            with suppress(Exception):
                self.iface.initializationCompleted.connect(self._sync_docks)
        return True

    def _adopt_saved_docks(self):
        """Panels a previous session left hidden by the start page are ours still."""
        names = QgsSettings().value(SETTING_HIDDEN, [], type=list) or []
        window = self.iface.mainWindow()
        for name in names:
            dock = (window.findChild(QDockWidget, str(name))
                    or window.findChild(QToolBar, str(name))
                    or window.findChild(QMenuBar, str(name)))
            # Only what is hidden now: a panel the user has switched on since is theirs.
            if dock is not None and not dock.isVisible() and dock not in self._hidden:
                self._hidden.append(dock)
        # _page_changed, called next, hides them again or brings them back.

    def _remember_docks(self):
        QgsSettings().setValue(SETTING_HIDDEN,
                               [dock.objectName() for dock in self._hidden])

    def _sync_docks(self):
        QTimer.singleShot(300, lambda: self._stack is not None
                          and self._page_changed(self._stack.currentIndex()))

    def _page_changed(self, index):
        """Hide Layers and Catalog while the start page is up; bring them back after."""
        try:
            if self._stack is not None and self._stack.widget(index) is self.page:
                self._hide_docks()
            else:
                self._restore_docks()
        except RuntimeError:
            pass

    def _hide_docks(self):
        window = self.iface.mainWindow()
        # Every open panel makes way for the start page, not just a fixed few.
        # Toolbars go too, leaving the menu bar: a clean page. Showing them again
        # puts each back where it was.
        for kind in (QDockWidget, QToolBar):
            for dock in window.findChildren(kind):
                if dock.objectName() and dock.isVisible():
                    if dock not in self._hidden:
                        self._hidden.append(dock)
                    dock.hide()
        # The menu bar as well when asked; the page has its own Settings, Help,
        # About and Exit, and Back to Map when a project is open.
        menu_bar = window.menuBar()
        if ks.start_page_hide_menu() and menu_bar.objectName() and menu_bar.isVisible():
            if menu_bar not in self._hidden:
                self._hidden.append(menu_bar)
            menu_bar.hide()
        self._remember_docks()

    @property
    def showing(self):
        """True while the start page is what the window shows."""
        return (self.installed and self._stack is not None
                and self._stack.currentWidget() is self.page)

    @contextmanager
    def full_chrome(self):
        """Toolbars and panels back on for a moment (to read or change the layout)."""
        showing = self.showing
        if showing:
            self._restore_docks(force_layers=False)
        try:
            yield
        finally:
            if showing:
                self._hide_docks()

    def reapply_chrome(self):
        """After something switched toolbars on: hide them again if the page is up."""
        if self.showing:
            self._hide_docks()

    def _restore_docks(self, force_layers=True, persist=True):
        docks, self._hidden = self._hidden, []
        if persist:
            self._remember_docks()
        # The Layers panel always comes back with a project: an earlier session
        # may have saved it closed while the start page was up.
        layers = self.iface.mainWindow().findChild(QDockWidget, 'Layers')
        if force_layers and layers is not None and layers not in docks:
            docks.append(layers)
        for dock in docks:
            with suppress(RuntimeError):
                dock.show()

    # -- no 'save changes?' for the blank start-up project --------------------

    def _keep_blank_project_clean(self):
        """Until start-up is over, an untitled project with no layers is never 'changed'.

        Plugins (this one included) touch the blank project while QGIS starts;
        QGIS then asks whether to save it when it makes its own new project.
        """
        self._starting = True
        project = QgsProject.instance()
        project.isDirtyChanged.connect(self._settle_start_up)
        with suppress(Exception):
            self.iface.initializationCompleted.connect(self._start_up_over)
        QTimer.singleShot(START_UP_LIMIT_MS, self._end_start_up)
        settle_blank_project()

    def _settle_start_up(self, dirty):
        if dirty and self._starting:
            QTimer.singleShot(0, settle_blank_project)

    def _start_up_over(self):
        settle_blank_project()
        QTimer.singleShot(START_UP_SETTLE_MS, self._end_start_up)

    def _end_start_up(self):
        self._starting = False
        with suppress(Exception):
            QgsProject.instance().isDirtyChanged.disconnect(self._settle_start_up)
        with suppress(Exception):
            self.iface.initializationCompleted.disconnect(self._start_up_over)

    # -- Project > Open raises the start page --------------------------------

    def _take_over_open(self):
        """Put our own Open action (same icon and Ctrl+O) where QGIS's is.

        QGIS's Open action opens a file dialog and cannot be intercepted
        cleanly, so it is swapped, in the Project menu and the File toolbar,
        for one that shows the Open page; its shortcut moves with it.
        """
        original = self.iface.actionOpenProject()
        ours = QAction(original.icon(), original.text(), self.iface.mainWindow())
        ours.setToolTip('Open a project (shows the start page)')
        ours.setShortcut(original.shortcut())
        ours.triggered.connect(lambda _checked=False: self.show_open())
        self._moved_open = []
        for widget in original.associatedWidgets():
            if isinstance(widget, (QMenu, QToolBar)):
                widget.insertAction(original, ours)
                widget.removeAction(original)
                self._moved_open.append((widget, ours))
        if not self._moved_open:
            ours.deleteLater()
            return
        original.setShortcut(QKeySequence())
        self._open_action = ours
        self._original_open = original
        self._original_shortcut = ours.shortcut()

    def _give_back_open(self):
        ours, original = self._open_action, getattr(self, '_original_open', None)
        self._open_action = None
        if ours is None or original is None:
            return
        with suppress(RuntimeError):
            original.setShortcut(self._original_shortcut)
            for widget, _action in self._moved_open:
                widget.insertAction(ours, original)
                widget.removeAction(ours)
            ours.deleteLater()
        self._moved_open = []

    def show_open(self):
        """Bring the start page (Open page) to the front over the open project."""
        if self._stack is None or self.start is None:
            return
        if self._stack.currentWidget() is not self.page:
            self._previous = self._stack.currentIndex()
            self._overlay = True
            self._stack.setCurrentWidget(self.page)
        self.start.set_overlay(self._overlay)
        self.start.show_page(1)

    def leave(self):
        """Back to what was on screen before the start page was raised."""
        if self._stack is None or self.start is None:
            return
        self._overlay = False
        self.start.set_overlay(False)
        if self._stack.currentWidget() is self.page:
            self._stack.setCurrentIndex(self._previous)

    def leave_after_project(self, *_args):
        if self._overlay:
            QTimer.singleShot(0, self.leave)

    def uninstall(self):
        self._tries = 99
        self._end_start_up()
        for signal in (self.iface.projectRead, self.iface.newProjectCreated):
            with suppress(Exception):
                signal.disconnect(self.leave_after_project)
        if self._overlay:
            with suppress(Exception):
                self.leave()
        self._give_back_open()
        if self._stack is not None:
            with suppress(RuntimeError, TypeError):
                self._stack.currentChanged.disconnect(self._page_changed)
        with suppress(Exception):
            self.iface.initializationCompleted.disconnect(self._sync_docks)
        # Keep the saved names: when QGIS quits from the start page its layout is
        # saved with everything hidden, and the next start needs them to bring
        # it all back (a plugin switched off just leaves harmless names behind).
        self._restore_docks(persist=False)
        self._stack = None
        page, start = self.page, self.start
        self.page = self.start = None
        if start is not None:
            with suppress(RuntimeError):
                layout = page.layout()
                layout.removeWidget(start)
                start.hide()
                start.deleteLater()
                if self._margins is not None:
                    layout.setContentsMargins(self._margins)
                for index, widget in self._moved:
                    widget.setParent(page)
                    layout.insertWidget(index, widget)
                    widget.show()
        self._moved = []
        if self._holder is not None:
            self._holder.deleteLater()
            self._holder = None

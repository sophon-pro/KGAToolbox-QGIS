# -*- coding: utf-8 -*-
"""Make the QGIS user-interface theme follow the operating system.

QGIS keeps its theme in `UI/UITheme` ("default" is the light one; the dark
ones are "Night Mapping" and friends). `follow_system()` reads whether the
computer is in light or dark mode, picks the matching QGIS theme, saves it (so
QGIS starts in it next time) and applies it to the running window.
"""

from contextlib import suppress
import subprocess  # nosec B404 - fixed argument list, macOS only
import sys

from qgis.core import QgsApplication, QgsSettings

LIGHT_THEME = 'default'
DARK_THEME = 'Night Mapping'
THEME_KEY = 'UI/UITheme'


def system_is_dark():
    """True when the operating system is in dark mode, False for light mode,
    None when it cannot be told."""
    with suppress(Exception):
        if sys.platform.startswith('win'):
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize')
            try:
                return winreg.QueryValueEx(key, 'AppsUseLightTheme')[0] == 0
            finally:
                winreg.CloseKey(key)
        if sys.platform == 'darwin':
            out = subprocess.run(  # nosec B603 B607 - fixed command
                ['defaults', 'read', '-g', 'AppleInterfaceStyle'],
                capture_output=True, text=True, timeout=5)
            return out.stdout.strip().lower() == 'dark'
    with suppress(Exception):
        # Qt 6.5+ (QGIS 4) reports the colour scheme; older Qt has no such call.
        from qgis.PyQt.QtGui import QGuiApplication
        from qgis.PyQt.QtCore import Qt
        scheme = QGuiApplication.styleHints().colorScheme()
        if scheme == Qt.ColorScheme.Dark:
            return True
        if scheme == Qt.ColorScheme.Light:
            return False
    return None


def theme_for_system():
    """The QGIS theme name matching the system mode, or '' if unknown."""
    dark = system_is_dark()
    if dark is None:
        return ''
    if not dark:
        return LIGHT_THEME
    names = list(QgsApplication.uiThemes() or [])
    return DARK_THEME if DARK_THEME in names or not names else next(
        (n for n in names if n != LIGHT_THEME), DARK_THEME)


def follow_system():
    """Switch QGIS to the theme matching the system. Returns the theme name
    applied, or '' when nothing was changed."""
    theme = theme_for_system()
    if not theme:
        return ''
    if QgsSettings().value(THEME_KEY, LIGHT_THEME, type=str) != theme:
        QgsSettings().setValue(THEME_KEY, theme)
        QgsApplication.setUITheme(theme)
    return theme

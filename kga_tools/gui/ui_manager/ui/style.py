# -*- coding: utf-8 -*-
"""Theme-aware style sheet.

All colours are derived from the active QGIS palette (light, dark or custom theme) so the
manager always looks native. Tints are computed in Python because QSS cannot blend colours.
"""
from qgis.PyQt.QtGui import QColor, QPalette

R = QPalette.ColorRole


def blend(a, b, t):
    """Mix colour ``a`` towards ``b`` by ``t`` (0..1)."""
    return QColor(
        int(a.red() + (b.red() - a.red()) * t),
        int(a.green() + (b.green() - a.green()) * t),
        int(a.blue() + (b.blue() - a.blue()) * t),
    )


def colors(pal):
    base = pal.color(R.Base)
    window = pal.color(R.Window)
    text = pal.color(R.Text)
    hl = pal.color(R.Highlight)
    return {
        "base": base,
        "window": window,
        "text": text,
        "hl": hl,
        "muted": blend(text, base, 0.45),
        "border": blend(base, text, 0.13),
        "border_soft": blend(base, text, 0.08),
        "hover": blend(base, hl, 0.05),
        "tint": blend(base, hl, 0.10),
        "tint_border": blend(base, hl, 0.45),
        "trough": blend(window, text, 0.06),
        "track_off": blend(base, text, 0.28),
    }


def build_stylesheet(pal):
    c = {k: v.name() for k, v in colors(pal).items()}
    return """
QWidget#uiRoot {{ background: {window}; }}

QLineEdit#uiSearch {{
    background: {base}; border: 1px solid {border}; border-radius: 6px; padding: 5px 6px;
}}
QLineEdit#uiSearch:focus {{ border-color: {hl}; }}

QFrame#uiSeg {{ background: {trough}; border: 1px solid {border_soft}; border-radius: 7px; }}
QFrame#uiSeg QToolButton {{
    border: 1px solid transparent; border-radius: 5px; padding: 3px 10px;
    background: transparent; color: {muted};
}}
QFrame#uiSeg QToolButton:hover:!checked {{ color: {text}; }}
QFrame#uiSeg QToolButton:checked {{
    background: {base}; color: {text}; border-color: {border}; font-weight: 600;
}}

QToolButton#uiLink {{ border: none; color: {hl}; padding: 2px 4px; background: transparent; }}
QToolButton#uiLink:hover {{ text-decoration: underline; }}
QToolButton#uiIconBtn {{ border: 1px solid transparent; border-radius: 5px; padding: 3px; background: transparent; }}
QToolButton#uiIconBtn:hover, QToolButton#uiIconBtn:checked {{ border-color: {border}; background: {base}; }}

QScrollArea#uiScroll, QWidget#uiListBody {{ background: {window}; border: none; }}

QFrame#uiRow {{ background: {base}; border: 1px solid {border_soft}; border-radius: 8px; }}
QFrame#uiRow:hover {{ background: {hover}; border-color: {border}; }}
QFrame#uiRow[active="true"] {{ background: {tint}; border-color: {tint_border}; }}

QLabel#uiTitle {{ font-weight: 600; background: transparent; }}
QLabel#uiMeta, QLabel#uiHint, QLabel#uiEmpty, QLabel#uiSection {{ color: {muted}; background: transparent; }}
QLabel#uiHint {{ font-style: italic; }}
QLabel#uiSection {{ font-weight: 600; letter-spacing: 1px; }}
QLabel#uiThumb {{ background: {window}; border: 1px solid {border_soft}; border-radius: 5px; }}

QFrame#uiFooter {{ background: {window}; border-top: 1px solid {border_soft}; }}
""".format(**c)

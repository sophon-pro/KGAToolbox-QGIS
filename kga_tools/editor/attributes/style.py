"""
Look of the Attributes pane (flat tabs with a blue underline, pale
blue selection, grey field list). One style sheet, applied to the pane's container widget only.
"""

import os

CHEVRON = os.path.join(os.path.dirname(__file__), "icons", "chevron_down.svg").replace(os.sep, "/")

PRO_BLUE = "#0079c1"
BANNER_ACTIVE = "#c5e1f5"
BANNER_BORDER = "#7ab6e3"
ROW_SELECTED = "#c5dff2"
READONLY_TEXT = "#8a8f94"
NULL_TEXT = "#9aa0a6"
PENDING_TEXT = "#0a5fa8"
ERROR_BG = "#fde8ea"
ERROR_TEXT = "#b3261e"

PANEL_QSS = f"""
QWidget#KgaAttrRoot {{ background: #f6f8f8; }}
QTabBar {{ background: transparent; font-size: 10pt; }}
QTabBar::tab {{
    background: transparent; border: none; border-bottom: 2px solid transparent;
    padding: 4px 4px 3px 4px; margin-right: 10px; color: #5f6368;
}}
QTabBar::tab:hover {{ color: {PRO_BLUE}; }}
QTabBar::tab:selected {{ color: #111111; border-bottom: 2px solid {PRO_BLUE}; }}

QToolButton#KgaAttrBanner {{
    background: #e6f0f8; border: 1px solid #b8d3e8; border-radius: 2px;
    padding: 5px 4px 5px 6px; text-align: left; color: #1f2d3a; font-size: 10pt;
}}
QToolButton#KgaAttrBanner:hover {{ background: #d7e8f5; }}
QToolButton#KgaAttrBanner:checked {{ background: {BANNER_ACTIVE}; border: 1px solid {BANNER_BORDER}; }}
QToolButton#KgaAttrBanner::menu-button {{
    border-left: 1px solid {BANNER_BORDER}; width: 20px;
}}

QTreeWidget {{
    background: #f6f8f8; border: none; outline: 0; font-size: 10pt; color: #2b2b2b;
}}
QTreeWidget::item {{ padding: 2px 2px; }}
QTreeWidget::item:hover {{ background: #e4eef6; }}
QTreeWidget::item:selected {{ background: {ROW_SELECTED}; color: #1b1b1b; }}

QHeaderView::section {{
    background: #eceff0; border: none; border-right: 1px solid #d4d8da; padding: 3px 6px; color: #3c4043;
}}
QTableView {{
    background: #f6f8f8; border: none; outline: 0; gridline-color: #d4d8da;
    font-size: 10pt; selection-background-color: transparent; selection-color: #1b1b1b;
}}
QTableView QLineEdit, QTableView QComboBox, QTableView QDateTimeEdit {{
    background: white; border: 1px solid {PRO_BLUE}; padding: 1px 3px; font-size: 10pt;
}}

QComboBox#KgaAttrLayerCombo {{
    background: white; border: 1px solid #a9adb1; border-radius: 3px; padding: 3px 6px; font-size: 10pt;
}}
QComboBox#KgaAttrLayerCombo:hover {{ border-color: {PRO_BLUE}; }}
QComboBox#KgaAttrLayerCombo::drop-down {{ border: none; width: 24px; subcontrol-origin: padding; subcontrol-position: center right; }}
QComboBox#KgaAttrLayerCombo::down-arrow {{ image: url({CHEVRON}); width: 14px; height: 14px; }}
QToolButton#KgaAttrNav, QToolButton#KgaAttrNavMenu {{ border: none; padding: 2px 3px; }}
QToolButton#KgaAttrNavMenu::menu-indicator {{ image: none; }}
QListView#KgaAttrFeatureList {{
    background: #f6f8f8; border: 1px solid #d4d8da; outline: 0; font-size: 10pt; color: #2b2b2b;
}}
QListView#KgaAttrFeatureList::item {{ padding: 2px 3px; }}
QListView#KgaAttrFeatureList::item:hover {{ background: #e4eef6; }}
QListView#KgaAttrFeatureList::item:selected {{
    background: {ROW_SELECTED}; border: 1px solid #8fc0e4; color: #1b1b1b;
}}
QListView#KgaAttrComboView {{
    background: white; border: 1px solid #a9adb1; outline: 0; font-size: 10pt; color: #202124;
}}
QListView#KgaAttrComboView::item {{ min-height: 22px; padding: 1px 6px; border: none; background: transparent; }}
QListView#KgaAttrComboView::item:hover {{ background: #e4eef6; color: #202124; }}
QListView#KgaAttrComboView::item:selected {{ background: {ROW_SELECTED}; color: #1b1b1b; border: none; }}

QLabel#KgaAttrHint {{ color: #7a7f84; font-size: 10pt; padding: 10px; }}
QLabel#KgaAttrNotice {{
    background: #fff4ce; border: 1px solid #e8d48a; color: #594a00; padding: 3px 6px; font-size: 9pt;
}}
QSplitter::handle {{ background: #e3e7e8; }}
QSplitter::handle:vertical {{ height: 7px; }}

QPushButton#KgaAttrButton {{
    background: #f6f8f8; border: 1px solid #8c9196; border-radius: 3px; padding: 3px 18px;
    min-width: 54px; font-size: 10pt; color: #202124;
}}
QPushButton#KgaAttrButton:hover {{ background: #e7eef4; border-color: {PRO_BLUE}; }}
QPushButton#KgaAttrButton:disabled {{ color: #a0a4a8; border-color: #c4c8cb; }}
QLabel#KgaAttrAutoApply {{ color: #202124; font-size: 10pt; }}
QToolButton#KgaAttrHelp {{ border: none; color: {PRO_BLUE}; font-size: 11pt; font-weight: 600; }}
"""

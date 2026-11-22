"""
nav.py - Two-level tab navigation for the dock panel.

Two main tabs ("SET UP", "RESULTS") each hold a row of sub tabs. Each section
is exposed through a QTabWidget-like adapter (addTab, insertTab,
setCurrentIndex, count, tabText, setTabText, setPageEnabled), so the rest of
the dock can treat a section like a tab widget.
"""

from qgis.PyQt.QtWidgets import QTabWidget, QVBoxLayout, QWidget

NAV_STYLE = """
QTabWidget#mainTabs > QTabBar::tab { padding: 8px 18px; margin-right: 2px; min-height: 16px;
                                     font-weight: 700; font-size: 8pt; color: palette(mid);
                                     border: none; border-bottom: 2px solid transparent; }
QTabWidget#mainTabs > QTabBar::tab:selected { color: #1E3A8A; border-bottom: 2px solid #2563EB; }
QTabWidget#mainTabs > QTabBar::tab:hover:!selected { color: palette(text); }
QTabWidget#subTabs > QTabBar::tab { padding: 7px 12px; margin-right: 2px; min-height: 16px;
                                    border: none; border-bottom: 2px solid transparent; }
QTabWidget#subTabs > QTabBar::tab:selected { color: #1E3A8A;
                                             border-bottom: 2px solid #2563EB; }
QTabWidget#subTabs > QTabBar::tab:hover:!selected { background: rgba(0,0,0,0.05); }
QTabWidget#subTabs > QTabBar::tab:disabled { color: palette(mid); }
"""


class _Section:
    """QTabWidget-like view on the sub tabs of one main tab."""

    def __init__(self, nav, index, tabs):
        self.nav = nav
        self.index = index          # position of the main tab
        self.tabs = tabs            # the sub-tab widget

    def addTab(self, widget, label):  # noqa: N802
        return self.tabs.addTab(widget, label)

    def insertTab(self, index, widget, label):  # noqa: N802
        return self.tabs.insertTab(index, widget, label)

    def count(self):
        return self.tabs.count()

    def tabText(self, i):  # noqa: N802
        return self.tabs.tabText(i)

    def setTabText(self, i, text):  # noqa: N802
        self.tabs.setTabText(i, text)

    def setCurrentIndex(self, i):  # noqa: N802
        if 0 <= i < self.tabs.count():
            self.nav.main.setCurrentIndex(self.index)
            self.tabs.setCurrentIndex(i)

    def setCurrentWidget(self, w):  # noqa: N802
        self.nav.main.setCurrentIndex(self.index)
        self.tabs.setCurrentWidget(w)

    def currentIndex(self):  # noqa: N802
        return self.tabs.currentIndex()

    def setPageEnabled(self, i, on):  # noqa: N802
        self.tabs.setTabEnabled(i, bool(on))


class SideNav(QWidget):
    """Main tabs on top, a row of sub tabs inside each."""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.main = QTabWidget()
        self.main.setObjectName("mainTabs")
        self.main.setDocumentMode(True)
        self.main.setStyleSheet(NAV_STYLE)
        lay.addWidget(self.main)

    def section(self, title):
        tabs = QTabWidget()
        tabs.setObjectName("subTabs")
        tabs.setDocumentMode(True)
        tabs.setUsesScrollButtons(True)
        tabs.tabBar().setExpanding(False)
        index = self.main.addTab(tabs, title)
        return _Section(self, index, tabs)

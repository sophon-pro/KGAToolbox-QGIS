# -*- coding: utf-8 -*-
"""The Check for Update window.

Asks GitHub for the latest release of the plugin and says whether this install
is behind. It does not install anything itself: QGIS owns plugin installation,
so a newer version is offered as a link to the release page and as a reminder
to use Plugins > Manage and Install Plugins.

The request goes through QgsNetworkAccessManager so the QGIS proxy settings
apply, and it is asynchronous so the window never freezes while it waits. It
only runs when the user opens this window or presses Check Again.
"""

from qgis.core import QgsNetworkAccessManager
from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtNetwork import QNetworkReply, QNetworkRequest
from qgis.PyQt.QtWidgets import (
    QDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

from ..core import update_check
from .about_dialog import _metadata

TIMEOUT_MS = 15000
GREEN = 'color: #1a7f37; font-weight: bold;'
AMBER = 'color: #b35900; font-weight: bold;'
RED = 'color: #b42318;'


class UpdateDialog(QDialog):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.current, _author = _metadata()
        self.release_url = update_check.RELEASES_PAGE
        self._reply = None

        self.setWindowTitle('KGA Toolbox - Check for Update')
        self.setMinimumWidth(480)
        self.resize(520, 440)

        title = QLabel('KGA Toolbox Update Checker', self)
        title.setStyleSheet('font-size: 15pt; font-weight: bold;')
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # --- version information ---------------------------------------------
        self.latest_label = QLabel('-', self)
        self.status_label = QLabel('', self)
        self.status_label.setWordWrap(True)
        current_label = QLabel(self.current or 'unknown', self)
        current_label.setStyleSheet('font-weight: bold;')

        grid = QGridLayout()
        grid.setColumnStretch(1, 1)
        for row, (name, widget) in enumerate((
                ('Installed version:', current_label),
                ('Latest version:', self.latest_label),
                ('Status:', self.status_label))):
            grid.addWidget(QLabel(name, self), row, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(widget, row, 1)
        info_box = QGroupBox('Version information', self)
        info_box.setLayout(grid)

        # --- release notes ---------------------------------------------------
        # Release notes are Markdown; QTextBrowser renders it and opens links.
        self.notes = QTextBrowser(self)
        self.notes.setOpenExternalLinks(True)
        self.notes.setPlaceholderText('Release notes appear here.')
        notes_box = QGroupBox('What is new in the latest release', self)
        notes_layout = QVBoxLayout(notes_box)
        notes_layout.addWidget(self.notes)

        # --- buttons ---------------------------------------------------------
        self.check_button = QPushButton('Check Again', self)
        self.check_button.clicked.connect(self.check)
        self.open_button = QPushButton('Open Release Page', self)
        self.open_button.clicked.connect(self.open_release)
        self.open_button.setEnabled(False)
        close_button = QPushButton('Close', self)
        close_button.clicked.connect(self.accept)

        buttons = QHBoxLayout()
        buttons.addWidget(self.check_button)
        buttons.addWidget(self.open_button)
        buttons.addStretch(1)
        buttons.addWidget(close_button)

        hint = QLabel(
            'Updates are installed from Plugins > Manage and Install Plugins. '
            'Restart QGIS afterwards.', self)
        hint.setWordWrap(True)
        hint.setStyleSheet('color: palette(mid); font-size: 8pt;')

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 14)
        layout.setSpacing(10)
        layout.addWidget(title)
        layout.addWidget(info_box)
        layout.addWidget(notes_box, 1)
        layout.addLayout(buttons)
        layout.addWidget(hint)

        self.check()

    # ----------------------------------------------------------------- request

    def check(self):
        """Ask GitHub for the latest release, without blocking the window."""
        self._abort()
        self.check_button.setEnabled(False)
        self.open_button.setEnabled(False)
        self.latest_label.setText('-')
        self.notes.clear()
        self._status('Checking for updates...', '')

        request = QNetworkRequest(QUrl(update_check.LATEST_RELEASE_API))
        request.setRawHeader(b'Accept', b'application/vnd.github+json')
        request.setRawHeader(b'User-Agent', b'KGA-Toolbox-QGIS')
        request.setTransferTimeout(TIMEOUT_MS)
        request.setAttribute(
            QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy)
        self._reply = QgsNetworkAccessManager.instance().get(request)
        self._reply.finished.connect(self._finished)

    def _abort(self):
        reply, self._reply = self._reply, None
        if reply is not None:
            reply.finished.disconnect(self._finished)
            reply.abort()
            reply.deleteLater()

    def _finished(self):
        reply, self._reply = self._reply, None
        if reply is None:
            return
        self.check_button.setEnabled(True)
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        error = reply.error()
        body = reply.readAll()
        reply.deleteLater()

        if status == 404:
            # GitHub answers 404 for a repository with no published release.
            self._status('No release has been published yet.', '')
            self.open_button.setEnabled(True)
            return
        if error != QNetworkReply.NetworkError.NoError:
            self._status('Could not reach GitHub. Check your internet '
                         'connection and try again.', RED)
            return
        try:
            latest, notes, url = update_check.parse_release(body.data())
        except ValueError:
            self._status('GitHub gave an answer this window could not '
                         'read. Try again later.', RED)
            return

        self.release_url = url
        self.latest_label.setText(latest)
        self.notes.setMarkdown(notes or 'The release has no notes.')
        self.open_button.setEnabled(True)

        order = update_check.compare_versions(self.current, latest)
        if order is None:
            self._status('Could not compare the version numbers.', RED)
        elif order < 0:
            self._status('A newer version is available.', AMBER)
        elif order == 0:
            self._status('You are running the latest version.', GREEN)
        else:
            self._status('This install is newer than the latest release.', '')

    def _status(self, text, style):
        self.status_label.setText(text)
        self.status_label.setStyleSheet(style)

    # ----------------------------------------------------------------- actions

    def open_release(self):
        QDesktopServices.openUrl(QUrl(self.release_url))

    def done(self, result):
        self._abort()
        super().done(result)

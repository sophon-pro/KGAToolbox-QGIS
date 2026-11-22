# -*- coding: utf-8 -*-
"""
Layer Path Manager - secondary dialogs.

ChangePreviewDialog   side-by-side preview & confirmation of proposed changes
RelocationDialog      options for "Repair broken paths" / "Relocate data sources"
NormalizeDialog       preview & apply path standardisation
ReportDialog          HTML viewer for health / migration reports with export
"""

import html
import os
from dataclasses import dataclass
from typing import Callable, List, Optional

from qgis.core import QgsProject, QgsSettings
from qgis.gui import QgsCollapsibleGroupBox, QgsFileWidget
from qgis.PyQt.QtCore import QModelIndex, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QGuiApplication
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFileDialog, QGridLayout, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMenu, QMessageBox, QProgressDialog, QPushButton, QRadioButton,
    QTableView, QTableWidget, QTableWidgetItem, QTextBrowser, QToolButton, QVBoxLayout, QWidget,
)

from .path_normalizer import (
    MODE_CLEAN, MODE_LABELS, MODE_TO_ABSOLUTE, MODE_TO_RELATIVE, NormalizationItem, PathNormalizer,
)
from .path_repair import MATCH_MANUAL, MatchOptions, PathMatcher, set_proposal_path
from .source_validator import SourceValidator
from .table_models import MiddleElideDelegate, PathDiffDelegate, ProposalTableModel
from .utils import (
    ChangeProposal, LayerRecord, clean_path, file_dialog_filter, project_uses_relative_paths,
    theme_icon,
)

SETTINGS_PREFIX = "LayerPathManager/"
IS_WIN = os.name == "nt"


def _settings_value(key, default=None, type_=None):
    s = QgsSettings()
    if type_ is not None:
        return s.value(SETTINGS_PREFIX + key, default, type=type_)
    return s.value(SETTINGS_PREFIX + key, default)


def _set_setting(key, value):
    QgsSettings().setValue(SETTINGS_PREFIX + key, value)


def last_dir():
    d = _settings_value("lastDir", "")
    if d and os.path.isdir(d):
        return d
    proj = QgsProject.instance()
    return proj.absolutePath() if proj.fileName() else os.path.expanduser("~")


def remember_dir(path):
    if path:
        _set_setting("lastDir", path if os.path.isdir(path) else os.path.dirname(path))


def browse_for_proposal(parent, p: ChangeProposal) -> str:
    """Native file/folder dialog filtered by the original provider and format."""
    start = os.path.dirname(p.new_path or p.original_path)
    if not os.path.isdir(start):
        start = last_dir()
    if p.is_directory:
        path = QFileDialog.getExistingDirectory(
            parent, f"Select replacement folder dataset for '{p.layer_name}'", start)
    else:
        path, _f = QFileDialog.getOpenFileName(
            parent, f"Select replacement data source for '{p.layer_name}'",
            start, file_dialog_filter(p.provider, p.layer_type, p.original_path))
    if path:
        remember_dir(path)
    return path


# ======================================================================
# Preview & confirmation
# ======================================================================


class ChangePreviewDialog(QDialog):
    """Shows original vs proposed paths and requires explicit confirmation."""

    def __init__(self, proposals: List[ChangeProposal], project: QgsProject,
                 title: str, intro: str, parent=None, auto_validate=True):
        super().__init__(parent)
        self.setWindowTitle(f"Layer Path Manager - {title}")
        self.setWindowIcon(theme_icon("mActionFileOpen.svg"))
        self.resize(1180, 600)
        self.project = project
        self.proposals = proposals
        self.validator = SourceValidator(project)

        lay = QVBoxLayout(self)
        head = QLabel(f"<span style='font-size:12pt;font-weight:600'>Check the matches</span>"
                      f"<br>{intro}")
        head.setWordWrap(True)
        lay.addWidget(head)

        # one "Change" menu instead of a row of buttons
        bar = QHBoxLayout()
        self.btn_change = QToolButton()
        self.btn_change.setText("Change")
        self.btn_change.setIcon(theme_icon("mActionFileOpen.svg"))
        self.btn_change.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.btn_change.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.btn_change.setToolTip("Pick a different file for the selected row(s)")
        menu = QMenu(self)
        self.btn_candidate = menu.addAction(theme_icon("mActionShowAllLayers.svg"),
                                            "Choose from the files found...",
                                            self._candidate_menu_current)
        menu.addAction(theme_icon("mActionFileOpen.svg"), "Browse for a file...", self._browse_current)
        menu.addSeparator()
        menu.addAction(theme_icon("mActionAddLayer.svg"), "Use one file for all selected rows...",
                       self._set_file_selected).setToolTip(
            "e.g. several layers of the same GeoPackage - each keeps its own layer name")
        menu.addAction(theme_icon("mIconFolderOpen.svg"), "Look in one folder for the selected rows...",
                       self._set_folder_selected)
        menu.addSeparator()
        menu.addAction(theme_icon("mActionRemove.svg"), "Don't change the selected rows",
                       self._clear_selected)
        menu.addAction(theme_icon("mIconSuccess.svg"), "Check all files again",
                       lambda: self.validate_all(force=True))
        menu.setToolTipsVisible(True)
        self.btn_change.setMenu(menu)
        bar.addWidget(self.btn_change)
        bar.addStretch(1)
        self.chk_attention = QCheckBox("Show only rows that need a decision")
        bar.addWidget(self.chk_attention)
        lay.addLayout(bar)

        # table
        self.model = ProposalTableModel(proposals, editor_callback=self._typed_path, parent=self)
        self.view = QTableView()
        self.view.setModel(self.model)
        self.view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.view.setEditTriggers(QAbstractItemView.EditTrigger.EditKeyPressed |
                                  QAbstractItemView.EditTrigger.SelectedClicked)
        self.view.setAlternatingRowColors(True)
        self.view.verticalHeader().setVisible(False)
        self.view.verticalHeader().setDefaultSectionSize(24)
        self.view.setWordWrap(False)
        self.view.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.view.setItemDelegateForColumn(ProposalTableModel.C_ORIG,
                                           PathDiffDelegate(QColor(207, 34, 46), True, self.view))
        self.view.setItemDelegateForColumn(ProposalTableModel.C_NEW,
                                           PathDiffDelegate(QColor(30, 140, 60), False, self.view))
        hh = self.view.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for col, w in ((0, 50), (1, 160), (2, 340), (3, 340), (4, 200), (5, 80), (6, 240)):
            self.view.setColumnWidth(col, w)
        hh.setStretchLastSection(True)
        self.view.setColumnHidden(ProposalTableModel.C_CONF, True)  # shown inside "Match"
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        lay.addWidget(self.view, 1)

        legend = QLabel("<span style='color:#cf222e'><s>Red</s></span> = old folder &nbsp; "
                        "<span style='color:#1e8c3c'><b>Green</b></span> = new folder &nbsp;&middot;&nbsp; "
                        "Double-click a row to pick another file. Uncertain matches stay unticked "
                        "until you decide.")
        legend.setWordWrap(True)
        legend.setStyleSheet("color: palette(mid); font-size: 8pt;")
        lay.addWidget(legend)

        bottom = QHBoxLayout()
        self.summary = QLabel()
        bottom.addWidget(self.summary, 1)
        self.buttons = QDialogButtonBox()
        self.apply_btn = self.buttons.addButton("Apply Changes", QDialogButtonBox.ButtonRole.AcceptRole)
        self.apply_btn.setIcon(theme_icon("mActionFileSave.svg"))
        self.apply_btn.setDefault(True)
        self.apply_btn.setStyleSheet(
            "QPushButton{background:#1f6feb;color:white;border:none;border-radius:5px;"
            "padding:6px 16px;font-weight:600} QPushButton:disabled{background:rgba(31,111,235,0.35)}")
        self.buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        bottom.addWidget(self.buttons)
        lay.addLayout(bottom)

        # wiring
        self.chk_attention.toggled.connect(self._apply_row_filter)
        self.view.doubleClicked.connect(self._double_clicked)
        self.view.customContextMenuRequested.connect(self._context_menu)
        self.view.selectionModel().currentRowChanged.connect(lambda *_: self._update_buttons())
        self.model.acceptedChanged.connect(self._update_summary)
        self.buttons.accepted.connect(self._confirm)
        self.buttons.rejected.connect(self.reject)

        self._update_summary()
        self._update_buttons()
        if proposals:
            self.view.selectRow(0)
        if auto_validate and any(p.new_source for p in proposals):
            # validated lazily after the dialog is shown
            from qgis.PyQt.QtCore import QTimer
            QTimer.singleShot(50, lambda: self.validate_all(force=False))

    # ------------------------------------------------------------------
    def _tool(self, text, icon, tip):
        b = QToolButton()
        b.setText(text)
        b.setIcon(theme_icon(icon))
        b.setToolTip(tip)
        b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        return b

    def _current(self) -> Optional[ChangeProposal]:
        idx = self.view.currentIndex()
        return self.proposals[idx.row()] if idx.isValid() else None

    def _selected_rows(self) -> List[int]:
        rows = sorted({i.row() for i in self.view.selectionModel().selectedRows()})
        if not rows and self.view.currentIndex().isValid():
            rows = [self.view.currentIndex().row()]
        return rows

    def _update_buttons(self):
        p = self._current()
        self.btn_candidate.setEnabled(bool(p and p.candidates))

    def _update_summary(self):
        n = len(self.proposals)
        acc = sum(1 for p in self.proposals if p.accepted)
        review = sum(1 for p in self.proposals if p.needs_review)
        failed = sum(1 for p in self.proposals if p.validation_state == "Failed")
        warn = sum(1 for p in self.proposals if p.validation_state == "Warning" and p.accepted)
        txt = f"<b>{acc}</b> of {n} layer(s) will be reconnected"
        if review:
            txt += f" &nbsp;|&nbsp; <span style='color:#d48c0a'>{review} need your decision</span>"
        if failed:
            txt += f" &nbsp;|&nbsp; <span style='color:#cf222e'>{failed} failed validation</span>"
        if warn:
            txt += f" &nbsp;|&nbsp; <span style='color:#d48c0a'>{warn} with warnings</span>"
        self.summary.setText(txt)
        self.apply_btn.setText(f"Apply {acc} Change(s)")
        self.apply_btn.setEnabled(acc > 0)
        self.chk_attention.setVisible(bool(review) or self.chk_attention.isChecked())
        self._apply_row_filter()

    def _apply_row_filter(self):
        only = self.chk_attention.isChecked()
        for row, p in enumerate(self.proposals):
            self.view.setRowHidden(row, only and not p.needs_review)

    def _set_path(self, p: ChangeProposal, path: str, label=MATCH_MANUAL, conf=100):
        set_proposal_path(p, path, label, conf)
        if p.new_source and p.match_type != "Same file as now":
            p.accepted = True
            self.validator.validate_proposal(p, self.project.mapLayer(p.layer_id))
            if p.validation_state == "Failed":
                p.accepted = False

    def _typed_path(self, p: ChangeProposal, text: str):
        self._set_path(p, text)

    # ---------------- actions ----------------
    def _double_clicked(self, index: QModelIndex):
        if index.column() == ProposalTableModel.C_APPLY:
            return
        p = self.proposals[index.row()]
        if p.candidates and len(p.candidates) > 1 or (p.candidates and not p.new_path):
            self._candidate_menu(index.row())
        else:
            self._browse_row(index.row())

    def _candidate_menu_current(self):
        idx = self.view.currentIndex()
        if idx.isValid():
            self._candidate_menu(idx.row())

    def _candidate_menu(self, row):
        p = self.proposals[row]
        menu = QMenu(self)
        for c in p.candidates[:40]:
            act = menu.addAction(c)
            act.setCheckable(True)
            act.setChecked(c == p.new_path)
            act.triggered.connect(lambda _c=False, path=c: self._pick(row, path))
        if p.candidates:
            menu.addSeparator()
        menu.addAction(theme_icon("mActionFileOpen.svg"), "Browse...", lambda: self._browse_row(row))
        rect = self.view.visualRect(self.model.index(row, ProposalTableModel.C_NEW))
        menu.exec(self.view.viewport().mapToGlobal(rect.bottomLeft()))

    def _pick(self, row, path):
        self._set_path(self.proposals[row], path, "Candidate chosen by user", 100)
        self.model.refresh_row(row)
        self._update_summary()

    def _browse_current(self):
        idx = self.view.currentIndex()
        if idx.isValid():
            self._browse_row(idx.row())

    def _browse_row(self, row):
        p = self.proposals[row]
        path = browse_for_proposal(self, p)
        if path:
            self._set_path(p, path)
            self.model.refresh_row(row)
            self._update_summary()

    def _set_file_selected(self):
        rows = self._selected_rows()
        if not rows:
            return
        first = self.proposals[rows[0]]
        path = browse_for_proposal(self, first)
        if not path:
            return
        for r in rows:
            self._set_path(self.proposals[r], path)
        self.model.refresh_all()
        self._update_summary()

    def _set_folder_selected(self):
        rows = self._selected_rows()
        if not rows:
            return
        folder = QFileDialog.getExistingDirectory(self, "Folder containing the datasets", last_dir())
        if not folder:
            return
        remember_dir(folder)
        missing = []
        for r in rows:
            p = self.proposals[r]
            cand = os.path.join(folder, os.path.basename(p.original_path.rstrip("/")))
            if os.path.exists(cand):
                self._set_path(p, cand, "Folder assigned by user", 100)
            else:
                set_proposal_path(p, "", "Not found in chosen folder", 0)
                missing.append(p.layer_name)
        self.model.refresh_all()
        self._update_summary()
        if missing:
            QMessageBox.information(self, "Layer Path Manager",
                                    f"{len(missing)} layer(s) have no file with the same name in "
                                    f"{folder}:\n\n" + "\n".join(missing[:20]))

    def _clear_selected(self):
        for r in self._selected_rows():
            set_proposal_path(self.proposals[r], "", "Cleared by user", 0)
        self.model.refresh_all()
        self._update_summary()

    def _context_menu(self, pos):
        idx = self.view.indexAt(pos)
        if not idx.isValid():
            return
        row = idx.row()
        p = self.proposals[row]
        menu = QMenu(self)
        if p.candidates:
            sub = menu.addMenu(f"Choose Candidate ({len(p.candidates)})")
            for c in p.candidates[:40]:
                sub.addAction(c, lambda path=c: self._pick(row, path))
        menu.addAction(theme_icon("mActionFileOpen.svg"), "Browse...", lambda: self._browse_row(row))
        menu.addAction("Edit Path as Text", lambda: self.view.edit(
            self.model.index(row, ProposalTableModel.C_NEW)))
        menu.addAction(theme_icon("mActionRemove.svg"), "Clear Proposal", self._clear_selected)
        menu.addSeparator()
        menu.addAction(theme_icon("mActionEditCopy.svg"), "Copy Proposed Source URI",
                       lambda: QGuiApplication.clipboard().setText(p.new_source or ""))
        menu.exec(self.view.viewport().mapToGlobal(pos))

    # ---------------- validation & confirmation ----------------
    def validate_all(self, force=True) -> bool:
        todo = [p for p in self.proposals if p.new_source and (force or not p.validation_state)]
        if not todo:
            self.model.refresh_all()
            return True
        prog = QProgressDialog("Validating proposed sources...", "Cancel", 0, len(todo), self)
        prog.setWindowModality(Qt.WindowModality.WindowModal)
        prog.setMinimumDuration(400)
        cancelled = False
        for i, p in enumerate(todo):
            prog.setValue(i)
            prog.setLabelText(f"Validating {p.layer_name} ({i + 1}/{len(todo)})")
            QApplication.processEvents()
            if prog.wasCanceled():
                cancelled = True
                break
            self.validator.validate_proposal(p, self.project.mapLayer(p.layer_id))
            if p.validation_state == "Failed":
                p.accepted = False
        prog.setValue(len(todo))
        self.model.refresh_all()
        self._update_summary()
        return not cancelled

    def _confirm(self):
        if not self.validate_all(force=False):
            return
        chosen = [p for p in self.proposals if p.accepted]
        if not chosen:
            QMessageBox.information(self, "Layer Path Manager", "No changes are selected.")
            return
        warns = [p for p in chosen if p.validation_state == "Warning"]
        unreviewed = [p for p in chosen if p.confidence < 80]
        msg = f"Update the data source of {len(chosen)} layer(s)?\n\n"
        msg += ("Layer IDs, names, symbology, labels, joins and other settings are kept. "
                "Original sources are stored so the change can be restored during this session. "
                "No dataset on disk is modified.")
        if warns:
            msg += f"\n\n{len(warns)} change(s) have validation warnings:\n" + "\n".join(
                f"  - {p.layer_name}: {p.validation_message}" for p in warns[:6])
        if unreviewed:
            msg += f"\n\n{len(unreviewed)} low-confidence match(es) were selected manually."
        res = QMessageBox.question(self, "Confirm data source changes", msg,
                                   QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                   QMessageBox.StandardButton.No)
        if res == QMessageBox.StandardButton.Yes:
            self.accept()

    def accepted_proposals(self) -> List[ChangeProposal]:
        return [p for p in self.proposals if p.accepted]


# ======================================================================
# Single layer: small, plain confirmation instead of a table
# ======================================================================


class SingleChangeDialog(QDialog):
    """'Use a different file for <layer>': shows now -> new and whether it opens."""

    def __init__(self, proposal: ChangeProposal, project: QgsProject, parent=None):
        super().__init__(parent)
        self.p = proposal
        self.project = project
        self.validator = SourceValidator(project)
        self.setWindowTitle("Layer Path Manager - Replace File")
        self.setMinimumWidth(620)
        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        lay.addWidget(QLabel(f"<span style='font-size:12pt;font-weight:600'>Use a different file "
                             f"for \u201c{html.escape(proposal.layer_name)}\u201d?</span>"))
        grid = QGridLayout()
        grid.setColumnStretch(1, 1)
        grid.addWidget(QLabel("<b>Now:</b>"), 0, 0, Qt.AlignmentFlag.AlignTop)
        grid.addWidget(QLabel("<b>New:</b>"), 1, 0, Qt.AlignmentFlag.AlignTop)
        self.now_label = QLabel()
        self.new_label = QLabel()
        for lbl in (self.now_label, self.new_label):
            lbl.setWordWrap(True)
            lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        grid.addWidget(self.now_label, 0, 1)
        grid.addWidget(self.new_label, 1, 1)
        lay.addLayout(grid)
        self.result = QLabel()
        self.result.setWordWrap(True)
        lay.addWidget(self.result)
        note = QLabel("The layer keeps its name, style, labels and joins. No file is copied or "
                      "deleted, and you can undo this until QGIS closes.")
        note.setWordWrap(True)
        lay.addWidget(note)
        row = QHBoxLayout()
        other = QPushButton(theme_icon("mActionFileOpen.svg"), "Choose Another File...")
        other.clicked.connect(self._choose_again)
        row.addWidget(other)
        row.addStretch()
        self.ok_btn = QPushButton("Use This File")
        self.ok_btn.setDefault(True)
        self.ok_btn.setStyleSheet(
            "QPushButton{background:#1f6feb;color:white;border:none;border-radius:5px;"
            "padding:6px 16px;font-weight:600} QPushButton:disabled{background:rgba(31,111,235,0.35)}")
        self.ok_btn.clicked.connect(self.accept)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        row.addWidget(self.ok_btn)
        row.addWidget(cancel)
        lay.addLayout(row)
        self._refresh()

    def _refresh(self):
        from .table_models import _split_diff
        p = self.p
        pre, mid, suf = _split_diff(p.new_path, p.original_path)
        opre, omid, osuf = _split_diff(p.original_path, p.new_path)
        e = html.escape
        self.now_label.setText(f"{e(opre)}<span style='color:#cf222e'><s>{e(omid)}</s></span>{e(osuf)}")
        self.new_label.setText(f"{e(pre)}<b style='color:#1e8c3c'>{e(mid)}</b>{e(suf)}")
        self.validator.validate_proposal(p, self.project.mapLayer(p.layer_id))
        if p.validation_state == "OK":
            self.result.setText("<span style='color:#1e8c3c'>&#10004; The new file opens fine "
                                f"({e(p.validation_message.replace('Readable', '').strip(' ()'))}).</span>")
        elif p.validation_state == "Warning":
            self.result.setText("<span style='color:#b26b00'>&#9888; The file opens, but: "
                                f"{e(p.validation_message.replace('Readable; ', ''))}</span>")
        else:
            self.result.setText("<span style='color:#cf222e'>&#10006; This file can't be used: "
                                f"{e(p.validation_message)}</span>")
        self.ok_btn.setEnabled(p.validation_state in ("OK", "Warning"))
        p.accepted = self.ok_btn.isEnabled()

    def _choose_again(self):
        path = browse_for_proposal(self, self.p)
        if path:
            set_proposal_path(self.p, path)
            if self.p.match_type == "Same file as now":
                QMessageBox.information(self, "Layer Path Manager",
                                        "That is the file the layer already uses.")
            self._refresh()


# ======================================================================
# Copy data to a new folder
# ======================================================================


class FolderPicker(QWidget):
    """Visible list of the folders the project's layers already use (one click picks
    one), plus an 'Other folder' box with Browse.

    API: filePath() / setFilePath() / fileChanged / folders().
    """
    fileChanged = pyqtSignal(str)

    def __init__(self, records: List[LayerRecord], parent=None, current_folders=None):
        super().__init__(parent)
        from qgis.PyQt.QtWidgets import QListWidget, QListWidgetItem
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        counts = {}
        for r in records:
            if r.is_file_based:
                counts[r.folder] = counts.get(r.folder, 0) + 1
        current_folders = set(current_folders or [])
        self.list = QListWidget()
        self.list.setAlternatingRowColors(True)
        self.list.setUniformItemSizes(True)
        self.list.setTextElideMode(Qt.TextElideMode.ElideMiddle)      # no sideways scrolling
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # keep the chosen folder clearly highlighted even when the list loses focus
        self.list.setStyleSheet(
            "QListWidget::item:selected, QListWidget::item:selected:!active "
            "{ background: #1f6feb; color: white; }")
        icon = theme_icon("mIconFolderOpen.svg")
        for folder, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower())):
            label = (f"{os.path.basename(folder) or folder}   \u2014   {os.path.dirname(folder)}"
                     f"   ({n} layer{'s' if n != 1 else ''})")
            if folder in current_folders:
                label += "   \u2190 current folder"
            it = QListWidgetItem(icon, label)
            it.setData(Qt.ItemDataRole.UserRole, folder)
            it.setToolTip(folder)
            self.list.addItem(it)
        rows = min(max(self.list.count(), 1), 5)
        self.list.setFixedHeight(rows * 26 + 6)
        self.list.setVisible(self.list.count() > 0)
        lay.addWidget(self.list)
        other = QHBoxLayout()
        other.addWidget(QLabel("Folder:"))
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("click a folder above, type a path, Browse, or New Folder")
        other.addWidget(self.edit, 1)
        browse = QToolButton()
        browse.setText("Browse...")
        browse.setIcon(icon)
        browse.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        browse.clicked.connect(self._browse)
        other.addWidget(browse)
        newf = QToolButton()
        newf.setText("New Folder...")
        newf.setIcon(theme_icon("mActionNewFolder.svg") if not theme_icon("mActionNewFolder.svg").isNull()
                     else icon)
        newf.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        newf.setToolTip("Make a new folder (e.g. 20261105) next to your folders, or inside the "
                        "folder shown in the box")
        newf.clicked.connect(self._new_folder)
        self.new_btn = newf
        other.addWidget(newf)
        lay.addLayout(other)
        self.dest = QLabel()
        self.dest.setWordWrap(True)
        lay.addWidget(self.dest)
        self._listed = [self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())]
        self._path = ""
        self._show_dest()
        self.list.itemSelectionChanged.connect(self._list_picked)
        self.edit.textEdited.connect(self._typed)

    def _list_picked(self):
        items = self.list.selectedItems()
        if items:
            path = items[0].data(Qt.ItemDataRole.UserRole)
            self.edit.blockSignals(True)
            self.edit.setText(path)          # show the chosen folder in the box
            self.edit.blockSignals(False)
            self._set(path)

    def _typed(self, text):
        self.list.blockSignals(True)
        self.list.clearSelection()
        self.list.blockSignals(False)
        self._set(text.strip())

    def _set(self, path):
        if path != self._path:
            self._path = path
            self._show_dest()
            self.fileChanged.emit(path)

    def _show_dest(self):
        p = self._path
        if not p:
            self.dest.setText("<span style='color:#b26b00'>No folder chosen yet.</span>")
        elif os.path.isdir(p):
            self.dest.setText(f"<span style='color:#1e8c3c'>&#10004; Destination: <b>{html.escape(p)}</b></span>")
        else:
            self.dest.setText(f"<span style='color:#1e8c3c'>&#10004; Destination (new folder, created "
                              f"when you start): <b>{html.escape(p)}</b></span>")

    def _new_folder(self):
        from qgis.PyQt.QtWidgets import QInputDialog
        # where to create it: inside the typed/browsed folder, or next to the listed folders
        base = self._path if (self._path and self._path not in self._listed and os.path.isdir(self._path)) \
            else ""
        if not base:
            parents = [os.path.dirname(f) for f in self._listed] or [last_dir()]
            try:
                base = clean_path(os.path.commonpath(parents))
            except ValueError:
                base = parents[0]
        name, ok = QInputDialog.getText(self, "New folder",
                                        f"Name of the new folder inside\n{base}:")
        name = (name or "").strip().strip("/\\")
        if not ok or not name:
            return
        if any(c in name for c in '<>:"|?*'):
            QMessageBox.warning(self, "Layer Path Manager", "A folder name can't contain < > : \" | ? *")
            return
        self.setFilePath(os.path.join(base, name))

    def _browse(self):
        start = self._path if os.path.isdir(self._path) else last_dir()
        path = QFileDialog.getExistingDirectory(self, "Folder to put the data in", start)
        if path:
            self.setFilePath(path)

    def filePath(self) -> str:
        return self._path

    def setFilePath(self, path: str):
        path = clean_path(path) if path else ""
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.ItemDataRole.UserRole) == path:
                self.list.setCurrentRow(i)       # selects -> _list_picked
                return
        self.list.blockSignals(True)
        self.list.clearSelection()
        self.list.blockSignals(False)
        self.edit.setText(path)
        self._set(path)

    def folders(self) -> List[str]:
        return [self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())]


class CopyDataDialog(QDialog):
    """Select layers -> choose a folder -> Copy or Move -> done."""

    def __init__(self, records: List[LayerRecord], checked_ids, parent=None, target=None,
                 preset_records=None):
        super().__init__(parent)
        self.setWindowTitle("Layer Path Manager - Copy / Move To")
        self.resize(1040, 720)
        self.records = records
        self.all_records = records
        self.checked_ids = set(checked_ids)
        self.preset_ids = {r.layer_id for r in preset_records if r.is_file_based} \
            if preset_records else None
        self.plan = None
        lay = QVBoxLayout(self)
        lay.setSpacing(8)
        n_sel = len(self.preset_ids) if self.preset_ids else 0
        n_all = sum(1 for r in records if r.is_file_based)
        lay.addWidget(QLabel("<span style='font-size:12pt;font-weight:600'>Put the layers' data in "
                             "one folder</span>"))

        # 1. which layers (plain sentence + small switch)
        lrow = QHBoxLayout()
        lrow.addWidget(QLabel("<b>Layers:</b>"))
        self.rb_sel = QRadioButton(f"The {n_sel} selected layer(s)")
        self.rb_all = QRadioButton(f"All file layers ({n_all})")
        grp_l = QButtonGroup(self)
        grp_l.addButton(self.rb_sel)
        grp_l.addButton(self.rb_all)
        self.rb_sel.setVisible(n_sel > 0)
        (self.rb_sel if n_sel else self.rb_all).setChecked(True)
        lrow.addWidget(self.rb_sel)
        lrow.addWidget(self.rb_all)
        lrow.addStretch()
        lay.addLayout(lrow)

        # 2. where
        lay.addWidget(QLabel("<b>Into folder:</b> click one of your folders, or choose another one below"))
        cur = {r.folder for r in records if r.is_file_based and
               (self.preset_ids is None or r.layer_id in self.preset_ids)} if self.preset_ids else set()
        self.target = FolderPicker(records, current_folders=cur)
        if target:
            self.target.setFilePath(target)
        lay.addWidget(self.target)

        # 3. copy or move
        mrow = QHBoxLayout()
        mrow.addWidget(QLabel("<b>How:</b>"))
        self.rb_copy = QRadioButton("Copy  (originals stay where they are)")
        self.rb_move = QRadioButton("Move  (originals are deleted after the copy is checked)")
        grp_m = QButtonGroup(self)
        grp_m.addButton(self.rb_copy)
        grp_m.addButton(self.rb_move)
        self.rb_copy.setChecked(True)
        mrow.addWidget(self.rb_copy)
        mrow.addWidget(self.rb_move)
        mrow.addStretch()
        lay.addLayout(mrow)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        lay.addWidget(self.summary)

        # naming rule - shown only when a name in the folder is already taken
        self.naming_row = QWidget()
        nr = QHBoxLayout(self.naming_row)
        nr.setContentsMargins(0, 0, 0, 0)
        self.naming_label = QLabel()
        nr.addWidget(self.naming_label)
        from qgis.PyQt.QtWidgets import QComboBox
        self.naming = QComboBox()
        self.naming.addItem("add a number  (canals_2.shp)", "number")
        self.naming.addItem("put the source folder in front  (20260521_canals.shp)", "folder")
        self.naming.addItem("put my prefix in front", "prefix")
        nr.addWidget(self.naming)
        self.prefix = QLineEdit()
        self.prefix.setPlaceholderText("prefix, e.g. v2_")
        self.prefix.setMaximumWidth(140)
        nr.addWidget(self.prefix)
        nr.addStretch()
        lay.addWidget(self.naming_row)
        self.overrides = {}

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Dataset", "Layers", "Size", "New name in folder",
                                              "Status", "What we found"])
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._row_menu)
        self.table.cellDoubleClicked.connect(lambda r, _c: self._row_menu(None, r))
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self._elide = MiddleElideDelegate(self.table)
        self.table.setItemDelegateForColumn(3, self._elide)
        for i, w in enumerate((150, 55, 70, 190, 130)):
            self.table.setColumnWidth(i, w)
        self.table.setWordWrap(True)                     # findings wrap instead of being cut
        lay.addWidget(self.table, 1)
        self.found = QLabel()
        self.found.setWordWrap(True)
        lay.addWidget(self.found)

        self.options = QgsCollapsibleGroupBox("Options")
        self.options.setSaveCollapsedState(False)
        ol = QVBoxLayout(self.options)
        self.chk_structure = QCheckBox("Keep the sub-folders (instead of putting everything "
                                       "directly in the folder)")
        self.chk_switch = QCheckBox("Switch the layers to the new files (recommended)")
        self.chk_switch.setChecked(True)
        ol.addWidget(self.chk_structure)
        ol.addWidget(self.chk_switch)
        self.options.setCollapsed(True)
        lay.addWidget(self.options)

        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setStyleSheet("font-size: 8pt;")
        lay.addWidget(self.note)
        brow = QHBoxLayout()
        brow.addStretch()
        self.ok_btn = QPushButton("Copy")
        self.ok_btn.setDefault(True)
        self.ok_btn.clicked.connect(self._accept)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        brow.addWidget(self.ok_btn)
        brow.addWidget(cancel)
        lay.addLayout(brow)

        self.target.fileChanged.connect(lambda *_: self.refresh())
        for w in (grp_l, grp_m):
            w.buttonToggled.connect(lambda *_: self.refresh())
        self.chk_structure.toggled.connect(lambda *_: self.refresh())
        self.naming.currentIndexChanged.connect(lambda *_: self.refresh())
        self.prefix.textChanged.connect(lambda *_: self.refresh())
        self.chk_switch.toggled.connect(lambda *_: self.refresh())
        self.refresh()

    def is_move(self) -> bool:
        return self.rb_move.isChecked()

    def selected_records(self) -> List[LayerRecord]:
        recs = [r for r in self.all_records if r.is_file_based]
        if self.preset_ids and self.rb_sel.isChecked():
            return [r for r in recs if r.layer_id in self.preset_ids]
        return recs

    def _style_button(self):
        color = ("#cf5a1f", "#b34c18") if self.is_move() else ("#1f6feb", "#1a5fcc")
        self.ok_btn.setStyleSheet(
            f"QPushButton{{background:{color[0]};color:white;border:none;border-radius:5px;"
            f"padding:6px 16px;font-weight:600}} QPushButton:hover{{background:{color[1]}}}"
            "QPushButton:disabled{background:rgba(128,128,128,0.35)}")

    def refresh(self):
        from .data_copier import (ST_CONFLICT, ST_FAILED, ST_MISSING, ST_READY, ST_REUSE,
                                  ST_SAME, build_plan)
        from .utils import human_size
        move = self.is_move()
        self.chk_switch.setEnabled(not move)
        if move:
            self.chk_switch.setChecked(True)
        self._style_button()
        self.note.setText(
            "<b>Move:</b> each original is deleted only after its copy is complete, checked and the "
            "layer opens from the new folder. Data still used by other layers is kept. "
            "<b>A move can't be undone.</b>" if move else
            "Nothing is overwritten. Files already in the folder are compared with yours (geometry "
            "type, features, attributes): same data is reused, anything else is kept side by side. "
            "Undo switches the layers back; the copies stay on disk.")
        folder = self.target.filePath().strip()
        self.table.setRowCount(0)
        sel = self.selected_records()
        if not folder:
            self.plan = None
            folders = len({r.folder for r in sel})
            self.summary.setText(f"{len(sel)} layer(s) from {folders} folder(s). "
                                 "Choose the folder to put them in - it can be empty or new.")
            self.ok_btn.setEnabled(False)
            self.ok_btn.setText("Move" if move else "Copy")
            return
        naming = self.naming.currentData()
        self.prefix.setVisible(naming == "prefix")
        prefix = self.prefix.text().strip()
        if naming == "prefix" and not prefix:
            naming = "number"
        self.plan = plan = build_plan(sel, folder, keep_structure=self.chk_structure.isChecked(),
                                      naming=naming, prefix=prefix, overrides=self.overrides)
        if move:
            # datasets also used by layers that are NOT being moved keep their original
            sel_ids = {r.layer_id for r in sel}
            others = {r.path.lower() if IS_WIN else r.path
                      for r in self.all_records if r.is_file_based and r.layer_id not in sel_ids}
            for it in plan.items:
                key = it.src_path.lower() if IS_WIN else it.src_path
                it.keep_original = key in others
        level_color = {"warn": QColor(212, 140, 10), "info": QColor(31, 111, 235)}
        colors = {ST_READY: None, ST_REUSE: "#1e8c3c", ST_SAME: "#808080",
                  ST_CONFLICT: "#cf222e", ST_MISSING: "#cf222e", ST_FAILED: "#cf222e"}
        verb = "Move" if move else "Copy"
        labels = {ST_READY: verb,
                  ST_REUSE: "Use existing file" + (", delete original" if move else ""),
                  ST_SAME: "Already here",
                  ST_MISSING: "Skipped (missing)", ST_FAILED: "Can't copy"}
        self.table.setRowCount(len(plan.items))
        for row, it in enumerate(plan.items):
            try:
                rel = os.path.relpath(it.dest_path, plan.target)
            except ValueError:
                rel = it.dest_path
            status = labels.get(it.status, it.status)
            if it.status == ST_READY and move and it.keep_original:
                status = "Copy (original kept: other layers use it)"
            elif it.status == ST_READY and it.renamed:
                status += ", new name"
            found = it.notice or ("-" if it.status in (ST_READY, ST_REUSE) else it.message)
            vals = (os.path.basename(it.src_path.rstrip("/")), str(len(it.layer_ids)),
                    human_size(it.size) if it.size else "-", rel, status, found)
            for col, v in enumerate(vals):
                cell = QTableWidgetItem(v)
                tip = f"From: {it.src_path}\nTo:   {it.dest_path}\nLayers: {', '.join(it.layer_names)}"
                if it.message:
                    tip += f"\n{it.message}"
                cell.setToolTip(tip)
                if col == 4 and colors.get(it.status):
                    cell.setForeground(QColor(colors[it.status]))
                if col == 5 and it.level in level_color:
                    cell.setForeground(level_color[it.level])
                if it.level == "warn":
                    bg = QColor(212, 140, 10)
                    bg.setAlpha(30)
                    cell.setBackground(bg)
                if it.duplicate_of or it.equivalent or it.src_path in self.overrides:
                    cell.setToolTip(tip + "\n\nDouble-click or right-click this row to choose "
                                    "what to do.")
                self.table.setItem(row, col, cell)
        n_copy = len(plan.to_copy)
        n_reuse = sum(1 for i in plan.items if i.status == ST_REUSE)
        problems = sum(1 for i in plan.items if i.status in (ST_MISSING, ST_FAILED))
        renamed = sum(1 for i in plan.items if i.status == ST_READY and
                      os.path.basename(i.dest_path) != os.path.basename(i.src_path.rstrip("/")))
        n_layers = sum(len(i.layer_ids) for i in plan.items if i.status in (ST_READY, ST_REUSE))
        bits = [f"<b>{n_layers}</b> layer(s) &rarr; <b>{n_copy}</b> dataset(s) to "
                f"{'move' if move else 'copy'} ({human_size(plan.copy_bytes)})"]
        if plan.free_bytes >= 0:
            bits.append(f"{human_size(plan.free_bytes)} free")
        if renamed:
            bits.append(f"{renamed} renamed to avoid a name clash")
        if n_reuse:
            bits.append(f"{n_reuse} already there")
        if problems:
            bits.append(f"<span style='color:#cf222e'>{problems} can't be copied</span>")
        txt = " &middot; ".join(bits)
        warns = [i for i in plan.items if i.level == "warn"]
        infos = [i for i in plan.items if i.level == "info"]
        clashes = [i for i in plan.items if i.renamed or (i.notice and "already there" in i.notice)]
        found = []
        if warns:
            found.append(f"<span style='color:#b26b00'>&#9888; {len(warns)} need a look</span> "
                         "(another version or the same data under another name)")
        if infos:
            found.append(f"<span style='color:#1f6feb'>&#9432; {len(infos)} note(s)</span>")
        if warns or any(i.equivalent or i.duplicate_of for i in plan.items):
            found.append("Double-click a row to choose what to do with it")
        self.table.resizeRowsToContents()
        self.found.setText(" &middot; ".join(found))
        self.found.setVisible(bool(found))
        self.naming_row.setVisible(bool(clashes) or self.naming.currentIndex() > 0)
        n_taken = sum(1 for i in plan.items if i.renamed)
        self.naming_label.setText(f"<b>{n_taken} name(s) already taken</b> - name yours:"
                                  if n_taken else "<b>If a name is taken</b>, name yours:")
        if not plan.enough_space:
            txt = f"<span style='color:#cf222e'><b>{plan.message}</b></span><br>" + txt
        self.summary.setText(txt)
        if n_copy:
            self.ok_btn.setText(f"{'Move' if move else 'Copy'} {n_layers} Layer(s)")
        elif n_reuse and move:
            self.ok_btn.setText(f"Move {n_layers} Layer(s)")
        elif n_reuse and self.chk_switch.isChecked():
            self.ok_btn.setText("Switch Layers to Existing Copies")
        else:
            self.ok_btn.setText("Move" if move else "Copy")
        self.ok_btn.setEnabled(plan.enough_space and (
            n_copy > 0 or (n_reuse > 0 and self.chk_switch.isChecked())))

    def _row_menu(self, pos, row=None):
        if self.plan is None:
            return
        if row is None:
            row = self.table.rowAt(pos.y())
        if row < 0 or row >= len(self.plan.items):
            return
        it = self.plan.items[row]
        menu = QMenu(self)
        if it.duplicate_of:
            menu.addAction(f"Use the existing {os.path.basename(it.duplicate_of)} instead "
                           "(don't copy)", lambda: self._override(it, "use:" + it.duplicate_of))
        if it.equivalent and it.src_path not in self.overrides:
            menu.addAction("Copy anyway as a new file", lambda: self._override(it, "copy"))
        if it.src_path in self.overrides:
            menu.addAction("Undo my choice for this row", lambda: self._override(it, None))
        if it.notice:
            if not menu.isEmpty():
                menu.addSeparator()
            info = menu.addAction(it.notice[:120] + ("..." if len(it.notice) > 120 else ""))
            info.setEnabled(False)
        if menu.isEmpty():
            return
        where = self.table.viewport().mapToGlobal(pos) if pos is not None else \
            self.table.viewport().mapToGlobal(self.table.visualItemRect(self.table.item(row, 0)).bottomLeft())
        menu.exec(where)

    def _override(self, it, value):
        if value is None:
            self.overrides.pop(it.src_path, None)
        else:
            self.overrides[it.src_path] = value
        self.refresh()

    def _accept(self):
        folder = self.target.filePath().strip()
        if not folder:
            return
        if not os.path.isdir(folder):
            # the dialog already says "new folder, created when you start" - just create it
            try:
                os.makedirs(folder)
            except OSError as exc:
                QMessageBox.critical(self, "Layer Path Manager", f"Could not create the folder:\n{exc}")
                return
        self.refresh()            # fresh plan right before copying
        if not self.ok_btn.isEnabled():
            return
        if self.is_move():
            from .data_copier import ST_REUSE
            movable = self.plan.to_copy + [i for i in self.plan.items if i.status == ST_REUSE]
            n = len(movable)
            kept = sum(1 for i in movable if i.keep_original)
            msg = (f"Move {n} dataset(s) to\n{folder}?\n\nThe original files are deleted after "
                   "each copy is checked and the layers open from the new folder."
                   + (f"\n{kept} dataset(s) used by other layers will be copied but kept." if kept else "")
                   + "\n\nThis can't be undone.")
            if QMessageBox.warning(self, "Move data", msg,
                                   QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                   QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
        remember_dir(folder)
        self.accept()


# ======================================================================
# Relocation options
# ======================================================================

SCOPE_BROKEN = "broken"
SCOPE_CHECKED = "checked"
SCOPE_ALL = "all"


@dataclass
class RelocationRequest:
    mode: str               # "search" or "prefix"
    search_root: str
    recursive: bool
    old_root: str
    scope: str
    options: MatchOptions


class RelocationDialog(QDialog):
    """Asks one question - where is the data now? - and keeps options folded away."""

    def __init__(self, records: List[LayerRecord], checked_ids, title, default_scope,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Layer Path Manager - {title}")
        self.setMinimumWidth(600)
        self.records = records
        self.checked_ids = set(checked_ids)
        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        question = "Where is the data now?" if default_scope == SCOPE_BROKEN \
            else "Where did you copy or move the data?"
        head = QLabel(f"<span style='font-size:12pt;font-weight:600'>{question}</span>")
        lay.addWidget(head)
        if default_scope != SCOPE_BROKEN:
            hint = QLabel("Choose the folder that <b>already contains</b> your data. "
                          "Haven't copied it yet? Close this and use <b>Copy / Move To...</b> instead.")
            hint.setWordWrap(True)
            lay.addWidget(hint)
        self.root_widget = QgsFileWidget()
        self.root_widget.setStorageMode(QgsFileWidget.StorageMode.GetDirectory)
        self.root_widget.setDialogTitle("Folder that contains the data")
        self.root_widget.setDefaultRoot(last_dir())
        self.root_widget.setFilePath(_settings_value("relocRoot", ""))
        self.root_widget.setMinimumHeight(30)
        lay.addWidget(self.root_widget)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        lay.addWidget(self.summary)

        # ---------------- everything else: folded away ----------------
        self.options = QgsCollapsibleGroupBox("Options")
        self.options.setSaveCollapsedState(False)
        ol = QVBoxLayout(self.options)
        ol.setSpacing(4)
        n_broken = sum(1 for r in records if r.is_file_based and r.status == "Broken")
        n_checked = sum(1 for r in records if r.is_file_based and r.layer_id in self.checked_ids)
        n_all = sum(1 for r in records if r.is_file_based)
        ol.addWidget(QLabel("<b>Layers</b>"))
        row = QHBoxLayout()
        self.rb_broken = QRadioButton(f"Broken ({n_broken})")
        self.rb_checked = QRadioButton(f"Checked ({n_checked})")
        self.rb_all = QRadioButton(f"All file layers ({n_all})")
        self.scope_group = QButtonGroup(self)
        for rb in (self.rb_broken, self.rb_checked, self.rb_all):
            self.scope_group.addButton(rb)
            row.addWidget(rb)
        row.addStretch()
        ol.addLayout(row)
        {SCOPE_BROKEN: self.rb_broken, SCOPE_CHECKED: self.rb_checked,
         SCOPE_ALL: self.rb_all}[default_scope].setChecked(True)
        self.rb_checked.setEnabled(n_checked > 0)
        self.rb_broken.setEnabled(n_broken > 0)

        ol.addWidget(QLabel("<b>How to find each file</b>"))
        self.rb_search = QRadioButton("Search the folder for the same file name (recommended)")
        self.rb_prefix = QRadioButton("Same sub-folders as before, only the main folder changed:")
        self.rb_search.setChecked(_settings_value("relocMode", "search") != "prefix")
        self.rb_prefix.setChecked(not self.rb_search.isChecked())
        ol.addWidget(self.rb_search)
        ol.addWidget(self.rb_prefix)
        pr = QHBoxLayout()
        pr.setContentsMargins(24, 0, 0, 0)
        pr.addWidget(QLabel("Old main folder:"))
        self.old_root = QLineEdit()
        self.old_root.setPlaceholderText("Detected automatically from the layers")
        pr.addWidget(self.old_root)
        ol.addLayout(pr)

        self.opt_box = QWidget()
        grid = QGridLayout(self.opt_box)
        grid.setContentsMargins(24, 0, 0, 0)
        self.chk_recursive = QCheckBox("Include sub-folders")
        self.chk_case = QCheckBox("Ignore upper/lower case")
        self.chk_ext = QCheckBox("Accept .tif/.tiff, .jpg/.jpeg ...")
        self.chk_struct = QCheckBox("Use folder names to tell same-named files apart")
        self.chk_fuzzy = QCheckBox("Also suggest similar names (you review them)")
        self.spin_fuzzy = QDoubleSpinBox()
        self.spin_fuzzy.setRange(0.5, 0.99)
        self.spin_fuzzy.setSingleStep(0.05)
        self.spin_fuzzy.setDecimals(2)
        self.spin_fuzzy.setPrefix("similarity ")
        grid.addWidget(self.chk_recursive, 0, 0)
        grid.addWidget(self.chk_case, 0, 1)
        grid.addWidget(self.chk_ext, 1, 0)
        grid.addWidget(self.chk_struct, 1, 1)
        grid.addWidget(self.chk_fuzzy, 2, 0)
        grid.addWidget(self.spin_fuzzy, 2, 1, Qt.AlignmentFlag.AlignLeft)
        ol.addWidget(self.opt_box)
        self.chk_recursive.setChecked(_settings_value("recursive", True, bool))
        self.chk_case.setChecked(_settings_value("caseInsensitive", True, bool))
        self.chk_ext.setChecked(_settings_value("extVariants", True, bool))
        self.chk_struct.setChecked(_settings_value("structure", True, bool))
        self.chk_fuzzy.setChecked(_settings_value("fuzzy", False, bool))
        self.spin_fuzzy.setValue(float(_settings_value("fuzzyThreshold", 0.8)))
        self.spin_fuzzy.setEnabled(self.chk_fuzzy.isChecked())
        self.chk_fuzzy.toggled.connect(self.spin_fuzzy.setEnabled)
        lay.addWidget(self.options)
        # keep options closed unless the user previously chose non-default ones
        self.options.setCollapsed(not self.rb_prefix.isChecked() and not self.chk_fuzzy.isChecked())

        note = QLabel("Nothing changes yet - you will see every match before anything is applied. "
                      "Files are never copied, moved or deleted.")
        note.setWordWrap(True)
        note.setStyleSheet("color: palette(mid); font-size: 8pt;")
        lay.addWidget(note)
        self.buttons = QDialogButtonBox()
        self.find_btn = self.buttons.addButton("Find Matches", QDialogButtonBox.ButtonRole.AcceptRole)
        self.find_btn.setIcon(theme_icon("mActionShowAllLayers.svg"))
        self.find_btn.setDefault(True)
        self.buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        lay.addWidget(self.buttons)

        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)
        self.rb_search.toggled.connect(self._mode_changed)
        self.scope_group.buttonToggled.connect(lambda *_: self._mode_changed())
        self.chk_recursive.toggled.connect(lambda *_: self._update_summary())
        self.root_widget.fileChanged.connect(lambda *_: self._update_summary())
        self.old_root.textChanged.connect(lambda *_: self._update_summary())
        self._mode_changed()

    def _update_summary(self):
        n = len(self.selected_records())
        what = {SCOPE_BROKEN: "broken layer(s)", SCOPE_CHECKED: "checked layer(s)",
                SCOPE_ALL: "file-based layer(s)"}[self.scope()]
        if self.rb_prefix.isChecked():
            how = (f"keeping their sub-folders, replacing <i>{html.escape(self.old_root.text())}</i> "
                   "with this folder")
        else:
            how = ("by file name in this folder" +
                   (" and all its sub-folders" if self.chk_recursive.isChecked() else ""))
        self.summary.setText(f"{n} {what} will be looked for {how}.")
        self.find_btn.setEnabled(n > 0)

    def scope(self):
        if self.rb_checked.isChecked():
            return SCOPE_CHECKED
        if self.rb_all.isChecked():
            return SCOPE_ALL
        return SCOPE_BROKEN

    def selected_records(self) -> List[LayerRecord]:
        s = self.scope()
        recs = [r for r in self.records if r.is_file_based]
        if s == SCOPE_BROKEN:
            return [r for r in recs if r.status == "Broken"]
        if s == SCOPE_CHECKED:
            return [r for r in recs if r.layer_id in self.checked_ids]
        return recs

    def _update_old_root(self):
        if self.rb_prefix.isChecked():
            self.old_root.setText(PathMatcher.common_root([r.path for r in self.selected_records()]))

    def _mode_changed(self):
        prefix = self.rb_prefix.isChecked()
        self.old_root.setEnabled(prefix)
        self.opt_box.setEnabled(not prefix)
        self._update_old_root()
        self._update_summary()

    def _accept(self):
        root = self.root_widget.filePath().strip()
        if not root or not os.path.isdir(root):
            QMessageBox.warning(self, "Layer Path Manager", "Please choose the folder that contains "
                                "the data.")
            return
        if not self.selected_records():
            QMessageBox.information(self, "Layer Path Manager", "No layers match the chosen scope.")
            return
        if self.rb_prefix.isChecked() and not self.old_root.text().strip():
            QMessageBox.warning(self, "Layer Path Manager", "Please enter the old root folder.")
            return
        _set_setting("relocMode", "prefix" if self.rb_prefix.isChecked() else "search")
        _set_setting("relocRoot", root)
        _set_setting("recursive", self.chk_recursive.isChecked())
        _set_setting("caseInsensitive", self.chk_case.isChecked())
        _set_setting("extVariants", self.chk_ext.isChecked())
        _set_setting("structure", self.chk_struct.isChecked())
        _set_setting("fuzzy", self.chk_fuzzy.isChecked())
        _set_setting("fuzzyThreshold", self.spin_fuzzy.value())
        remember_dir(root)
        self.accept()

    def request(self) -> RelocationRequest:
        return RelocationRequest(
            mode="prefix" if self.rb_prefix.isChecked() else "search",
            search_root=clean_path(self.root_widget.filePath().strip()),
            recursive=self.chk_recursive.isChecked(),
            old_root=self.old_root.text().strip(),
            scope=self.scope(),
            options=MatchOptions(
                case_insensitive=self.chk_case.isChecked(),
                extension_variants=self.chk_ext.isChecked(),
                use_structure=self.chk_struct.isChecked(),
                fuzzy=self.chk_fuzzy.isChecked(),
                fuzzy_threshold=self.spin_fuzzy.value()),
        )


# ======================================================================
# Normalization
# ======================================================================


class NormalizeDialog(QDialog):
    def __init__(self, normalizer: PathNormalizer, records: List[LayerRecord], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Layer Path Manager - Normalize Paths")
        self.resize(1000, 560)
        self.normalizer = normalizer
        self.records = records
        self.items: List[NormalizationItem] = []
        lay = QVBoxLayout(self)

        proj = normalizer.project
        saved = normalizer.project_saved()
        mode_txt = "relative" if project_uses_relative_paths(proj) else "absolute"
        info = QLabel(
            f"<b>Project file:</b> {proj.fileName() or '<i>not saved</i>'}<br>"
            f"<b>Base folder for relative paths:</b> {normalizer.project_dir() or '-'}<br>"
            f"<b>Current path storage:</b> {mode_txt}. In QGIS this is a project-wide setting: "
            "layer paths are held absolute in memory and written relative or absolute when the "
            "project is saved.")
        info.setWordWrap(True)
        lay.addWidget(info)
        if not saved:
            warn = QLabel("<span style='color:#cf222e'><b>The project has not been saved.</b> "
                          "Relative paths need a project location - save the project first.</span>")
            warn.setWordWrap(True)
            lay.addWidget(warn)

        modes = QHBoxLayout()
        self.group = QButtonGroup(self)
        self.radios = {}
        for mode in (MODE_TO_RELATIVE, MODE_TO_ABSOLUTE, MODE_CLEAN):
            rb = QRadioButton(MODE_LABELS[mode])
            self.group.addButton(rb)
            self.radios[mode] = rb
            modes.addWidget(rb)
        modes.addStretch()
        lay.addLayout(modes)
        (self.radios[MODE_TO_ABSOLUTE] if mode_txt == "relative" else
         self.radios[MODE_TO_RELATIVE]).setChecked(True)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Layer", "Current (as stored)", "Proposed", "Result"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self._elide = MiddleElideDelegate(self.table)
        self.table.setItemDelegateForColumn(1, self._elide)
        self.table.setItemDelegateForColumn(2, self._elide)
        self.table.setWordWrap(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        for i, w in enumerate((170, 300, 300)):
            self.table.setColumnWidth(i, w)
        lay.addWidget(self.table, 1)
        self.summary = QLabel()
        lay.addWidget(self.summary)

        self.chk_save = QCheckBox("Save the project after applying (writes only the .qgs/.qgz file)")
        self.chk_save.setEnabled(saved)
        lay.addWidget(self.chk_save)
        self.buttons = QDialogButtonBox()
        self.apply_btn = self.buttons.addButton("Apply", QDialogButtonBox.ButtonRole.AcceptRole)
        self.buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        lay.addWidget(self.buttons)
        self.buttons.accepted.connect(self._confirm)
        self.buttons.rejected.connect(self.reject)
        self.group.buttonToggled.connect(lambda *_: self.refresh())
        self.refresh()

    def mode(self):
        for m, rb in self.radios.items():
            if rb.isChecked():
                return m
        return MODE_TO_RELATIVE

    def refresh(self):
        self.items = self.normalizer.preview(self.records, self.mode())
        self.table.setRowCount(len(self.items))
        changed = convertible_changes = blocked = 0
        for row, it in enumerate(self.items):
            vals = (it.layer_name, it.current, it.proposed, it.message)
            for col, v in enumerate(vals):
                item = QTableWidgetItem(v)
                item.setToolTip(v)
                if col == 2 and it.changed and it.convertible:
                    item.setForeground(QColor(30, 140, 60))
                    f = item.font()
                    f.setBold(True)
                    item.setFont(f)
                if col == 3 and not it.convertible:
                    item.setForeground(QColor(207, 34, 46))
                self.table.setItem(row, col, item)
            if it.changed:
                changed += 1
            if it.changed and it.convertible:
                convertible_changes += 1
            if not it.convertible:
                blocked += 1
        self.summary.setText(f"{len(self.items)} file-based layer(s): <b>{convertible_changes}</b> "
                             f"will change, {blocked} cannot be converted.")
        mode = self.mode()
        if mode == MODE_CLEAN:
            self.apply_btn.setEnabled(convertible_changes > 0)
        else:
            self.apply_btn.setEnabled(self.normalizer.project_saved() and bool(self.items))

    def _confirm(self):
        mode = self.mode()
        if mode == MODE_CLEAN:
            msg = (f"Rewrite the in-memory source of {sum(1 for i in self.items if i.proposal)} "
                   "layer(s) with the normalized path? Provider parameters are preserved.")
        else:
            target = "relative" if mode == MODE_TO_RELATIVE else "absolute"
            msg = (f"Set the project to store paths as {target}?\n\nThe preview shows exactly how "
                   "each path will be written. The change is applied when the project is saved "
                   "and can be restored with 'Restore All' during this session.")
        if QMessageBox.question(self, "Normalize Paths", msg) == QMessageBox.StandardButton.Yes:
            self.accept()


# ======================================================================
# Report viewer
# ======================================================================


class ReportDialog(QDialog):
    def __init__(self, title, html_text, plain_text, parent=None,
                 csv_rows_provider: Optional[Callable[[], list]] = None, base_name="report"):
        super().__init__(parent)
        self.setWindowTitle(f"Layer Path Manager - {title}")
        self.resize(820, 680)
        self.title = title
        self.html_text = html_text
        self.plain_text = plain_text
        self.csv_rows_provider = csv_rows_provider
        self.base_name = base_name
        lay = QVBoxLayout(self)
        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(True)
        self.browser.setHtml(html_text)
        lay.addWidget(self.browser, 1)
        row = QHBoxLayout()
        b_save = QPushButton(theme_icon("mActionFileSave.svg"), "Save Report...")
        b_save.clicked.connect(self._save)
        row.addWidget(b_save)
        if csv_rows_provider:
            b_csv = QPushButton(theme_icon("mActionFileSaveAs.svg"), "Export Dependency List (CSV)...")
            b_csv.clicked.connect(self._save_csv)
            row.addWidget(b_csv)
        b_copy = QPushButton(theme_icon("mActionEditCopy.svg"), "Copy as Text")
        b_copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.plain_text))
        row.addWidget(b_copy)
        row.addStretch()
        b_close = QPushButton("Close")
        b_close.clicked.connect(self.accept)
        row.addWidget(b_close)
        lay.addLayout(row)

    def _save(self):
        from .report_exporter import export_report_document
        path, _ = QFileDialog.getSaveFileName(
            self, "Save report", os.path.join(last_dir(), f"{self.base_name}.html"),
            "HTML report (*.html);;Text report (*.txt)")
        if not path:
            return
        try:
            export_report_document(path, self.plain_text, self.html_text, self.title)
            remember_dir(path)
            QMessageBox.information(self, "Layer Path Manager", f"Report saved:\n{path}")
        except OSError as exc:
            QMessageBox.critical(self, "Layer Path Manager", f"Could not save the report:\n{exc}")

    def _save_csv(self):
        from .report_exporter import export_rows_csv
        path, _ = QFileDialog.getSaveFileName(
            self, "Export dependency list", os.path.join(last_dir(), f"{self.base_name}_dependencies.csv"),
            "CSV (*.csv)")
        if not path:
            return
        try:
            export_rows_csv(path, self.csv_rows_provider())
            remember_dir(path)
            QMessageBox.information(self, "Layer Path Manager", f"Dependency list saved:\n{path}")
        except OSError as exc:
            QMessageBox.critical(self, "Layer Path Manager", f"Could not save the file:\n{exc}")

"""
Update COGO, laid out like the Update COGO pane of ArcGIS Pro: "Change the selection." and the selected lines, the COGO
settings (direction type, direction units, distance units) and the Update button. The Direction, Distance, Radius and
ArcLength fields of every selected line are rewritten from its geometry: a single course gives its own direction and
distance (an arc also its radius and arc length); a line with several vertices gets the chord from its first to its
last point and its full length. Missing COGO fields are created when "Create the COGO fields" is ticked. One undoable
edit command per layer.
"""

from qgis.core import Qgis
from qgis.PyQt import sip
from qgis.PyQt.QtWidgets import QHBoxLayout

from ...core.modify_features import COGO_FIELDS
from .cogo import CogoSettings, SettingsForm, update_cogo_values
from .page import DoneStrip, SelectedTree, button, section_label, selection_banner
from .reshape import PickTool
from .transform import editable_selection

LINE = Qgis.GeometryType.Line
NEED_SELECTION = "Select one or more lines"


def selected_lines(canvas):
    """[(layer, ids)] of the selected features of the editable line layers shown on the map."""
    return [(layer, list(ids)) for layer, ids in editable_selection(canvas) if layer.geometryType() == LINE]


class UpdateCogoTool(PickTool):
    KEY = "update_cogo"
    LABEL = "Update COGO"
    HINT = "select lines, choose the COGO settings, then press Update."
    SHOW_HINT = False
    SHOW_STATUS = False
    SHOWS_SELECTION = False
    SELECTS = True
    AUTO_SELECT = True
    SELECT_ONE_SHOT = False

    def __init__(self, canvas, iface, select_tool=None):
        self.info = self.tree = self.banner = self.form = self.update_button = None
        self._result = None
        self._result_sig = frozenset()
        super().__init__(canvas, iface, select_tool)

    # ------------------------------------------------------------------ page
    def build_selection_row(self, box):
        self.info = DoneStrip()
        box.addWidget(self.info)

    def build_page(self, box):
        self.banner = selection_banner(self._banner_clicked)
        self.banner.setChecked(self._selecting)
        self._select_button = self.banner
        box.addWidget(self.banner)
        self.tree = SelectedTree()
        box.addWidget(self.tree)
        box.addWidget(section_label("COGO settings"))
        self.form = SettingsForm(CogoSettings.load())
        for signal in (self.form.type_box.currentIndexChanged, self.form.unit_box.currentIndexChanged,
                       self.form.distance_box.currentIndexChanged, self.form.create_box.toggled):
            signal.connect(lambda _v=None: self.form.settings().save())
        box.addWidget(self.form)

    def build_footer(self, box):
        row = QHBoxLayout()
        row.addStretch(1)
        self.update_button = button("Update", self.update, "Rewrite the COGO fields of the selected lines from their "
                                    "geometry")
        row.addWidget(self.update_button)
        box.addLayout(row)

    def _banner_clicked(self):
        self.set_selecting(not self._selecting)

    def _signature(self):
        return frozenset((layer.id(), fid) for layer, ids in selected_lines(self.canvas()) for fid in ids)

    def update_page(self):
        if self.update_button is None or sip.isdeleted(self.update_button):
            return
        self.tree.refresh(self.canvas())
        lines = selected_lines(self.canvas())
        self.update_button.setEnabled(bool(lines))
        self.banner.setText("Change the selection." if lines else NEED_SELECTION)
        if self._result and self._signature() != self._result_sig:
            self._result = None                         # the selection moved on: the result no longer applies
        if self._result:
            self.info.show_text(*self._result)
        else:
            self.info.hide()

    # ------------------------------------------------------------------ the run
    def update(self):
        lines = selected_lines(self.canvas())
        if not lines:
            self.info.show_text(NEED_SELECTION + " in an editable layer.", ok="info")
            return 0
        settings = self.form.settings()
        settings.save()
        total, notes = 0, []
        for layer, ids in lines:
            try:
                count, added, missing = update_cogo_values(layer, ids, settings)
            except ValueError as exc:
                notes.append(f"{layer.name()}: {exc}")
                continue
            total += count
            if added:
                notes.append(f"added {', '.join(added)} to {layer.name()}")
            if missing and count == 0 and len(missing) == len(COGO_FIELDS):
                notes.append(f"{layer.name()} has no COGO fields (Direction, Distance, Radius, ArcLength)")
        text = "Update COGO completed." if total else "Nothing was updated."
        if total:
            text += f" {total} line(s)."
        if notes:
            text += f" ({'; '.join(notes)})"
        self._result = (text, bool(total))
        self._result_sig = self._signature()
        self.say(text + (" (Ctrl+Z undoes it)" if total else ""))
        self.refresh_page()
        return total


TOOL_CLASSES = {UpdateCogoTool.KEY: UpdateCogoTool}

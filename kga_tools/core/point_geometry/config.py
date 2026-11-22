"""Settings and result containers shared by the GUI, the task and the engine."""

from dataclasses import dataclass, field
from typing import List, Optional

MODE_LINE = "line"
MODE_POLYGON = "polygon"

LINE_CONTINUOUS = "continuous"
LINE_SEGMENTS = "segments"

ORDER_FID = "fid"
ORDER_FIELD = "field"
ORDER_EXPRESSION = "expression"

NULL_GROUP_MERGE = "merge"  # NULL/empty values form one group
NULL_GROUP_SKIP = "skip"  # points with NULL/empty values are excluded (reported)

POLY_ONLY = "polygon"  # polygon layer only
POLY_AND_BOUNDARY = "both"  # polygon layer + matching boundary line layer
POLY_BOUNDARY_ONLY = "boundary"  # closed boundary lines only, no polygon layer

FORMAT_GPKG = "GPKG"
FORMAT_SHP = "ESRI Shapefile"
FORMAT_MEMORY = "memory"


@dataclass
class ConversionSettings:
    mode: str = MODE_LINE
    line_structure: str = LINE_CONTINUOUS
    selected_only: bool = False

    order_method: str = ORDER_FID
    order_field: str = ""
    order_expression: str = ""
    reverse_order: bool = False

    group_field: str = ""  # empty -> no grouping
    null_group_policy: str = NULL_GROUP_MERGE

    remove_duplicates: bool = True
    validate: bool = True
    repair: bool = True
    preserve_attributes: bool = True
    polygon_output: str = POLY_ONLY

    output_format: str = FORMAT_GPKG
    output_path: str = ""
    output_layer_name: str = ""
    add_to_project: bool = True

    @property
    def grouped(self):
        return bool(self.group_field)

    @property
    def writes_polygons(self):
        return self.mode == MODE_POLYGON and self.polygon_output != POLY_BOUNDARY_ONLY

    @property
    def writes_boundary(self):
        return self.mode == MODE_POLYGON and self.polygon_output in (
            POLY_AND_BOUNDARY, POLY_BOUNDARY_ONLY)

    # Backwards-compatible alias: True = polygon + boundary
    @property
    def boundary_line(self):
        return self.polygon_output == POLY_AND_BOUNDARY

    @boundary_line.setter
    def boundary_line(self, value):
        self.polygon_output = POLY_AND_BOUNDARY if value else POLY_ONLY


@dataclass
class Issue:
    level: str  # "info" | "warning" | "error"
    where: str
    message: str

    def text(self):
        return "{}: {}".format(self.where, self.message) if self.where else self.message


@dataclass
class OutputInfo:
    role: str  # "main" | "boundary"
    layer_name: str
    uri: str = ""  # for file outputs
    path: str = ""
    feature_count: int = 0
    memory_layer: object = None  # QgsVectorLayer for temporary output


@dataclass
class ConversionResult:
    input_layer_name: str = ""
    scope: str = ""
    mode_label: str = ""
    points_read: int = 0
    points_used: int = 0
    points_skipped: int = 0
    groups: int = 0
    groups_failed: int = 0
    output_features: int = 0
    successful_geometries: int = 0
    repaired_geometries: int = 0
    invalid_geometries: int = 0
    elapsed: float = 0.0
    cancelled: bool = False
    fatal_error: Optional[str] = None
    outputs: List[OutputInfo] = field(default_factory=list)
    issues: List[Issue] = field(default_factory=list)

    MAX_ISSUES = 500

    def _add(self, level, where, message):
        if len(self.issues) < self.MAX_ISSUES:
            self.issues.append(Issue(level, where, message))
        elif len(self.issues) == self.MAX_ISSUES:
            self.issues.append(
                Issue("warning", "", "Further messages truncated (see QGIS log panel).")
            )

    def info(self, where, message):
        self._add("info", where, message)

    def warning(self, where, message):
        self._add("warning", where, message)

    def error(self, where, message):
        self._add("error", where, message)

    def count(self, level):
        return sum(1 for i in self.issues if i.level == level)

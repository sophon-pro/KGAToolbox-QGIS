# -*- coding: utf-8 -*-
"""
Layer Path Manager - path normalisation and standardisation.

How QGIS stores paths
---------------------
Inside a running QGIS session every file-based layer source is held as an
*absolute* path.  Whether paths are written to the .qgs/.qgz file as relative
or absolute is a single project-wide setting (Project Properties > General >
Save paths).  QGIS resolves relative paths against the folder that contains
the project file - for .qgz this is the folder of the .qgz archive, never the
plugin directory.

Therefore:

* "Convert to relative / absolute" previews, per layer, exactly how the path
  will be written using ``QgsPathResolver`` and then switches the project
  setting.  Paths that cannot be expressed relatively (another drive letter or
  network share) are reported and stay absolute.
* "Clean paths" rewrites in-memory layer URIs whose *path part* contains
  back-slashes, duplicated separators, '.' or '..' segments.  Only the path is
  touched; all provider parameters are preserved.  A cleaned path is applied
  only when it provably points to the same file.
"""

import os
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from qgis.core import QgsPathResolver, QgsProject

from .path_repair import (
    ProjectSettingEntry, SessionHistory, SourceUpdater, make_proposal, set_proposal_path,
)
from .utils import (
    IS_WINDOWS, ChangeProposal, LayerRecord, clean_path, decode_source, looks_absolute,
    project_uses_relative_paths, set_project_relative_paths,
)

MODE_TO_RELATIVE = "to_relative"
MODE_TO_ABSOLUTE = "to_absolute"
MODE_CLEAN = "clean"

MODE_LABELS = {
    MODE_TO_RELATIVE: "Convert absolute paths to relative",
    MODE_TO_ABSOLUTE: "Convert relative paths to absolute",
    MODE_CLEAN: "Normalize separators / remove redundant segments",
}


@dataclass
class NormalizationItem:
    layer_id: str
    layer_name: str
    current: str          # current stored form (or raw path for clean)
    proposed: str         # proposed stored form (or cleaned path)
    changed: bool
    convertible: bool
    message: str
    proposal: Optional[ChangeProposal] = None   # only for MODE_CLEAN


class PathNormalizer:

    def __init__(self, project: Optional[QgsProject] = None,
                 history: Optional[SessionHistory] = None):
        self.project = project or QgsProject.instance()
        self.history = history or SessionHistory()

    # ------------------------------------------------------------------
    def project_saved(self) -> bool:
        return bool(self.project.fileName())

    def project_dir(self) -> str:
        return clean_path(self.project.absolutePath()) if self.project_saved() else ""

    def preview(self, records: List[LayerRecord], mode: str) -> List[NormalizationItem]:
        file_recs = [r for r in records if r.is_file_based]
        if mode == MODE_CLEAN:
            return [self._preview_clean(r) for r in file_recs]

        items = []
        if not self.project_saved():
            for r in file_recs:
                items.append(NormalizationItem(
                    r.layer_id, r.name, r.path, r.path, False, False,
                    "Project has not been saved - save it first so paths can be "
                    "resolved against the project folder"))
            return items

        rel_resolver = QgsPathResolver(self.project.absoluteFilePath())
        currently_relative = project_uses_relative_paths(self.project)
        for r in file_recs:
            rel = rel_resolver.writePath(r.path)
            current = rel if currently_relative else r.path
            if mode == MODE_TO_RELATIVE:
                if looks_absolute(rel):
                    items.append(NormalizationItem(
                        r.layer_id, r.name, current, rel, False, False,
                        "Cannot be relative: dataset is on another drive or network share"))
                else:
                    changed = current != rel
                    items.append(NormalizationItem(
                        r.layer_id, r.name, current, rel, changed, True,
                        "Will be stored relative to the project folder" if changed
                        else "Already stored relative"))
            else:  # to absolute
                changed = current != r.path
                items.append(NormalizationItem(
                    r.layer_id, r.name, current, r.path, changed, True,
                    "Will be stored as an absolute path" if changed else "Already absolute"))
        return items

    def _preview_clean(self, r: LayerRecord) -> NormalizationItem:
        raw = decode_source(r.provider, r.source).get("path", "")
        cleaned = clean_path(raw)
        if not raw or raw == cleaned:
            return NormalizationItem(r.layer_id, r.name, raw, raw, False, True, "Already normalized")
        safe, why = self._is_safe(raw, cleaned)
        item = NormalizationItem(r.layer_id, r.name, raw, cleaned, True, safe, why)
        if safe:
            p = make_proposal(r, "Normalize path")
            p.original_path = raw
            set_proposal_path(p, cleaned, "Path normalization", 100)
            item.proposal = p
        return item

    @staticmethod
    def _is_safe(raw: str, cleaned: str):
        """A cleaned path is only applied if it refers to the same dataset."""
        if os.path.exists(raw) and os.path.exists(cleaned):
            try:
                if os.path.samefile(raw, cleaned):
                    return True, "Same dataset - path will be normalized"
                return False, "Not applied: cleaned path points to a different file (symbolic links?)"
            except OSError as exc:
                return False, f"Not applied: cannot verify ({exc})"
        only_separators = raw.replace("\\", "/") == cleaned or \
            "/".join(p for p in raw.replace("\\", "/").split("/") if p) == \
            "/".join(p for p in cleaned.split("/") if p)
        if only_separators and IS_WINDOWS:
            return True, "Separator-only change (dataset currently missing)"
        return False, "Not applied: dataset missing, '..' segments cannot be verified safely"

    # ------------------------------------------------------------------
    def apply_storage_mode(self, relative: bool) -> ProjectSettingEntry:
        """Switch the project-wide path storage setting (reversible)."""
        old = project_uses_relative_paths(self.project)
        set_project_relative_paths(self.project, relative)
        entry = ProjectSettingEntry(
            SessionHistory.new_operation_id(), datetime.now(),
            "Project path storage set to " + ("relative" if relative else "absolute"),
            old, relative)
        self.history.add_setting(entry)
        return entry

    def restore_storage_mode(self, entry: ProjectSettingEntry):
        set_project_relative_paths(self.project, entry.old_relative)
        entry.restored = True

    def apply_clean(self, items: List[NormalizationItem], updater: SourceUpdater):
        proposals = [i.proposal for i in items if i.proposal is not None and i.changed]
        for p in proposals:
            p.accepted = True
        return updater.apply(proposals, "Normalize path")

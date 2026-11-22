# -*- coding: utf-8 -*-
"""
Layer Path Manager - path matching, safe source updating and session undo.

Components
----------
FileIndex       Recursive (cancellable, thread-safe) index of candidate
                datasets below one or more search roots.
PathMatcher     Proposes replacement paths using exact / case-insensitive /
                extension-variant / folder-structure / fuzzy matching, or a
                simple old-root -> new-root prefix substitution.
SourceUpdater   Applies confirmed proposals with QgsMapLayer.setDataSource(),
                preserving layer IDs, names and styles, re-validating every
                layer and rolling back automatically on failure.
SessionHistory  Stores original URIs so changes can be restored during the
                current QGIS session.

No dataset is ever written, moved or deleted by this module.
"""

import difflib
import os
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, List, Optional

from qgis.core import (
    QgsDataProvider, QgsFeedback, QgsMapLayer, QgsMapLayerStyle, QgsProject,
)

from .utils import (
    IS_WINDOWS, ChangeProposal, LayerRecord, clean_path, error_summary, extension_family,
    logger, path_extension, replace_path_in_source,
)

# Match type labels shown in the preview
MATCH_EXACT = "Exact filename"
MATCH_CASE = "Filename (case differs)"
MATCH_STRUCTURE = "Filename + folder structure"
MATCH_EXT_VARIANT = "Extension variant"
MATCH_FUZZY = "Fuzzy filename"
MATCH_AMBIGUOUS = "Ambiguous"
MATCH_NONE = "No match"
MATCH_PREFIX = "Root folder substitution"
MATCH_MANUAL = "Manual selection"
MATCH_SAME = "Same file as now"

AUTO_ACCEPT_CONFIDENCE = 80

SKIP_DIRS = {".git", ".svn", "__pycache__", "$recycle.bin", "system volume information"}


# ======================================================================
# File index
# ======================================================================


class FileIndex:
    """Index of files and directory datasets below search roots.

    The index is plain Python data and is built without touching any QGIS
    layer, so it can be built in a background task.
    """

    def __init__(self):
        self.by_name: Dict[str, List[str]] = defaultdict(list)  # lower(name) -> paths
        self.by_ext: Dict[str, List[str]] = defaultdict(list)   # ext -> paths
        self.roots: List[str] = []
        self.errors: List[str] = []
        self.entry_count = 0

    def build(self, feedback: Optional[QgsFeedback], roots: List[str],
              recursive: bool = True, max_entries: int = 2_000_000) -> "FileIndex":
        self.roots = [clean_path(r) for r in roots]
        for root in self.roots:
            if not os.path.isdir(root):
                self.errors.append(f"Search folder not found: {root}")
                continue
            self._walk(feedback, root, recursive, max_entries)
            if feedback is not None and feedback.isCanceled():
                break
        return self

    def _walk(self, feedback, root, recursive, max_entries):
        def onerror(err):
            self.errors.append(f"Cannot read {getattr(err, 'filename', '')}: {err.strerror}")

        dirs_seen = 0
        for cur, dirs, files in os.walk(root, onerror=onerror, followlinks=False):
            if feedback is not None:
                if feedback.isCanceled():
                    return
                dirs_seen += 1
                if dirs_seen % 50 == 0:
                    # progress is unknown for a walk; cycle to show activity
                    feedback.setProgress((dirs_seen // 50) % 100)
            keep = []
            for d in dirs:
                if d.lower() in SKIP_DIRS or d.startswith("."):
                    continue
                full = clean_path(os.path.join(cur, d))
                self._add(full)
                # directory datasets (e.g. File Geodatabases) are leaves
                if d.lower().endswith(".gdb"):
                    continue
                keep.append(d)
            dirs[:] = keep if recursive else []
            for f in files:
                self._add(clean_path(os.path.join(cur, f)))
            if self.entry_count >= max_entries:
                self.errors.append(f"Index limit of {max_entries} entries reached; "
                                   "narrow the search folder")
                return

    def _add(self, path):
        name = os.path.basename(path)
        self.by_name[name.lower()].append(path)
        self.by_ext[path_extension(name)].append(path)
        self.entry_count += 1

    def lookup(self, name: str) -> List[str]:
        return list(self.by_name.get(name.lower(), []))


# ======================================================================
# Matching
# ======================================================================


@dataclass
class MatchOptions:
    case_insensitive: bool = True
    extension_variants: bool = True
    use_structure: bool = True
    fuzzy: bool = False
    fuzzy_threshold: float = 0.80


def _components(path: str) -> List[str]:
    return [c for c in clean_path(path).split("/") if c]


def structure_score(original: str, candidate: str) -> int:
    """Number of trailing parent folders shared by two paths (case-insensitive)."""
    a = _components(os.path.dirname(clean_path(original).rstrip("/")))
    b = _components(os.path.dirname(clean_path(candidate).rstrip("/")))
    score = 0
    while a and b and a[-1].lower() == b[-1].lower():
        score += 1
        a.pop()
        b.pop()
    return score


def make_proposal(rec: LayerRecord, operation: str, layer: Optional[QgsMapLayer] = None) -> ChangeProposal:
    p = ChangeProposal(
        layer_id=rec.layer_id, layer_name=rec.name, provider=rec.provider,
        layer_type=rec.layer_type, original_source=rec.source,
        original_path=rec.path, operation=operation, expected_geometry=rec.geometry,
        is_directory=rec.is_directory or rec.path.lower().rstrip("/").endswith(".gdb"),
    )
    return p


def set_proposal_path(p: ChangeProposal, new_path: str, match_type: str = MATCH_MANUAL,
                      confidence: int = 100):
    """Point a proposal at ``new_path`` and rebuild the full URI."""
    p.validation_state = ""
    p.validation_message = ""
    if not new_path:
        p.new_path, p.new_source = "", ""
        p.match_type, p.confidence, p.accepted = match_type, 0, False
        return
    new_path = clean_path(new_path)
    p.new_path = new_path
    try:
        p.new_source = replace_path_in_source(p.provider, p.original_source,
                                              p.original_path, new_path)
    except ValueError as exc:
        p.new_source = ""
        p.validation_state = "Failed"
        p.validation_message = str(exc)
    p.match_type = match_type
    p.confidence = confidence
    p.accepted = bool(p.new_source) and confidence >= AUTO_ACCEPT_CONFIDENCE
    if p.new_source and p.new_source == p.original_source:
        # the layer already uses this file - nothing to change
        p.match_type, p.accepted = MATCH_SAME, False


class PathMatcher:
    """Finds replacement candidates for layer paths."""

    def __init__(self, index: Optional[FileIndex] = None, options: Optional[MatchOptions] = None):
        self.index = index
        self.options = options or MatchOptions()

    # ---------------- filename based search ----------------
    def match(self, p: ChangeProposal) -> ChangeProposal:
        orig = p.original_path.rstrip("/")
        name = os.path.basename(orig)
        if not name or self.index is None:
            set_proposal_path(p, "", MATCH_NONE, 0)
            return p

        cands = self.index.lookup(name)
        if p.is_directory:
            cands = [c for c in cands if os.path.isdir(c)]
        else:
            cands = [c for c in cands if not os.path.isdir(c) or c.lower().endswith(".gdb")]
        exact_case = [c for c in cands if os.path.basename(c) == name]
        if not self.options.case_insensitive:
            cands = exact_case

        if cands:
            return self._choose(p, cands, MATCH_EXACT if exact_case else MATCH_CASE,
                                100 if exact_case else 95)

        if self.options.extension_variants:
            stem, ext = os.path.splitext(name)
            variants = []
            for e in extension_family(ext.lower()):
                if e == ext.lower():
                    continue
                variants += self.index.lookup(stem + e)
            if variants:
                return self._choose(p, variants, MATCH_EXT_VARIANT, 85)

        if self.options.fuzzy:
            return self._fuzzy(p, name)

        set_proposal_path(p, "", MATCH_NONE, 0)
        p.candidates = []
        return p

    def _choose(self, p, cands, label, base_conf):
        cands = sorted(set(cands))
        scored = sorted(((structure_score(p.original_path, c), c) for c in cands),
                        key=lambda t: (-t[0], t[1]))
        p.candidates = [c for _s, c in scored]
        if len(scored) == 1:
            s, c = scored[0]
            lbl = MATCH_STRUCTURE if (s > 0 and label == MATCH_EXACT) else label
            set_proposal_path(p, c, lbl, base_conf)
            return p
        top, second = scored[0][0], scored[1][0]
        if self.options.use_structure and top > second:
            set_proposal_path(p, scored[0][1],
                              f"{MATCH_STRUCTURE} ({len(cands)} files share this name)",
                              min(base_conf, 85))
            return p
        # genuinely ambiguous: never pick automatically
        set_proposal_path(p, "", f"{MATCH_AMBIGUOUS} - {len(cands)} candidates", 0)
        p.candidates = [c for _s, c in scored]
        return p

    def _fuzzy(self, p, name):
        stem, ext = os.path.splitext(name)
        pool = []
        for e in extension_family(ext.lower()):
            pool += self.index.by_ext.get(e, [])
        best = []
        stem_l = stem.lower()
        for c in pool:
            cstem = os.path.splitext(os.path.basename(c))[0].lower()
            ratio = difflib.SequenceMatcher(None, stem_l, cstem).ratio()
            if ratio >= self.options.fuzzy_threshold:
                best.append((ratio, c))
        if not best:
            set_proposal_path(p, "", MATCH_NONE, 0)
            p.candidates = []
            return p
        best.sort(key=lambda t: (-t[0], -structure_score(p.original_path, t[1]), t[1]))
        p.candidates = [c for _r, c in best[:20]]
        ratio, chosen = best[0]
        conf = int(ratio * 70)  # always below auto-accept threshold
        if len(best) > 1 and abs(best[1][0] - ratio) < 1e-6:
            set_proposal_path(p, "", f"{MATCH_AMBIGUOUS} - {len(best)} similar names", 0)
            p.candidates = [c for _r, c in best[:20]]
            return p
        set_proposal_path(p, chosen, f"{MATCH_FUZZY} ({ratio:.0%} similar)", conf)
        p.accepted = False
        return p

    # ---------------- prefix substitution ----------------
    @staticmethod
    def common_root(paths: List[str]) -> str:
        paths = [clean_path(os.path.dirname(p.rstrip("/"))) for p in paths if p]
        if not paths:
            return ""
        try:
            root = os.path.commonpath(paths)
        except ValueError:  # different drives
            return ""
        return clean_path(root)

    @staticmethod
    def match_prefix(p: ChangeProposal, old_root: str, new_root: str) -> ChangeProposal:
        old_root = clean_path(old_root).rstrip("/")
        new_root = clean_path(new_root).rstrip("/")
        orig = clean_path(p.original_path)
        cmp_orig = orig.lower() if IS_WINDOWS else orig
        cmp_root = old_root.lower() if IS_WINDOWS else old_root
        if not old_root or not (cmp_orig == cmp_root or cmp_orig.startswith(cmp_root + "/")):
            set_proposal_path(p, "", "Not under the old root folder", 0)
            return p
        target = new_root + orig[len(old_root):]
        p.candidates = [target]
        if os.path.exists(target):
            set_proposal_path(p, target, MATCH_PREFIX, 100)
        else:
            set_proposal_path(p, target, MATCH_PREFIX + " - target missing", 30)
            p.accepted = False
        return p


# ======================================================================
# Session history (undo / restore)
# ======================================================================


@dataclass
class HistoryEntry:
    op_id: str
    timestamp: datetime
    layer_id: str
    layer_name: str
    provider: str
    old_source: str
    new_source: str
    operation: str
    old_valid: bool
    restored: bool = False


@dataclass
class ProjectSettingEntry:
    op_id: str
    timestamp: datetime
    description: str
    old_relative: bool
    new_relative: bool
    restored: bool = False


class SessionHistory:
    """In-memory record of every source change made in this QGIS session."""

    def __init__(self):
        self.entries: List[HistoryEntry] = []
        self.settings: List[ProjectSettingEntry] = []

    @staticmethod
    def new_operation_id() -> str:
        return uuid.uuid4().hex[:8]

    def add(self, entry: HistoryEntry):
        self.entries.append(entry)

    def add_setting(self, entry: ProjectSettingEntry):
        self.settings.append(entry)

    def restorable_for(self, layer_id: str) -> Optional[HistoryEntry]:
        for e in reversed(self.entries):
            if e.layer_id == layer_id and not e.restored:
                return e
        return None

    def first_for(self, layer_id: str) -> Optional[HistoryEntry]:
        """Earliest unrestored change: the layer's source before this session's edits."""
        for e in self.entries:
            if e.layer_id == layer_id and not e.restored:
                return e
        return None

    def changed_layer_ids(self) -> List[str]:
        seen = []
        for e in self.entries:
            if not e.restored and e.layer_id not in seen:
                seen.append(e.layer_id)
        return seen

    def clear(self):
        self.entries.clear()
        self.settings.clear()


# ======================================================================
# Updater
# ======================================================================


@dataclass
class UpdateResult:
    layer_id: str
    layer_name: str
    old_source: str
    new_source: str
    old_valid: bool
    new_valid: bool
    result: str               # Success / Failed / Skipped
    message: str = ""
    operation: str = ""


class SourceUpdater:
    """Re-points layers at new sources safely.

    For every layer:
      1. snapshot source, provider, name and style;
      2. ``setDataSource`` with the original provider (layer ID, name and
         style are preserved; default style is NOT loaded);
      3. check ``isValid()``; on failure restore the original source and
         style (rollback) and report the error;
      4. record the change in the session history for undo.
    """

    def __init__(self, project: Optional[QgsProject] = None,
                 history: Optional[SessionHistory] = None):
        self.project = project or QgsProject.instance()
        self.history = history or SessionHistory()

    def _options(self):
        opts = QgsDataProvider.ProviderOptions()
        opts.transformContext = self.project.transformContext()
        return opts

    @staticmethod
    def _renderer_type(layer) -> str:
        try:
            r = layer.renderer()
            return r.type() if r is not None else ""
        except Exception:
            return ""

    def set_source(self, layer: QgsMapLayer, new_source: str, provider: str = None) -> bool:
        provider = provider or layer.providerType()
        layer.setDataSource(new_source, layer.name(), provider, self._options())
        return layer.isValid()

    def apply(self, proposals: List[ChangeProposal], operation: str,
              progress: Optional[Callable[[int, int], bool]] = None) -> List[UpdateResult]:
        """Apply accepted proposals. ``progress(i, n)`` may return False to stop."""
        results = []
        todo = [p for p in proposals if p.accepted]
        op_id = self.history.new_operation_id()
        for i, p in enumerate(todo):
            if progress is not None and progress(i, len(todo)) is False:
                for rest in todo[i:]:
                    results.append(UpdateResult(rest.layer_id, rest.layer_name, rest.original_source,
                                                rest.new_source, False, False, "Skipped",
                                                "Cancelled by user", operation))
                break
            results.append(self._apply_one(p, operation, op_id))
        if any(r.result == "Success" for r in results):
            self.project.setDirty(True)
        return results

    def _apply_one(self, p: ChangeProposal, operation: str, op_id: str) -> UpdateResult:
        layer = self.project.mapLayer(p.layer_id)
        if layer is None:
            return UpdateResult(p.layer_id, p.layer_name, p.original_source, p.new_source,
                                False, False, "Skipped", "Layer no longer in project", operation)
        old_source = layer.source()
        old_provider = layer.providerType()
        old_valid = layer.isValid()
        if not p.new_source:
            return UpdateResult(p.layer_id, layer.name(), old_source, "", old_valid, old_valid,
                                "Skipped", "No replacement source", operation)
        if p.new_source == old_source and old_valid:
            return UpdateResult(p.layer_id, layer.name(), old_source, p.new_source, old_valid,
                                old_valid, "Skipped", "Source already up to date", operation)
        if p.validation_state == "Failed":
            return UpdateResult(p.layer_id, layer.name(), old_source, p.new_source, old_valid,
                                old_valid, "Skipped",
                                "Validation failed: " + p.validation_message, operation)

        style = QgsMapLayerStyle()
        style_ok = False
        old_renderer_type = self._renderer_type(layer)
        try:
            if old_valid:
                style.readFromLayer(layer)
                style_ok = style.isValid()
        except Exception:
            style_ok = False

        try:
            ok = self.set_source(layer, p.new_source, old_provider)
        except Exception as exc:
            ok = False
            logger.exception("setDataSource raised for %s", layer.name())
            err = str(exc)
        else:
            err = "" if ok else error_summary(layer)

        if ok:
            # keep the user's symbology if the provider swap reset it
            new_renderer_type = self._renderer_type(layer)
            if style_ok and old_renderer_type and new_renderer_type != old_renderer_type:
                style.writeToLayer(layer)
                logger.info("Re-applied original style to %s after source change", layer.name())
            layer.triggerRepaint()
            self.history.add(HistoryEntry(op_id, datetime.now(), layer.id(), layer.name(),
                                          old_provider, old_source, layer.source(), operation,
                                          old_valid))
            return UpdateResult(layer.id(), layer.name(), old_source, layer.source(), old_valid,
                                True, "Success", "Layer is valid with the new source", operation)

        # ---- rollback ----
        rollback_msg = ""
        try:
            layer.setDataSource(old_source, layer.name(), old_provider, self._options())
            if style_ok:
                style.writeToLayer(layer)
            rollback_msg = "original source restored"
            if layer.source() != old_source:
                rollback_msg = "WARNING: original source could not be fully restored"
        except Exception as exc:
            rollback_msg = f"WARNING: rollback failed: {exc}"
        return UpdateResult(layer.id(), layer.name(), old_source, p.new_source, old_valid,
                            layer.isValid(), "Failed",
                            f"Layer invalid with new source ({err or 'provider could not open it'}); "
                            f"{rollback_msg}", operation)

    # ------------------------------------------------------------------
    def restore(self, layer_ids: List[str], to_original: bool = True) -> List[UpdateResult]:
        """Restore layers to their pre-session source (or just undo the last change)."""
        results = []
        for lid in layer_ids:
            entry = self.history.first_for(lid) if to_original else self.history.restorable_for(lid)
            layer = self.project.mapLayer(lid)
            if entry is None:
                results.append(UpdateResult(lid, layer.name() if layer else lid, "", "", False, False,
                                            "Skipped", "No restorable change recorded", "Restore"))
                continue
            if layer is None:
                results.append(UpdateResult(lid, entry.layer_name, entry.new_source, entry.old_source,
                                            False, False, "Skipped",
                                            "Layer was removed from the project - cannot restore",
                                            "Restore"))
                continue
            current = layer.source()
            was_valid = layer.isValid()
            try:
                layer.setDataSource(entry.old_source, layer.name(), entry.provider, self._options())
                layer.triggerRepaint()
            except Exception as exc:
                results.append(UpdateResult(lid, layer.name(), current, entry.old_source, was_valid,
                                            layer.isValid(), "Failed", str(exc), "Restore"))
                continue
            # mark restored entries
            if to_original:
                for e in self.history.entries:
                    if e.layer_id == lid:
                        e.restored = True
            else:
                entry.restored = True
            note = "Original source restored"
            if not layer.isValid():
                note += " (the original source is itself broken, as it was before)"
            results.append(UpdateResult(lid, layer.name(), current, entry.old_source, was_valid,
                                        layer.isValid(), "Success", note, "Restore"))
        if results:
            self.project.setDirty(True)
        return results

# -*- coding: utf-8 -*-
"""
Layer Path Manager - project path health report and migration assistant.

Both reports are derived purely from the inspection records plus the project
settings, so they reflect exactly what the user sees in the main table.
"""

import html
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List

from qgis.core import QgsProject

from .layer_inspector import summarize
from .utils import (
    KIND_DATABASE, KIND_REMOTE, STATUS_BROKEN, LayerRecord, clean_path, human_size, project_uses_relative_paths,
)

SEV_CRITICAL = "Critical"
SEV_WARNING = "Warning"
SEV_INFO = "Info"
SEVERITY_ORDER = {SEV_CRITICAL: 0, SEV_WARNING: 1, SEV_INFO: 2}
SEVERITY_COLORS = {SEV_CRITICAL: "#c0392b", SEV_WARNING: "#d68910", SEV_INFO: "#2471a3"}


@dataclass
class HealthIssue:
    severity: str
    category: str
    message: str
    recommendation: str
    layers: List[str] = field(default_factory=list)


@dataclass
class HealthReport:
    generated: str
    project_file: str
    score: int
    grade: str
    summary: Dict[str, int]
    issues: List[HealthIssue]

    # ------------------------------------------------------------------
    def to_text(self) -> str:
        out = ["LAYER PATH MANAGER - PROJECT PATH HEALTH REPORT", "=" * 50,
               f"Generated : {self.generated}",
               f"Project   : {self.project_file or '(unsaved project)'}",
               f"Score     : {self.score}/100  ({self.grade})", "",
               "Summary", "-" * 7]
        s = self.summary
        out += [f"  Total layers      : {s['total']}", f"  Valid             : {s['valid']}",
                f"  Broken            : {s['broken']}", f"  Remote            : {s['remote']}",
                f"  Unsupported/other : {s['unsupported']}",
                f"  File-based        : {s['file_based']} "
                f"(relative {s['relative']}, absolute {s['absolute']})", ""]
        if not self.issues:
            out.append("No issues found. The project's data sources are healthy.")
        for sev in (SEV_CRITICAL, SEV_WARNING, SEV_INFO):
            items = [i for i in self.issues if i.severity == sev]
            if not items:
                continue
            out += [f"{sev.upper()} ({len(items)})", "-" * (len(sev) + 5)]
            for i in items:
                out.append(f"* [{i.category}] {i.message}")
                if i.layers:
                    shown = i.layers[:15]
                    out.append("    Layers: " + ", ".join(shown) +
                               (f" (+{len(i.layers) - 15} more)" if len(i.layers) > 15 else ""))
                out.append(f"    Recommendation: {i.recommendation}")
            out.append("")
        return "\n".join(out)

    def to_html(self) -> str:
        e = html.escape
        color = "#1e8449" if self.score >= 85 else "#d68910" if self.score >= 60 else "#c0392b"
        s = self.summary
        parts = [
            "<h2 style='margin-bottom:2px'>Project Path Health Report</h2>",
            f"<p style='color:#666;margin-top:0'>{e(self.project_file or '(unsaved project)')}"
            f"<br>Generated {e(self.generated)}</p>",
            f"<p style='font-size:15pt'><b style='color:{color}'>{self.score}/100</b> "
            f"&nbsp;<span style='color:{color}'>{e(self.grade)}</span></p>",
            "<table cellpadding='4' style='border-collapse:collapse'>",
        ]
        for label, key in (("Total layers", "total"), ("Valid", "valid"), ("Broken", "broken"),
                           ("Remote", "remote"), ("Unsupported / other", "unsupported"),
                           ("File-based", "file_based"), ("Stored relative", "relative"),
                           ("Stored absolute", "absolute")):
            parts.append(f"<tr><td>{label}</td><td><b>{s[key]}</b></td></tr>")
        parts.append("</table>")
        if not self.issues:
            parts.append("<p style='color:#1e8449'><b>No issues found.</b> "
                         "All data sources are healthy.</p>")
        for sev in (SEV_CRITICAL, SEV_WARNING, SEV_INFO):
            items = [i for i in self.issues if i.severity == sev]
            if not items:
                continue
            parts.append(f"<h3 style='color:{SEVERITY_COLORS[sev]}'>{sev} ({len(items)})</h3><ul>")
            for i in items:
                layers = ""
                if i.layers:
                    shown = ", ".join(e(n) for n in i.layers[:15])
                    more = f" (+{len(i.layers) - 15} more)" if len(i.layers) > 15 else ""
                    layers = f"<br><span style='color:#555'>Layers: {shown}{more}</span>"
                parts.append(f"<li><b>{e(i.category)}</b> - {e(i.message)}{layers}"
                             f"<br><i>Recommendation:</i> {e(i.recommendation)}</li>")
            parts.append("</ul>")
        return "".join(parts)


def build_health_report(project: QgsProject, records: List[LayerRecord]) -> HealthReport:
    s = summarize(records)
    issues: List[HealthIssue] = []
    by = defaultdict(list)
    for r in records:
        if r.status == STATUS_BROKEN:
            if r.is_file_based:
                if r.exists:
                    by["broken_unreadable"].append(r.name)
                else:
                    by["broken_missing"].append(r.name)
            elif r.source_kind == KIND_DATABASE:
                by["broken_db"].append(r.name)
            else:
                by["broken_other"].append(r.name)
        if r.source_kind == KIND_REMOTE and not r.layer_valid:
            by["remote_down"].append(r.name)
        if r.provider == "memory":
            by["memory"].append(r.name)
        if r.is_file_based and r.path.startswith(("//", "\\\\")):
            by["unc"].append(r.name)
        if r.is_file_based and "Missing sidecar" in r.notes:
            by["sidecar"].append(r.name)
        if "Same data source as" in r.notes:
            by["duplicate"].append(r.name)
        if r.is_file_based and "Cannot be stored relative" in r.notes:
            by["cross_drive"].append(r.name)

    saved = bool(project.fileName())
    relative = project_uses_relative_paths(project)
    proj_dir = clean_path(project.absolutePath()) if saved else ""
    outside = [r.name for r in records if r.is_file_based and proj_dir and
               not clean_path(r.path).lower().startswith(proj_dir.lower() + "/")]

    def add(key, sev, cat, msg, rec):
        if by.get(key):
            issues.append(HealthIssue(sev, cat, msg.format(n=len(by[key])), rec, by[key]))

    add("broken_missing", SEV_CRITICAL, "Missing data",
        "{n} layer(s) reference files or folders that do not exist.",
        "Use 'Repair Broken Paths' and point it at the folder that now holds the data; "
        "matches are proposed by filename and folder structure.")
    add("broken_unreadable", SEV_CRITICAL, "Unreadable data",
        "{n} layer(s) point to files that exist but cannot be opened.",
        "Check permissions, missing sidecar files (.shx/.dbf), sublayer names and file "
        "corruption; use 'Edit Selected Paths' to choose a correct dataset.")
    add("broken_db", SEV_CRITICAL, "Database connection",
        "{n} database layer(s) cannot connect.",
        "Verify host, port, credentials and network access; edit the connection in the "
        "Data Source Manager or Layer Properties > Source.")
    add("broken_other", SEV_CRITICAL, "Invalid layer",
        "{n} other layer(s) are invalid.",
        "Open Layer Properties > Source to inspect these provider-specific sources.")
    add("remote_down", SEV_WARNING, "Remote service",
        "{n} remote layer(s) could not be reached.",
        "Check the internet connection, service URL and authentication configuration.")
    add("memory", SEV_WARNING, "Temporary layers",
        "{n} scratch layer(s) live only in memory and are lost when QGIS closes.",
        "Export them (right-click > Make Permanent) to GeoPackage before sharing or closing.")
    add("cross_drive", SEV_WARNING, "Non-portable paths",
        "{n} layer(s) are on another drive/share and cannot be stored relative.",
        "Copy those datasets next to the project (e.g. a 'data' sub-folder) and relocate them.")
    add("unc", SEV_INFO, "Network paths",
        "{n} layer(s) are read from network shares (UNC paths).",
        "Network shares must be reachable with the same path on every computer.")
    add("sidecar", SEV_WARNING, "Incomplete datasets",
        "{n} Shapefile(s) are missing sidecar files.",
        "Restore the missing .shx/.dbf/.prj files alongside the .shp.")
    add("duplicate", SEV_INFO, "Duplicate sources",
        "{n} layer(s) share an identical data source with another layer.",
        "Usually intentional (different styles); remove duplicates if not.")

    if not saved:
        issues.append(HealthIssue(SEV_WARNING, "Unsaved project",
                                  "The project has never been saved, so paths cannot be relative.",
                                  "Save the project (.qgz) inside or next to your data folder."))
    elif not relative and s["file_based"]:
        issues.append(HealthIssue(SEV_WARNING, "Absolute path storage",
                                  "The project stores absolute paths; it will break if the "
                                  "project and data are moved together.",
                                  "Use 'Normalize Paths > Convert to relative' and save the project."))
    if outside and saved:
        issues.append(HealthIssue(SEV_INFO, "Data outside project folder",
                                  f"{len(outside)} file-based layer(s) are stored outside the "
                                  "project folder.",
                                  "For a self-contained, portable project keep data in a "
                                  "sub-folder of the project folder.", outside))

    # score: critical issues weigh most, proportional to affected layers
    total = max(1, s["total"])
    score = 100.0
    score -= 70.0 * (s["broken"] / total)
    for i in issues:
        if i.severity == SEV_CRITICAL:
            score -= 10
        elif i.severity == SEV_WARNING:
            score -= 5
        else:
            score -= 1
    score = int(max(0, min(100, round(score))))
    if s["total"] == 0:
        score, grade = 100, "Empty project"
    elif any(i.severity == SEV_CRITICAL for i in issues):
        grade = "Needs repair"
    elif score >= 85:
        grade = "Healthy"
    elif score >= 60:
        grade = "Fair - review warnings"
    else:
        grade = "Poor"
    issues.sort(key=lambda i: SEVERITY_ORDER[i.severity])
    return HealthReport(datetime.now().isoformat(sep=" ", timespec="seconds"),
                        project.fileName(), score, grade, s, issues)


# ======================================================================
# Migration assistant
# ======================================================================


@dataclass
class MigrationReport:
    generated: str
    project_file: str
    project_dir: str
    relative_storage: bool
    readiness: str
    readiness_notes: List[str]
    folders: Dict[str, List[LayerRecord]]
    inside_project: int
    outside_project: int
    total_size: int
    missing: List[LayerRecord]
    remote: List[LayerRecord]
    database: List[LayerRecord]
    memory: List[LayerRecord]
    datasets: List[LayerRecord]

    def to_text(self) -> str:
        out = ["LAYER PATH MANAGER - PROJECT MIGRATION READINESS", "=" * 50,
               f"Generated : {self.generated}",
               f"Project   : {self.project_file or '(unsaved project)'}",
               f"Path mode : {'relative' if self.relative_storage else 'absolute'}",
               f"Readiness : {self.readiness}", ""]
        out += ["Checklist:"] + [f"  - {n}" for n in self.readiness_notes] + [""]
        out.append(f"External file dependencies: {len(self.datasets)} dataset(s), "
                   f"{human_size(self.total_size)} "
                   f"({self.inside_project} inside / {self.outside_project} outside project folder)")
        for folder, recs in sorted(self.folders.items()):
            out.append(f"  [{folder}]")
            for r in recs:
                flag = "MISSING " if r.status == STATUS_BROKEN else ""
                out.append(f"     {flag}{os.path.basename(r.path)}  <- {r.name}")
        for title, lst in (("Missing datasets", self.missing), ("Remote services", self.remote),
                           ("Database connections", self.database),
                           ("Temporary (memory) layers", self.memory)):
            if lst:
                out += ["", f"{title} ({len(lst)}):"]
                out += [f"  - {r.name}: {r.path or r.source[:120]}" for r in lst]
        return "\n".join(out)

    def to_html(self) -> str:
        e = html.escape
        col = {"Ready": "#1e8449", "Ready with warnings": "#d68910"}.get(self.readiness, "#c0392b")
        p = [f"<h2 style='margin-bottom:2px'>Migration Readiness</h2>"
             f"<p style='color:#666;margin-top:0'>{e(self.project_file or '(unsaved project)')}</p>",
             f"<p style='font-size:14pt'><b style='color:{col}'>{e(self.readiness)}</b></p><ul>"]
        p += [f"<li>{e(n)}</li>" for n in self.readiness_notes]
        p.append("</ul>")
        p.append(f"<p><b>{len(self.datasets)}</b> file dataset(s), <b>{human_size(self.total_size)}"
                 f"</b> &mdash; {self.inside_project} inside, {self.outside_project} outside the "
                 f"project folder.</p>")
        p.append("<h3>Data folders to transfer</h3><table cellpadding='3'>")
        for folder, recs in sorted(self.folders.items()):
            miss = sum(1 for r in recs if r.status == STATUS_BROKEN)
            size = sum(r.size_bytes for r in recs)
            p.append(f"<tr><td><code>{e(folder)}</code></td><td>{len(recs)} layer(s)</td>"
                     f"<td>{human_size(size)}</td><td style='color:#c0392b'>"
                     f"{f'{miss} missing' if miss else ''}</td></tr>")
        p.append("</table>")
        for title, lst in (("Missing datasets", self.missing), ("Remote services", self.remote),
                           ("Database connections", self.database),
                           ("Temporary (memory) layers - will not transfer", self.memory)):
            if lst:
                p.append(f"<h3>{e(title)} ({len(lst)})</h3><ul>")
                p += [f"<li>{e(r.name)}: <code>{e(r.path or r.source[:150])}</code></li>" for r in lst]
                p.append("</ul>")
        return "".join(p)

    def dependency_rows(self) -> List[Dict]:
        rows = []
        for r in self.datasets + self.remote + self.database + self.memory:
            rows.append({"layer_name": r.name, "layer_id": r.layer_id, "kind": r.source_kind,
                         "status": r.status, "path_or_uri": r.path or r.source,
                         "format": r.file_format, "size_bytes": r.size_bytes,
                         "storage": r.path_storage})
        return rows


def build_migration_report(project: QgsProject, records: List[LayerRecord]) -> MigrationReport:
    saved = bool(project.fileName())
    proj_dir = clean_path(project.absolutePath()) if saved else ""
    relative = project_uses_relative_paths(project)
    files = [r for r in records if r.is_file_based]
    # one entry per physical dataset for size purposes
    unique_paths = {}
    for r in files:
        unique_paths.setdefault(clean_path(r.path).lower(), r)
    folders = defaultdict(list)
    inside = outside = 0
    for r in files:
        folder = clean_path(os.path.dirname(r.path.rstrip("/")))
        folders[folder].append(r)
    for key, r in unique_paths.items():
        if proj_dir and key.startswith(proj_dir.lower() + "/"):
            inside += 1
        else:
            outside += 1
    total = sum(r.size_bytes for r in unique_paths.values())
    # Database connections are environment specific: an unreachable database is a
    # warning for migration, while a missing file blocks it.
    missing = [r for r in records if r.status == STATUS_BROKEN and r.source_kind != KIND_DATABASE]
    remote = [r for r in records if r.source_kind == KIND_REMOTE]
    database = [r for r in records if r.source_kind == KIND_DATABASE]
    db_down = [r for r in database if r.status == STATUS_BROKEN]
    memory = [r for r in records if r.provider == "memory"]

    notes, blocking, warn = [], False, False
    if not saved:
        notes.append("Project is not saved - save it before migrating.")
        blocking = True
    if missing:
        notes.append(f"{len(missing)} broken source(s) must be repaired first.")
        blocking = True
    if saved and not relative and files:
        notes.append("Switch to relative path storage so the project keeps working after "
                     "the folder is moved.")
        warn = True
    if outside and saved:
        notes.append(f"{outside} dataset(s) are outside the project folder - copy them into the "
                     "project folder (then Relocate) or transfer the folders listed below.")
        warn = True
    if database:
        notes.append(f"{len(database)} database layer(s) need the same connection/credentials "
                     "on the target computer" +
                     (f" ({len(db_down)} currently unreachable)." if db_down else "."))
        warn = True
    if remote:
        notes.append(f"{len(remote)} remote layer(s) require network access.")
        warn = True
    if memory:
        notes.append(f"{len(memory)} temporary layer(s) will be lost - make them permanent.")
        warn = True
    if not notes:
        notes.append("All file datasets are inside the project folder and stored relative.")
    readiness = "Not ready" if blocking else ("Ready with warnings" if warn else "Ready")
    return MigrationReport(datetime.now().isoformat(sep=" ", timespec="seconds"),
                           project.fileName(), proj_dir, relative, readiness, notes,
                           dict(folders), inside, outside, total, missing, remote,
                           database, memory, files)

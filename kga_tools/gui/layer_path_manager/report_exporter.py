# -*- coding: utf-8 -*-
"""
Layer Path Manager - report export (CSV / TXT / JSON / HTML).

Only report files chosen by the user are written; GIS datasets are never
touched.  CSV files are written with a UTF-8 BOM so that Excel opens
non-ASCII layer names correctly.
"""

import csv
import json
import os
from datetime import datetime
from typing import Dict, List, Optional

from qgis.core import Qgis, QgsProject

from .utils import PLUGIN_NAME, LayerRecord

AUDIT_FIELDS = ["timestamp", "operation", "layer_name", "layer_id", "original_source",
                "new_source", "previous_status", "updated_status", "result", "message",
                "operation_id"]

SCAN_FIELDS = ["layer_name", "layer_id", "layer_type", "provider", "geometry_type",
               "source_uri", "resolved_path", "folder", "file_format", "source_kind", "status",
               "path_storage", "stored_path", "error", "notes", "size_bytes", "group"]


def build_dashboard(scan_summary: Dict[str, int], audit_counts: Dict[str, int]) -> Dict[str, int]:
    """Summary dashboard figures used in every export format."""
    return {
        "total_layers_scanned": scan_summary.get("total", 0),
        "valid_layers": scan_summary.get("valid", 0),
        "broken_layers": scan_summary.get("broken", 0),
        "remote_layers": scan_summary.get("remote", 0),
        "unsupported_layers": scan_summary.get("unsupported", 0),
        "layers_successfully_repaired": scan_summary.get("repaired", 0),
        "successful_operations": audit_counts.get("Success", 0),
        "failed_updates": audit_counts.get("Failed", 0),
        "skipped_layers": audit_counts.get("Skipped", 0),
        "remaining_broken_connections": scan_summary.get("broken", 0),
    }


def _header(project: Optional[QgsProject]) -> Dict[str, str]:
    project = project or QgsProject.instance()
    return {
        "tool": PLUGIN_NAME,
        "generated": datetime.now().isoformat(timespec="seconds"),
        "qgis_version": Qgis.version(),
        "project_file": project.fileName() or "(unsaved project)",
    }


def export_audit(path: str, entries: List, dashboard: Dict[str, int],
                 records: Optional[List[LayerRecord]] = None,
                 project: Optional[QgsProject] = None) -> str:
    """Export the audit log; the format is chosen from the file extension."""
    ext = os.path.splitext(path)[1].lower()
    rows = [e.as_dict() for e in entries]
    if ext == ".csv":
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=AUDIT_FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
    elif ext == ".json":
        payload = {"report": _header(project), "dashboard": dashboard, "operations": rows}
        if records is not None:
            payload["layers"] = [r.as_dict() for r in records]
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
    else:  # .txt and anything else
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(audit_text(entries, dashboard, records, project))
    return path


def audit_text(entries, dashboard, records=None, project=None) -> str:
    h = _header(project)
    lines = [f"{PLUGIN_NAME.upper()} - AUDIT REPORT", "=" * 60]
    lines += [f"{k.replace('_', ' ').title():<14}: {v}" for k, v in h.items() if k != "tool"]
    lines += ["", "SUMMARY", "-" * 7]
    lines += [f"  {k.replace('_', ' ').capitalize():<32}{v}" for k, v in dashboard.items()]
    lines += ["", f"OPERATIONS ({len(entries)})", "-" * 10]
    if not entries:
        lines.append("  No operations recorded in this session.")
    for e in entries:
        lines += [f"[{e.timestamp}] {e.operation} - {e.layer_name} ({e.layer_id})",
                  f"    Result   : {e.result}   ({e.previous_status} -> {e.updated_status})",
                  f"    From     : {e.original_source}",
                  f"    To       : {e.new_source}"]
        if e.message:
            lines.append(f"    Details  : {e.message}")
    if records:
        broken = [r for r in records if r.status == "Broken"]
        lines += ["", f"REMAINING BROKEN CONNECTIONS ({len(broken)})", "-" * 28]
        lines += [f"  - {r.name}: {r.path or r.source}  [{r.error}]" for r in broken] or ["  None"]
    return "\n".join(lines) + "\n"


def export_scan(path: str, records: List[LayerRecord], project=None) -> str:
    ext = os.path.splitext(path)[1].lower()
    rows = [r.as_dict() for r in records]
    if ext == ".json":
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"report": _header(project), "layers": rows}, fh, indent=2, ensure_ascii=False)
    else:
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=SCAN_FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
    return path


def export_rows_csv(path: str, rows: List[Dict]) -> str:
    if not rows:
        rows = [{"info": "no rows"}]
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return path


def export_report_document(path: str, text: str, html_body: str, title: str) -> str:
    """Save a health / migration report as .html or .txt."""
    if os.path.splitext(path)[1].lower() in (".html", ".htm"):
        doc = (f"<!doctype html><html><head><meta charset='utf-8'><title>{title}</title>"
               "<style>body{font-family:Segoe UI,Arial,sans-serif;max-width:900px;margin:24px auto;"
               "padding:0 16px;color:#222}code{background:#f3f3f3;padding:1px 4px}"
               "li{margin-bottom:6px}</style></head><body>" + html_body + "</body></html>")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(doc)
    else:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    return path

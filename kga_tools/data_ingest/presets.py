"""Ingest settings and presets (the equivalent of Premiere Pro's
Ingest Settings + Adobe Media Encoder ingest presets).

A preset is a plain dict so it can be saved as JSON in QgsSettings,
exported to a file and shared with students or colleagues.
"""

import copy
import json

from qgis.core import QgsSettings

SETTINGS_KEY = "kga_data_ingest/"

# Ingest modes, mirroring Premiere Pro
MODE_LINK = "link"              # import in place (Ingest checkbox off)
MODE_COPY = "copy"              # Copy
MODE_CONVERT = "convert"        # Transcode
MODE_PROXY = "proxy"            # Create Proxies
MODE_COPY_PROXY = "copy_proxy"  # Copy and Create Proxies
MODE_CONVERT_PROXY = "convert_proxy"

MODE_LABELS = {
    MODE_LINK: "Link in place (no ingest)",
    MODE_COPY: "Copy to destination",
    MODE_CONVERT: "Convert (transcode) to analysis-ready formats",
    MODE_PROXY: "Create proxies (overviews / low-res copies)",
    MODE_COPY_PROXY: "Copy and create proxies",
    MODE_CONVERT_PROXY: "Convert and create proxies",
}

BIN_NONE = "none"
BIN_TYPE = "type"
BIN_FOLDER = "folder"
BIN_CUSTOM = "custom"

BIN_LABELS = {
    BIN_NONE: "No bins (add to root of Layers panel)",
    BIN_TYPE: "Bin by data type (Vectors, Rasters, Tables...)",
    BIN_FOLDER: "Bin by source folder (mirrors folder structure)",
    BIN_CUSTOM: "Single custom bin",
}

DEFAULT_SETTINGS = {
    "name": "Link only (fast)",
    "mode": MODE_LINK,
    "destination": "",              # empty = <project folder>/ingest
    "vector_format": "GPKG",        # GPKG | ESRI Shapefile | FlatGeobuf | GeoJSON
    "raster_format": "COG",         # COG | GTiff
    "raster_compress": "DEFLATE",   # DEFLATE | LZW | ZSTD | NONE
    "reproject": False,             # reproject to project CRS while converting
    "build_overviews": True,        # pyramids on rasters (proxy modes)
    "proxy_raster_percent": 25,     # size of low-res proxy raster (proxy modes)
    "spatial_index": True,          # build spatial index on vectors (proxy modes)
    "bin_mode": BIN_NONE,
    "custom_bin": "Imported",
    "load_sublayers": "ask",        # ask | all | first
    "zoom_to": False,
    "write_history": True,          # record provenance in layer metadata
    "auto_xy": True,                # smart detection of X/Y or WKT columns in CSV
    "default_csv_crs": "EPSG:4326",
}

BUILTIN_PRESETS = [
    dict(DEFAULT_SETTINGS),
    dict(DEFAULT_SETTINGS, name="Copy to project folder", mode=MODE_COPY),
    dict(DEFAULT_SETTINGS, name="Analysis-ready (GeoPackage + COG)",
         mode=MODE_CONVERT),
    dict(DEFAULT_SETTINGS, name="Analysis-ready, reprojected to project CRS",
         mode=MODE_CONVERT, reproject=True),
    dict(DEFAULT_SETTINGS, name="Big imagery (overviews + 25% proxies)",
         mode=MODE_PROXY, proxy_raster_percent=25),
    dict(DEFAULT_SETTINGS, name="Field data hand-off (copy + proxies)",
         mode=MODE_COPY_PROXY),
]


def _settings():
    return QgsSettings()


def load_user_presets():
    raw = _settings().value(SETTINGS_KEY + "user_presets", "[]")
    try:
        data = json.loads(raw)
        return [p for p in data if isinstance(p, dict) and p.get("name")]
    except (ValueError, TypeError):
        return []


def save_user_presets(presets):
    _settings().setValue(SETTINGS_KEY + "user_presets", json.dumps(presets))


def all_presets():
    builtin_names = {p["name"] for p in BUILTIN_PRESETS}
    user = [p for p in load_user_presets() if p["name"] not in builtin_names]
    return [normalize(p) for p in BUILTIN_PRESETS + user]


def is_builtin(name):
    return any(p["name"] == name for p in BUILTIN_PRESETS)


def normalize(preset):
    out = copy.deepcopy(DEFAULT_SETTINGS)
    out.update({k: v for k, v in preset.items() if k in DEFAULT_SETTINGS})
    return out


def upsert_user_preset(preset):
    preset = normalize(preset)
    presets = [p for p in load_user_presets() if p["name"] != preset["name"]]
    presets.append(preset)
    save_user_presets(presets)


def delete_user_preset(name):
    save_user_presets([p for p in load_user_presets() if p["name"] != name])


def current_settings():
    raw = _settings().value(SETTINGS_KEY + "current", "")
    try:
        return normalize(json.loads(raw)) if raw else normalize(DEFAULT_SETTINGS)
    except (ValueError, TypeError):
        return normalize(DEFAULT_SETTINGS)


def save_current_settings(settings):
    _settings().setValue(SETTINGS_KEY + "current", json.dumps(normalize(settings)))


def ingest_enabled():
    return str(_settings().value(SETTINGS_KEY + "ingest_enabled", "false")).lower() == "true"


def set_ingest_enabled(flag):
    _settings().setValue(SETTINGS_KEY + "ingest_enabled", "true" if flag else "false")


def recent_folders():
    raw = _settings().value(SETTINGS_KEY + "recent_folders", "[]")
    try:
        return [p for p in json.loads(raw) if isinstance(p, str)]
    except (ValueError, TypeError):
        return []


def push_recent_folder(path, limit=12):
    items = [p for p in recent_folders() if p != path]
    items.insert(0, path)
    _settings().setValue(SETTINGS_KEY + "recent_folders", json.dumps(items[:limit]))


def favorite_folders():
    raw = _settings().value(SETTINGS_KEY + "favorites", "[]")
    try:
        return [p for p in json.loads(raw) if isinstance(p, str)]
    except (ValueError, TypeError):
        return []


def set_favorite_folders(items):
    _settings().setValue(SETTINGS_KEY + "favorites", json.dumps(items))


def export_preset_file(preset, path):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(normalize(preset), fh, indent=2)


def import_preset_file(path):
    with open(path, "r", encoding="utf-8") as fh:
        preset = json.load(fh)
    if not isinstance(preset, dict) or not preset.get("name"):
        raise ValueError("Not a Data Explorer preset file")
    preset = normalize(preset)
    upsert_user_preset(preset)
    return preset


# ---------------------------------------------------------------- search
def recent_searches():
    raw = _settings().value(SETTINGS_KEY + "recent_searches", "[]")
    try:
        return [q for q in json.loads(raw) if isinstance(q, str)]
    except (ValueError, TypeError):
        return []


def push_recent_search(query, limit=25):
    query = (query or "").strip()
    if not query:
        return recent_searches()
    items = [q for q in recent_searches() if q != query]
    items.insert(0, query)
    _settings().setValue(SETTINGS_KEY + "recent_searches", json.dumps(items[:limit]))
    return items[:limit]


def search_bins():
    """Saved searches, like Premiere Pro's Search Bins."""
    raw = _settings().value(SETTINGS_KEY + "search_bins", "[]")
    try:
        return [b for b in json.loads(raw) if isinstance(b, dict) and b.get("name")]
    except (ValueError, TypeError):
        return []


def save_search_bin(name, query, scope, extent, kind_index, folder):
    bins = [b for b in search_bins() if b["name"] != name]
    bins.append({"name": name, "query": query, "scope": scope, "extent": bool(extent),
                 "kind_index": int(kind_index), "folder": folder})
    _settings().setValue(SETTINGS_KEY + "search_bins", json.dumps(bins))


def delete_search_bin(name):
    _settings().setValue(SETTINGS_KEY + "search_bins",
                         json.dumps([b for b in search_bins() if b["name"] != name]))


def extra_index_roots():
    raw = _settings().value(SETTINGS_KEY + "index_roots", "[]")
    try:
        return [p for p in json.loads(raw) if isinstance(p, str)]
    except (ValueError, TypeError):
        return []


def set_extra_index_roots(items):
    _settings().setValue(SETTINGS_KEY + "index_roots", json.dumps(items))

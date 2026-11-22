# -*- coding: utf-8 -*-
"""Persistent plugin settings stored in QgsSettings (falls back to a dict outside QGIS)."""

PREFIX = "kga_tools/historical_imagery/"

DEFAULTS = {
    # Google Earth Engine
    "gee_project": "",
    # Microsoft Planetary Computer (optional, raises rate limits)
    "pc_subscription_key": "",
    # Custom STAC catalog
    "custom_stac_url": "https://earth-search.aws.element84.com/v1",
    "custom_stac_collection": "sentinel-2-l2a",
    "custom_stac_bands": "red,green,blue",
    "custom_stac_cloud_property": "eo:cloud_cover",
    "custom_stac_sign_pc": False,
    # Safety / network
    "max_output_megapixels": 400,
    "http_timeout": 60,
    # Remembered UI state
    "last_provider": "earth_search_s2",
    "last_output_dir": "",
    "last_cloud": 30,
    "last_format": "GTiff",
    "auto_load": True,
    "write_sidecar": True,
    "show_report": True,
}

_FALLBACK = {}


def _qsettings():
    try:
        from qgis.core import QgsSettings
        return QgsSettings()
    except ImportError:
        return None


def get(key, default=None):
    if default is None:
        default = DEFAULTS.get(key)
    s = _qsettings()
    if s is None:
        return _FALLBACK.get(key, default)
    value = s.value(PREFIX + key, default)
    # QSettings returns strings for some backends: coerce to the default's type.
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.lower() in ("1", "true", "yes", "on")
        return bool(value)
    if isinstance(default, int) and not isinstance(default, bool):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    if value is None:
        return default
    return value


def set(key, value):  # noqa: A001 - mirrors QgsSettings API
    s = _qsettings()
    if s is None:
        _FALLBACK[key] = value
    else:
        s.setValue(PREFIX + key, value)

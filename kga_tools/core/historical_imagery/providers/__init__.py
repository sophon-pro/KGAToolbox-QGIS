# -*- coding: utf-8 -*-
"""Registry of available imagery providers (order = order in the dropdown)."""

from collections import OrderedDict

from .base import ImageryProvider, SearchCanceled, compute_aoi_coverage  # noqa: F401
from .gee import EarthEngineProvider
from .oam import OpenAerialMapProvider
from .stac import CustomStacProvider, EarthSearchSentinel2, PlanetaryComputerLandsat

_REGISTRY = None


def providers():
    """Return an ordered {id: provider} mapping (singletons)."""
    global _REGISTRY
    if _REGISTRY is None:
        items = [
            EarthSearchSentinel2(),
            PlanetaryComputerLandsat(),
            EarthEngineProvider(),
            OpenAerialMapProvider(),
            CustomStacProvider(),
        ]
        _REGISTRY = OrderedDict((p.id, p) for p in items)
    return _REGISTRY


def get(provider_id):
    return providers().get(provider_id)

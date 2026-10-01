# -*- coding: utf-8 -*-
"""Imagery source registry. Adding a source means adding one entry to SOURCES."""

from dataclasses import dataclass, replace
from typing import Tuple


@dataclass(frozen=True)
class ImagerySource:
    id: str
    name: str
    #: XYZ template with {x} {y} {z} and, optionally, {s} for a subdomain.
    url_template: str
    min_zoom: int = 0
    max_zoom: int = 20
    tile_size: int = 256
    attribution: str = ''
    crs: str = 'EPSG:3857'
    requires_acknowledgement: bool = False
    notes: str = ''
    subdomains: Tuple[str, ...] = ()
    #: Typical bytes per tile, used for the download estimate until a sample
    #: has been fetched.
    avg_tile_bytes: int = 22000

    def tile_url(self, x, y, z):
        subs = self.subdomains
        s = subs[(x + y) % len(subs)] if subs else ''
        return self.url_template.format(x=x, y=y, z=z, s=s)

    def with_url(self, url_template):
        return replace(self, url_template=url_template, subdomains=())


SOURCES = [
    ImagerySource(
        id='google_satellite',
        name='Google Satellite',
        url_template='https://mt{s}.google.com/vt/lyrs=s&x={x}&y={y}&z={z}',
        subdomains=('0', '1', '2', '3'),
        min_zoom=0,
        max_zoom=21,
        attribution='Imagery (c) Google',
        requires_acknowledgement=True,
        notes='Subject to Google\'s Terms of Service.',
    ),
]


def get_source(source_id):
    for source in SOURCES:
        if source.id == source_id:
            return source
    return None

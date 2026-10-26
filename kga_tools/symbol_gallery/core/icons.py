# -*- coding: utf-8 -*-
"""
Original SVG glyphs drawn for Symbol Gallery (GPL-2.0-or-later, same as the plugin).

All glyphs use QGIS SVG parameters  param(fill) / param(outline) /
param(outline-width)  so their colours can be changed in the normal QGIS
symbol editor after they have been applied.
"""

_HEAD = ('<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" '
         'viewBox="0 0 24 24">')
_TAIL = "</svg>"

F = 'fill="param(fill) #333333"'
OUTLINE = 'stroke="param(outline) #ffffff" stroke-width="param(outline-width) 0"'


def _svg(body):
    return _HEAD + body + _TAIL


MAP_PIN = _svg(
    '<path %s stroke="param(outline) #ffffff" stroke-width="param(outline-width) 1.2" '
    'd="M12 1.2C7.7 1.2 4.4 4.5 4.4 8.7c0 5.6 7.6 14.1 7.6 14.1s7.6-8.5 '
    '7.6-14.1c0-4.2-3.3-7.5-7.6-7.5z"/>'
    '<circle cx="12" cy="8.8" r="3" fill="#ffffff"/>' % F)

HOSPITAL = _svg(
    '<rect x="1.5" y="1.5" width="21" height="21" rx="5" %s %s/>'
    '<path fill="#ffffff" d="M10 5.5h4v4.5h4.5v4H14v4.5h-4V14H5.5v-4H10z"/>' % (F, OUTLINE))

SCHOOL = _svg(
    '<rect x="1.5" y="1.5" width="21" height="21" rx="5" %s %s/>'
    '<path fill="#ffffff" d="M12 5.5 3.8 9.6 12 13.7l6.4-3.2v4.3h1.4V9.6z"/>'
    '<path fill="#ffffff" d="M7.2 12.6v3.1c0 1.3 2.2 2.6 4.8 2.6s4.8-1.3 '
    '4.8-2.6v-3.1L12 15z"/>' % (F, OUTLINE))

PAGODA = _svg(
    '<circle cx="12" cy="12" r="10.8" %s %s/>'
    '<path fill="#ffffff" d="M12 3.2l.9 3.2h-1.8z'
    'M9.6 7.2h4.8l1.2 2.2H8.4z'
    'M7.6 10.2h8.8l1.6 2.4H6z'
    'M8.2 13.4h7.6v4.2H8.2z'
    'M5.4 18.2h13.2v1.6H5.4z"/>'
    '<rect x="11" y="14.6" width="2" height="3" fill="param(fill) #333333"/>' % (F, OUTLINE))

TREE = _svg(
    '<rect x="11" y="13" width="2" height="9" rx="0.6" fill="#6b4a2b"/>'
    '<circle cx="12" cy="9.5" r="7.5" %s %s/>'
    '<circle cx="9.6" cy="7.4" r="2.2" fill="#ffffff" fill-opacity="0.22"/>' % (F, OUTLINE))

CONIFER = _svg(
    '<rect x="11" y="17" width="2" height="5.5" fill="#6b4a2b"/>'
    '<path %s %s d="M12 1.5 5 10h3.2L4 15.5h4L3 21h18l-5-5.5h4L15.8 10H19z"/>' % (F, OUTLINE))

MARSH_TUFT = _svg(
    '<g fill="none" stroke="param(fill) #2f7d6d" stroke-width="1.6" '
    'stroke-linecap="round">'
    '<path d="M4 20h16"/><path d="M12 19V8"/><path d="M12 19 7.5 11"/>'
    '<path d="M12 19l4.5-8"/><path d="M12 19 5 15"/><path d="M12 19l7-4"/></g>')

RICE_TUFT = _svg(
    '<g fill="none" stroke="param(fill) #3f8f3a" stroke-width="1.5" '
    'stroke-linecap="round">'
    '<path d="M12 21V9"/><path d="M12 21C10 16 8 13 5 11"/>'
    '<path d="M12 21c2-5 4-8 7-10"/>'
    '<path d="M12 9c-.8-2 .2-4 1.8-5.5" stroke-width="1.2"/></g>')

WATER_DROP = _svg(
    '<path %s %s d="M12 1.8S4.8 10.2 4.8 15a7.2 7.2 0 0 0 14.4 0C19.2 10.2 '
    '12 1.8 12 1.8z"/>'
    '<path fill="none" stroke="#ffffff" stroke-opacity="0.75" stroke-width="1.4" '
    'stroke-linecap="round" d="M8.6 15.4a3.5 3.5 0 0 0 3 3.2"/>' % (F, OUTLINE))

PEAK = _svg(
    '<path %s %s d="M1.5 21.5 9.2 6.5l3.6 6 2.4-3.6 7.3 12.6z"/>'
    '<path fill="#ffffff" d="M9.2 6.5 6.9 11l1.5-.8 1.2 1.4 1.5-1.6 1.1.9z"/>' % (F, OUTLINE))

GAUGE = _svg(
    '<rect x="1.5" y="1.5" width="21" height="21" rx="5" %s %s/>'
    '<path fill="none" stroke="#ffffff" stroke-width="1.6" stroke-linecap="round" '
    'd="M4.5 15c1.3-1.3 2.5-1.3 3.8 0s2.5 1.3 3.7 0 2.5-1.3 3.8 0 2.4 1.3 3.7 0"/>'
    '<path fill="#ffffff" d="M11 4h2v9h-2z"/>'
    '<path fill="#ffffff" d="M13 5h2.4v1.2H13zM13 7.6h1.6v1.2H13zM13 10.2h2.4v1.2H13z"/>'
    % (F, OUTLINE))

CAMPSITE = _svg(
    '<path %s %s d="M12 3.2 2 20.8h20z"/>'
    '<path fill="#ffffff" d="M12 11.5l-3.6 9.3h7.2z"/>' % (F, OUTLINE))

VIEWPOINT = _svg(
    '<circle cx="12" cy="12" r="10.8" %s %s/>'
    '<path fill="#ffffff" d="M12 7.2c-4.2 0-7.2 4.8-7.2 4.8s3 4.8 7.2 4.8 '
    '7.2-4.8 7.2-4.8-3-4.8-7.2-4.8z"/>'
    '<circle cx="12" cy="12" r="2.6" fill="param(fill) #333333"/>' % (F, OUTLINE))

SAMPLE = _svg(
    '<circle cx="12" cy="12" r="8" fill="none" stroke="param(fill) #222222" '
    'stroke-width="1.8"/>'
    '<path stroke="param(fill) #222222" stroke-width="1.8" '
    'd="M12 1v6M12 17v6M1 12h6M17 12h6"/>'
    '<circle cx="12" cy="12" r="1.8" fill="param(fill) #222222"/>')


# --------------------------------------------------------------------------
# irrigation / utilities
# --------------------------------------------------------------------------
PUMP = _svg(
    '<circle cx="12" cy="12" r="10.8" %s %s/>'
    '<circle cx="12" cy="12" r="2" fill="#ffffff"/>'
    '<path fill="#ffffff" d="M12 4.6c2.6 0 3.4 2.4 2 4.4l-1.2 1.2-1.6-.4c-.2-2 '
    '-.6-3.6.8-5.2zM19.4 12c0 2.6-2.4 3.4-4.4 2l-1.2-1.2.4-1.6c2-.2 3.6-.6 '
    '5.2.8zM12 19.4c-2.6 0-3.4-2.4-2-4.4l1.2-1.2 1.6.4c.2 2 .6 3.6-.8 5.2zM4.6 '
    '12c0-2.6 2.4-3.4 4.4-2l1.2 1.2-.4 1.6c-2 .2-3.6.6-5.2-.8z"/>' % (F, OUTLINE))

GATE = _svg(
    '<rect x="1.5" y="4" width="21" height="16" rx="2.5" %s %s/>'
    '<path fill="#ffffff" d="M5 7.5h2.2v9H5zM9.4 7.5h2.2v9H9.4zM13.8 7.5H16v9h-2.2z'
    'M18.2 7.5h.8v9h-.8z"/>'
    '<path fill="#ffffff" d="M3.5 11h17v1.6h-17z"/>' % (F, OUTLINE))

WEIR = _svg(
    '<rect x="1.5" y="1.5" width="21" height="21" rx="5" %s %s/>'
    '<path fill="#ffffff" d="M4 15.5h16V18H4z"/>'
    '<path fill="none" stroke="#ffffff" stroke-width="1.6" stroke-linecap="round" '
    'd="M4.5 11c1.4-1.4 2.6-1.4 3.8 0s2.5 1.4 3.7 0 2.5-1.4 3.8 0 2.4 1.4 3.7 0"/>'
    '<path fill="#ffffff" d="M10.5 6l1.5-2 1.5 2v4h-3z"/>' % (F, OUTLINE))

TOWER = _svg(
    '<g fill="none" stroke="param(fill) #444444" stroke-width="1.5" '
    'stroke-linejoin="round" stroke-linecap="round">'
    '<path d="M12 2 7 22M12 2l5 20M8.6 15h6.8M9.6 10.4h4.8M5 6.5h14M8 22h8"/>'
    '<path d="M8.6 15l5.8 7M15.4 15l-5.8 7M9.6 10.4l5.8 4.6M14.4 10.4l-5.8 4.6"/></g>')

SUBSTATION = _svg(
    '<rect x="1.5" y="1.5" width="21" height="21" rx="3" %s %s/>'
    '<path fill="#ffffff" d="M13.6 3.5 6.5 13.4h4.6l-1.4 7.1 7.8-10.4h-4.9z"/>' % (F, OUTLINE))

# --------------------------------------------------------------------------
# agriculture
# --------------------------------------------------------------------------
FARMHOUSE = _svg(
    '<path %s %s d="M2 11.5 12 3l10 8.5v10H2z"/>'
    '<path fill="#ffffff" d="M9.6 21.5v-6.3h4.8v6.3zM5.5 12.5h3v3h-3zM15.5 12.5h3v3h-3z"/>'
    % (F, OUTLINE))

SILO = _svg(
    '<path %s %s d="M5 9a7 7 0 0 1 14 0v13H5z"/>'
    '<path fill="none" stroke="#ffffff" stroke-width="1.2" '
    'd="M5 12h14M5 15.5h14M5 19h14"/>' % (F, OUTLINE))

SPROUT = _svg(
    '<circle cx="12" cy="12" r="10.8" %s %s/>'
    '<path fill="#ffffff" d="M11.2 19h1.6v-6.2c2.6.3 5-1.6 5.2-5.3-3 0-4.9 1.4-5.6 '
    '3.6-.7-2.5-2.8-4.1-6-4.1.2 3.6 2.4 5.6 4.8 5.6z"/>' % (F, OUTLINE))

CORN_DOT = _svg(
    '<g fill="param(fill) #c58a10">'
    '<ellipse cx="12" cy="12" rx="3.2" ry="7.5"/></g>'
    '<path fill="none" stroke="#5b8f2b" stroke-width="1.4" stroke-linecap="round" '
    'd="M12 21c-1-3-3.5-5-6-5.5M12 21c1-3 3.5-5 6-5.5"/>')

FISH = _svg(
    '<path %s %s d="M2 12c3.5-5 9.5-6.5 14-3l5-3.5-1.6 6.5 1.6 6.5-5-3.5c-4.5 '
    '3.5-10.5 2-14-3z"/>'
    '<circle cx="7" cy="11" r="1.1" fill="#ffffff"/>' % (F, OUTLINE))

# --------------------------------------------------------------------------
# urban
# --------------------------------------------------------------------------
BUS = _svg(
    '<rect x="1.5" y="1.5" width="21" height="21" rx="5" %s %s/>'
    '<path fill="#ffffff" d="M6.5 5.5h11a1.5 1.5 0 0 1 1.5 1.5v9.5H5V7a1.5 1.5 0 0 1 '
    '1.5-1.5zM6.6 7.4v4.4h10.8V7.4z"/>'
    '<circle cx="8" cy="18" r="1.5" fill="#ffffff"/>'
    '<circle cx="16" cy="18" r="1.5" fill="#ffffff"/>' % (F, OUTLINE))

TRAIN = _svg(
    '<rect x="1.5" y="1.5" width="21" height="21" rx="10.5" %s %s/>'
    '<path fill="#ffffff" d="M8 4.5h8a2 2 0 0 1 2 2V15a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2V6.5a2 '
    '2 0 0 1 2-2zM7.8 6.6v4.2h8.4V6.6zM9 13a1 1 0 1 0 0 2 1 1 0 0 0 0-2zm6 0a1 1 0 1 '
    '0 0 2 1 1 0 0 0 0-2zM8.4 17.4h1.8L8.8 20H7zm5.4 0h1.8L17 20h-1.8z"/>' % (F, OUTLINE))

PARKING = _svg(
    '<rect x="1.5" y="1.5" width="21" height="21" rx="5" %s %s/>'
    '<path fill="#ffffff" d="M8 5h5.2a4.4 4.4 0 0 1 0 8.8H10.6V19H8zm2.6 2.4v4h2.4a2 2 0 0 0 '
    '0-4z"/>' % (F, OUTLINE))

# --------------------------------------------------------------------------
# military / tactical  (simplified APP-6 style frames)
# frames are filled with param(fill); icons drawn in black on top
# --------------------------------------------------------------------------
_MS = 'stroke="#000000" stroke-width="1.1"'


def _mil(frame, icon=""):
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="36" height="36" '
            'viewBox="0 0 36 36">' + frame + icon + '</svg>')


FRAME_FRIEND = '<rect x="3" y="8.5" width="30" height="19" fill="param(fill) #80e0ff" %s/>' % _MS
FRAME_HOSTILE = '<path d="M18 3 33 18 18 33 3 18z" fill="param(fill) #ff8080" %s/>' % _MS
FRAME_NEUTRAL = '<rect x="5" y="5" width="26" height="26" fill="param(fill) #aaffaa" %s/>' % _MS
FRAME_UNKNOWN = ('<path d="M18 4.5a6.5 6.5 0 0 1 6.4 7.6A6.5 6.5 0 0 1 24 25 6.5 6.5 0 0 1 '
                 '12 25a6.5 6.5 0 0 1-.4-12.9A6.5 6.5 0 0 1 18 4.5z" '
                 'fill="param(fill) #ffff80" %s/>' % _MS)

ICON_INF_RECT = '<path d="M3 8.5 33 27.5M33 8.5 3 27.5" fill="none" %s/>' % _MS
ICON_ARMOR = '<rect x="9" y="13" width="18" height="10" rx="5" fill="none" %s/>' % _MS
ICON_ARTY = '<circle cx="18" cy="18" r="2.6" fill="#000000"/>'
ICON_RECON = '<path d="M3 27.5 33 8.5" fill="none" %s/>' % _MS
ICON_INF_DIAMOND = '<path d="M10.5 10.5l15 15M25.5 10.5l-15 15" fill="none" %s/>' % _MS
ICON_MED = '<path d="M18 8.5v19M3 18h30" fill="none" %s/>' % _MS
ICON_ENGINEER = ('<path d="M11 21v-6h14v6M18 15v6" fill="none" %s/>' % _MS)

MIL_FRIEND_INF = _mil(FRAME_FRIEND, ICON_INF_RECT)
MIL_FRIEND_ARMOR = _mil(FRAME_FRIEND, ICON_ARMOR)
MIL_FRIEND_ARTY = _mil(FRAME_FRIEND, ICON_ARTY)
MIL_FRIEND_RECON = _mil(FRAME_FRIEND, ICON_RECON)
MIL_FRIEND_MED = _mil(FRAME_FRIEND, ICON_MED)
MIL_FRIEND_ENG = _mil(FRAME_FRIEND, ICON_ENGINEER)
MIL_HOSTILE_INF = _mil(FRAME_HOSTILE, ICON_INF_DIAMOND)
MIL_NEUTRAL = _mil(FRAME_NEUTRAL)
MIL_UNKNOWN = _mil(FRAME_UNKNOWN)

OBS_POST = _svg(
    '<path d="M12 2.5 22 20.5H2z" fill="param(fill) #ffffff" stroke="#000000" '
    'stroke-width="1.2" stroke-linejoin="round"/>'
    '<circle cx="12" cy="14" r="2" fill="#000000"/>')

# --------------------------------------------------------------------------
# environment / hazard
# --------------------------------------------------------------------------
WARNING = _svg(
    '<path %s %s stroke-linejoin="round" d="M12 2.2 22.6 21H1.4z"/>'
    '<path fill="#111111" d="M11 8.5h2l-.3 6.8h-1.4zM12 16.6a1.3 1.3 0 1 1 0 2.6 1.3 1.3 0 '
    '0 1 0-2.6z"/>' % (F, OUTLINE))

FIRE = _svg(
    '<circle cx="12" cy="12" r="10.8" %s %s/>'
    '<path fill="#ffffff" d="M12.4 4.5c.6 3-2.6 4.2-2.6 7.3 0 .9.4 1.6 1 2.1-.3-1.8 '
    '1.2-2.6 1.6-4 1.6 1.5 3 3.4 3 5.6a4.4 4.4 0 0 1-8.8 0c0-4.6 4.2-6.5 5.8-11z"/>'
    % (F, OUTLINE))

LANDSLIDE = _svg(
    '<path %s %s stroke-linejoin="round" d="M12 2.2 22.6 21H1.4z"/>'
    '<path fill="#111111" d="M6 18.4l4-6.5 2.4 2.6 1.8-1.8 3.8 5.7z"/>'
    '<circle cx="9.2" cy="16.6" r=".9" fill="#ffffff"/>'
    '<circle cx="12.6" cy="17.3" r=".8" fill="#ffffff"/>' % (F, OUTLINE))


# --------------------------------------------------------------------------
# extra POI / water / terrain glyphs  (badge = rounded square + white glyph)
# --------------------------------------------------------------------------
def _badge(glyph, round_=False):
    shape = ('<circle cx="12" cy="12" r="10.8" %s %s/>' % (F, OUTLINE) if round_ else
             '<rect x="1.5" y="1.5" width="21" height="21" rx="5" %s %s/>' % (F, OUTLINE))
    return _svg(shape + glyph)


AIRPORT = _badge(
    '<path fill="#ffffff" d="M12 3.5c.8 0 1.3.8 1.3 1.8v4.6l6.2 3.6v1.8l-6.2-1.9v3.8l1.8 '
    '1.4v1.4L12 19l-3.1 1v-1.4l1.8-1.4v-3.8l-6.2 1.9v-1.8l6.2-3.6V5.3c0-1 .5-1.8 1.3-1.8z"/>',
    True)

PORT = _badge(
    '<g fill="none" stroke="#ffffff" stroke-width="1.6" stroke-linecap="round">'
    '<circle cx="12" cy="6" r="1.6"/><path d="M12 7.6V19M8.5 10.5h7"/>'
    '<path d="M5.5 13.5c0 3.4 2.9 5.5 6.5 5.5s6.5-2.1 6.5-5.5"/></g>', True)

FUEL = _badge(
    '<path fill="#ffffff" d="M6 5.5A1.5 1.5 0 0 1 7.5 4h5A1.5 1.5 0 0 1 14 5.5V19H6zm1.6 '
    '1.2v3.6h4.8V6.7z"/>'
    '<path fill="none" stroke="#ffffff" stroke-width="1.4" stroke-linecap="round" '
    'd="M14 9.5h1.6a1 1 0 0 1 1 1v5.5a1.2 1.2 0 0 0 2.4 0V9l-2-2.5"/>')

POLICE = _badge(
    '<path fill="#ffffff" d="M12 3.6 18.5 6v5.2c0 4.1-2.7 7.6-6.5 9.2-3.8-1.6-6.5-5.1-6.5-9.2V6z"/>'
    '<path fill="param(fill) #333333" d="M12 7.4l1.2 2.5 2.7.3-2 1.9.6 2.7L12 13.4l-2.5 '
    '1.4.6-2.7-2-1.9 2.7-.3z"/>')

FIRE_STATION = _badge(
    '<path fill="#ffffff" d="M12.4 4c.6 3-2.6 4.2-2.6 7.3 0 .9.4 1.6 1 2.1-.3-1.8 1.2-2.6 '
    '1.6-4 1.6 1.5 3 3.4 3 5.6a4.4 4.4 0 0 1-8.8 0C6.6 10.4 10.8 8.5 12.4 4z"/>')

MARKET = _badge(
    '<path fill="#ffffff" d="M5.5 8.5h13l-1 11h-11z"/>'
    '<path fill="none" stroke="#ffffff" stroke-width="1.5" '
    'd="M9 10V7.5a3 3 0 0 1 6 0V10"/>')

HOTEL = _badge(
    '<path fill="#ffffff" d="M4.5 7h1.8v6.2h12.4a1 1 0 0 1 1 1V18h-1.8v-2.2H6.3V18H4.5z"/>'
    '<circle cx="9" cy="10.6" r="1.8" fill="#ffffff"/>'
    '<path fill="#ffffff" d="M11.5 9h5.4a1.8 1.8 0 0 1 1.8 1.8v1.4h-7.2z"/>')

INFO = _badge(
    '<circle cx="12" cy="6.8" r="1.7" fill="#ffffff"/>'
    '<path fill="#ffffff" d="M10 10h3.4v6.8h1.4V19H9.2v-2.2h1.4v-4.6H10z"/>', True)

BANK = _badge(
    '<path fill="#ffffff" d="M12 3.8 20 8v1.6H4V8zM5.5 10.8h2v5.6h-2zM9.4 10.8h2v5.6h-2zM12.6 '
    '10.8h2v5.6h-2zM16.5 10.8h2v5.6h-2zM4 17.4h16v2H4z"/>')

CAFE = _badge(
    '<path fill="#ffffff" d="M5.5 9h10v5a4 4 0 0 1-4 4h-2a4 4 0 0 1-4-4z"/>'
    '<path fill="none" stroke="#ffffff" stroke-width="1.5" d="M15.5 10.5h1.2a1.9 1.9 0 0 1 0 '
    '3.8h-1.4"/>'
    '<path fill="none" stroke="#ffffff" stroke-width="1.2" stroke-linecap="round" '
    'd="M8.5 7c-.6-.8.6-1.4 0-2.4M11.5 7c-.6-.8.6-1.4 0-2.4"/>', True)

MUSEUM = _badge(
    '<path fill="#ffffff" d="M12 4 19.5 8H4.5zM6 9.2h1.8v7H6zM9.6 9.2h1.8v7H9.6zM12.6 '
    '9.2h1.8v7h-1.8zM16.2 9.2H18v7h-1.8zM4.5 17.2h15V19h-15z"/>')

SPRING = _badge(
    '<path fill="#ffffff" d="M12 4.2s-4.4 5-4.4 7.9a4.4 4.4 0 0 0 8.8 0C16.4 9.2 12 4.2 12 4.2z"/>'
    '<path fill="none" stroke="#ffffff" stroke-width="1.4" stroke-linecap="round" '
    'd="M5 19c1.2-1 2.3-1 3.5 0s2.3 1 3.5 0 2.3-1 3.5 0 2.3 1 3.5 0"/>', True)

WATERFALL = _badge(
    '<path fill="#ffffff" d="M4.5 5h15v2.4h-15z"/>'
    '<path fill="none" stroke="#ffffff" stroke-width="1.5" stroke-linecap="round" '
    'd="M8 8.5v8M12 8.5v9.5M16 8.5v8M5 19.5c2.3-1.2 4.6-1.2 7 0s4.7 1.2 7 0"/>', True)

VOLCANO = _svg(
    '<path %s %s d="M1.5 21.5 8 9h8l6.5 12.5z"/>'
    '<path fill="#ef4444" d="M8 9h8l-1.5 3.2-2-1.4-1.6 2-1.4-1.6-1.6.9z"/>'
    '<path fill="none" stroke="#9ca3af" stroke-width="1.4" stroke-linecap="round" '
    'd="M10 6.5c-1-1.5.5-2.8 2-2.5s2.5-1.5 4-.5"/>' % (F, OUTLINE))

CAVE = _badge(
    '<path fill="#ffffff" d="M3.5 19 10 6.5l3 4.5 1.6-2L20.5 19z"/>'
    '<path fill="param(fill) #333333" d="M9.5 19v-3a2.5 2.5 0 0 1 5 0v3z"/>')

WIND_TURBINE = _svg(
    '<g fill="param(fill) #475569">'
    '<path d="M11.3 10.5h1.4l.6 12h-2.6z"/>'
    '<path d="M12 9.6c.6-2.6.8-5.4.2-8.1-.9 2.6-1.3 5.4-1.2 8.1z"/>'
    '<path d="M12.9 10.6c2.5.9 5.1 1.4 7.9 1.1-2.4-1.3-5-2.2-7.6-2.6z"/>'
    '<path d="M11 10.6c-2 1.8-3.6 4-4.7 6.6 2.1-1.7 4-3.8 5.4-6.1z"/>'
    '<circle cx="12" cy="10.2" r="1.2"/></g>')

CELL_TOWER = _svg(
    '<g fill="none" stroke="param(fill) #475569" stroke-width="1.5" stroke-linecap="round">'
    '<path d="M12 9v13M9 22l3-13 3 13"/>'
    '<path d="M8.3 4.8a5.2 5.2 0 0 0 0 7.4M15.7 4.8a5.2 5.2 0 0 1 0 7.4'
    'M5.6 2.4a8.8 8.8 0 0 0 0 12.2M18.4 2.4a8.8 8.8 0 0 1 0 12.2"/></g>'
    '<circle cx="12" cy="8.5" r="1.6" fill="param(fill) #475569"/>')

WATER_TANK = _badge(
    '<path fill="#ffffff" d="M6 5h12v7H6z"/>'
    '<path fill="none" stroke="#ffffff" stroke-width="1.5" d="M8 12l-1.5 7.5M16 12l1.5 '
    '7.5M7.5 15.5h9"/>')

STREET_LIGHT = _svg(
    '<path fill="none" stroke="param(fill) #475569" stroke-width="1.6" stroke-linecap="round" '
    'd="M9 22V6a3 3 0 0 1 3-3h3"/>'
    '<path fill="param(fill) #475569" d="M13 4h6l-1 2.6h-4z"/>'
    '<circle cx="16" cy="9" r="1.6" fill="#facc15"/>')

WASTE = _badge(
    '<path fill="#ffffff" d="M6.5 7.5h11l-1 12h-9z"/>'
    '<path fill="#ffffff" d="M5 5.5h14v1.6H5zM10 4h4v1.5h-4z"/>'
    '<path fill="none" stroke="param(fill) #333333" stroke-width="1.1" '
    'd="M10 10v7M14 10v7"/>')

BRIDGE = _svg(
    '<g fill="none" stroke="param(fill) #222222" stroke-width="1.8" stroke-linecap="round">'
    '<path d="M3 5.5l3 2.5h12l3-2.5M3 18.5l3-2.5h12l3 2.5"/></g>')


# --------------------------------------------------------------------------
# v1.2 glyphs: POI, utilities, weather, irrigation structures
# --------------------------------------------------------------------------
PHARMACY = _badge(
    '<path fill="#ffffff" d="M10 5h4v5h5v4h-5v5h-4v-5H5v-4h5z"/>', True)

POST = _badge(
    '<path fill="#ffffff" d="M4.5 7h15v10h-15z"/>'
    '<path fill="none" stroke="param(fill) #333333" stroke-width="1.3" '
    'd="M4.8 7.4 12 13l7.2-5.6"/>')

LIBRARY = _badge(
    '<path fill="#ffffff" d="M4.5 6.5c2.6-.6 5-.3 7 1.2v11c-2-1.4-4.4-1.7-7-1.1z'
    'M19.5 6.5c-2.6-.6-5-.3-7 1.2v11c2-1.4 4.4-1.7 7-1.1z"/>')

MOSQUE = _badge(
    '<path fill="#ffffff" d="M12 4.5c3 2 5 3.6 5 6.5H7c0-2.9 2-4.5 5-6.5zM6.5 12h11v7h-11z"/>'
    '<path fill="param(fill) #333333" d="M10.6 19v-3.2a1.4 1.4 0 0 1 2.8 0V19z"/>')

CHURCH = _badge(
    '<path fill="#ffffff" d="M11 3.5h2v2.5h2.2v2H13v2.2l4.5 3V20h-11v-6.8l4.5-3V8H8.8V6H11z"/>'
    '<path fill="param(fill) #333333" d="M10.8 20v-3.4a1.2 1.2 0 0 1 2.4 0V20z"/>')

STADIUM = _badge(
    '<ellipse cx="12" cy="12" rx="7.5" ry="5" fill="none" stroke="#ffffff" stroke-width="1.8"/>'
    '<ellipse cx="12" cy="12" rx="3.6" ry="2" fill="#ffffff"/>', True)

TOILET = _badge(
    '<circle cx="8" cy="5.8" r="1.6" fill="#ffffff"/><circle cx="16" cy="5.8" r="1.6" fill="#ffffff"/>'
    '<path fill="#ffffff" d="M6.4 8.5h3.2v5H9V19H7v-5.5h-.6zM16 8.5l2.6 6.4h-1.6V19h-2v-4.1h-1.6z"/>'
    '<path fill="#ffffff" d="M11.5 5h1v14h-1z"/>')

PLAYGROUND = _badge(
    '<g fill="none" stroke="#ffffff" stroke-width="1.6" stroke-linecap="round">'
    '<path d="M5 19 9 5h6l4 14M8 12h8"/><path d="M12 5v4.5"/></g>'
    '<circle cx="12" cy="10.8" r="1.4" fill="#ffffff"/>', True)

HYDRANT = _svg(
    '<path %s %s d="M8 8a4 4 0 0 1 8 0v1h1.5v2.6H16V19h1.5v2.5h-11V19H8v-7.4H6.5V9H8z"/>'
    '<circle cx="12" cy="13.5" r="1.6" fill="#ffffff"/>' % (F, OUTLINE))

METER = _badge(
    '<circle cx="12" cy="12.5" r="6.5" fill="none" stroke="#ffffff" stroke-width="1.6"/>'
    '<path fill="#ffffff" d="M11.3 12.8 15 8.6l.9.8-3.7 4.2z"/>'
    '<circle cx="12" cy="12.8" r="1.2" fill="#ffffff"/>', True)

TRANSFORMER = _badge(
    '<g fill="none" stroke="#ffffff" stroke-width="1.6">'
    '<circle cx="9.5" cy="12" r="4"/><circle cx="14.5" cy="12" r="4"/></g>')

WELL_BORE = _svg(
    '<circle cx="12" cy="12" r="8" fill="param(fill) #0369a1" '
    'stroke="param(outline) #ffffff" stroke-width="param(outline-width) 0"/>'
    '<circle cx="12" cy="12" r="3.2" fill="#ffffff"/>'
    '<path stroke="#ffffff" stroke-width="1.4" d="M12 1.5V6M12 18v4.5M1.5 12H6M18 12h4.5"/>')

SHELTER = _badge(
    '<path fill="#ffffff" d="M12 4 3.5 11h2.5v8h12v-8h2.5z"/>'
    '<path fill="param(fill) #333333" d="M10.5 13h3v6h-3z"/>')

EPICENTER = _svg(
    '<g fill="none" stroke="param(fill) #b91c1c" stroke-width="1.5">'
    '<circle cx="12" cy="12" r="9.5"/><circle cx="12" cy="12" r="6"/></g>'
    '<circle cx="12" cy="12" r="2.6" fill="param(fill) #b91c1c"/>')

HELIPAD = _badge(
    '<circle cx="12" cy="12" r="7.5" fill="none" stroke="#ffffff" stroke-width="1.5"/>'
    '<path fill="#ffffff" d="M8.5 7.5h2v3.6h3V7.5h2v9h-2v-3.4h-3v3.4h-2z"/>', True)

TOLL = _badge(
    '<path fill="#ffffff" d="M4 9h16v2H4zM6 11h2v8H6zM16 11h2v8h-2z"/>'
    '<path fill="#ffffff" d="M9.5 5h5v3h-5z"/>')

SIGNAL = _svg(
    '<rect x="7.5" y="1.5" width="9" height="21" rx="3" fill="param(fill) #222222" '
    'stroke="param(outline) #ffffff" stroke-width="param(outline-width) 0"/>'
    '<circle cx="12" cy="6" r="2.2" fill="#ef4444"/><circle cx="12" cy="12" r="2.2" fill="#facc15"/>'
    '<circle cx="12" cy="18" r="2.2" fill="#22c55e"/>')

WEATHER_STATION = _svg(
    '<path d="M12 6 20 20H4z" fill="param(fill) #0ea5e9" stroke="#ffffff" stroke-width="1"/>'
    '<path fill="none" stroke="param(fill) #0ea5e9" stroke-width="1.4" stroke-linecap="round" '
    'd="M12 6V2.5M12 2.5h5"/>')

PIVOT = _svg(
    '<circle cx="12" cy="12" r="9.5" fill="param(fill) #9fd88a" stroke="#4d7c0f" '
    'stroke-width="0.8"/><path stroke="#1e3a8a" stroke-width="1.4" d="M12 12 19 6.5"/>'
    '<circle cx="12" cy="12" r="1.5" fill="#1e3a8a"/>')

CYCLONE = _svg(
    '<circle cx="12" cy="12" r="3.6" fill="none" stroke="param(fill) #dc2626" '
    'stroke-width="2"/>'
    '<path fill="param(fill) #dc2626" d="M12 6.2c-1.4-2.5-.2-4.7 2.8-5.2-1.4 1.5-1.3 3.2 0 '
    '5.4zM12 17.8c1.4 2.5.2 4.7-2.8 5.2 1.4-1.5 1.3-3.2 0-5.4z"/>'
    '<circle cx="12" cy="12" r="1.2" fill="param(fill) #dc2626"/>')

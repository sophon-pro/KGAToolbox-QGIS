# -*- coding: utf-8 -*-
"""Layout map grid settings, held as plain values.

A `QgsLayoutItemMapGrid` is configured through about thirty setters, several of
which take a border side as a second argument, and the QGIS item properties
panel spreads them over five collapsible sections. `GridSpec` is the whole of
that as one flat object of strings and numbers: the dialog edits a spec, and
`apply_spec` is the only place that touches the QGIS object.

Keeping it that way buys three things. The spec round-trips through JSON, so
the last settings used can be remembered and a named preset is just a dict.
`read_spec` reads an existing grid back into one, so the dialog can edit a grid
that is already on the map rather than only adding new ones. And none of it
needs a widget, so it can be exercised head-lessly.

Enum members are looked up scoped first (`GridStyle.Solid`) and unscoped second
(`Solid`), because the unscoped spelling is the one PyQt6 drops and the scoped
one is missing on the oldest build the plugin supports.
"""

import json
import math
from contextlib import suppress

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsLayoutItemMap,
    QgsLayoutItemMapGrid,
    QgsLineSymbol,
    QgsMarkerSymbol,
    QgsProject,
    QgsTextFormat,
)
from qgis.PyQt.QtGui import QColor, QFont

from . import interior_labels as IL
from . import mgrs


def _enum(enum_name, member):
    """One `QgsLayoutItemMapGrid` enum member, whichever spelling this build has."""
    holder = getattr(QgsLayoutItemMapGrid, enum_name, None)
    value = getattr(holder, member, None) if holder is not None else None
    if value is None:
        value = getattr(QgsLayoutItemMapGrid, member, None)
    return value


def points_unit():
    """The render unit meaning "points", for a text format's size."""
    holder = getattr(Qgis, 'RenderUnit', None)
    value = getattr(holder, 'Points', None) if holder is not None else None
    if value is None:                       # pragma: no cover - QGIS < 3.30
        # Looked up by name: the attribute only exists on those old builds, and
        # a literal `QgsUnitTypes.RenderPoints` is what PyQt6 scanners flag.
        from qgis.core import QgsUnitTypes
        value = getattr(QgsUnitTypes, 'RenderPoints')
    return value


# --------------------------------------------------------------- vocabularies
#
# Each is (key, label, value). The key is what goes in the JSON, so it has to
# stay stable; the label is what the combo shows; the value is what QGIS wants.
# The dialog builds every combo straight off these lists.

GRID_STYLES = (
    ('solid', 'Solid lines', _enum('GridStyle', 'Solid')),
    ('cross', 'Crosses', _enum('GridStyle', 'Cross')),
    ('markers', 'Markers', _enum('GridStyle', 'Markers')),
    ('frame_only', 'Frame and labels only',
     _enum('GridStyle', 'FrameAnnotationsOnly')),
)

GRID_UNITS = (
    ('map', 'Map units', _enum('GridUnit', 'MapUnit')),
    ('mm', 'Millimetres on the page', _enum('GridUnit', 'MM')),
    ('cm', 'Centimetres on the page', _enum('GridUnit', 'CM')),
    ('fit', 'Fit the page automatically',
     _enum('GridUnit', 'DynamicPageSizeBased')),
)

FRAME_STYLES = (
    ('none', 'No frame', _enum('FrameStyle', 'NoFrame')),
    ('zebra', 'Zebra', _enum('FrameStyle', 'Zebra')),
    ('zebra_nautical', 'Zebra (nautical)', _enum('FrameStyle', 'ZebraNautical')),
    ('interior', 'Interior ticks', _enum('FrameStyle', 'InteriorTicks')),
    ('exterior', 'Exterior ticks', _enum('FrameStyle', 'ExteriorTicks')),
    ('both_ticks', 'Interior and exterior ticks',
     _enum('FrameStyle', 'InteriorExteriorTicks')),
    ('line', 'Line border', _enum('FrameStyle', 'LineBorder')),
    ('line_nautical', 'Line border (nautical)',
     _enum('FrameStyle', 'LineBorderNautical')),
)

ANNOTATION_FORMATS = (
    ('decimal', 'Decimal', _enum('AnnotationFormat', 'Decimal')),
    ('decimal_suffix', 'Decimal with N/S/E/W',
     _enum('AnnotationFormat', 'DecimalWithSuffix')),
    ('dm', 'Degree, minute', _enum('AnnotationFormat', 'DegreeMinute')),
    ('dm_padded', 'Degree, minute (padded)',
     _enum('AnnotationFormat', 'DegreeMinutePadded')),
    ('dms', 'Degree, minute, second',
     _enum('AnnotationFormat', 'DegreeMinuteSecond')),
    ('dms_padded', 'Degree, minute, second (padded)',
     _enum('AnnotationFormat', 'DegreeMinuteSecondPadded')),
    # The four below all write QGIS's CustomFormat and differ only in the
    # expression that goes with it. See EXPRESSION_FORMATS.
    ('mgrs_gzd', 'MGRS zone and band letter',
     _enum('AnnotationFormat', 'CustomFormat')),
    ('mgrs_square', 'MGRS 100 km square letter',
     _enum('AnnotationFormat', 'CustomFormat')),
    ('mgrs_digits', 'MGRS principal digits',
     _enum('AnnotationFormat', 'CustomFormat')),
    ('mgrs_corner', 'MGRS corner value',
     _enum('AnnotationFormat', 'CustomFormat')),
    ('custom', 'Custom expression',
     _enum('AnnotationFormat', 'CustomFormat')),
)

#: The formats whose labels come from an expression rather than from one of
#: QGIS's own number formats. All four carry the same enum value, which is why
#: `_key_for` alone cannot tell them apart - see `_format_key`.
EXPRESSION_FORMATS = ('mgrs_gzd', 'mgrs_square', 'mgrs_digits',
                      'mgrs_corner', 'custom')

#: Written onto the grid itself, because the enum cannot say which of the four
#: was meant. QGIS stores custom properties in the layout XML, so the answer
#: survives saving and reopening the project.
FORMAT_PROPERTY = 'kga_tools/annotation_format'
ZONE_PROPERTY = 'kga_tools/mgrs_zone'
SKIP_PROPERTY = 'kga_tools/skip_corner'
INTERIOR_PROPERTY = 'kga_tools/interior_role'

#: What a side shows. "Latitude" and "longitude" are QGIS's own words for the
#: two families of line; they mean northing and easting on a projected grid.
ANNOTATION_DISPLAY = (
    ('all', 'Show all', _enum('DisplayMode', 'ShowAll')),
    ('lat', 'Latitude / Y only', _enum('DisplayMode', 'LatitudeOnly')),
    ('lon', 'Longitude / X only', _enum('DisplayMode', 'LongitudeOnly')),
    ('none', 'Hide', _enum('DisplayMode', 'HideAll')),
)

ANNOTATION_POSITIONS = (
    ('outside', 'Outside the frame',
     _enum('AnnotationPosition', 'OutsideMapFrame')),
    ('inside', 'Inside the frame',
     _enum('AnnotationPosition', 'InsideMapFrame')),
)

#: The "orientation" of the labels on one side.
ANNOTATION_DIRECTIONS = (
    ('horizontal', 'Horizontal', _enum('AnnotationDirection', 'Horizontal')),
    ('vertical', 'Vertical, reading up', _enum('AnnotationDirection', 'Vertical')),
    ('vertical_down', 'Vertical, reading down',
     _enum('AnnotationDirection', 'VerticalDescending')),
    ('boundary', 'Along the frame',
     _enum('AnnotationDirection', 'BoundaryDirection')),
    ('above_tick', 'Above the tick', _enum('AnnotationDirection', 'AboveTick')),
    ('on_tick', 'On the tick', _enum('AnnotationDirection', 'OnTick')),
    ('under_tick', 'Under the tick', _enum('AnnotationDirection', 'UnderTick')),
)

#: (key, label, border side, frame side flag), in the order the dialog lists them.
SIDES = (
    ('left', 'Left', _enum('BorderSide', 'Left'),
     _enum('FrameSideFlag', 'FrameLeft')),
    ('right', 'Right', _enum('BorderSide', 'Right'),
     _enum('FrameSideFlag', 'FrameRight')),
    ('top', 'Top', _enum('BorderSide', 'Top'),
     _enum('FrameSideFlag', 'FrameTop')),
    ('bottom', 'Bottom', _enum('BorderSide', 'Bottom'),
     _enum('FrameSideFlag', 'FrameBottom')),
)

SIDE_KEYS = tuple(side[0] for side in SIDES)


def _lookup(vocabulary, key, fallback_index=0):
    """The QGIS value for `key`, or the vocabulary's `fallback_index` entry."""
    for entry_key, _label, value in vocabulary:
        if entry_key == key:
            return value
    return vocabulary[fallback_index][2]


def _key_for(vocabulary, value, fallback_index=0):
    """Inverse of `_lookup`: the key whose value matches, for reading a grid back."""
    for entry_key, _label, entry_value in vocabulary:
        if entry_value is not None and int(entry_value) == int(value):
            return entry_key
    return vocabulary[fallback_index][0]


# --------------------------------------------------------------------- colours

def color_to_text(color):
    """'#aarrggbb', so opacity survives the round trip through JSON."""
    fmt = getattr(QColor, 'NameFormat', None)
    if fmt is not None:
        return color.name(fmt.HexArgb)
    return color.name()                     # pragma: no cover - very old Qt


def color_from_text(text, fallback='#ff000000'):
    color = QColor(text)
    return color if color.isValid() else QColor(fallback)


def font_to_text(font):
    return font.toString()


def font_from_text(text):
    font = QFont()
    if text and font.fromString(text):
        return font
    return QFont()


# ------------------------------------------------------------------- the spec

class GridSpec(object):
    """Every grid setting the dialog offers, as plain values.

    The defaults are what the tool proposes before anything is touched: a thin
    black graticule in map units, an exterior-tick frame on all four sides, and
    decimal labels outside the frame, horizontal everywhere.
    """

    #: Scalar fields and their defaults. Everything except the per-side table,
    #: which is handled separately because it is four rows of four.
    DEFAULTS = (
        ('name', 'Grid'),
        ('style', 'solid'),
        ('crs_authid', ''),                 # empty means "the map's own CRS"
        ('unit', 'map'),
        ('interval_x', 1000.0),
        ('interval_y', 1000.0),
        ('offset_x', 0.0),
        ('offset_y', 0.0),
        ('min_width', 50.0),                # 'fit' unit only, mm
        ('max_width', 100.0),               # 'fit' unit only, mm
        ('line_color', '#ff000000'),
        ('line_width', 0.2),                # mm
        ('cross_size', 3.0),                # mm
        ('marker_size', 2.0),               # mm
        ('frame', 'exterior'),
        ('frame_size', 2.0),                # mm
        ('frame_line_width', 0.3),          # mm
        ('frame_pen_color', '#ff000000'),
        ('frame_fill_1', '#ff000000'),
        ('frame_fill_2', '#ffffffff'),
        ('annotations', True),
        ('annotation_format', 'decimal'),
        # Read only by the 'custom' format. The MGRS formats rebuild their
        # expression on every apply instead, so theirs can never go stale.
        ('annotation_expression', ''),
        # 0 means "work it out from the grid's CRS, or the map's".
        ('mgrs_zone', 0),
        # MGRS digits only: leave the first line of each margin unlabelled
        # because the corner-value grid writes it in full.
        ('skip_corner', False),
        # Interior label strips: 'northing' or 'easting' for the two grids
        # that carry them, '' for every other grid. `halo` puts a white
        # buffer round the text so the line behind a label looks broken.
        ('interior_role', ''),
        ('halo', False),
        ('annotation_precision', 0),
        ('annotation_distance', 1.0),       # mm out from the frame
        ('font', ''),                       # empty means the Qt default family
        ('font_size', 10.0),                 # points
        ('font_color', '#ff000000'),
    )

    #: What the MGRS interior strips start as, whatever the labels beside
    #: them are: smaller, and grey so they sit back from the map.
    INTERIOR_FONT_SIZE = 8.0
    INTERIOR_FONT_COLOR = '#ff808080'

    SIDE_DEFAULTS = (
        ('display', 'all'),
        ('position', 'outside'),
        ('direction', 'horizontal'),
        ('frame_enabled', True),
    )

    def __init__(self, **values):
        for name, default in self.DEFAULTS:
            setattr(self, name, values.get(name, default))
        #: side key -> {'display':…, 'position':…, 'direction':…, 'frame_enabled':…}
        self.sides = {}
        given = values.get('sides') or {}
        for side_key in SIDE_KEYS:
            row = dict(self.SIDE_DEFAULTS)
            row.update(given.get(side_key) or {})
            self.sides[side_key] = row

    # ------------------------------------------------------------ persistence

    def to_dict(self):
        data = {name: getattr(self, name) for name, _default in self.DEFAULTS}
        data['sides'] = {key: dict(row) for key, row in self.sides.items()}
        return data

    @classmethod
    def from_dict(cls, data):
        return cls(**(data or {}))

    def to_json(self):
        return json.dumps(self.to_dict())

    @classmethod
    def from_json(cls, text):
        """Never raises: a settings string from an older build just gives defaults."""
        try:
            data = json.loads(text) if text else {}
        except (TypeError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        return cls.from_dict(data)

    def copy_with(self, **changes):
        """A new spec with `changes` applied. `sides` is merged, not replaced."""
        data = self.to_dict()
        sides = changes.pop('sides', None)
        data.update(changes)
        if sides:
            for side_key, row in sides.items():
                data['sides'].setdefault(side_key, {}).update(row)
        return GridSpec.from_dict(data)

    # ---------------------------------------------------------------- helpers

    def crs(self):
        """The grid's CRS, or an invalid one, which QGIS reads as "follow the map"."""
        if not self.crs_authid:
            return QgsCoordinateReferenceSystem()
        return QgsCoordinateReferenceSystem(self.crs_authid)

    def label_font(self):
        font = font_from_text(self.font)
        font.setPointSizeF(float(self.font_size))
        return font


#: Named starting points, applied over the defaults. They exist because the
#: complaint that produced this tool was the number of steps, not the absence
#: of any one setting: picking a preset and an interval is the whole job for
#: most sheets.
PRESETS = (
    ('default', 'Thin black graticule', {}),
    ('ticks', 'Exterior ticks, labels outside', {
        'style': 'frame_only',
        'frame': 'exterior',
        'frame_size': 2.0,
        'annotations': True,
        'annotation_position': 'outside',
    }),
    ('zebra', 'Zebra frame, labels outside', {
        'style': 'frame_only',
        'frame': 'zebra',
        'frame_size': 2.5,
        'frame_line_width': 0.3,
        'annotation_position': 'outside',
    }),
    ('graticule', 'Lat/long graticule in degrees', {
        'style': 'solid',
        'crs_authid': 'EPSG:4326',
        'unit': 'map',
        'interval_x': 0.25,
        'interval_y': 0.25,
        'frame': 'zebra',
        'frame_size': 2.0,
        'annotation_format': 'dm',
        'annotation_precision': 0,
    }),
    ('inside', 'Crosses, labels inside', {
        'style': 'cross',
        'cross_size': 3.0,
        'frame': 'none',
        'annotation_position': 'inside',
    }),
)


def preset_spec(key, base=None):
    """One preset as a full spec, over `base` when the caller has one to keep."""
    changes = {}
    for preset_key, _label, preset_changes in PRESETS:
        if preset_key == key:
            changes = dict(preset_changes)
            break

    # 'annotation_position' is a per-side field, so a preset naming it means
    # "on every side" rather than anything scalar.
    position = changes.pop('annotation_position', None)
    spec = (base or GridSpec()).copy_with(**changes)
    if position is not None:
        for side_key in SIDE_KEYS:
            spec.sides[side_key]['position'] = position
    return spec


# ------------------------------------------------------------------ grid sets
#
# A preset fills the form. A set does not: there is no single form state that
# means "three grids", so a set is built from the map at Apply time and the
# form goes on showing only the one the user will tune afterwards. Kept in its
# own tuple for that reason rather than folded into PRESETS.

GRID_SETS = (
    ('mgrs', 'MGRS grid - GZD, 100 km squares and a fine grid (adds 3)'),
)


#: The intervals the MGRS set offers. 0 means "work it out from the extent",
#: which is right for most sheets and wrong for the ones a reader has a scale
#: in mind for - a 1:50,000 sheet is a 1 km grid whatever its extent works out
#: to.
MGRS_INTERVALS = (
    ('auto', 'Automatic, from the map extent', 0.0),
    ('10km', '10 km', 10000.0),
    ('1km', '1 km', 1000.0),
    ('100m', '100 m', 100.0),
    ('10m', '10 m', 10.0),
    ('1m', '1 m', 1.0),
)


#: Default distance, in mm, of the MGRS corner label from the map frame.
CORNER_DISTANCE = 2.5


#: Where the MGRS 100 km square letters go.
LETTER_PLACEMENTS = (
    ('margin', 'In the margin, outside the numbers'),
    ('inside', 'Inside the map, against the frame'),
)


def grid_set_specs(key, map_item, base=None, project=None,
                   letters='margin', one_km=False, interior=False, step=0.0,
                   positioning=IL.DEFAULT_POSITION):
    """The specs one grid set asks for, and anything the user should be told.

    Returns `(specs, warnings)`. `specs` is a list of `GridSpec` in drawing
    order, coarsest first; an empty list means the set cannot be built on this
    map and the warnings say why. Warnings alongside a non-empty list are
    caveats, not refusals.

    The last spec is the one the caller should go on editing, which is why the
    fine grid is last rather than in strict drawing order.
    """
    if key == 'mgrs':
        return mgrs_specs(map_item, base, project, letters, one_km, interior,
                          step, positioning)
    return [], ['There is no grid set called "{}".'.format(key)]


def _extent_in_degrees(map_item, project=None):
    """(extent, (lon, lat)) for a map item in EPSG:4326, or (None, None)."""
    if map_item is None:
        return None, None
    extent = map_item.extent()
    crs = map_item.crs()
    if not crs.isValid() or extent.isEmpty():
        return None, None
    wgs84 = QgsCoordinateReferenceSystem('EPSG:4326')
    if crs != wgs84:
        try:
            transform = QgsCoordinateTransform(
                crs, wgs84, project or QgsProject.instance())
            extent = transform.transformBoundingBox(extent)
        except Exception:                   # pragma: no cover - unprojectable
            return None, None
    if extent.isEmpty():
        return None, None
    centre = extent.center()
    return extent, (centre.x(), centre.y())


def extent_in_crs(map_item, authid, project=None):
    """A map item's extent in the CRS named by `authid`, or None.

    An empty authid means the map's own CRS, which is what a grid with no CRS
    of its own is drawn in.
    """
    if map_item is None:
        return None
    try:
        extent = map_item.extent()
        source = map_item.crs()
    except Exception:                       # pragma: no cover - defensive
        return None
    if extent.isEmpty() or not source.isValid():
        return None
    if not authid:
        return extent
    target = QgsCoordinateReferenceSystem(authid)
    if not target.isValid() or target == source:
        return extent
    try:
        transform = QgsCoordinateTransform(
            source, target, project or QgsProject.instance())
        return transform.transformBoundingBox(extent)
    except Exception:                       # pragma: no cover - unprojectable
        return None


def mgrs_zone_span(map_item, project=None):
    """(zone, band, northern, lowest, highest) for a map's extent, in MGRS terms.

    The zone is sampled at the four corners and the edge midpoints, not just
    the corners: a map wide enough to matter has curved zone boundaries across
    it, and a boundary can cross the middle of an edge without touching either
    end of it.

    Returns zone 0 when the extent cannot be put into degrees at all.
    """
    extent, centre = _extent_in_degrees(map_item, project)
    if extent is None:
        return (0, '', True, 0, 0)

    longitude, latitude = centre
    zone = mgrs.zone_for(longitude, latitude)
    band = mgrs.band_letter(latitude)

    middle_x = (extent.xMinimum() + extent.xMaximum()) / 2.0
    middle_y = (extent.yMinimum() + extent.yMaximum()) / 2.0
    sampled = [
        mgrs.zone_for(x, y)
        for x in (extent.xMinimum(), middle_x, extent.xMaximum())
        for y in (extent.yMinimum(), middle_y, extent.yMaximum())
    ]
    return (zone, band, latitude >= 0.0, min(sampled), max(sampled))


def mgrs_specs(map_item, base=None, project=None, letters='margin',
               one_km=False, interior=False, step=0.0,
               positioning=IL.DEFAULT_POSITION):
    """The three grids an MGRS reference is made of, coarsest first.

    MGRS is not one grid. '48P VT 92 77' is a zone number, a latitude band, a
    pair of 100 km square letters and a numeric location, and each of the four
    is read off a different level. So the set is a graticule for the zone and
    band, a 100 km grid for the letters, and a fine grid for the digits.

    Everything the set does not name - font, colours, the frame, each side's
    orientation - comes off `base`, so whatever the user has already set in the
    form is inherited by all four.

    `letters` decides where the 100 km square letters go: 'margin' puts them
    outside the frame past the numbers, 'inside' against the frame within the
    map, which is how a printed sheet does it and which keeps the margin to
    the numbers alone.

    `one_km` adds the 1 km mesh a 1:50,000 sheet carries, under whatever the
    main interval turned out to be. `interior` adds two more grids that draw
    no lines, only strips of numbers inside the frame - northings down vertical
    strips, eastings along horizontal ones - placed by `positioning` (see
    `interior_labels`). A grid has a single inside distance, hence two grids.

    `step` is the grid interval in metres, or 0 to work one out from the
    extent. Naming it matters on a sheet drawn to a stated scale: the extent
    of a 1:50,000 sheet does not always suggest the 1 km grid such a sheet is
    supposed to carry.
    """
    base = base or GridSpec()
    warnings = []

    zone, band, northern, lowest, highest = mgrs_zone_span(map_item, project)
    if not zone:
        return [], ['This map has no usable coordinate system or extent, so '
                    'there is nothing to work an MGRS zone out from. Set the '
                    "map item's CRS and give it an extent, then try again."]
    if not band:
        return [], ['MGRS does not reach this map. Above 84 N and below 80 S '
                    'the grid in use is UPS, which has its own letters and is '
                    'not something this tool draws.']

    if lowest and highest and lowest != highest:
        warnings.append(
            'This map spans UTM zones {} to {}. All three grids use the '
            'centre zone, {}. Eastings, northings and 100 km letters read '
            'correctly inside zone {} only - to either side they are the '
            "wrong zone's numbers. Split the sheet by zone if it has to be "
            'read as a grid reference.'.format(lowest, highest, zone, zone))

    authid = mgrs.utm_authid(zone, northern)
    suggested_x, suggested_y = suggest_interval(map_item, authid,
                                                project=project)
    chosen = mgrs.mgrs_interval(step) if step else 0.0
    if chosen:
        step = chosen
        if suggested_x > 0:
            lines = int(round(5.0 * min(suggested_x, suggested_y) / step))
            if lines > 200:
                warnings.append(
                    'A {} grid is about {} lines across this map, which will '
                    'read as a solid tint at print size. Leave the interval '
                    'on Automatic, or pick a coarser one.'.format(
                        mgrs.step_label(step), lines))
    elif suggested_x <= 0 or suggested_y <= 0:
        step = 1000.0
        warnings.append(
            'The map has no extent to work an interval out from, so the fine '
            'grid is 1 km. Change its interval once the map is showing '
            'something.')
    else:
        # One step for both axes. MGRS quotes a reference to the same
        # precision in both directions, and a rectangular grid would print the
        # two margins at precisions that do not match.
        step = mgrs.mgrs_interval(min(suggested_x, suggested_y))
        lines = int(round(5.0 * min(suggested_x, suggested_y) / step))
        if lines > 40:
            warnings.append(
                'The fine grid works out at about {} lines across the map. '
                'MGRS steps go up by tens, so the next one up would be far '
                'too sparse - set the interval by hand if this is too '
                'dense.'.format(lines))

    # Three things can want the same margin, and QGIS measures every one of
    # them from the map frame rather than from each other, so they have to be
    # stacked by hand. A point is 0.353 mm and a line of text is about 1.2
    # points tall, which is where 0.45 comes from.
    line_height = base.font_size * 0.45 + 0.6
    digits_distance = base.annotation_distance
    # The corner label now takes the slot of the digit label it replaces, so it
    # no longer needs stacking outside the digits row: a fixed 2.5 mm off the
    # frame, as on an ArcGIS Pro sheet.
    corner_distance = CORNER_DISTANCE
    letters_distance = corner_distance + line_height

    common = {'unit': 'map', 'offset_x': 0.0, 'offset_y': 0.0,
              'annotations': True, 'style': 'solid', 'mgrs_zone': zone,
              'annotation_precision': 0, 'annotation_expression': ''}

    gzd = base.copy_with(
        name='MGRS zone {}{}'.format(zone, band),
        crs_authid='EPSG:4326', interval_x=6.0, interval_y=8.0,
        line_width=max(0.5, base.line_width * 2.5),
        frame='none', annotation_format='mgrs_gzd',
        annotation_distance=base.annotation_distance, **common)
    for side_key in SIDE_KEYS:
        # The designator belongs on the map. The margin is already carrying two
        # rows of numbers, and a zone boundary crossing the sheet is exactly
        # where the reader needs to be told which side is which.
        gzd.sides[side_key]['position'] = 'inside'
        gzd.sides[side_key]['frame_enabled'] = False

    inside = letters == 'inside'
    square = base.copy_with(
        name='MGRS 100 km squares', crs_authid=authid,
        interval_x=100000.0, interval_y=100000.0,
        line_width=max(0.3, base.line_width * 1.5),
        frame='none', annotation_format='mgrs_square',
        annotation_distance=(base.annotation_distance if inside
                             else letters_distance), **common)
    for side_key in SIDE_KEYS:
        square.sides[side_key]['frame_enabled'] = False
        square.sides[side_key]['position'] = 'inside' if inside else 'outside'

    # Same interval as the fine grid, because it labels one of the fine
    # grid's own lines - the first - and draws nothing else at all.
    corner = base.copy_with(
        name='MGRS corner values', crs_authid=authid,
        interval_x=step, interval_y=step,
        style='frame_only', frame='none',
        annotation_format='mgrs_corner',
        annotation_distance=corner_distance,
        **{k: v for k, v in common.items() if k != 'style'})
    for side_key in SIDE_KEYS:
        corner.sides[side_key]['frame_enabled'] = False
        corner.sides[side_key]['position'] = 'outside'

    fine = base.copy_with(
        name='MGRS {}'.format(mgrs.step_label(step)),
        crs_authid=authid, interval_x=step, interval_y=step,
        annotation_format='mgrs_digits', skip_corner=True,
        annotation_distance=digits_distance, **common)

    specs = [gzd, square, corner]

    if one_km and step != 1000.0:
        # Lines only, and lighter than the labelled grid they sit under. A
        # second set of labels would land in the same margin row as the first
        # and overlap it, and at this spacing there are far too many of them
        # to read anyway - the mesh is there to measure against, not to name.
        mesh_lines = int(round(5.0 * min(suggested_x, suggested_y) / 1000.0)) \
            if suggested_x > 0 else 0
        specs.append(base.copy_with(
            name='MGRS 1 km mesh', crs_authid=authid,
            interval_x=1000.0, interval_y=1000.0,
            line_width=max(0.05, base.line_width * 0.5),
            frame='none', annotations=False,
            annotation_format='decimal',
            annotation_distance=digits_distance,
            **{k: v for k, v in common.items() if k != 'annotations'}))
        for side_key in SIDE_KEYS:
            specs[-1].sides[side_key]['frame_enabled'] = False
            specs[-1].sides[side_key]['display'] = 'none'
        if mesh_lines > 200:
            warnings.append(
                'The 1 km mesh is about {} lines across this map, which will '
                'read as a solid tint at print size. It suits a 1:50,000 '
                'sheet rather than a whole country.'.format(mesh_lines))
    elif one_km:
        warnings.append(
            'The grid is already 1 km, so no extra mesh was added.')

    if interior:
        specs.extend(interior_specs(map_item, base, authid, step, common,
                                    positioning))

    # The fine grid is last because it is the one the caller goes on editing,
    # not because it draws last; the overlay grids draw no lines to be under.
    specs.append(fine)
    return specs, warnings


def map_size_mm(map_item):
    """(width, height) of a map item in millimetres."""
    layout = map_item.layout()
    rect = map_item.rect()
    unit = getattr(Qgis, 'LayoutUnit', None)
    mm = getattr(unit, 'Millimeters', None)
    if layout is None or mm is None:        # pragma: no cover - defensive
        return rect.width(), rect.height()
    return (layout.convertFromLayoutUnits(rect.width(), mm).length(),
            layout.convertFromLayoutUnits(rect.height(), mm).length())


def place_interior(spec, option, width_mm, height_mm):
    """Point an interior-label spec at the strip `option` asks for.

    The northing grid labels the left and right sides (their lines are the
    horizontal ones), the easting grid the top and bottom. With one strip per
    axis only the left and top side are shown; with two, the far side mirrors
    the near one, which is why the fractions have to be symmetric.
    """
    half_w, half_h = IL.half_label_sizes(
        spec.font_size, mgrs.label_digits(spec.interval_x))
    northing = spec.interior_role == 'northing'
    distance, both = IL.strip_setup(
        option, width_mm if northing else height_mm,
        half_w if northing else half_h)
    spec.annotation_distance = distance
    for side_key in SIDE_KEYS:
        row = spec.sides[side_key]
        row['position'] = 'inside'
        row['direction'] = 'horizontal'
        row['frame_enabled'] = False
        wanted = (side_key in ('left', 'right')) == northing
        far = side_key in ('right', 'bottom')
        row['display'] = (('lat' if northing else 'lon')
                          if wanted and (both or not far) else 'none')
    return spec


def interior_specs(map_item, base, authid, step, common, positioning):
    """The two grids that carry the interior label strips."""
    size = GridSpec.INTERIOR_FONT_SIZE
    width_mm, height_mm = map_size_mm(map_item)
    made = []
    for role in ('northing', 'easting'):
        spec = base.copy_with(
            name='MGRS interior {} labels'.format(role),
            crs_authid=authid, interval_x=step, interval_y=step,
            style='frame_only', frame='none', annotation_format='mgrs_digits',
            font_size=size, font_color=GridSpec.INTERIOR_FONT_COLOR,
            halo=True, interior_role=role,
            # `base` is often the fine grid, which skips its first line for the
            # corner label; the strips have no corner label to make room for.
            skip_corner=False,
            **{k: v for k, v in common.items() if k != 'style'})
        made.append(place_interior(spec, positioning, width_mm, height_mm))
    return made


def update_interior_position(map_item, option):
    """Re-place the interior label grids already on `map_item`.

    Returns how many grids changed. One undo step.
    """
    width_mm, height_mm = map_size_mm(map_item)
    targets = [grid for grid in map_item.grids().asList()
               if grid.customProperty(INTERIOR_PROPERTY, '')]
    if not targets:
        return 0
    map_item.beginCommand('Move interior labels')
    for grid in targets:
        spec = place_interior(read_spec(grid), option, width_mm, height_mm)
        apply_spec(grid, spec, map_item)
    refresh(map_item)
    map_item.endCommand()
    return len(targets)


# ---------------------------------------------------------------- applying it

def _line_symbol(spec):
    symbol = QgsLineSymbol.createSimple({})
    symbol.setColor(color_from_text(spec.line_color))
    symbol.setWidth(float(spec.line_width))
    return symbol


def _marker_symbol(spec):
    symbol = QgsMarkerSymbol.createSimple({'name': 'circle'})
    symbol.setColor(color_from_text(spec.line_color))
    symbol.setSize(float(spec.marker_size))
    return symbol


def _text_format(spec):
    text_format = QgsTextFormat()
    text_format.setFont(spec.label_font())
    text_format.setSize(float(spec.font_size))
    text_format.setSizeUnit(points_unit())
    text_format.setColor(color_from_text(spec.font_color))
    buffer = text_format.buffer()
    buffer.setEnabled(bool(spec.halo))
    if spec.halo:
        buffer.setSize(0.6)                 # mm, the default unit
        buffer.setColor(QColor('white'))
    text_format.setBuffer(buffer)
    return text_format


def zone_for_spec(spec, map_item=None):
    """The UTM zone `spec`'s MGRS letters belong to, or 0 if there is none.

    Three places are asked, in descending order of how deliberate the answer
    is: the zone the user typed, the zone implied by the grid's own CRS, and
    the zone implied by the map's.
    """
    if spec.mgrs_zone:
        return int(spec.mgrs_zone)
    zone, _northern = mgrs.zone_from_authid(spec.crs_authid)
    if zone:
        return zone
    if spec.crs_authid:
        # An explicit CRS that is not a UTM zone. Borrowing the map's zone
        # here would be worse than having none: the expression would divide
        # Web Mercator eastings of eleven million by 100000 and label every
        # line blank, which reads as a broken tool rather than a wrong CRS.
        return 0
    if map_item is not None:
        try:
            zone, _northern = mgrs.zone_from_authid(map_item.crs().authid())
        except Exception:                   # pragma: no cover - defensive
            zone = 0
    return zone


def annotation_expression(spec, map_item=None):
    """The label expression `spec`'s format asks for, or '' if it wants none.

    Built here, at apply time, from the interval the spec holds now - never
    stored. That is what keeps the labels honest: change a 1 km grid to 100 m
    and the digits go from two to three by themselves, instead of leaving a
    two-digit label on a three-digit grid.
    """
    key = spec.annotation_format
    if key == 'custom':
        return spec.annotation_expression or ''
    if key == 'mgrs_gzd':
        return mgrs.gzd_expression()
    if key == 'mgrs_square':
        zone = zone_for_spec(spec, map_item)
        return mgrs.square_expression(zone) if zone else ''
    if key == 'mgrs_digits':
        skip = None
        if spec.skip_corner:
            if shares_map_crs(spec, map_item):
                skip = True
            else:
                extent = extent_in_crs(map_item, spec.crs_authid)
                if extent is not None:
                    skip = (mgrs.first_line(extent.xMinimum(), spec.interval_x),
                            mgrs.first_line(extent.yMinimum(), spec.interval_y))
        return mgrs.digits_expression(spec.interval_x, spec.interval_y, skip,
                                      plain=bool(spec.interior_role))
    if key == 'mgrs_corner':
        if spec.interval_x <= 0 or spec.interval_y <= 0:
            return ''
        if shares_map_crs(spec, map_item):
            # The grid can read the map's own extent, so the label finds the
            # first line for itself and keeps finding it as the map is panned.
            return mgrs.corner_expression(spec.interval_x, spec.interval_y)
        # Different CRSs: `@map_extent` is in the map's units and comparing a
        # metre grid against a degree extent would be meaningless, so the
        # first line is worked out here instead and goes stale on a pan.
        extent = extent_in_crs(map_item, spec.crs_authid)
        if extent is None:
            return ''
        return mgrs.corner_expression_fixed(
            mgrs.first_line(extent.xMinimum(), spec.interval_x),
            mgrs.first_line(extent.yMinimum(), spec.interval_y),
            spec.interval_x, spec.interval_y)
    return ''


def shares_map_crs(spec, map_item):
    """True when a grid is drawn in the same coordinates as its map.

    An unset grid CRS means "follow the map", so that counts too.
    """
    if map_item is None:
        return False
    if not spec.crs_authid:
        return True
    try:
        return map_item.crs().authid() == spec.crs_authid
    except Exception:                       # pragma: no cover - defensive
        return False


def _sniff_expression(text):
    """Which builder wrote `text`, judged by the letter table in it.

    The last resort, for a grid written before this plugin stamped its format
    onto the grid - or written by hand in QGIS's own panel.
    """
    if not text:
        return 'custom'
    # COLUMN_LETTERS first: ROW_LETTERS is a prefix of it, and the 100 km
    # expression contains both.
    if mgrs.COLUMN_LETTERS in text:
        return 'mgrs_square'
    if mgrs.BAND_LETTERS in text:
        return 'mgrs_gzd'
    if 'm.E.' in text and 'm.N.' in text:
        return 'mgrs_corner'
    if 'lpad(to_string((floor(@grid_number /' in text:
        return 'mgrs_digits'
    return 'custom'


def _format_key(grid):
    """The key naming `grid`'s label format, the four CustomFormat ones included.

    Three passes, most trustworthy first: the enum, when it is not
    CustomFormat; the key this plugin left on the grid; and failing that the
    shape of the expression itself. The fallback matters because a grid whose
    expression somebody wrote by hand must come back as *Custom expression*
    with its text intact, rather than as *Decimal* to be overwritten on the
    next Apply.
    """
    value = grid.annotationFormat()
    custom = _lookup(ANNOTATION_FORMATS, 'custom')
    if custom is None or int(value) != int(custom):
        return _key_for(ANNOTATION_FORMATS, value)

    stored = grid.customProperty(FORMAT_PROPERTY, '')
    if stored in EXPRESSION_FORMATS:
        return stored
    return _sniff_expression(grid.annotationExpression() or '')


def apply_spec(grid, spec, map_item=None):
    """Write `spec` onto a `QgsLayoutItemMapGrid`. The only writer in the module."""
    grid.setName(spec.name or 'Grid')
    grid.setEnabled(True)
    grid.setStyle(_lookup(GRID_STYLES, spec.style))
    grid.setCrs(spec.crs())

    grid.setUnits(_lookup(GRID_UNITS, spec.unit))
    if spec.unit == 'fit':
        # The interval is recomputed per render, to sit between these two
        # widths on the page, so intervalX/Y are not read at all.
        grid.setMinimumIntervalWidth(float(spec.min_width))
        grid.setMaximumIntervalWidth(float(spec.max_width))
    else:
        grid.setIntervalX(float(spec.interval_x))
        grid.setIntervalY(float(spec.interval_y))
    grid.setOffsetX(float(spec.offset_x))
    grid.setOffsetY(float(spec.offset_y))

    grid.setLineSymbol(_line_symbol(spec))
    grid.setMarkerSymbol(_marker_symbol(spec))
    grid.setCrossLength(float(spec.cross_size))

    grid.setFrameStyle(_lookup(FRAME_STYLES, spec.frame, fallback_index=4))
    grid.setFrameWidth(float(spec.frame_size))
    grid.setFramePenSize(float(spec.frame_line_width))
    grid.setFramePenColor(color_from_text(spec.frame_pen_color))
    grid.setFrameFillColor1(color_from_text(spec.frame_fill_1))
    grid.setFrameFillColor2(color_from_text(spec.frame_fill_2))

    grid.setAnnotationEnabled(bool(spec.annotations))
    key = spec.annotation_format
    expression = annotation_expression(spec, map_item)
    if key in EXPRESSION_FORMATS and expression:
        grid.setAnnotationFormat(_lookup(ANNOTATION_FORMATS, key))
        grid.setAnnotationExpression(expression)
        grid.setCustomProperty(FORMAT_PROPERTY, key)
        grid.setCustomProperty(ZONE_PROPERTY, int(zone_for_spec(spec, map_item)))
        grid.setCustomProperty(SKIP_PROPERTY, bool(spec.skip_corner))
    else:
        # An MGRS format with no zone to work from would print nothing at all,
        # and a blank margin looks like a broken tool rather than a missing
        # setting. Plain numbers are wrong-looking, which is easier to spot.
        grid.setAnnotationFormat(_lookup(
            ANNOTATION_FORMATS,
            'decimal' if key in EXPRESSION_FORMATS else key))
        grid.setAnnotationExpression('')
        grid.removeCustomProperty(FORMAT_PROPERTY)
        grid.removeCustomProperty(ZONE_PROPERTY)
        grid.removeCustomProperty(SKIP_PROPERTY)
    if spec.interior_role:
        grid.setCustomProperty(INTERIOR_PROPERTY, spec.interior_role)
    else:
        grid.removeCustomProperty(INTERIOR_PROPERTY)
    grid.setAnnotationPrecision(int(spec.annotation_precision))
    grid.setAnnotationFrameDistance(float(spec.annotation_distance))
    grid.setAnnotationTextFormat(_text_format(spec))

    for side_key, _label, border, frame_flag in SIDES:
        row = spec.sides[side_key]
        grid.setFrameSideFlag(frame_flag, bool(row['frame_enabled']))
        grid.setAnnotationDisplay(
            _lookup(ANNOTATION_DISPLAY, row['display']), border)
        grid.setAnnotationPosition(
            _lookup(ANNOTATION_POSITIONS, row['position']), border)
        grid.setAnnotationDirection(
            _lookup(ANNOTATION_DIRECTIONS, row['direction']), border)
    return grid


def read_spec(grid):
    """`grid`'s current settings as a spec, so the dialog can edit what is there."""
    spec = GridSpec()
    spec.name = grid.name() or 'Grid'
    spec.style = _key_for(GRID_STYLES, grid.style())
    crs = grid.crs()
    spec.crs_authid = crs.authid() if crs.isValid() else ''
    spec.unit = _key_for(GRID_UNITS, grid.units())
    spec.interval_x = grid.intervalX()
    spec.interval_y = grid.intervalY()
    spec.offset_x = grid.offsetX()
    spec.offset_y = grid.offsetY()
    spec.min_width = grid.minimumIntervalWidth()
    spec.max_width = grid.maximumIntervalWidth()

    symbol = grid.lineSymbol()
    if symbol is not None:
        spec.line_color = color_to_text(symbol.color())
        spec.line_width = symbol.width()
    marker = grid.markerSymbol()
    if marker is not None:
        spec.marker_size = marker.size()
    spec.cross_size = grid.crossLength()

    spec.frame = _key_for(FRAME_STYLES, grid.frameStyle(), fallback_index=4)
    spec.frame_size = grid.frameWidth()
    spec.frame_line_width = grid.framePenSize()
    spec.frame_pen_color = color_to_text(grid.framePenColor())
    spec.frame_fill_1 = color_to_text(grid.frameFillColor1())
    spec.frame_fill_2 = color_to_text(grid.frameFillColor2())

    spec.annotations = grid.annotationEnabled()
    spec.annotation_format = _format_key(grid)
    if spec.annotation_format == 'custom':
        # The only format whose expression is the user's to keep. The MGRS
        # ones rebuild theirs, so reading one back would only preserve a
        # string that is about to be regenerated anyway.
        spec.annotation_expression = grid.annotationExpression() or ''
    spec.mgrs_zone = (int(grid.customProperty(ZONE_PROPERTY, 0) or 0)
                      or mgrs.zone_from_authid(spec.crs_authid)[0])
    spec.skip_corner = str(grid.customProperty(SKIP_PROPERTY, '')).lower() \
        in ('true', '1')
    spec.interior_role = str(grid.customProperty(INTERIOR_PROPERTY, '') or '')
    spec.annotation_precision = grid.annotationPrecision()
    spec.annotation_distance = grid.annotationFrameDistance()

    text_format = grid.annotationTextFormat()
    spec.halo = bool(text_format.buffer().enabled())
    spec.font = font_to_text(text_format.font())
    spec.font_size = text_format.size()
    spec.font_color = color_to_text(text_format.color())

    for side_key, _label, border, frame_flag in SIDES:
        spec.sides[side_key] = {
            'display': _key_for(ANNOTATION_DISPLAY,
                                grid.annotationDisplay(border)),
            'position': _key_for(ANNOTATION_POSITIONS,
                                 grid.annotationPosition(border)),
            'direction': _key_for(ANNOTATION_DIRECTIONS,
                                  grid.annotationDirection(border)),
            'frame_enabled': bool(grid.testFrameSideFlag(frame_flag)),
        }
    return spec


# ------------------------------------------------------------ writing to maps

def add_grid(map_item, spec):
    """Add a new grid to `map_item` and return it.

    Wrapped in the map item's own undo command, so Ctrl+Z in the layout
    designer takes the grid away again the way it would had it been added from
    the item properties panel.
    """
    map_item.beginCommand('Add grid')
    grid = _build_grid(map_item, spec)
    refresh(map_item)
    map_item.endCommand()
    return grid


def add_grids(map_item, specs, label='Add grids'):
    """Add several grids to `map_item` inside ONE undo command.

    A grid set - MGRS is the one there is - is three grids that only mean
    anything read together, so taking one back on its own would leave a
    half-drawn reference system on the sheet. One command means one Ctrl+Z.

    `unique_grid_name` re-reads the map on every pass, so the names stay
    distinct even when the same set is added twice.
    """
    map_item.beginCommand(label)
    grids = [_build_grid(map_item, spec) for spec in specs]
    refresh(map_item)
    map_item.endCommand()
    return grids


def _build_grid(map_item, spec):
    """One grid, named and applied and attached. No undo command of its own."""
    grid = QgsLayoutItemMapGrid(unique_grid_name(map_item, spec.name), map_item)
    apply_spec(grid, spec.copy_with(name=grid.name()), map_item)
    map_item.grids().addGrid(grid)
    return grid


def update_grid(map_item, grid, spec):
    """Write `spec` onto an existing grid of `map_item`, as one undo step."""
    map_item.beginCommand('Change grid')
    apply_spec(grid, spec, map_item)
    refresh(map_item)
    map_item.endCommand()
    return grid


def remove_grid(map_item, grid):
    map_item.beginCommand('Remove grid')
    map_item.grids().removeGrid(grid.id())
    refresh(map_item)
    map_item.endCommand()


def refresh(map_item):
    """Redraw the map item, bounding box included.

    A grid with an exterior frame or labels outside the frame draws beyond the
    map item's own rectangle, so the bounding rect has to be recomputed; without
    that the new frame is painted into a region the scene never repaints.
    """
    map_item.updateBoundingRect()
    map_item.invalidateCache()
    map_item.update()


def unique_grid_name(map_item, wanted):
    """`wanted`, or `wanted 2`, `wanted 3`… when the map already has that name."""
    wanted = (wanted or 'Grid').strip() or 'Grid'
    taken = {grid.name() for grid in map_item.grids().asList()}
    if wanted not in taken:
        return wanted
    index = 2
    while '{} {}'.format(wanted, index) in taken:
        index += 1
    return '{} {}'.format(wanted, index)


# ------------------------------------------------------- finding what to grid

def print_layouts(project=None):
    """Every print layout in the project, in the layout manager's order."""
    project = project or QgsProject.instance()
    manager = project.layoutManager()
    if manager is None:
        return []
    return list(manager.printLayouts())


def map_items(layout):
    """The map items of one layout, in a stable order."""
    if layout is None:
        return []
    items = [item for item in layout.items()
             if isinstance(item, QgsLayoutItemMap)]
    return sorted(items, key=lambda item: item.displayName().lower())


def selected_map(layout):
    """The map item selected in `layout`, or None."""
    if layout is None:
        return None
    for item in layout.selectedLayoutItems():
        if isinstance(item, QgsLayoutItemMap):
            return item
    return None


def missing_target(project=None):
    """What stops the grid tool from running, or None when nothing does.

    Returns `(title, message)` ready to put in front of the user. A grid lives
    on a map item inside a print layout, so a project with no layout - or with
    layouts that carry no map - has nothing for the tool to act on, and saying
    so plainly beats opening a window with three empty combo boxes in it.
    """
    layouts = print_layouts(project)
    if not layouts:
        return (
            'No print layout in this project',
            'A grid goes on a map item inside a print layout, and this '
            'project has no layout yet.\n\n'
            'Make one first: Project > New Print Layout, give it a name, then '
            'in the layout designer choose Add Item > Add Map and drag a '
            'rectangle on the page.\n\n'
            'Run Add Grid to Layout again once the layout has a map on it.')

    for layout in layouts:
        if map_items(layout):
            return None

    if len(layouts) == 1:
        where = 'The layout "{}" has no map item'.format(
            layouts[0].name() or 'Layout')
    else:
        where = ('None of the {} print layouts in this project has a map '
                 'item'.format(len(layouts)))
    return (
        'No map to put a grid on',
        '{}, so there is nothing for a grid to go on.\n\n'
        'Open the layout from Project > Layouts, choose Add Item > Add Map, '
        'and drag a rectangle on the page.\n\n'
        'Run Add Grid to Layout again once that is done.'.format(where))


def open_designers(iface):
    """The open layout designers, or an empty list head-lessly."""
    if iface is None or not hasattr(iface, 'openLayoutDesigners'):
        return []
    try:
        return list(iface.openLayoutDesigners())
    except Exception:                       # pragma: no cover - defensive
        return []


def designer_window(iface, layout):
    """The top-level window of the designer showing `layout`, or None.

    A layout designer is a window of its own, not a panel of the QGIS main
    window, so anything that wants to stay in front of a sheet has to know
    which window that sheet is in.
    """
    designer = find_designer(iface, layout)
    if designer is None:
        return None
    try:
        return designer.window()
    except Exception:                       # pragma: no cover - defensive
        return None


def _same_layout(left, right):
    """True when two wrappers are one C++ layout (`is` alone can miss it)."""
    if left is right:
        return left is not None
    try:
        from qgis.PyQt import sip
        return sip.unwrapinstance(left) == sip.unwrapinstance(right)
    except Exception:
        return False


def find_designer(iface, layout):
    """The open designer showing `layout`, or None."""
    if layout is None:
        return None
    for designer in open_designers(iface):
        with suppress(Exception):           # pragma: no cover - defensive
            if _same_layout(designer.layout(), layout):
                return designer
    return None


def open_designer(iface, layout):
    """Open `layout` in a designer, or bring its open one to the front.

    Returns the designer, or None when there is no iface or no layout.
    """
    if iface is None or layout is None:
        return None
    designer = find_designer(iface, layout)
    if designer is None:
        designer = iface.openLayoutDesigner(layout)
    if designer is not None:
        with suppress(Exception):           # pragma: no cover - defensive
            window = designer.window()
            window.show()
            window.raise_()
            window.activateWindow()
    return designer


def active_selection(iface):
    """The (layout, map item) the user most likely means, from the open designers.

    The tool is reached from the main window's toolbar, so by the time it runs
    the layout designer is no longer the active window and Qt cannot say which
    one was in front. A selected map item is the better signal anyway, and is
    what "select the map, then Mapping > Add Grid" relies on, so the four
    passes below are in order of how much the guess is worth: a selection in an
    open designer, a selection anywhere in the project, the first map of an
    open designer, and the first map in the project.
    """
    open_layouts = []
    for designer in open_designers(iface):
        layout = None
        with suppress(Exception):           # pragma: no cover - defensive
            layout = designer.layout()
        if layout is not None:
            open_layouts.append(layout)

    all_layouts = list(open_layouts)
    for layout in print_layouts():
        if layout not in all_layouts:
            all_layouts.append(layout)

    for layout in all_layouts:
        map_item = selected_map(layout)
        if map_item is not None:
            return layout, map_item

    for layout in all_layouts:
        items = map_items(layout)
        if items:
            return layout, items[0]
    return (all_layouts[0] if all_layouts else None), None


# ------------------------------------------------------------ interval advice

def nice_step(value):
    """`value` rounded to the nearest 1, 2, 2.5 or 5 times a power of ten."""
    if not value or value <= 0 or not math.isfinite(value):
        return 0.0
    exponent = math.floor(math.log10(value))
    base = 10.0 ** exponent
    fraction = value / base
    best = min((1.0, 2.0, 2.5, 5.0, 10.0),
               key=lambda candidate: abs(math.log10(candidate / fraction)))
    return best * base


def suggest_interval(map_item, crs_authid='', divisions=5, project=None):
    """A round interval cutting `map_item`'s extent into roughly `divisions` parts.

    Returns (x, y). The two differ only when the map is far from square, which
    is why both are computed rather than one being reused for the other.
    """
    if map_item is None:
        return 0.0, 0.0
    extent = map_item.extent()
    map_crs = map_item.crs()
    target = QgsCoordinateReferenceSystem(crs_authid) if crs_authid else map_crs

    if target.isValid() and map_crs.isValid() and target != map_crs:
        project = project or QgsProject.instance()
        try:
            transform = QgsCoordinateTransform(map_crs, target, project)
            extent = transform.transformBoundingBox(extent)
        except Exception:                   # pragma: no cover - unusable pair
            return 0.0, 0.0

    divisions = max(1, int(divisions))
    return (nice_step(extent.width() / divisions),
            nice_step(extent.height() / divisions))

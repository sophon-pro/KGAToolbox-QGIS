# -*- coding: utf-8 -*-
"""The Military Grid Reference System, written out by hand.

QGIS has no MGRS. Not in the C++ API, not in the Python API, not in
`QgsCoordinateFormatter`, which offers decimal degrees and degrees/minutes/
seconds and nothing else. The `mgrs` package on PyPI is not in the QGIS Python
environment, and `geographiclib`, which is, ships only the geodesic solver - not
the `GeoCoords` conversion the C++ GeographicLib has. So a grid reference has to
be computed here or not at all.

Nothing in this module imports QGIS or Qt. That is deliberate twice over: every
assertion below can be checked without starting a `QgsApplication`, and there is
no unbundled import to take the provider down at start-up if it is missing.

**Why the letters are what they are.** MGRS leaves I and O out of every
alphabet, because on a photocopied sheet read by torchlight they are 1 and 0.
That is the only reason the tables are 24 and 20 letters long instead of 26, and
it is why none of the index arithmetic below can be replaced with `chr(65 + n)`.

**Why there is no hemisphere argument on `row_letter`.** The twenty row letters
repeat every 2,000,000 m, and the false northing the southern hemisphere uses,
10,000,000, is a whole number of those cycles. One formula therefore serves both
hemispheres, which is a property of the numbers and not a convenience.

**Why the expression builders are here.** A QGIS map grid can label its lines
from an expression, and the expression is handed exactly two things: the
coordinate value of the line (`@grid_number`) and which family of line it is
(`@grid_axis`, 'x' or 'y'). That is enough for MGRS, because a 100 km column
letter is a function of the zone and the easting alone, and a row letter of the
zone and the northing alone. Building the labels as plain expression text rather
than registering a Python function means a project sent to somebody without this
plugin still draws the right labels.

**On the duplication with `modify_features.utm_crs_for`.** That function picks a
UTM zone too, with the naive six-degree rule and no Norway or Svalbard cases.
That is right for what it does - give an edit somewhere metric to happen, where
zone 31 against zone 32 in Bergen moves a buffer by millimetres - and its
behaviour is pinned by `backup/tests/test_modify.py`. MGRS cannot be that
relaxed: the exceptions change the zone number printed on the sheet and the
100 km letters with it. The two rules are different on purpose. Do not merge
them.
"""

import math
import string

#: The alphabet with I and O left out - 24 letters. The three tables below are
#: slices of it, so none of them is typed out as a literal.
_ALPHABET = ''.join(c for c in string.ascii_uppercase if c not in 'IO')

#: 24 letters for the 100 km column, I and O left out.
COLUMN_LETTERS = _ALPHABET

#: 20 letters for the 100 km row, I and O left out. See the module docstring
#: for why twenty is the number that makes one formula serve both hemispheres.
ROW_LETTERS = _ALPHABET[:20]

#: The latitude bands, C at 80 S through X. I and O are skipped here too. A, B,
#: Y and Z belong to the polar UPS grid, which this module does not do.
BAND_LETTERS = _ALPHABET[2:22]

#: The grid spacings MGRS actually names, coarsest first. A reference quoted to
#: one digit is a 10 km square, to five digits a 1 m square.
STEPS = (10000.0, 1000.0, 100.0, 10.0, 1.0)

#: The fewest digits a grid line label is ever printed with. See `label_digits`.
LABEL_DIGITS_MINIMUM = 2

#: Raised digits, for the 100 km part of a label. '1', '2' and '3' come from
#: Latin-1 and the rest from Unicode's superscripts block, which is why they
#: are written out rather than computed from a code point offset. A font
#: without them draws boxes, so the plain digits are the fallback everywhere
#: this is optional.
SUPERSCRIPT_DIGITS = '⁰¹²³⁴⁵⁶⁷⁸⁹'

#: A full label is printed on every tenth line. On a 1 km grid that is every
#: 10 km, which is where a reader re-orients; in between, the two principal
#: digits are enough and the margin stays legible.
PREFIX_EVERY = 10

#: The latitudes MGRS reaches. Outside them the grid in use is UPS.
MIN_LATITUDE = -80.0
MAX_LATITUDE = 84.0

# WGS84, and the UTM scale factor on the central meridian.
SEMI_MAJOR = 6378137.0
FLATTENING = 1.0 / 298.257223563
SCALE_FACTOR = 0.9996
FALSE_EASTING = 500000.0
FALSE_NORTHING_SOUTH = 10000000.0


# ------------------------------------------------------------------- zones

def normalise_longitude(longitude):
    """`longitude` wrapped into -180 <= x < 180.

    Wrapping rather than clamping matters at the seam: 180 is the west edge of
    zone 1, not the east edge of a zone 61 that does not exist.
    """
    return ((float(longitude) + 180.0) % 360.0) - 180.0


def zone_for(longitude, latitude):
    """The UTM zone a point falls in, Norway and Svalbard included.

    Two places on Earth break the six-degree rule, and both are written into the
    standard rather than being rounding errors:

    * South-west Norway: zone 32 is widened westwards so Bergen and the coast
      are not split down the middle.
    * Svalbard: zones 32, 34 and 36 are abolished and 31, 33, 35 and 37 widened
      to cover the gap.

    Getting these wrong does not move a point slightly - it prints a different
    zone number and a different pair of 100 km letters on the sheet.
    """
    longitude = normalise_longitude(longitude)
    latitude = float(latitude)

    if 56.0 <= latitude < 64.0 and 3.0 <= longitude < 12.0:
        return 32

    if 72.0 <= latitude < 84.0:
        if 0.0 <= longitude < 9.0:
            return 31
        if 9.0 <= longitude < 21.0:
            return 33
        if 21.0 <= longitude < 33.0:
            return 35
        if 33.0 <= longitude < 42.0:
            return 37

    return min(60, max(1, int(math.floor((longitude + 180.0) / 6.0)) + 1))


def central_meridian(zone):
    """The longitude a zone is projected about."""
    return int(zone) * 6.0 - 183.0


def band_letter(latitude):
    """The latitude band letter, or '' where MGRS does not reach.

    Every band is 8 degrees tall except X, which is 12 - the standard stretched
    it rather than add a 13th band four degrees deep. That is what the clamp
    below is doing; it is not defensive programming.
    """
    latitude = float(latitude)
    if latitude < MIN_LATITUDE or latitude > MAX_LATITUDE:
        return ''
    index = int(math.floor((latitude - MIN_LATITUDE) / 8.0))
    index = min(len(BAND_LETTERS) - 1, max(0, index))
    return BAND_LETTERS[index]


def utm_epsg(zone, northern=True):
    """The EPSG code of a UTM zone on WGS84."""
    return (32600 if northern else 32700) + int(zone)


def utm_authid(zone, northern=True):
    """`utm_epsg` in the 'EPSG:32648' form a GridSpec stores."""
    return 'EPSG:{}'.format(utm_epsg(zone, northern))


def zone_from_authid(authid):
    """(zone, northern) read back out of a UTM authid.

    Returns (0, True) for anything that is not a WGS84 UTM code, which is the
    caller's signal that the zone has to come from somewhere else.
    """
    text = (authid or '').strip().upper()
    if not text.startswith('EPSG:'):
        return (0, True)
    try:
        code = int(text[5:])
    except ValueError:
        return (0, True)
    if 32601 <= code <= 32660:
        return (code - 32600, True)
    if 32701 <= code <= 32760:
        return (code - 32700, False)
    return (0, True)


# ----------------------------------------------------------- 100 km squares

def column_offset(zone):
    """Where a zone's eight column letters start in `COLUMN_LETTERS`.

    The 24 letters are three sets of eight, and the sets cycle with the zone:
    zone 1 uses A-H, zone 2 J-R, zone 3 S-Z, zone 4 A-H again.
    """
    return ((int(zone) - 1) % 3) * 8


def row_offset(zone):
    """How far up the row alphabet an even-numbered zone starts.

    Odd zones begin at A on the equator, even zones five squares further up.
    The stagger is what stops two neighbouring zones carrying the same pair of
    letters at the same latitude.
    """
    return 0 if int(zone) % 2 else 5


def column_letter(zone, easting):
    """The 100 km square's column letter, or '' for an easting off the zone.

    The test is on the square number, not on the index it produces. A zone is
    eight squares wide - eastings 100000 to 900000 - and an easting outside
    that still lands on a perfectly valid index belonging to the *previous*
    zone's set of eight, so an index-range check would return a confident
    wrong letter instead of nothing.
    """
    square = int(math.floor(float(easting) / 100000.0))
    if not 1 <= square <= 8:
        return ''
    return COLUMN_LETTERS[column_offset(zone) + square - 1]


def row_letter(zone, northing):
    """The 100 km square's row letter."""
    index = int(math.floor(float(northing) / 100000.0)) + row_offset(zone)
    return ROW_LETTERS[index % len(ROW_LETTERS)]


def square_letters(zone, easting, northing):
    """Both 100 km square letters, e.g. 'VT'."""
    return column_letter(zone, easting) + row_letter(zone, northing)


# ------------------------------------------------------------- projection

def _kruger_alpha(n):
    """The six Krueger series coefficients for a third flattening of `n`."""
    return (
        n / 2.0 - 2.0 / 3.0 * n ** 2 + 5.0 / 16.0 * n ** 3
        + 41.0 / 180.0 * n ** 4 - 127.0 / 288.0 * n ** 5
        + 7891.0 / 37800.0 * n ** 6,
        13.0 / 48.0 * n ** 2 - 3.0 / 5.0 * n ** 3 + 557.0 / 1440.0 * n ** 4
        + 281.0 / 630.0 * n ** 5 - 1983433.0 / 1935360.0 * n ** 6,
        61.0 / 240.0 * n ** 3 - 103.0 / 140.0 * n ** 4
        + 15061.0 / 26880.0 * n ** 5 + 167603.0 / 181440.0 * n ** 6,
        49561.0 / 161280.0 * n ** 4 - 179.0 / 168.0 * n ** 5
        + 6601661.0 / 7257600.0 * n ** 6,
        34729.0 / 80640.0 * n ** 5 - 3418889.0 / 1995840.0 * n ** 6,
        212378941.0 / 319334400.0 * n ** 6,
    )


def utm_forward(longitude, latitude, zone=0):
    """(easting, northing) for a WGS84 point, by the Krueger n-series.

    Carried to sixth order, which agrees with PROJ to well under a micrometre
    anywhere UTM is defined - checked against `QgsCoordinateTransform` at the
    equator, at a zone edge, and in the far south. Six orders rather than four
    because the arithmetic costs nothing and a reference printed on a sheet is
    not the place to discover a centimetre of series truncation.

    The northing is the southern-hemisphere one below the equator, so it feeds
    `row_letter` directly.
    """
    longitude = float(longitude)
    latitude = float(latitude)
    if not zone:
        zone = zone_for(longitude, latitude)

    n = FLATTENING / (2.0 - FLATTENING)
    radius = (SEMI_MAJOR / (1.0 + n)
              * (1.0 + n ** 2 / 4.0 + n ** 4 / 64.0 + n ** 6 / 256.0))
    alpha = _kruger_alpha(n)

    phi = math.radians(latitude)
    delta = math.radians(normalise_longitude(longitude - central_meridian(zone)))

    two_root_n = 2.0 * math.sqrt(n) / (1.0 + n)
    sin_phi = math.sin(phi)
    # atanh(sin(phi)) blows up at the poles; UTM stops long before that, and
    # band_letter has already refused anything past 84 N / 80 S.
    tau = math.sinh(math.atanh(sin_phi)
                    - two_root_n * math.atanh(two_root_n * sin_phi))

    xi = math.atan2(tau, math.cos(delta))
    eta = math.atanh(math.sin(delta) / math.sqrt(1.0 + tau * tau))

    xi_sum = xi
    eta_sum = eta
    for index, coefficient in enumerate(alpha, start=1):
        xi_sum += coefficient * math.sin(2.0 * index * xi) * math.cosh(
            2.0 * index * eta)
        eta_sum += coefficient * math.cos(2.0 * index * xi) * math.sinh(
            2.0 * index * eta)

    easting = FALSE_EASTING + SCALE_FACTOR * radius * eta_sum
    northing = SCALE_FACTOR * radius * xi_sum
    if latitude < 0.0:
        northing += FALSE_NORTHING_SOUTH
    return easting, northing


def mgrs_reference(longitude, latitude, digits=5):
    """A full grid reference, e.g. '48P VT 92171 77502'.

    Returns '' above 84 N or below 80 S, where the grid in use is UPS.

    `digits` is the precision per axis: 1 names a 10 km square, 5 a 1 m square.
    The digits are **truncated**, not rounded, which is what MGRS specifies - a
    reference names the square a point is in, and rounding would sometimes name
    the one next door.
    """
    band = band_letter(latitude)
    if not band:
        return ''
    digits = min(5, max(1, int(digits)))

    zone = zone_for(longitude, latitude)
    easting, northing = utm_forward(longitude, latitude, zone)
    letters = square_letters(zone, easting, northing)

    divisor = 10 ** (5 - digits)
    east_part = (int(math.floor(easting)) % 100000) // divisor
    north_part = (int(math.floor(northing)) % 100000) // divisor
    return '{}{} {} {} {}'.format(
        zone, band, letters,
        str(east_part).zfill(digits), str(north_part).zfill(digits))


# ------------------------------------------------------------------- steps

def mgrs_interval(value):
    """`value` rounded **down** to an MGRS step, clamped to 1 m .. 10 km.

    Down, never to nearest. A 5 km spacing rounded up to 10 km would print the
    same principal digits on two neighbouring lines - 1, 1, 2, 2 - which is
    worse than no label at all. Rounded down it reads 50, 55, 60.
    """
    try:
        value = float(value)
    except (TypeError, ValueError):
        return STEPS[-1]
    for step in STEPS:
        if value >= step:
            return step
    return STEPS[-1]


def digits_for_step(step):
    """How many principal digits one step needs: 1 for 10 km, 5 for 1 m.

    This is the strict precision - the fewest digits that still name a unique
    square of that size within a 100 km square. `label_digits` is what the
    margin actually prints.
    """
    snapped = mgrs_interval(step)
    return STEPS.index(snapped) + 1


def label_digits(step):
    """How many digits the margin prints for one step: never fewer than two.

    A 10 km grid needs only one digit to be unambiguous inside a 100 km
    square, but no printed sheet labels it that way: a margin of bare single
    digits reads as a ruler rather than as coordinates, and a reader looking
    for 46 has to find the 4 and then work out which 100 km square it is in.
    Two digits covers 1000 km, which is more than a UTM zone is wide, so the
    second digit is free.

    Finer steps keep their strict precision, so a reference read off a 100 m
    grid is still three digits and still unique.
    """
    return max(LABEL_DIGITS_MINIMUM, digits_for_step(step))


def step_label(step):
    """'1 km', '100 m' - what a grid of this spacing should be called."""
    step = mgrs_interval(step)
    if step >= 1000.0:
        return '{:g} km'.format(step / 1000.0)
    return '{:g} m'.format(step)


# ------------------------------------------------------- label expressions
#
# Three builders, each returning QGIS expression text. They are plain strings
# on purpose: a registered Python function would be missing the moment the
# project is opened on a machine without this plugin, and the labels would go
# with it.
#
# Two things about the expression dialect are load-bearing and were checked
# rather than assumed. `substr` is one-based, which is where every `+ 1` below
# comes from. And `%` follows C, so it can return a negative for a negative
# left operand - hence the `((x % n) + n) % n` around anything that could go
# below zero, which costs nothing and removes the whole question.


def gzd_expression():
    """Zone number on the x lines, latitude band letter on the y lines.

    The x branch can only ever see a longitude and the y branch only a
    latitude, so this draws the plain six-degree zone number: the expression
    has no way to know it is over Norway or Svalbard. `zone_for` does know, and
    it is what names the grid and builds the 100 km letters.
    """
    return (
        "if(@grid_axis = 'y', "
        "substr('{bands}', min({count}, max(1, "
        "floor((@grid_number + 80) / 8) + 1)), 1), "
        "to_string(min(60, max(1, floor((@grid_number + 180) / 6) + 1))))"
    ).format(bands=BAND_LETTERS, count=len(BAND_LETTERS))


def square_expression(zone):
    """100 km column letter on the x lines, row letter on the y lines.

    The zone is baked in as a number because neither axis can work it out: an
    easting line knows its easting and nothing else. That is also why the grids
    this builds go stale if the map is panned into another zone, and why the
    caller re-derives the zone on every apply.
    """
    return (
        "if(@grid_axis = 'y', "
        "substr('{rows}', ((floor(@grid_number / 100000) + {row_off}) "
        "% {row_count} + {row_count}) % {row_count} + 1, 1), "
        # A zone is eight squares wide. Outside that the arithmetic still lands
        # on a letter - the previous zone's - so the line is left unlabelled
        # instead, which is the same rule `column_letter` follows.
        "if(floor(@grid_number / 100000) < 1 "
        "OR floor(@grid_number / 100000) > 8, '', "
        "substr('{cols}', floor(@grid_number / 100000) + {col_off}, 1)))"
    ).format(rows=ROW_LETTERS, row_count=len(ROW_LETTERS),
             row_off=row_offset(zone),
             cols=COLUMN_LETTERS,
             # +1 for substr being one-based, -1 because easting 100000 is the
             # first square, not the zeroth. The two cancel into the offset.
             col_off=column_offset(zone))


def superscript(value):
    """Digits written raised: 13 -> '¹³', '0000' -> '⁰⁰⁰⁰'.

    Takes a string as readily as a number, because the trailing zeros of a
    corner label have to keep their padding.
    """
    text = value if isinstance(value, str) else str(int(value))
    return ''.join(SUPERSCRIPT_DIGITS[int(digit)] for digit in text)


def trailing_zeros(step):
    """How many digits sit below one step: 4 for 10 km, 0 for 1 m."""
    return int(round(math.log10(mgrs_interval(step))))


def corner_label(value, suffix, step=10000.0):
    """One grid line written out in full, the way a printed sheet does it.

    Three parts, and only the middle one is full size:

        ¹ 26 ⁰⁰⁰⁰ m.N.     a 10 km grid at northing 1,260,000
        ¹² 60 ⁰⁰⁰ m.N.     the same line on a 1 km grid

    The large part is the principal digits - exactly what the margin prints
    beside every other line - so that the corner label and the short labels
    along the rest of the margin read as the same number. The small part in
    front is everything above them, the small part behind everything below,
    which is why the split moves when the interval does.
    """
    step = mgrs_interval(step)
    width = label_digits(step)
    span = 10 ** width
    value = int(round(float(value)))
    units = int(math.floor(abs(value) / step))
    trail = trailing_zeros(step)

    text = '{}{}{}m.{}.'.format(
        superscript(units // span) if units // span else '',
        str(units % span).zfill(width),
        superscript(str(abs(value) % int(step)).zfill(trail)) if trail else '',
        suffix)
    return ('-' + text) if value < 0 else text


def _raised(inner):
    """Wrap a numeric-string expression so its digits come out raised."""
    digits = ', '.join("'{}'".format(digit) for digit in '0123456789')
    raised = ', '.join("'{}'".format(digit) for digit in SUPERSCRIPT_DIGITS)
    return "replace({inner}, array({digits}), array({raised}))".format(
        inner=inner, digits=digits, raised=raised)


def _prefix_branch(step, width):
    """The raised 100 km prefix, on every tenth line and nowhere else.

    Two conditions, both needed. The line has to be a tenth one, or every
    label would carry a prefix and the margin would be unreadable. And the
    prefix has to be non-zero: on a 10 km grid the principal digits already
    span 1000 km, so the part above them is nought and printing a raised zero
    would say nothing at all.
    """
    divisor = step * (10 ** width)
    return (
        "if(floor(@grid_number / {step:g}) % {every} = 0 "
        "AND floor(@grid_number / {divisor:g}) > 0, {raised}, '')"
    ).format(step=step, every=PREFIX_EVERY, divisor=divisor,
             raised=_raised(
                 "to_string(floor(@grid_number / {:g}))".format(divisor)))


def _written_value(suffix, step):
    """`corner_label` in expression form, for whichever line turns out first."""
    step = mgrs_interval(step)
    width = label_digits(step)
    span = 10 ** width
    divisor = step * span
    trail = trailing_zeros(step)

    prefix = ("if(floor(@grid_number / {divisor:g}) > 0, {raised}, '')"
              ).format(divisor=divisor,
                       raised=_raised("to_string(floor(@grid_number / "
                                      "{:g}))".format(divisor)))
    principal = ("lpad(to_string((floor(@grid_number / {step:g}) % {span} "
                 "+ {span}) % {span}), {width}, '0')"
                 ).format(step=step, span=span, width=width)
    pieces = [prefix, principal]
    if trail:
        # Worked out rather than a constant string of noughts: a grid with an
        # offset does not put its lines on round numbers, and the label should
        # still say what the line actually is.
        pieces.append(_raised(
            "lpad(to_string(floor(@grid_number) % {step:g}), {trail}, '0')"
            .format(step=step, trail=trail)))
    pieces.append("'m.{}.'".format(suffix))
    return ' || '.join(pieces)


def corner_expression(step_x, step_y):
    """Write the first line of each margin out in full, wherever it lands.

    A map grid labels every line it draws, so "only the corner" has to be said
    in the expression. The test is against the map's own extent, which a grid
    annotation can see: the first line is the one lying less than a full
    interval in from the edge, and since the lines are an interval apart
    exactly one of them can be.

    Reading the extent rather than a number worked out when the grid was
    written is what makes this survive panning and zooming - the label moves
    to whatever the new first line is, with no need to press Apply again.
    """
    return (
        "if(@grid_axis = 'y', "
        "if(@grid_number - y_min(@map_extent) < {step_y:g}, {y_label}, ''), "
        "if(@grid_number - x_min(@map_extent) < {step_x:g}, {x_label}, ''))"
    ).format(step_y=step_y, y_label=_written_value('N', step_y),
             step_x=step_x, x_label=_written_value('E', step_x))


def corner_expression_fixed(first_x, first_y, step_x, step_y):
    """`corner_expression` for a grid whose CRS is not the map's.

    `@map_extent` is in the map's coordinates, so comparing a grid line
    against it only means anything when the two share a CRS. When they do not
    - a metre grid over a degree map - the first line has to be worked out in
    the grid's own CRS beforehand and baked in, and the label then goes stale
    if the map is panned until the next Apply.
    """
    return (
        "if(@grid_axis = 'y', "
        "if(@grid_number < {y_limit:.3f}, '{y_label}', ''), "
        "if(@grid_number < {x_limit:.3f}, '{x_label}', ''))"
    ).format(y_limit=first_y + step_y / 2.0,
             y_label=corner_label(first_y, 'N', step_y),
             x_limit=first_x + step_x / 2.0,
             x_label=corner_label(first_x, 'E', step_x))


def first_line(minimum, step):
    """The first grid line at or after `minimum`, for a grid of `step`."""
    step = float(step)
    if step <= 0:
        return float(minimum)
    # The tolerance keeps a line sitting on the frame edge visible when float
    # error puts it a hair outside.
    return math.ceil(float(minimum) / step - 1e-6) * step


def corner_slots(x_min, x_max, y_min, y_max, step_x, step_y=None):
    """(corner_E, corner_N): the first visible grid line on each axis.

    The slot each margin's corner label owns; the regular label there is
    suppressed. Either value is None when no line falls inside the extent on
    that axis (map smaller than one interval), so no corner label is drawn.
    Extent and steps are in the grid's CRS. Compute it per extent, never cache.
    """
    if step_y is None:
        step_y = step_x
    east = first_line(x_min, step_x)
    north = first_line(y_min, step_y)
    slack_x = 1e-6 * step_x
    slack_y = 1e-6 * step_y
    return (east if east <= x_max + slack_x else None,
            north if north <= y_max + slack_y else None)


def _digits_branch(step, plain=False):
    """The principal-digits expression for one axis.

    The span is ten to the power of the width rather than 100000/step,
    because the width is floored at two: a 10 km grid prints 46, not 6, and
    the modulus has to be the one that keeps two digits rather than the one
    that wraps every 100 km.
    """
    step = mgrs_interval(step)
    width = label_digits(step)
    span = 10 ** width
    if plain:
        # No raised 100 km prefix: the interior strips print the digits only.
        return ("lpad(to_string((floor(@grid_number / {step:g}) "
                "% {span} + {span}) % {span}), {width}, '0')"
                ).format(step=step, span=span, width=width)
    return ("{prefix} || lpad(to_string((floor(@grid_number / {step:g}) "
            "% {span} + {span}) % {span}), {width}, '0')"
            ).format(prefix=_prefix_branch(step, width),
                     step=step, span=span, width=width)


def digits_expression(step_x, step_y=None, skip_first=None, plain=False):
    """The principal digits of a line's coordinate, per axis.

    `skip_first` hands the corner label its slot: the label on the first
    visible line of each margin comes out empty, because `corner_expression`
    writes that line in full instead. The line and tick are the grid's, so
    they are still drawn. It is None for no suppression, True to find the first
    line from the map's extent (same test as `corner_expression`, so the two
    always agree and follow a pan), or a `(first_x, first_y)` pair for a grid
    whose CRS is not the map's (same test as `corner_expression_fixed`).

    `plain` leaves out the raised prefix that every tenth line carries.

    Rotated maps: `@map_extent` is the axis-aligned extent, so on a rotated map
    the "first line" is the first one of that extent; it stays consistent with
    the corner grid, which reads the same variable.

    'The principal digits' is the military map convention: only the part of the
    number that changes across the sheet. An easting of 512000 on a 1 km grid
    reads 12, on a 100 m grid 123, and on a 10 km grid 51 - two digits, never
    one. See `label_digits`.

    The two axes are built separately so an asymmetric grid still labels each
    margin at its own precision, even though the tool asks for a square step.
    """
    if step_y is None:
        step_y = step_x
    y_branch = _digits_branch(step_y, plain)
    x_branch = _digits_branch(step_x, plain)
    if skip_first is True:
        y_branch = "if(@grid_number - y_min(@map_extent) < {:g}, '', {})".format(
            step_y, y_branch)
        x_branch = "if(@grid_number - x_min(@map_extent) < {:g}, '', {})".format(
            step_x, x_branch)
    elif skip_first:
        first_x, first_y = skip_first
        y_branch = "if(@grid_number < {:.3f}, '', {})".format(
            first_y + step_y / 2.0, y_branch)
        x_branch = "if(@grid_number < {:.3f}, '', {})".format(
            first_x + step_x / 2.0, x_branch)
    return "if(@grid_axis = 'y', {y}, {x})".format(y=y_branch, x=x_branch)

# -*- coding: utf-8 -*-
"""Where the MGRS interior label strips go.

The strips are ordinary map grids (see `layout_grid.interior_specs`): a grid
that draws no lines, only annotations placed *inside* the frame. A native grid
puts an inside annotation at one distance from the frame edge, so a strip is
made by choosing that distance - a fraction of the map's width for the
vertical strips that carry northings, of its height for the horizontal strips
that carry eastings. This module holds the option table and the arithmetic;
it needs no QGIS.
"""

CENTER = 'center'
THIRDS = 'thirds'
QUARTERS = 'quarters'

#: The one place that says where the strips go: each option is the list of
#: fractions of the frame width (vertical strips) and height (horizontal
#: strips) they are aimed at. The reference sheet shows QUARTERS; if the
#: selector icons turn out to mean something else, correct it here only.
#: The fractions must be symmetric about 1/2, because a grid has one
#: distance and its far side is measured from the opposite edge.
POSITION_FRACTIONS = {
    CENTER: (0.5,),
    THIRDS: (1.0 / 3.0, 2.0 / 3.0),
    QUARTERS: (0.25, 0.75),
}

#: (key, label) in the order the selector lists them.
POSITION_OPTIONS = (
    (CENTER, 'One strip per axis, centred'),
    (THIRDS, 'Two strips per axis, at 1/3 and 2/3'),
    (QUARTERS, 'Two strips per axis, at 1/4 and 3/4'),
)
DEFAULT_POSITION = QUARTERS

#: Rough glyph metrics, as fractions of the font size, used to centre a label
#: on its strip: annotations are placed by their near edge, not their middle.
DIGIT_WIDTH = 0.56
CAP_HEIGHT = 0.72
POINT_MM = 0.3528


def strip_setup(option, length_mm, half_label_mm):
    """(distance, both_sides) for one pair of opposite sides.

    `length_mm` is the map's size across that pair (width for the left and
    right sides, height for top and bottom). `distance` is how far in from the
    near edge the annotations go, so the label - `half_label_mm` either side of
    its anchor - ends up centred on the strip. `both_sides` is False when the
    option has a single strip, which then sits on the first side only.
    """
    fractions = POSITION_FRACTIONS.get(option,
                                       POSITION_FRACTIONS[DEFAULT_POSITION])
    distance = min(fractions) * length_mm - half_label_mm
    return max(distance, 0.0), len(fractions) > 1


def half_label_sizes(font_size_pt, digits=2):
    """(half width, half height) in mm of a `digits`-digit label."""
    em = font_size_pt * POINT_MM
    return digits * DIGIT_WIDTH * em / 2.0, CAP_HEIGHT * em / 2.0

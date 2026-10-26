# -*- coding: utf-8 -*-
"""
The Symbol Gallery catalog.

Every entry is an original design written for this plugin.  Many follow
long-standing public cartographic conventions (hydraulic profile plots,
topographic map line work, geological map conventions, OSM-like road
casings, tint-band boundaries, vintage water lines, neon dark-map styling),
but no third-party style file is copied.

Each entry builds a *fresh* QgsSymbol / colour ramp on demand, so callers
can modify what they get without touching the catalog.
"""
from . import icons
from .builders import (
    marker_symbol, line_symbol, fill_symbol,
    line, marker_line, hash_line, arrow_line,
    simple_marker, svg_marker,
    simple_fill, no_fill_outline, hatch, point_pattern, random_markers,
    shapeburst, gradient,
    with_glow, with_shadow, ramp,
)

MARKER, LINE, FILL, RAMP = "marker", "line", "fill", "ramp"

KIND_LABELS = {
    MARKER: "Point",
    LINE: "Line",
    FILL: "Polygon",
    RAMP: "Color ramp",
}


class Entry:
    __slots__ = ("id", "name", "category", "kind", "tags", "description", "_build",
                 "color_layer", "version", "_hay")

    def __init__(self, id, name, category, kind, tags, description, build,
                 color_layer=None):
        self.id = id
        self.name = name
        self.category = category
        self.kind = kind
        self.tags = list(tags)
        self.description = description
        self._build = build
        # index of the symbol layer that should take a class colour when the
        # symbol is applied to a categorized/graduated layer (None = auto)
        self.color_layer = color_layer
        self.version = ""          # changes invalidate cached thumbnails
        self._hay = None

    def build(self):
        """Return a brand-new symbol (or QgsGradientColorRamp)."""
        return self._build()

    @property
    def is_ramp(self):
        return self.kind == RAMP

    def haystack(self):
        if self._hay is None:
            self._hay = " ".join([self.name, self.category, KIND_LABELS[self.kind],
                                  self.description] + self.tags).lower()
        return self._hay

    def matches(self, text):
        if not text:
            return True
        hay = self.haystack()
        return all(tok in hay for tok in text.lower().split())

    def __repr__(self):
        return "<Entry %s (%s)>" % (self.id, self.kind)


ENTRIES = []


def entry(category, kind, name, tags, description, color_layer=None):
    def deco(fn):
        ENTRIES.append(Entry(fn.__name__, name, category, kind, tags,
                             description, fn, color_layer))
        return fn
    return deco


# ==========================================================================
# 1. HYDRAULICS  (HEC-RAS style plot conventions)
# ==========================================================================
HYD = "Hydraulics & Flood"


@entry(HYD, LINE, "Ground profile", ["hec-ras", "terrain", "cross section", "profile"],
       "Black ground line with square vertex markers, as in hydraulic profile plots.")
def hyd_ground():
    return line_symbol(
        line("#111111", 0.35),
        marker_line(marker_symbol(simple_marker("square", 1.0, "#111111", "#111111", 0)),
                    placement="vertex"))


@entry(HYD, MARKER, "Bank station", ["hec-ras", "bank", "station", "cross section"],
       "Red diamond used to mark left/right bank stations.")
def hyd_bank_station():
    return marker_symbol(simple_marker("diamond", 2.6, "#e3001b", "#7a0010", 0.2))


@entry(HYD, LINE, "Water surface (WS)", ["hec-ras", "water surface", "ws", "profile"],
       "Solid blue water-surface line.")
def hyd_ws():
    return line_symbol(line("#1f4fff", 0.45))


@entry(HYD, LINE, "Energy grade (EG)", ["hec-ras", "energy grade", "eg", "profile"],
       "Long-dashed green energy grade line.")
def hyd_eg():
    return line_symbol(line("#16a34a", 0.4, dash=[4, 2.2]))


@entry(HYD, LINE, "Critical depth (Crit)", ["hec-ras", "critical depth", "profile"],
       "Red dash-dot line for critical water surface.")
def hyd_crit():
    return line_symbol(line("#e3001b", 0.35, dash=[3, 1.2, 0.4, 1.2]))


@entry(HYD, LINE, "River centerline + flow", ["river", "flow direction", "hec-ras", "arrow"],
       "Blue centreline with repeating chevrons that point downstream (line direction).")
def hyd_centerline():
    chev = marker_symbol(simple_marker("arrowhead", 2.2, "#1d4ed8", "#1d4ed8", 0.35))
    return line_symbol(line("#1d4ed8", 0.5), marker_line(chev, interval=12, offset_along=6))


@entry(HYD, LINE, "Bank lines", ["hec-ras", "bank", "ras mapper"],
       "Red bank line with small outward ticks.")
def hyd_bank_lines():
    return line_symbol(line("#dc2626", 0.4),
                       hash_line("#dc2626", 0.25, 1.0, 2.5, offset=0.5))


@entry(HYD, LINE, "XS cut line", ["hec-ras", "cross section", "xs", "cut line"],
       "Green cross-section cut line with end caps and a centre station dot.")
def hyd_xs():
    cap = marker_symbol(simple_marker("line", 2.6, "#15803d", "#15803d", 0.45))
    dot = marker_symbol(simple_marker("circle", 1.4, "#ffffff", "#15803d", 0.35))
    return line_symbol(line("#15803d", 0.4),
                       marker_line(cap, placement="firstvertex"),
                       marker_line(cap, placement="lastvertex"),
                       marker_line(dot, placement="centralpoint"))


@entry(HYD, LINE, "Levee", ["levee", "embankment", "dike", "hec-ras"],
       "Brown levee crest with triangle teeth on the protected side.")
def hyd_levee():
    tooth = marker_symbol(simple_marker("triangle", 1.2, "#7c4a1e", "#7c4a1e", 0))
    return line_symbol(line("#7c4a1e", 0.6),
                       marker_line(tooth, interval=3.0, offset=-0.7))


@entry(HYD, LINE, "2D breakline", ["hec-ras", "2d", "breakline", "mesh"],
       "Magenta dashed breakline used to align a 2D mesh.")
def hyd_breakline():
    return line_symbol(line("#d946ef", 0.4, dash=[2.2, 1.0]))


@entry(HYD, FILL, "Ineffective flow area", ["hec-ras", "ineffective", "cross hatch"],
       "Red outline with diagonal cross-hatch, like the HEC-RAS fill pattern.")
def hyd_ineffective():
    return fill_symbol(hatch("#e11d48", 45, 1.6, 0.22),
                       hatch("#e11d48", -45, 1.6, 0.22),
                       no_fill_outline("#e11d48", 0.5))


@entry(HYD, FILL, "Blocked obstruction", ["hec-ras", "obstruction", "blocked"],
       "Dense black hatch for blocked obstructions.")
def hyd_blocked():
    return fill_symbol(simple_fill("#111111", "#111111", 0.4, alpha=40),
                       hatch("#111111", 45, 0.9, 0.25))


@entry(HYD, FILL, "2D flow area", ["hec-ras", "2d", "flow area", "mesh"],
       "Teal perimeter with a crisp inner tint band.")
def hyd_2d_area():
    return fill_symbol(line("#14b8a6", 3.0, alpha=70, cap="flat", join="miter", inside=True),
                       no_fill_outline("#0f766e", 0.6))


@entry(HYD, FILL, "Inundation extent", ["flood", "inundation", "extent", "water"],
       "Translucent blue flood extent with a darker edge.")
def hyd_inundation():
    return fill_symbol(simple_fill("#3b82f6", "#1e40af", 0.4, alpha=110))


@entry(HYD, FILL, "Flood hazard – high", ["flood", "hazard", "risk"],
       "Red diagonal hatch over a light tint for high-hazard zones.")
def hyd_hazard_high():
    return fill_symbol(simple_fill("#fca5a5", "#b91c1c", 0.4, alpha=110),
                       hatch("#b91c1c", 45, 2.2, 0.3))


@entry(HYD, MARKER, "Stream gauge", ["gauge", "station", "hydrometric", "monitoring"],
       "Rounded gauge badge with water-level staff.")
def hyd_gauge():
    return marker_symbol(svg_marker(icons.GAUGE, 6, "#0369a1", "#ffffff", 0.6))


@entry(HYD, MARKER, "Culvert / structure", ["culvert", "bridge", "structure", "inline"],
       "Dark square with white ring – inline structure.")
def hyd_structure():
    return marker_symbol(simple_marker("square", 3.4, "#1f2937", "#ffffff", 0.5),
                         simple_marker("circle", 1.6, "#1f2937", "#ffffff", 0.4))


@entry(HYD, RAMP, "Velocity (m/s)", ["velocity", "hec-ras", "ras mapper", "speed"],
       "Pale yellow → greens → teal → navy, matching the classic 0–3.5 m/s velocity legend.")
def ramp_velocity():
    return ramp(["#ffffcc", "#c2f08a", "#86ef7f", "#3dff8a", "#22c983",
                 "#0f8f7a", "#0b4f86", "#000080"])


@entry(HYD, RAMP, "Water depth", ["depth", "flood", "water"],
       "Very light cyan for shallow water to deep navy.")
def ramp_depth():
    return ramp(["#e0f7ff", "#9edcf5", "#52b2e8", "#1f78c8", "#0b3f8c", "#061f4a"])


@entry(HYD, RAMP, "Flood hazard (H1–H6)", ["hazard", "flood", "risk", "dv"],
       "Green → yellow → orange → red → purple hazard classes.", )
def ramp_hazard():
    return ramp(["#2fbf71", "#f7e04a", "#f59e0b", "#ef4444", "#b91c1c", "#6b21a8"])


# ==========================================================================
# 2. WATER & HYDROLOGY
# ==========================================================================
WAT = "Water & Hydrology"


@entry(WAT, LINE, "Major river", ["river", "water", "hydrography"],
       "Two-tone river: deep blue body with a lighter core highlight.")
def wat_river():
    return line_symbol(line("#2f6db5", 1.6), line("#7fb6ea", 0.6))


@entry(WAT, LINE, "Stream (perennial)", ["stream", "creek", "water"],
       "Clean mid-blue stream line.")
def wat_stream():
    return line_symbol(line("#3b82c4", 0.45))


@entry(WAT, LINE, "Stream (intermittent)", ["stream", "intermittent", "seasonal"],
       "Blue dash-dot-dot line for seasonal streams.")
def wat_intermittent():
    return line_symbol(line("#3b82c4", 0.4, dash=[3, 0.8, 0.4, 0.8, 0.4, 0.8]))


@entry(WAT, LINE, "Canal / irrigation", ["canal", "irrigation", "channel"],
       "Cased canal – dark blue banks with a light water core.")
def wat_canal():
    return line_symbol(line("#1e3a8a", 1.4, cap="flat"), line("#bfdbfe", 0.8, cap="flat"))


@entry(WAT, FILL, "Lake – vintage water lines", ["lake", "vintage", "water lines", "reservoir"],
       "Light water fill with fading shore lines inside the lake – a classic hand-drawn effect.")
def wat_vintage():
    return fill_symbol(
        simple_fill("#d6ecf7", "#000000", 0, outline_style="no"),
        line("#3f86bd", 0.25, offset=3.4, alpha=70),
        line("#3f86bd", 0.25, offset=2.4, alpha=120),
        line("#3f86bd", 0.28, offset=1.5, alpha=170),
        line("#3f86bd", 0.3, offset=0.7, alpha=220),
        line("#245f8f", 0.45))


@entry(WAT, FILL, "Island – vintage coast ripples", ["coast", "island", "vintage",
                                                       "water lines", "land"],
       "For LAND polygons: parchment fill with water ripples radiating out into the sea.")
def wat_coast_ripples():
    return fill_symbol(
        line("#3f86bd", 0.25, offset=-3.4, alpha=60),
        line("#3f86bd", 0.25, offset=-2.4, alpha=110),
        line("#3f86bd", 0.28, offset=-1.5, alpha=160),
        line("#3f86bd", 0.3, offset=-0.7, alpha=210),
        simple_fill("#f3ead3", "#6b5b3e", 0.4))


@entry(WAT, FILL, "Lake – soft shoreline", ["lake", "reservoir", "water", "shapeburst"],
       "Blue fill that darkens toward the shore (shapeburst).")
def wat_lake_soft():
    return fill_symbol(simple_fill("#a9d4f2", "#000000", 0, outline_style="no"),
                       shapeburst("#4f97cf", distance=3, blur=2),
                       no_fill_outline("#3c7fb6", 0.3))


@entry(WAT, FILL, "Wetland / marsh", ["wetland", "marsh", "swamp"],
       "Pale teal with staggered marsh tufts.")
def wat_wetland():
    tuft = marker_symbol(svg_marker(icons.MARSH_TUFT, 3.2, "#2f7d6d"))
    return fill_symbol(simple_fill("#dff3ec", "#2f7d6d", 0.25),
                       point_pattern(tuft, 6, 5))


@entry(WAT, FILL, "Ocean", ["ocean", "sea", "water", "background"],
       "Flat sea blue with a fine coastline – clean background water.")
def wat_ocean():
    return fill_symbol(simple_fill("#a9d2ef", "#5b9bd5", 0.3))


# ==========================================================================
# 3. TRANSPORT
# ==========================================================================
TRA = "Transport"


@entry(TRA, LINE, "Motorway", ["road", "highway", "motorway", "casing"],
       "Orange motorway with dark casing and a thin centre line.", color_layer=1)
def tra_motorway():
    return line_symbol(line("#8a3b12", 2.0), line("#f59e42", 1.5),
                       line("#ffe5c4", 0.15))


@entry(TRA, LINE, "Primary road", ["road", "primary", "casing"],
       "Yellow primary road with a brown-grey casing.", color_layer=1)
def tra_primary():
    return line_symbol(line("#8d7a4a", 1.5), line("#fde68a", 1.1))


@entry(TRA, LINE, "Local road", ["road", "street", "residential", "casing"],
       "White local street with grey casing – reads well on any basemap.", color_layer=1)
def tra_local():
    return line_symbol(line("#9ca3af", 1.0), line("#ffffff", 0.7))


@entry(TRA, LINE, "Unpaved track", ["track", "dirt road", "unpaved"],
       "Brown dashed casing for tracks and farm roads.")
def tra_track():
    return line_symbol(line("#a16207", 0.55, dash=[2.4, 1.0], cap="flat"))


@entry(TRA, LINE, "Railway (classic)", ["railway", "train", "rail"],
       "Black and white alternating railway line.")
def tra_rail():
    return line_symbol(line("#1f2937", 1.0, cap="flat"),
                       line("#ffffff", 0.6, dash=[3, 3], cap="flat"))


@entry(TRA, LINE, "Railway (sleepers)", ["railway", "rail", "ties", "sleepers"],
       "Thin rail with perpendicular sleepers.")
def tra_rail_ties():
    return line_symbol(line("#374151", 0.35),
                       hash_line("#374151", 0.3, 1.6, 1.6))


@entry(TRA, LINE, "Footpath / trail", ["trail", "footpath", "hiking"],
       "Short red-brown dashes, classic trail styling.")
def tra_trail():
    return line_symbol(line("#b45309", 0.45, dash=[1.2, 0.9]))


@entry(TRA, LINE, "Bike route", ["bicycle", "cycling", "bike"],
       "Green dotted route with a soft halo.")
def tra_bike():
    dot = marker_symbol(simple_marker("circle", 0.9, "#16a34a", "#16a34a", 0))
    return line_symbol(line("#bbf7d0", 1.2, alpha=180),
                       marker_line(dot, interval=1.6))


@entry(TRA, LINE, "Ferry route", ["ferry", "boat", "waterway"],
       "Navy dashes for ferry/boat routes.")
def tra_ferry():
    return line_symbol(line("#1e3a8a", 0.4, dash=[2.4, 1.6]))


@entry(TRA, LINE, "Flight path (arc arrow)", ["flight", "route", "arrow", "flow map"],
       "Curved semi-transparent arrow for flow and origin–destination maps.")
def tra_flight():
    return line_symbol(arrow_line("#6366f1", 1.0, 3.2, "#ffffff"), opacity=0.85)


# ==========================================================================
# 4. BOUNDARIES
# ==========================================================================
BND = "Boundaries"


@entry(BND, LINE, "International border", ["boundary", "border", "country"],
       "Dash-dot border over a soft violet halo.")
def bnd_international():
    return line_symbol(line("#c4b5fd", 2.2, alpha=160),
                       line("#4c1d95", 0.5, dash=[3.2, 1, 0.6, 1]))


@entry(BND, LINE, "Province / state border", ["boundary", "province", "state"],
       "Grey long-dash with a light halo.")
def bnd_province():
    return line_symbol(line("#e5e7eb", 1.4, alpha=200),
                       line("#6b7280", 0.4, dash=[2.4, 1.0]))


@entry(BND, LINE, "District border", ["boundary", "district", "commune"],
       "Fine dotted grey border.")
def bnd_district():
    return line_symbol(line("#6b7280", 0.3, dash=[0.5, 0.7]))


@entry(BND, FILL, "Tint band – protected area", ["protected area", "park", "tint band"],
       "Transparent interior with a green inner band – the cartographer's favourite.")
def bnd_protected():
    return fill_symbol(line("#16a34a", 4.8, alpha=90, cap="flat", join="miter", inside=True),
                       no_fill_outline("#15803d", 0.45))


@entry(BND, FILL, "Tint band – study area", ["study area", "aoi", "tint band", "focus"],
       "Red inner band and crisp outline to highlight an area of interest.")
def bnd_study():
    return fill_symbol(line("#ef4444", 6.0, alpha=80, cap="flat", join="miter", inside=True),
                       no_fill_outline("#b91c1c", 0.6))


@entry(BND, FILL, "Admin area – soft shadow", ["admin", "region", "shadow", "highlight"],
       "Light fill lifted off the map with a subtle drop shadow.")
def bnd_shadow():
    return fill_symbol(with_shadow(simple_fill("#fafaf9", "#78716c", 0.35),
                                   distance=1.2, blur=5, opacity=0.35))


@entry(BND, FILL, "Mask – outside focus", ["mask", "inverted", "highlight", "dim"],
       "Dark translucent fill to dim everything outside your focus area.")
def bnd_mask():
    return fill_symbol(simple_fill("#0f172a", "#0f172a", 0.0, alpha=110, outline_style="no"))


# ==========================================================================
# 5. LAND COVER
# ==========================================================================
LND = "Land Cover"


@entry(LND, FILL, "Forest (broadleaf)", ["forest", "trees", "woodland", "vegetation"],
       "Green fill with randomly scattered round tree crowns.")
def lnd_forest():
    tree = marker_symbol(svg_marker(icons.TREE, 3.6, "#3f8f3a"))
    return fill_symbol(simple_fill("#cfe8c4", "#5a9a4a", 0.25),
                       random_markers(tree, density=30, seed=11))


@entry(LND, FILL, "Forest (conifer)", ["forest", "conifer", "pine", "evergreen"],
       "Darker green with staggered conifer glyphs.")
def lnd_conifer():
    tree = marker_symbol(svg_marker(icons.CONIFER, 3.4, "#1f5f3a"))
    return fill_symbol(simple_fill("#c6dfcf", "#2f6f4a", 0.25),
                       point_pattern(tree, 5, 5))


@entry(LND, FILL, "Mangrove", ["mangrove", "coastal", "forest", "wetland"],
       "Teal-green with scattered crowns and water hatching.")
def lnd_mangrove():
    tree = marker_symbol(svg_marker(icons.TREE, 2.8, "#1f7a5c"))
    return fill_symbol(simple_fill("#d2efe6", "#1f7a5c", 0.25),
                       hatch("#7cc4dd", 0, 1.6, 0.18),
                       random_markers(tree, density=22, seed=5))


@entry(LND, FILL, "Rice paddy", ["rice", "paddy", "agriculture", "cropland"],
       "Fresh green with horizontal bunds and rice tufts in rows.")
def lnd_paddy():
    tuft = marker_symbol(svg_marker(icons.RICE_TUFT, 2.6, "#3f8f3a"))
    return fill_symbol(simple_fill("#e6f4c8", "#7aa33a", 0.25),
                       hatch("#a8c97a", 0, 3.0, 0.2),
                       point_pattern(tuft, 3.6, 3.0, offset_y=0))


@entry(LND, FILL, "Cropland", ["cropland", "agriculture", "farm", "field"],
       "Warm yellow with thin diagonal furrows.")
def lnd_crop():
    return fill_symbol(simple_fill("#fbefb8", "#c8a43c", 0.25),
                       hatch("#d8b54a", 45, 1.8, 0.2))


@entry(LND, FILL, "Plantation / orchard", ["plantation", "orchard", "rubber", "palm"],
       "Regular grid of small crowns – rubber, cashew, palm, orchard.")
def lnd_plantation():
    dot = marker_symbol(simple_marker("circle", 1.4, "#4d9a44", "#2f6b2a", 0.2))
    return fill_symbol(simple_fill("#e8f3d6", "#4d7a3a", 0.25),
                       point_pattern(dot, 3, 3, offset_x=0))


@entry(LND, FILL, "Grassland", ["grassland", "pasture", "meadow"],
       "Light green with tiny scattered grass ticks.")
def lnd_grass():
    tick = marker_symbol(simple_marker("line", 1.0, "#6aa84f", "#6aa84f", 0.25, angle=20))
    return fill_symbol(simple_fill("#eaf6d8", "#8cbf6a", 0.25),
                       random_markers(tick, density=4, seed=3))


@entry(LND, FILL, "Built-up / urban", ["urban", "built-up", "settlement", "city"],
       "Warm grey with fine cross-hatch.")
def lnd_urban():
    return fill_symbol(simple_fill("#e7e2dc", "#9b8f84", 0.25),
                       hatch("#b8aca0", 45, 1.4, 0.15),
                       hatch("#b8aca0", -45, 1.4, 0.15))


@entry(LND, FILL, "Building (lifted)", ["building", "footprint", "3d", "shadow"],
       "Building footprint with drop shadow for a light 3-D look.")
def lnd_building():
    return fill_symbol(with_shadow(simple_fill("#d9d3cc", "#8f857a", 0.2),
                                   distance=0.8, angle=135, blur=2, opacity=0.5))


@entry(LND, FILL, "Sand / beach", ["sand", "beach", "desert", "dune"],
       "Pale sand with stippled dots.")
def lnd_sand():
    dot = marker_symbol(simple_marker("circle", 0.45, "#c49a5a", "#c49a5a", 0, no_outline=True))
    return fill_symbol(simple_fill("#f7ead0", "#d4b483", 0.25),
                       random_markers(dot, density=1.6, seed=21))


@entry(LND, FILL, "Bare rock", ["rock", "bare", "outcrop"],
       "Grey with random small triangles.")
def lnd_rock():
    tri = marker_symbol(simple_marker("triangle", 0.9, "#8b8b8b", "#6b6b6b", 0.1))
    return fill_symbol(simple_fill("#e4e4e2", "#8b8b8b", 0.25),
                       random_markers(tri, density=6, seed=9))


# ==========================================================================
# 6. TERRAIN & GEOLOGY
# ==========================================================================
GEO = "Terrain & Geology"


@entry(GEO, LINE, "Contour – index", ["contour", "elevation", "topographic"],
       "Bold brown index contour.")
def geo_contour_index():
    return line_symbol(line("#8b5a2b", 0.45))


@entry(GEO, LINE, "Contour – intermediate", ["contour", "elevation", "topographic"],
       "Thin, lighter brown intermediate contour.")
def geo_contour():
    return line_symbol(line("#b08060", 0.18, alpha=200))


@entry(GEO, LINE, "Normal fault", ["fault", "geology", "normal"],
       "Black fault with ball-and-bar ticks on the downthrown side.")
def geo_normal_fault():
    tick = marker_symbol(simple_marker("line", 1.4, "#111111", "#111111", 0.3,
                                       offset=(0, 0)),
                         simple_marker("circle", 0.9, "#111111", "#111111", 0,
                                       offset=(0, 0.9)))
    return line_symbol(line("#111111", 0.5), marker_line(tick, interval=6, offset=-0.7))


@entry(GEO, LINE, "Thrust fault", ["fault", "thrust", "geology"],
       "Black line with solid triangles on the upper plate.")
def geo_thrust():
    tri = marker_symbol(simple_marker("triangle", 1.6, "#111111", "#111111", 0))
    return line_symbol(line("#111111", 0.55), marker_line(tri, interval=5, offset=-0.8))


@entry(GEO, LINE, "Geological contact", ["contact", "geology"],
       "Fine black contact line (dashed if approximate).")
def geo_contact():
    return line_symbol(line("#111111", 0.25, dash=[3, 1]))


@entry(GEO, LINE, "Cliff / escarpment", ["cliff", "escarpment", "scarp", "terrain"],
       "Dark line with downslope hachures.")
def geo_cliff():
    return line_symbol(line("#3f2d20", 0.45),
                       hash_line("#3f2d20", 0.25, 1.6, 0.9, offset=-0.8))


@entry(GEO, LINE, "Ridge line", ["ridge", "watershed divide", "terrain"],
       "Brown dash-dot ridge / divide line.")
def geo_ridge():
    return line_symbol(line("#7c5a3a", 0.4, dash=[4, 1, 0.8, 1]))


@entry(GEO, MARKER, "Summit / peak", ["peak", "summit", "mountain", "elevation"],
       "Mountain glyph with snow cap.")
def geo_peak():
    return marker_symbol(svg_marker(icons.PEAK, 5.5, "#6b4f3a"))


@entry(GEO, MARKER, "Survey control point", ["survey", "benchmark", "gnss", "control"],
       "Triangle with centre dot – geodetic control / benchmark.")
def geo_control():
    return marker_symbol(simple_marker("triangle", 3.6, "#ffffff", "#111111", 0.35),
                         simple_marker("circle", 0.8, "#111111", "#111111", 0))


@entry(GEO, MARKER, "Field sample", ["sample", "field", "survey", "soil", "target"],
       "Target crosshair for field samples and ground-truth points.")
def geo_sample():
    return marker_symbol(svg_marker(icons.SAMPLE, 5.0, "#be123c"))


@entry(GEO, RAMP, "Hypsometric tint", ["elevation", "dem", "hypsometric", "terrain",
                                       "dem ramp", "relief"],
       "Lowland green → yellow → tan → brown → snow white.")
def ramp_hypso():
    return ramp(["#4f9a5b", "#9cc56b", "#e9e38f", "#d9b16a", "#a8744a",
                 "#8a6a5a", "#f5f2ee"], [0, .15, .32, .5, .7, .88, 1])


@entry(GEO, RAMP, "Bathymetry", ["bathymetry", "depth", "ocean", "sea", "dem ramp",
                                 "relief"],
       "Abyssal navy → shelf turquoise.")
def ramp_bathy():
    return ramp(["#08133a", "#123f7a", "#1e6fae", "#41a3cf", "#8fd3e6", "#d7f1f5"])


# ==========================================================================
# 7. POINTS OF INTEREST
# ==========================================================================
POI = "Points of Interest"


@entry(POI, MARKER, "Map pin", ["pin", "location", "marker", "poi"],
       "Modern drop pin anchored at its tip.")
def poi_pin():
    return marker_symbol(svg_marker(icons.MAP_PIN, 7, "#e8463a", "#ffffff", 0.3,
                                    anchor_bottom=True))


@entry(POI, MARKER, "Hospital", ["hospital", "health", "clinic"],
       "Red rounded badge with white cross.")
def poi_hospital():
    return marker_symbol(svg_marker(icons.HOSPITAL, 5.5, "#dc2626", "#ffffff", 0.4))


@entry(POI, MARKER, "School", ["school", "education", "university"],
       "Blue rounded badge with graduation cap.")
def poi_school():
    return marker_symbol(svg_marker(icons.SCHOOL, 5.5, "#2563eb", "#ffffff", 0.4))


@entry(POI, MARKER, "Pagoda / temple", ["pagoda", "temple", "wat", "religion"],
       "Golden roundel with a tiered pagoda silhouette.")
def poi_pagoda():
    return marker_symbol(svg_marker(icons.PAGODA, 6, "#c88a12", "#ffffff", 0.4))


@entry(POI, MARKER, "Well / water point", ["well", "borehole", "water point", "spring"],
       "Blue water drop.")
def poi_well():
    return marker_symbol(svg_marker(icons.WATER_DROP, 5, "#0284c7", "#ffffff", 0.3))


@entry(POI, MARKER, "Campsite", ["camp", "campsite", "tent", "recreation"],
       "Green tent glyph.")
def poi_camp():
    return marker_symbol(svg_marker(icons.CAMPSITE, 5, "#15803d", "#ffffff", 0.3))


@entry(POI, MARKER, "Viewpoint", ["viewpoint", "scenic", "tourism"],
       "Purple roundel with an eye.")
def poi_view():
    return marker_symbol(svg_marker(icons.VIEWPOINT, 5.5, "#7c3aed", "#ffffff", 0.4))


@entry(POI, MARKER, "Capital city", ["capital", "city", "settlement"],
       "Star inside a ring – national capital.", color_layer=1)
def poi_capital():
    return marker_symbol(simple_marker("circle", 4.2, "#ffffff", "#111111", 0.4),
                         simple_marker("star", 3.2, "#b91c1c", "#b91c1c", 0))


@entry(POI, MARKER, "Town", ["town", "city", "settlement"],
       "Dot with white ring – readable on any background.")
def poi_town():
    return marker_symbol(simple_marker("circle", 2.6, "#111827", "#ffffff", 0.6))


@entry(POI, MARKER, "Classic dot (shadowed)", ["dot", "point", "default", "shadow"],
       "Orange point with a soft drop shadow – a better default point.")
def poi_dot():
    return marker_symbol(with_shadow(simple_marker("circle", 3.0, "#f97316", "#ffffff", 0.5),
                                     distance=0.5, blur=2, opacity=0.5))


# ==========================================================================
# 8. DARK & NEON  (modern / web-map look)
# ==========================================================================
NEO = "Dark & Neon"


@entry(NEO, LINE, "Neon cyan", ["neon", "glow", "dark", "cyber"],
       "Bright cyan core with outer glow – for dark basemaps.")
def neo_cyan():
    return line_symbol(with_glow(line("#a5f3fc", 0.5), "#06b6d4", spread=1.8, blur=3))


@entry(NEO, LINE, "Neon magenta", ["neon", "glow", "dark", "pink"],
       "Hot magenta glowing line.")
def neo_magenta():
    return line_symbol(with_glow(line("#fbcfe8", 0.5), "#ec4899", spread=1.8, blur=3))


@entry(NEO, LINE, "Neon amber", ["neon", "glow", "dark", "orange", "traffic"],
       "Warm amber glow – great for road networks at night.")
def neo_amber():
    return line_symbol(with_glow(line("#fde68a", 0.45), "#f59e0b", spread=1.5, blur=3))


@entry(NEO, MARKER, "Glow point – cyan", ["glow", "neon", "dark", "pulse"],
       "Small bright point with a big soft halo.")
def neo_pt_cyan():
    return marker_symbol(with_glow(simple_marker("circle", 1.8, "#e0f2fe", "#22d3ee", 0.3),
                                   "#22d3ee", spread=2.5, blur=4))


@entry(NEO, MARKER, "Glow point – hotspot", ["glow", "hotspot", "heat", "fire", "dark"],
       "Yellow-orange glowing hotspot – fires, events, incidents.")
def neo_pt_hot():
    return marker_symbol(with_glow(simple_marker("circle", 2.0, "#fef08a", "#f97316", 0.3),
                                   "#f97316", spread=2.5, blur=4))


@entry(NEO, MARKER, "Radar ring", ["ring", "radar", "target", "dark"],
       "Concentric translucent rings around a bright centre.")
def neo_ring():
    return marker_symbol(simple_marker("circle", 6.5, "#22d3ee", "#22d3ee", 0.2, alpha=25,
                                       outline_alpha=90),
                         simple_marker("circle", 4.0, "#22d3ee", "#22d3ee", 0.25, alpha=40,
                                       outline_alpha=160),
                         simple_marker("circle", 1.6, "#ecfeff", "#22d3ee", 0.3))


@entry(NEO, FILL, "Glowing edge polygon", ["neon", "glow", "dark", "polygon"],
       "Dark translucent interior with a glowing violet edge.")
def neo_poly():
    return fill_symbol(simple_fill("#4c1d95", "#000000", 0, alpha=60, outline_style="no"),
                       with_glow(line("#ddd6fe", 0.4), "#8b5cf6", spread=1.6, blur=3))


@entry(NEO, FILL, "Blueprint", ["blueprint", "technical", "grid", "dark"],
       "Blueprint blue with a fine white grid and crisp outline.")
def neo_blueprint():
    return fill_symbol(simple_fill("#1e3a8a", "#000000", 0, outline_style="no"),
                       hatch("#ffffff", 0, 2.0, 0.1, alpha=70),
                       hatch("#ffffff", 90, 2.0, 0.1, alpha=70),
                       no_fill_outline("#e0f2fe", 0.45))


@entry(NEO, FILL, "Gradient glass", ["gradient", "modern", "glass", "dashboard"],
       "Smooth teal → indigo gradient with a white hairline.")
def neo_gradient():
    return fill_symbol(gradient("#2dd4bf", "#6366f1", angle=0),
                       no_fill_outline("#ffffff", 0.3, alpha=200))


@entry(NEO, RAMP, "Night lights", ["night", "dark", "lights", "density"],
       "Black → deep purple → magenta → orange → pale yellow.")
def ramp_night():
    return ramp(["#000004", "#2c115f", "#721f81", "#b73779", "#f1605d", "#feb078", "#fcfdbf"])


# ==========================================================================
# 9. REMOTE SENSING RAMPS
# ==========================================================================
RS = "Remote Sensing"


@entry(RS, RAMP, "NDVI", ["ndvi", "vegetation", "index", "sentinel", "landsat"],
       "Water blue → bare brown → sparse yellow → dense dark green (-1 to 1).")
def ramp_ndvi():
    return ramp(["#2b5ca8", "#a6611a", "#dfc27d", "#f5f0b4", "#a6d96a", "#4dac26", "#0b5d1e"],
                [0, .45, .52, .58, .68, .82, 1])


@entry(RS, RAMP, "NDWI / water index", ["ndwi", "mndwi", "water", "index"],
       "Dry tan → white → bright water blue.")
def ramp_ndwi():
    return ramp(["#8c510a", "#d8b365", "#f6f4ee", "#7fc4e8", "#2166ac", "#08306b"])


@entry(RS, RAMP, "Land surface temperature", ["lst", "temperature", "thermal", "heat"],
       "Cool blue → white → warm orange → hot dark red.")
def ramp_lst():
    return ramp(["#313695", "#4575b4", "#abd9e9", "#fffbe0", "#fdae61", "#d73027", "#7f0000"])


@entry(RS, RAMP, "Rainfall", ["rainfall", "precipitation", "climate"],
       "Light to heavy precipitation: pale → green → blue → purple.")
def ramp_rain():
    return ramp(["#f7fcf0", "#ccebc5", "#7bccc4", "#2b8cbe", "#253494", "#5b1a8a"])


@entry(RS, RAMP, "Change (loss ↔ gain)", ["change detection", "difference", "diverging"],
       "Diverging red–white–green for change detection maps.")
def ramp_change():
    return ramp(["#b2182b", "#ef8a62", "#fddbc7", "#f7f7f7", "#d9f0d3", "#7fbf7b", "#1b7837"])


# ==========================================================================
# 10. IRRIGATION & DRAINAGE
# ==========================================================================
IRR = "Irrigation & Drainage"


@entry(IRR, LINE, "Main canal", ["irrigation", "canal", "primary", "main"],
       "Bold cased canal with a bright water core and downstream chevrons.", color_layer=1)
def irr_main():
    chev = marker_symbol(simple_marker("arrowhead", 1.6, "#ffffff", "#ffffff", 0.3))
    return line_symbol(line("#0b3d91", 2.0, cap="flat"), line("#4aa3df", 1.3, cap="flat"),
                       marker_line(chev, interval=10, offset_along=5))


@entry(IRR, LINE, "Secondary canal", ["irrigation", "canal", "secondary", "lateral"],
       "Medium cased canal for laterals and branch canals.", color_layer=1)
def irr_secondary():
    return line_symbol(line("#1e5aa8", 1.2, cap="flat"), line("#8cc8ef", 0.6, cap="flat"))


@entry(IRR, LINE, "Tertiary / field channel", ["irrigation", "tertiary", "field channel",
                                                "quaternary"],
       "Thin dashed blue line for on-farm channels.")
def irr_tertiary():
    return line_symbol(line("#2f86d6", 0.35, dash=[2.2, 0.8]))


@entry(IRR, LINE, "Lined canal", ["irrigation", "lined", "concrete", "canal"],
       "Grey concrete banks with a blue core – lined canal.", color_layer=1)
def irr_lined():
    return line_symbol(line("#8a8f98", 1.5, cap="flat"), line("#3b8fd6", 0.8, cap="flat"),
                       hash_line("#5f6670", 0.18, 1.5, 3.0))


@entry(IRR, LINE, "Main drain", ["drainage", "drain", "collector"],
       "Teal-green drain with dash-dot pattern and flow ticks.")
def irr_drain():
    return line_symbol(line("#0f766e", 0.7, dash=[3.5, 0.9, 0.6, 0.9]))


@entry(IRR, LINE, "Field drain", ["drainage", "field drain", "ditch"],
       "Light teal dotted line for minor drains and ditches.")
def irr_field_drain():
    return line_symbol(line("#14b8a6", 0.35, dash=[0.6, 0.8]))


@entry(IRR, LINE, "Pressure pipeline", ["pipeline", "pipe", "pressurised", "irrigation"],
       "Dark blue pipe with joint dots at regular spacing.")
def irr_pipe():
    joint = marker_symbol(simple_marker("circle", 1.1, "#ffffff", "#1e3a8a", 0.3))
    return line_symbol(line("#1e3a8a", 0.6), marker_line(joint, interval=6, offset_along=3))


@entry(IRR, LINE, "Siphon / culvert crossing", ["siphon", "culvert", "crossing"],
       "Short dashed segment with end bars where water passes under a road.")
def irr_siphon():
    bar = marker_symbol(simple_marker("line", 2.6, "#1e3a8a", "#1e3a8a", 0.45))
    return line_symbol(line("#1e3a8a", 0.5, dash=[1.2, 0.8]),
                       marker_line(bar, placement="firstvertex"),
                       marker_line(bar, placement="lastvertex"))


@entry(IRR, MARKER, "Pump station", ["pump", "station", "lift irrigation"],
       "Navy roundel with an impeller.")
def irr_pump():
    return marker_symbol(svg_marker(icons.PUMP, 5.5, "#1e3a8a", "#ffffff", 0.4))


@entry(IRR, MARKER, "Gate / regulator", ["gate", "regulator", "sluice", "control structure"],
       "Rounded badge with sluice-gate bars.")
def irr_gate():
    return marker_symbol(svg_marker(icons.GATE, 5.5, "#0369a1", "#ffffff", 0.4))


@entry(IRR, MARKER, "Weir / headworks", ["weir", "headworks", "intake", "diversion"],
       "Badge with crest and overflowing water.")
def irr_weir():
    return marker_symbol(svg_marker(icons.WEIR, 5.5, "#0e7490", "#ffffff", 0.4))


@entry(IRR, FILL, "Command area", ["command area", "irrigation scheme", "service area"],
       "Light aqua tint band with a dashed scheme boundary.")
def irr_command():
    return fill_symbol(line("#06b6d4", 4.0, alpha=80, cap="flat", join="miter", inside=True),
                       simple_fill("#000000", "#0e7490", 0.5, style="no", alpha=0,
                                   outline_style="dash"))


@entry(IRR, FILL, "Irrigated field", ["irrigated", "field", "farmland", "wet season"],
       "Fresh green field with fine blue irrigation lines.")
def irr_field():
    return fill_symbol(simple_fill("#dff2d0", "#5d9c45", 0.25),
                       hatch("#5aa9e6", 0, 1.8, 0.18))


@entry(IRR, FILL, "Pond / farm reservoir", ["pond", "reservoir", "tank", "storage"],
       "Clear blue pond with a firm earth-bank outline.")
def irr_pond():
    return fill_symbol(simple_fill("#9fd3f3", "#000000", 0, outline_style="no"),
                       shapeburst("#3d8fd1", distance=1.5, blur=1),
                       line("#8b6b43", 0.8, offset=-0.4, join="round"))


@entry(IRR, RAMP, "Soil moisture", ["soil moisture", "smap", "wetness", "drought"],
       "Dry brown → beige → light teal → deep blue.")
def ramp_soil_moisture():
    return ramp(["#8c510a", "#d8b365", "#f6e8c3", "#c7eae5", "#5ab4ac", "#01665e", "#08306b"])


@entry(IRR, RAMP, "Evapotranspiration (ET)", ["evapotranspiration", "et", "water use"],
       "Low (sand) to high (deep green-blue) water use.")
def ramp_et():
    return ramp(["#f7f4e8", "#e6d79a", "#a8cf75", "#4fae6c", "#1c7f7a", "#13416b"])


# ==========================================================================
# 11. URBAN PLANNING  (zoning colours follow common planning practice:
#     residential yellows, commercial reds, industrial purples,
#     institutional blues, open space greens)
# ==========================================================================
URB = "Urban Planning"


def _zone(color, outline, hatch_color=None, angle=45, spacing=2.4):
    layers = [simple_fill(color, outline, 0.3)]
    if hatch_color:
        layers.insert(1, hatch(hatch_color, angle, spacing, 0.25))
    return fill_symbol(*layers)


@entry(URB, FILL, "Residential – low density", ["zoning", "residential", "housing", "land use"],
       "Pale yellow – detached housing.")
def urb_res_low():
    return _zone("#fff3a3", "#c9a800")


@entry(URB, FILL, "Residential – medium density", ["zoning", "residential", "housing"],
       "Yellow – townhouses and low-rise apartments.")
def urb_res_med():
    return _zone("#ffe14d", "#b38f00")


@entry(URB, FILL, "Residential – high density", ["zoning", "residential", "apartments"],
       "Amber – high-rise residential.")
def urb_res_high():
    return _zone("#ffb000", "#a86f00")


@entry(URB, FILL, "Commercial", ["zoning", "commercial", "retail", "business"],
       "Red – shops, offices and services.")
def urb_commercial():
    return _zone("#f26b6b", "#a61b1b")


@entry(URB, FILL, "Mixed use", ["zoning", "mixed use", "commercial", "residential"],
       "Yellow with red diagonal stripes – residential over commercial.")
def urb_mixed():
    return _zone("#ffe14d", "#a61b1b", "#e23d3d", 45, 2.4)


@entry(URB, FILL, "Industrial", ["zoning", "industrial", "factory", "warehouse"],
       "Purple – manufacturing and logistics.")
def urb_industrial():
    return _zone("#c7a6e8", "#5b2a86")


@entry(URB, FILL, "Institutional / public", ["zoning", "institutional", "public",
                                              "government", "school"],
       "Blue – government, schools, hospitals.")
def urb_institutional():
    return _zone("#9cc3f0", "#1d4f91")


@entry(URB, FILL, "Parks & open space", ["zoning", "park", "open space", "recreation", "green"],
       "Green – parks, plazas and sports grounds.")
def urb_park():
    return _zone("#a8dc8f", "#3b7a2a")


@entry(URB, FILL, "Conservation / green belt", ["zoning", "conservation", "green belt",
                                                 "protected"],
       "Dark green cross-hatch on light green.")
def urb_conservation():
    return fill_symbol(simple_fill("#d6efcb", "#21612f", 0.3),
                       hatch("#3f8a4f", 45, 2.0, 0.22), hatch("#3f8a4f", -45, 2.0, 0.22))


@entry(URB, FILL, "Transport & utilities", ["zoning", "transport", "utilities", "infrastructure"],
       "Neutral grey – roads, depots, utility land.")
def urb_transport():
    return _zone("#d1d5db", "#6b7280")


@entry(URB, FILL, "Agricultural reserve", ["zoning", "agriculture", "rural", "farmland"],
       "Light olive with horizontal furrows.")
def urb_agri():
    return _zone("#e9efc4", "#7c8a2a", "#b7c46a", 0, 2.2)


@entry(URB, FILL, "Vacant / undeveloped", ["zoning", "vacant", "undeveloped", "brownfield"],
       "Off-white with sparse grey stipple.")
def urb_vacant():
    dot = marker_symbol(simple_marker("circle", 0.5, "#9ca3af", "#9ca3af", 0, no_outline=True))
    return fill_symbol(simple_fill("#f7f7f4", "#9ca3af", 0.3),
                       point_pattern(dot, 2.4, 2.4))


@entry(URB, FILL, "Proposed development", ["proposed", "planned", "future", "development"],
       "Transparent with a dashed orange outline and soft tint band.")
def urb_proposed():
    return fill_symbol(line("#f97316", 3.0, alpha=60, cap="flat", join="miter", inside=True),
                       line("#c2410c", 0.5, dash=[2, 1.2]))


@entry(URB, LINE, "Urban growth boundary", ["growth boundary", "ugb", "urban edge", "planning"],
       "Thick dark red dash-dot line with an inner halo.")
def urb_ugb():
    return line_symbol(line("#fecaca", 2.2, alpha=200),
                       line("#991b1b", 0.6, dash=[4, 1.2, 0.8, 1.2]))


@entry(URB, LINE, "Proposed road", ["proposed", "road", "planned", "alignment"],
       "Dashed double-line casing for a planned road alignment.")
def urb_proposed_road():
    return line_symbol(line("#6b7280", 1.4, dash=[2.5, 1.2], cap="flat"),
                       line("#ffffff", 1.0, cap="flat"))


@entry(URB, LINE, "Bus / BRT route", ["transit", "bus", "brt", "route"],
       "Bold transit-red route with a white centre stripe.")
def urb_bus_route():
    return line_symbol(line("#dc2626", 1.4), line("#ffffff", 0.25, dash=[1.6, 1.2]))


@entry(URB, MARKER, "Bus stop", ["bus", "transit", "stop"],
       "Rounded badge with a bus front.")
def urb_bus_stop():
    return marker_symbol(svg_marker(icons.BUS, 5, "#dc2626", "#ffffff", 0.4))


@entry(URB, MARKER, "Rail / metro station", ["rail", "metro", "station", "transit"],
       "Circular badge with a train front.")
def urb_station():
    return marker_symbol(svg_marker(icons.TRAIN, 5.5, "#1d4ed8", "#ffffff", 0.4))


@entry(URB, MARKER, "Parking", ["parking", "car park"],
       "Blue square P badge.")
def urb_parking():
    return marker_symbol(svg_marker(icons.PARKING, 5, "#2563eb", "#ffffff", 0.4))


# ==========================================================================
# 12. LAND PARCELS / CADASTRE
# ==========================================================================
CAD = "Land Parcels & Cadastre"


@entry(CAD, FILL, "Parcel boundary", ["parcel", "cadastre", "lot", "property"],
       "Hollow parcel with a crisp thin black boundary – the cadastral classic.")
def cad_parcel():
    return fill_symbol(simple_fill("#000000", "#1f2937", 0.25, style="no", alpha=0))


@entry(CAD, FILL, "Parcel – soft fill", ["parcel", "cadastre", "lot"],
       "Warm off-white fill with a thin brown boundary – good under labels.")
def cad_parcel_fill():
    return fill_symbol(simple_fill("#fbf6ec", "#8b5e34", 0.25))


@entry(CAD, FILL, "Subject parcel (highlight)", ["parcel", "subject", "highlight", "selected"],
       "Bold red outline with an inner red tint band to spotlight one parcel.")
def cad_subject():
    return fill_symbol(line("#ef4444", 3.0, alpha=90, cap="flat", join="miter", inside=True),
                       simple_fill("#000000", "#b91c1c", 0.7, style="no", alpha=0))


@entry(CAD, FILL, "State / public land", ["state land", "public land", "government", "parcel"],
       "Light green fill with fine diagonal hatch.")
def cad_state():
    return fill_symbol(simple_fill("#e3f4dc", "#3f7d3a", 0.3),
                       hatch("#86c27a", 45, 2.5, 0.18))


@entry(CAD, FILL, "Private land", ["private", "parcel", "ownership"],
       "Pale beige fill with brown boundary.")
def cad_private():
    return fill_symbol(simple_fill("#f6ecd9", "#8b5e34", 0.3))


@entry(CAD, FILL, "Communal / indigenous land", ["communal", "indigenous", "community",
                                                  "collective"],
       "Light violet with staggered dots.")
def cad_communal():
    dot = marker_symbol(simple_marker("circle", 0.7, "#7c3aed", "#7c3aed", 0, no_outline=True))
    return fill_symbol(simple_fill("#efe7fb", "#6d28d9", 0.3),
                       point_pattern(dot, 3.0, 3.0))


@entry(CAD, FILL, "Easement / right of way", ["easement", "right of way", "row", "servitude"],
       "Transparent with orange diagonal hatch and a dashed edge.")
def cad_easement():
    return fill_symbol(hatch("#f59e0b", 45, 1.4, 0.22),
                       simple_fill("#000000", "#b45309", 0.35, style="no", alpha=0,
                                   outline_style="dash"))


@entry(CAD, FILL, "Disputed parcel", ["disputed", "conflict", "claim", "parcel"],
       "Red cross-hatch over a light tint – boundary or ownership dispute.")
def cad_disputed():
    return fill_symbol(simple_fill("#fde2e2", "#dc2626", 0.4, outline_style="dash"),
                       hatch("#f87171", 45, 2.0, 0.2), hatch("#f87171", -45, 2.0, 0.2))


@entry(CAD, LINE, "Block / section boundary", ["block", "section", "cadastre", "boundary"],
       "Heavier dark line for block or section limits.")
def cad_block():
    return line_symbol(line("#111827", 0.6))


@entry(CAD, LINE, "Building setback line", ["setback", "building line", "regulation"],
       "Thin magenta long-dash setback line.")
def cad_setback():
    return line_symbol(line("#c026d3", 0.3, dash=[3, 1.2]))


@entry(CAD, LINE, "Unsurveyed / approximate boundary", ["approximate", "unsurveyed",
                                                       "boundary", "parcel"],
       "Short black dashes for boundaries not yet surveyed.")
def cad_approx():
    return line_symbol(line("#374151", 0.3, dash=[1.2, 1.0]))


@entry(CAD, MARKER, "Boundary monument / pillar", ["monument", "pillar", "corner",
                                                   "boundary marker", "survey"],
       "Circle with cross – found survey monument.")
def cad_monument():
    return marker_symbol(simple_marker("circle", 2.4, "#ffffff", "#111111", 0.35),
                         simple_marker("cross", 2.4, "#111111", "#111111", 0.3))


@entry(CAD, MARKER, "Parcel corner (computed)", ["corner", "vertex", "computed", "survey"],
       "Small hollow square for computed corners.")
def cad_corner():
    return marker_symbol(simple_marker("square", 1.6, "#ffffff", "#111111", 0.3))


# ==========================================================================
# 13. MILITARY & TACTICAL  (simplified, APP-6 / MIL-STD-2525 *style*;
#     not a certified implementation of either standard)
# ==========================================================================
MIL = "Military & Tactical"

_MIL_NOTE = " Simplified APP-6 style – not a certified symbol set."


def _mil_marker(svg, fill, size=7.0):
    return marker_symbol(svg_marker(svg, size, fill, "#000000", 0))


@entry(MIL, MARKER, "Friendly – infantry", ["military", "friendly", "infantry", "app-6", "unit"],
       "Blue rectangle frame with crossed lines." + _MIL_NOTE)
def mil_friend_inf():
    return _mil_marker(icons.MIL_FRIEND_INF, "#80e0ff")


@entry(MIL, MARKER, "Friendly – armour", ["military", "friendly", "armour", "armor", "app-6"],
       "Blue rectangle frame with track oval." + _MIL_NOTE)
def mil_friend_armor():
    return _mil_marker(icons.MIL_FRIEND_ARMOR, "#80e0ff")


@entry(MIL, MARKER, "Friendly – artillery", ["military", "friendly", "artillery", "app-6"],
       "Blue rectangle frame with a solid dot." + _MIL_NOTE)
def mil_friend_arty():
    return _mil_marker(icons.MIL_FRIEND_ARTY, "#80e0ff")


@entry(MIL, MARKER, "Friendly – reconnaissance", ["military", "friendly", "recon",
                                                  "cavalry", "app-6"],
       "Blue rectangle frame with a single diagonal." + _MIL_NOTE)
def mil_friend_recon():
    return _mil_marker(icons.MIL_FRIEND_RECON, "#80e0ff")


@entry(MIL, MARKER, "Friendly – medical", ["military", "friendly", "medical", "app-6"],
       "Blue rectangle frame with a cross." + _MIL_NOTE)
def mil_friend_med():
    return _mil_marker(icons.MIL_FRIEND_MED, "#80e0ff")


@entry(MIL, MARKER, "Friendly – engineer", ["military", "friendly", "engineer", "app-6"],
       "Blue rectangle frame with the engineer bracket." + _MIL_NOTE)
def mil_friend_eng():
    return _mil_marker(icons.MIL_FRIEND_ENG, "#80e0ff")


@entry(MIL, MARKER, "Hostile – infantry", ["military", "hostile", "enemy", "infantry", "app-6"],
       "Red diamond frame with crossed lines." + _MIL_NOTE)
def mil_hostile_inf():
    return _mil_marker(icons.MIL_HOSTILE_INF, "#ff8080")


@entry(MIL, MARKER, "Neutral unit", ["military", "neutral", "app-6"],
       "Green square frame." + _MIL_NOTE)
def mil_neutral():
    return _mil_marker(icons.MIL_NEUTRAL, "#aaffaa", 6.0)


@entry(MIL, MARKER, "Unknown contact", ["military", "unknown", "contact", "app-6"],
       "Yellow quatrefoil frame." + _MIL_NOTE)
def mil_unknown():
    return _mil_marker(icons.MIL_UNKNOWN, "#ffff80", 6.5)


@entry(MIL, MARKER, "Observation post", ["military", "observation post", "op", "lookout"],
       "Triangle with centre dot.")
def mil_op():
    return marker_symbol(svg_marker(icons.OBS_POST, 4.5, "#ffffff"))


@entry(MIL, LINE, "Phase line", ["military", "phase line", "control measure"],
       "Solid black control line.")
def mil_phase():
    return line_symbol(line("#000000", 0.6))


@entry(MIL, LINE, "Unit boundary", ["military", "boundary", "sector"],
       "Black line with spaced bar pairs.")
def mil_boundary():
    bar = marker_symbol(simple_marker("line", 2.6, "#000000", "#000000", 0.45))
    return line_symbol(line("#000000", 0.5),
                       marker_line(bar, interval=14, offset_along=6),
                       marker_line(bar, interval=14, offset_along=7.2))


@entry(MIL, LINE, "Forward line of own troops (FLOT)", ["military", "flot", "front line",
                                                         "line of contact"],
       "Blue line with semicircles facing the enemy side.")
def mil_flot():
    semi = marker_symbol(simple_marker("semi_circle", 2.2, "#1d4ed8", "#1d4ed8", 0))
    return line_symbol(line("#1d4ed8", 0.6),
                       marker_line(semi, interval=3.2, offset=0.0))


@entry(MIL, LINE, "Axis of advance", ["military", "axis of advance", "main attack", "arrow"],
       "Broad hollow arrow showing direction of movement.")
def mil_axis():
    return line_symbol(arrow_line("#ffffff", 3.0, 5.5, "#000000", curved=True))


@entry(MIL, LINE, "Planned route (dashed)", ["military", "route", "planned", "movement"],
       "Black dashed route – planned or anticipated.")
def mil_route_planned():
    return line_symbol(line("#000000", 0.5, dash=[2.4, 1.4]))


@entry(MIL, LINE, "Obstacle – wire", ["military", "obstacle", "wire", "barrier"],
       "Line with repeated X markers.")
def mil_wire():
    x = marker_symbol(simple_marker("cross2", 1.8, "#000000", "#000000", 0.3))
    return line_symbol(line("#000000", 0.35), marker_line(x, interval=3.0, offset_along=1.5))


@entry(MIL, LINE, "Anti-tank ditch", ["military", "obstacle", "ditch", "barrier"],
       "Line with small triangles on one side.")
def mil_ditch():
    tri = marker_symbol(simple_marker("triangle", 1.4, "#000000", "#000000", 0))
    return line_symbol(line("#000000", 0.45), marker_line(tri, interval=2.4, offset=-0.7))


@entry(MIL, FILL, "Area of operations", ["military", "area of operations", "ao", "sector"],
       "Black boundary with a pale blue inner band.")
def mil_ao():
    return fill_symbol(line("#60a5fa", 3.0, alpha=70, cap="flat", join="miter", inside=True),
                       simple_fill("#000000", "#000000", 0.6, style="no", alpha=0))


@entry(MIL, FILL, "Objective", ["military", "objective", "target area"],
       "Black outline with light tint – objective area.")
def mil_objective():
    return fill_symbol(simple_fill("#f3f4f6", "#000000", 0.6, alpha=120))


@entry(MIL, FILL, "Restricted / no-go area", ["military", "restricted", "no-go", "exclusion"],
       "Red hatch with a red dashed edge.")
def mil_restricted():
    return fill_symbol(hatch("#dc2626", 45, 1.8, 0.25),
                       simple_fill("#000000", "#dc2626", 0.6, style="no", alpha=0,
                                   outline_style="dash"))


@entry(MIL, FILL, "Minefield (area)", ["military", "minefield", "hazard", "obstacle"],
       "Outline with a regular grid of hollow circles.")
def mil_minefield():
    c = marker_symbol(simple_marker("circle", 1.4, "#ffffff", "#000000", 0.3))
    return fill_symbol(simple_fill("#fff7ed", "#000000", 0.5),
                       point_pattern(c, 4, 4))


# ==========================================================================
# 14. AGRICULTURE
# ==========================================================================
AGR = "Agriculture"


@entry(AGR, FILL, "Maize / corn", ["maize", "corn", "crop", "cereal"],
       "Golden field with staggered corn-cob glyphs.")
def agr_maize():
    cob = marker_symbol(svg_marker(icons.CORN_DOT, 2.8, "#d29a12"))
    return fill_symbol(simple_fill("#fbefc4", "#b48a1c", 0.25), point_pattern(cob, 3.6, 3.6))


@entry(AGR, FILL, "Cassava / root crops", ["cassava", "root crop", "tuber", "crop"],
       "Soft green with small star-shaped leaves.")
def agr_cassava():
    leaf = marker_symbol(simple_marker("star", 1.8, "#4d8f3a", "#2f6b2a", 0.1))
    return fill_symbol(simple_fill("#e6f2d5", "#5f8f3e", 0.25),
                       random_markers(leaf, density=10, seed=4))


@entry(AGR, FILL, "Sugarcane", ["sugarcane", "cane", "crop"],
       "Bright green with dense vertical stalks.")
def agr_cane():
    return fill_symbol(simple_fill("#d7efb6", "#4f8a2b", 0.25),
                       hatch("#6eae43", 90, 1.1, 0.22))


@entry(AGR, FILL, "Vegetables / horticulture", ["vegetables", "horticulture", "garden", "crop"],
       "Light green with tidy rows of small squares (beds).")
def agr_veg():
    bed = marker_symbol(simple_marker("square", 1.0, "#7fb24f", "#4f7d2a", 0.1))
    return fill_symbol(simple_fill("#eef7e1", "#6a9b3b", 0.25),
                       point_pattern(bed, 2.2, 2.6, offset_x=0))


@entry(AGR, FILL, "Orchard / fruit trees", ["orchard", "fruit", "mango", "cashew", "trees"],
       "Regular rows of round fruit-tree crowns.")
def agr_orchard():
    tree = marker_symbol(svg_marker(icons.TREE, 2.8, "#5a9f3a"))
    return fill_symbol(simple_fill("#eaf5dc", "#4d7a3a", 0.25),
                       point_pattern(tree, 4.2, 4.2, offset_x=0))


@entry(AGR, FILL, "Fallow land", ["fallow", "idle", "resting", "bare soil"],
       "Light brown with scattered soil dots.")
def agr_fallow():
    dot = marker_symbol(simple_marker("circle", 0.6, "#a47148", "#a47148", 0, no_outline=True))
    return fill_symbol(simple_fill("#f1e3cf", "#a47148", 0.25),
                       random_markers(dot, density=3.5, seed=13))


@entry(AGR, FILL, "Pasture / livestock", ["pasture", "grazing", "livestock", "rangeland"],
       "Yellow-green with sparse grass ticks.")
def agr_pasture():
    tick = marker_symbol(simple_marker("line", 1.2, "#7aa33a", "#7aa33a", 0.25, angle=-15))
    return fill_symbol(simple_fill("#f0f5cf", "#8aa83a", 0.25),
                       random_markers(tick, density=6, seed=17))


@entry(AGR, FILL, "Aquaculture / fish pond", ["aquaculture", "fish pond", "shrimp", "pond"],
       "Blue-green water with wave ripples and a bank outline.")
def agr_aqua():
    return fill_symbol(simple_fill("#b9e3ea", "#5b7b46", 0.6),
                       hatch("#5fb4c7", 0, 1.6, 0.2, alpha=180))


@entry(AGR, FILL, "Greenhouse / net house", ["greenhouse", "net house", "protected cultivation"],
       "White-grey with a fine glazing grid.")
def agr_greenhouse():
    return fill_symbol(simple_fill("#f3f6f8", "#64748b", 0.35),
                       hatch("#94a3b8", 0, 1.2, 0.12), hatch("#94a3b8", 90, 3.0, 0.12))


@entry(AGR, FILL, "Crop field – boundary only", ["field", "farm", "boundary", "plot"],
       "Transparent field with a soft brown edge – for overlaying imagery.")
def agr_field_outline():
    return fill_symbol(line("#ffffff", 1.2, alpha=150),
                       simple_fill("#000000", "#a16207", 0.45, style="no", alpha=0))


@entry(AGR, LINE, "Farm track / bund", ["farm road", "bund", "dyke", "track"],
       "Brown double-dotted line for paddy bunds and farm tracks.")
def agr_bund():
    return line_symbol(line("#c9a36a", 0.9, cap="flat"), line("#8b5e34", 0.3, dash=[0.8, 0.6]))


@entry(AGR, LINE, "Windbreak / hedgerow", ["windbreak", "hedgerow", "shelterbelt", "trees"],
       "Chain of small green tree crowns along a line.")
def agr_windbreak():
    tree = marker_symbol(simple_marker("circle", 1.5, "#4d9a44", "#2f6b2a", 0.2))
    return line_symbol(marker_line(tree, interval=1.6))


@entry(AGR, MARKER, "Farmhouse", ["farm", "farmhouse", "homestead", "household"],
       "House glyph in warm brown.")
def agr_farmhouse():
    return marker_symbol(svg_marker(icons.FARMHOUSE, 4.5, "#9a5b2e", "#ffffff", 0.3))


@entry(AGR, MARKER, "Silo / storage", ["silo", "storage", "warehouse", "granary"],
       "Silo glyph for grain storage and mills.")
def agr_silo():
    return marker_symbol(svg_marker(icons.SILO, 4.5, "#6b7280", "#ffffff", 0.3))


@entry(AGR, MARKER, "Demo plot / trial", ["demo plot", "trial", "research", "extension"],
       "Green roundel with a sprout.")
def agr_demo():
    return marker_symbol(svg_marker(icons.SPROUT, 5, "#16a34a", "#ffffff", 0.4))


@entry(AGR, MARKER, "Fish farm", ["fish", "aquaculture", "fishery"],
       "Teal fish glyph.")
def agr_fish():
    return marker_symbol(svg_marker(icons.FISH, 5, "#0e7490", "#ffffff", 0.3))


@entry(AGR, RAMP, "Crop health (NDRE / GNDVI)", ["crop health", "ndre", "gndvi", "precision"],
       "Stressed red → yellow → healthy green, tuned for in-season crop monitoring.")
def ramp_crop_health():
    return ramp(["#a50026", "#f46d43", "#fee08b", "#d9ef8b", "#66bd63", "#006837"])


@entry(AGR, RAMP, "Yield", ["yield", "harvest", "productivity", "precision"],
       "Low (pale) → high (deep purple-blue) yield map ramp.")
def ramp_yield():
    return ramp(["#fff7bc", "#fec44f", "#fe9929", "#d95f0e", "#8c2d04"])


# ==========================================================================
# 15. UTILITIES & INFRASTRUCTURE
# ==========================================================================
UTL = "Utilities & Infrastructure"


@entry(UTL, LINE, "Power line (HV)", ["power line", "transmission", "electricity", "hv"],
       "Dark line with tower ticks – high-voltage transmission.")
def utl_power_hv():
    tick = marker_symbol(simple_marker("line", 2.4, "#374151", "#374151", 0.35))
    return line_symbol(line("#374151", 0.4), marker_line(tick, interval=6, offset_along=3))


@entry(UTL, LINE, "Power line (distribution)", ["power line", "distribution", "electricity"],
       "Light grey line with small pole dots.")
def utl_power_lv():
    dot = marker_symbol(simple_marker("circle", 0.9, "#6b7280", "#6b7280", 0))
    return line_symbol(line("#9ca3af", 0.3), marker_line(dot, interval=4, offset_along=2))


@entry(UTL, LINE, "Water main", ["water main", "water supply", "pipe", "network"],
       "Blue utility line (APWA style colour).")
def utl_water():
    return line_symbol(line("#1d4ed8", 0.6))


@entry(UTL, LINE, "Sewer", ["sewer", "wastewater", "sanitary", "pipe"],
       "Green utility line with flow chevrons.")
def utl_sewer():
    chev = marker_symbol(simple_marker("arrowhead", 1.4, "#15803d", "#15803d", 0.3))
    return line_symbol(line("#15803d", 0.6), marker_line(chev, interval=8, offset_along=4))


@entry(UTL, LINE, "Gas / oil pipeline", ["gas", "oil", "pipeline", "petroleum"],
       "Yellow utility line with a dark dash overlay.")
def utl_gas():
    return line_symbol(line("#eab308", 0.9), line("#713f12", 0.2, dash=[3, 2]))


@entry(UTL, LINE, "Telecom / fibre", ["telecom", "fibre", "fiber", "communication"],
       "Orange dash-dot utility line.")
def utl_telecom():
    return line_symbol(line("#ea580c", 0.45, dash=[2.6, 0.8, 0.5, 0.8]))


@entry(UTL, MARKER, "Transmission tower", ["tower", "pylon", "transmission"],
       "Lattice pylon glyph.")
def utl_tower():
    return marker_symbol(svg_marker(icons.TOWER, 5, "#374151"))


@entry(UTL, MARKER, "Substation", ["substation", "electricity", "power"],
       "Dark badge with a lightning bolt.")
def utl_substation():
    return marker_symbol(svg_marker(icons.SUBSTATION, 5, "#ca8a04", "#ffffff", 0.4))


@entry(UTL, MARKER, "Manhole / valve", ["manhole", "valve", "node", "utility"],
       "Hollow circle with dot – network node.")
def utl_node():
    return marker_symbol(simple_marker("circle", 2.2, "#ffffff", "#111827", 0.4),
                         simple_marker("circle", 0.7, "#111827", "#111827", 0))


# ==========================================================================
# 16. WEATHER & CLIMATE
# ==========================================================================
WTH = "Weather & Climate"


@entry(WTH, LINE, "Cold front", ["weather", "front", "cold front", "synoptic"],
       "Blue line with triangles pointing in the direction of travel.")
def wth_cold():
    tri = marker_symbol(simple_marker("triangle", 2.4, "#1d4ed8", "#1d4ed8", 0))
    return line_symbol(line("#1d4ed8", 0.7), marker_line(tri, interval=6, offset=-0.9))


@entry(WTH, LINE, "Warm front", ["weather", "front", "warm front", "synoptic"],
       "Red line with semicircles.")
def wth_warm():
    semi = marker_symbol(simple_marker("semi_circle", 2.6, "#dc2626", "#dc2626", 0))
    return line_symbol(line("#dc2626", 0.7), marker_line(semi, interval=6, offset=-0.2))


@entry(WTH, LINE, "Occluded front", ["weather", "front", "occluded", "synoptic"],
       "Purple line with alternating triangles and semicircles.")
def wth_occluded():
    tri = marker_symbol(simple_marker("triangle", 2.4, "#7e22ce", "#7e22ce", 0))
    semi = marker_symbol(simple_marker("semi_circle", 2.6, "#7e22ce", "#7e22ce", 0))
    return line_symbol(line("#7e22ce", 0.7),
                       marker_line(tri, interval=10, offset=-0.9),
                       marker_line(semi, interval=10, offset=-0.2, offset_along=5))


@entry(WTH, LINE, "Stationary front", ["weather", "front", "stationary", "synoptic"],
       "Alternating red/blue with symbols on opposite sides.")
def wth_stationary():
    tri = marker_symbol(simple_marker("triangle", 2.4, "#1d4ed8", "#1d4ed8", 0))
    semi = marker_symbol(simple_marker("semi_circle", 2.6, "#dc2626", "#dc2626", 0, angle=180))
    return line_symbol(line("#dc2626", 0.7, dash=[5, 5], cap="flat"),
                       line("#1d4ed8", 0.7, dash=[5, 5], cap="flat"),
                       marker_line(tri, interval=10, offset=-0.9, offset_along=7.5),
                       marker_line(semi, interval=10, offset=0.2, offset_along=2.5))


@entry(WTH, LINE, "Isobar", ["isobar", "pressure", "isoline", "weather"],
       "Smooth thin dark-grey isoline.")
def wth_isobar():
    return line_symbol(line("#4b5563", 0.3))


@entry(WTH, LINE, "Isohyet (rainfall)", ["isohyet", "rainfall", "isoline", "climate"],
       "Thin blue isoline for rainfall contours.")
def wth_isohyet():
    return line_symbol(line("#2563eb", 0.3, dash=[3, 0.8]))


@entry(WTH, RAMP, "Temperature anomaly", ["temperature", "anomaly", "climate", "diverging"],
       "Diverging blue–white–red, centred on zero.")
def ramp_temp_anom():
    return ramp(["#053061", "#2166ac", "#92c5de", "#f7f7f7", "#f4a582", "#b2182b", "#67001f"])


@entry(WTH, RAMP, "Wind speed", ["wind", "speed", "weather"],
       "Calm pale blue → green → yellow → red → magenta gale.")
def ramp_wind():
    return ramp(["#e0f3f8", "#91cf60", "#ffffbf", "#fdae61", "#d7191c", "#9e0142"])


# ==========================================================================
# 17. ENVIRONMENT & HAZARDS
# ==========================================================================
ENV = "Environment & Hazards"


@entry(ENV, FILL, "Burned area", ["burned area", "fire scar", "wildfire", "burn"],
       "Charcoal fill with ember-orange inner glow.")
def env_burn():
    return fill_symbol(simple_fill("#3f3a36", "#7c2d12", 0.3, alpha=200),
                       shapeburst("#f97316", distance=2.0, alpha=170))


@entry(ENV, FILL, "Landslide zone", ["landslide", "mass movement", "slope failure", "hazard"],
       "Brown hatch with scattered debris triangles.")
def env_landslide():
    tri = marker_symbol(simple_marker("triangle", 1.0, "#7c4a1e", "#7c4a1e", 0))
    return fill_symbol(simple_fill("#f3e2cc", "#7c4a1e", 0.4),
                       hatch("#b07a4a", -30, 2.2, 0.2),
                       random_markers(tri, density=10, seed=2))


@entry(ENV, FILL, "Erosion area", ["erosion", "gully", "degradation", "soil"],
       "Orange-brown dense stipple.")
def env_erosion():
    dot = marker_symbol(simple_marker("circle", 0.5, "#9a3412", "#9a3412", 0, no_outline=True))
    return fill_symbol(simple_fill("#fde7d1", "#9a3412", 0.3),
                       random_markers(dot, density=1.4, seed=8))


@entry(ENV, FILL, "Contaminated site", ["contamination", "pollution", "hazardous", "site"],
       "Yellow-black warning hatch.")
def env_contam():
    return fill_symbol(simple_fill("#fde047", "#111111", 0.5),
                       hatch("#111111", 45, 2.4, 0.6))


@entry(ENV, FILL, "Deforestation (loss)", ["deforestation", "forest loss", "change"],
       "Magenta tint with fine crosshatch – classic forest-loss highlight.")
def env_defor():
    return fill_symbol(simple_fill("#f0abfc", "#a21caf", 0.35, alpha=170),
                       hatch("#a21caf", 45, 1.6, 0.15))


@entry(ENV, FILL, "Buffer zone", ["buffer", "riparian", "setback", "zone"],
       "Soft dashed teal outline with a light inner tint band.")
def env_buffer():
    return fill_symbol(line("#14b8a6", 3.0, alpha=60, cap="flat", join="miter", inside=True),
                       line("#0f766e", 0.4, dash=[1.6, 0.9]))


@entry(ENV, MARKER, "Hazard warning", ["hazard", "warning", "danger", "caution"],
       "Yellow warning triangle.")
def env_warning():
    return marker_symbol(svg_marker(icons.WARNING, 5, "#facc15", "#111111", 0.3))


@entry(ENV, MARKER, "Fire hotspot", ["fire", "hotspot", "firms", "viirs", "modis"],
       "Red roundel with flame – active fire detection.")
def env_fire():
    return marker_symbol(svg_marker(icons.FIRE, 5, "#dc2626", "#ffffff", 0.4))


@entry(ENV, MARKER, "Landslide point", ["landslide", "inventory", "hazard"],
       "Warning triangle with sliding debris.")
def env_landslide_pt():
    return marker_symbol(svg_marker(icons.LANDSLIDE, 5, "#f59e0b", "#111111", 0.3))


@entry(ENV, RAMP, "Burn severity (dNBR)", ["dnbr", "burn severity", "fire", "nbr"],
       "Green (regrowth) → yellow → orange → red → purple (high severity).")
def ramp_dnbr():
    return ramp(["#1a9850", "#a6d96a", "#ffffbf", "#fdae61", "#f46d43", "#a50026", "#54278f"])


@entry(ENV, RAMP, "Susceptibility (low–high)", ["susceptibility", "risk", "hazard", "index"],
       "Five-step green → red risk ramp for susceptibility maps.")
def ramp_suscept():
    return ramp(["#1a9641", "#a6d96a", "#ffffbf", "#fdae61", "#d7191c"])


# ==========================================================================
# 18. CLASSIC PATTERNS  (flat engineering-plot look: crisp outline, simple
#     line patterns, the same palette as classic hydraulic plot dialogs)
# ==========================================================================
CLS = "Classic Patterns"

_CLASSIC = [
    # id, name, colour, pattern, tags
    ("solid_white", "Solid – white", "#ffffff", "solid", ["white", "blank"]),
    ("solid_black", "Solid – black", "#000000", "solid", ["black"]),
    ("solid_silver", "Solid – silver", "#c0c0c0", "solid", ["grey", "gray", "silver"]),
    ("solid_red", "Solid – red", "#ff0000", "solid", ["red"]),
    ("solid_green", "Solid – green", "#008000", "solid", ["green"]),
    ("solid_lime", "Solid – lime", "#00ff00", "solid", ["lime", "green"]),
    ("solid_yellow", "Solid – yellow", "#ffff00", "solid", ["yellow"]),
    ("solid_teal", "Solid – teal", "#008080", "solid", ["teal"]),
    ("solid_cyan", "Solid – cyan", "#00c0c0", "solid", ["cyan"]),
    ("solid_navy", "Solid – navy", "#000080", "solid", ["navy", "blue"]),
    ("solid_blue", "Solid – blue", "#0000ff", "solid", ["blue"]),
    ("solid_purple", "Solid – purple", "#800080", "solid", ["purple"]),
    ("solid_magenta", "Solid – magenta", "#ff00ff", "solid", ["magenta", "pink"]),
    ("solid_orange", "Solid – orange", "#ff8040", "solid", ["orange"]),
    ("solid_brown", "Solid – brown", "#804000", "solid", ["brown"]),
    ("solid_olive", "Solid – olive", "#c09010", "solid", ["olive", "gold"]),
    ("hz_red", "Horizontal lines – red", "#ff0000", "horizontal", ["horizontal", "lines"]),
    ("hz_blue", "Horizontal lines – blue", "#0000ff", "horizontal", ["horizontal", "lines"]),
    ("vt_yellow", "Vertical lines – yellow", "#d4c400", "vertical", ["vertical", "lines"]),
    ("vt_green", "Vertical lines – green", "#008000", "vertical", ["vertical", "lines"]),
    ("bd_green", "Diagonal (\\) – green", "#008000", "bdiag", ["diagonal", "hatch"]),
    ("fd_cyan", "Diagonal (/) – cyan", "#00a0a0", "fdiag", ["diagonal", "hatch"]),
    ("fd_brown", "Diagonal (/) – brown", "#804000", "fdiag", ["diagonal", "hatch"]),
    ("grid_navy", "Grid – navy", "#000080", "grid", ["grid", "cross"]),
    ("grid_grey", "Grid – grey", "#808080", "grid", ["grid", "cross"]),
    ("xh_magenta", "Cross-hatch – magenta", "#ff00ff", "crosshatch", ["crosshatch", "hatch"]),
    ("xh_black", "Cross-hatch – black", "#000000", "crosshatch", ["crosshatch", "hatch"]),
    ("dots_black", "Dots – black", "#000000", "dots", ["dots", "stipple"]),
    ("dots_red", "Dots – red", "#ff0000", "dots", ["dots", "stipple"]),
]


def _classic_fill(color, pattern):
    if pattern == "solid":
        return fill_symbol(simple_fill(color, "#000000", 0.26))
    if pattern == "dots":
        dot = marker_symbol(simple_marker("circle", 0.6, color, color, 0, no_outline=True))
        return fill_symbol(simple_fill("#ffffff", "#000000", 0.26),
                           point_pattern(dot, 1.8, 1.8),
                           no_fill_outline(color, 0.35))
    angles = {"horizontal": [0], "vertical": [90], "bdiag": [-45], "fdiag": [45],
              "grid": [0, 90], "crosshatch": [45, -45]}[pattern]
    layers = [simple_fill("#ffffff", "#000000", 0.26)]
    layers += [hatch(color, a, 1.5, 0.26) for a in angles]
    layers.append(no_fill_outline(color, 0.4))
    return fill_symbol(*layers)


def _register_classic():
    for cid, name, color, pattern, tags in _CLASSIC:
        desc = ("Flat %s fill with a thin black outline." % name.split("– ")[1]
                if pattern == "solid" else
                "White fill with %s pattern and matching outline – engineering-plot style."
                % name.split(" –")[0].lower())
        ENTRIES.append(Entry("cls_" + cid, name, CLS, FILL,
                             ["classic", "hec-ras", "pattern", "flat"] + tags, desc,
                             (lambda c=color, p=pattern: _classic_fill(c, p)),
                             color_layer=0 if pattern == "solid" else 1))


_register_classic()

_CLASSIC_LINES = [
    ("ln_black", "Line – solid black", "#000000", None),
    ("ln_blue", "Line – solid blue", "#0000ff", None),
    ("ln_red_dash", "Line – red dashed", "#ff0000", [3, 1.5]),
    ("ln_green_dash", "Line – green long dash", "#008000", [5, 2]),
    ("ln_magenta_dot", "Line – magenta dotted", "#ff00ff", [0.6, 1.0]),
    ("ln_navy_dashdot", "Line – navy dash-dot", "#000080", [3.5, 1, 0.6, 1]),
    ("ln_brown_dashdotdot", "Line – brown dash-dot-dot", "#804000", [3.5, 1, 0.6, 1, 0.6, 1]),
    ("ln_teal", "Line – solid teal", "#008080", None),
]


def _register_classic_lines():
    for cid, name, color, dash in _CLASSIC_LINES:
        ENTRIES.append(Entry(
            "cls_" + cid, name, CLS, LINE, ["classic", "hec-ras", "line style", "flat"],
            "Clean 0.4 mm line in the classic plot style.",
            (lambda c=color, d=dash: line_symbol(line(c, 0.4, dash=d, cap="flat")))))
    for cid, name, shape, color in [
            ("mk_square", "Marker – black square", "square", "#000000"),
            ("mk_diamond", "Marker – red diamond", "diamond", "#ff0000"),
            ("mk_circle", "Marker – blue circle", "circle", "#0000ff"),
            ("mk_triangle", "Marker – green triangle", "triangle", "#008000"),
            ("mk_cross", "Marker – black cross", "cross", "#000000"),
            ("mk_star", "Marker – orange star", "star", "#ff8040")]:
        ENTRIES.append(Entry(
            "cls_" + cid, name, CLS, MARKER, ["classic", "hec-ras", "marker", "flat"],
            "Simple flat marker with a thin black outline.",
            (lambda s=shape, c=color: marker_symbol(
                simple_marker(s, 2.6, c, "#000000" if s != "cross" else c,
                              0.2 if s != "cross" else 0.4)))))


_register_classic_lines()


# ==========================================================================
# top-ups so every field has a well-stocked set
# ==========================================================================

# ---- Water & Hydrology -----------------------------------------------------
@entry(WAT, LINE, "Coastline", ["coast", "shoreline", "sea", "water"],
       "Navy coastline with a soft blue halo on the water side.")
def wat_coastline():
    return line_symbol(line("#bfe0f5", 1.6, alpha=200), line("#1e3a8a", 0.35))


@entry(WAT, LINE, "Dam / embankment", ["dam", "embankment", "barrage", "hydropower"],
       "Thick dark-grey crest with hachures on the downstream side.")
def wat_dam():
    return line_symbol(line("#374151", 1.0, cap="flat"),
                       hash_line("#374151", 0.3, 1.4, 1.0, offset=-0.9))


@entry(WAT, LINE, "Rapids / cascade", ["rapids", "cascade", "waterfall", "river"],
       "River line crossed by short white-blue bars.")
def wat_rapids():
    return line_symbol(line("#3b82c4", 0.8),
                       hash_line("#1e3a8a", 0.3, 1.8, 1.2))


@entry(WAT, LINE, "Watershed divide", ["watershed", "catchment", "basin", "divide"],
       "Brown dash-dot-dot catchment divide with a light halo.")
def wat_divide():
    return line_symbol(line("#f5e6d3", 1.6, alpha=200),
                       line("#7c4a1e", 0.45, dash=[4, 1, 0.6, 1, 0.6, 1]))


@entry(WAT, FILL, "River polygon", ["river", "water", "channel", "polygon"],
       "Flat river blue with a crisp darker bank line.")
def wat_river_poly():
    return fill_symbol(simple_fill("#9ccbee", "#2f6db5", 0.3))


@entry(WAT, FILL, "Catchment / sub-basin", ["catchment", "watershed", "basin", "sub-basin"],
       "Transparent area with a brown inner tint band – overlays well on DEMs.")
def wat_catchment():
    return fill_symbol(line("#b45309", 3.0, alpha=60, cap="flat", join="miter", inside=True),
                       simple_fill("#000000", "#7c4a1e", 0.45, style="no", alpha=0))


@entry(WAT, FILL, "Tidal flat / mudflat", ["tidal flat", "mudflat", "intertidal", "coast"],
       "Grey-brown with fine horizontal water lines.")
def wat_tidal():
    return fill_symbol(simple_fill("#e6e0d4", "#8a8170", 0.25),
                       hatch("#7fa9c9", 0, 1.2, 0.15))


@entry(WAT, FILL, "Glacier / permanent ice", ["glacier", "ice", "snow", "permanent"],
       "Ice white-blue with blue contour-like inner lines.")
def wat_glacier():
    return fill_symbol(simple_fill("#eef7ff", "#4a90c2", 0.3),
                       line("#9cc7e6", 0.25, offset=1.2), line("#9cc7e6", 0.25, offset=2.4))


@entry(WAT, FILL, "Salt pan / dry lake", ["salt pan", "playa", "dry lake", "ephemeral"],
       "Pale lilac with blue dots – seasonal or dry lake.")
def wat_saltpan():
    dot = marker_symbol(simple_marker("circle", 0.6, "#5b8fc7", "#5b8fc7", 0, no_outline=True))
    return fill_symbol(simple_fill("#f3eef8", "#5b8fc7", 0.3, outline_style="dash"),
                       point_pattern(dot, 2.4, 2.4))


@entry(WAT, MARKER, "Spring", ["spring", "source", "water point"],
       "Blue round badge with a drop above water.")
def wat_spring():
    return marker_symbol(svg_marker(icons.SPRING, 5, "#0284c7", "#ffffff", 0.4))


@entry(WAT, MARKER, "Waterfall", ["waterfall", "falls", "cascade"],
       "Blue round badge with falling water.")
def wat_waterfall():
    return marker_symbol(svg_marker(icons.WATERFALL, 5, "#0369a1", "#ffffff", 0.4))


@entry(WAT, RAMP, "Turbidity / sediment", ["turbidity", "sediment", "tss", "water quality"],
       "Clear blue → green → muddy brown.")
def ramp_turbidity():
    return ramp(["#0b4f8c", "#2a9bc4", "#7fd1b9", "#d9d36b", "#b07a3a", "#6b3e17"])


# ---- Transport -------------------------------------------------------------
@entry(TRA, LINE, "Trunk road", ["road", "trunk", "national road", "casing"],
       "Green trunk road with a dark green casing.", color_layer=1)
def tra_trunk():
    return line_symbol(line("#1f5f3a", 1.8), line("#4caf6a", 1.3))


@entry(TRA, LINE, "Secondary road", ["road", "secondary", "provincial road", "casing"],
       "Light orange secondary road with a thin casing.", color_layer=1)
def tra_secondary():
    return line_symbol(line("#a0784a", 1.3), line("#fcd9a6", 0.9))


@entry(TRA, LINE, "Bridge", ["bridge", "road", "overpass"],
       "Black bridge abutment lines either side of a white deck.")
def tra_bridge():
    return line_symbol(line("#111111", 1.9, cap="flat"), line("#ffffff", 1.3, cap="flat"))


@entry(TRA, LINE, "Tunnel", ["tunnel", "underpass", "road"],
       "Dashed casing with a pale core – road in tunnel.")
def tra_tunnel():
    return line_symbol(line("#6b7280", 1.4, dash=[1.5, 1.0], cap="flat"),
                       line("#f3f4f6", 0.9, cap="flat"))


@entry(TRA, LINE, "Metro / subway", ["metro", "subway", "rail", "transit"],
       "Bold transit blue line with white station-like dots.")
def tra_metro():
    dot = marker_symbol(simple_marker("circle", 1.0, "#ffffff", "#ffffff", 0))
    return line_symbol(line("#1d4ed8", 1.3), marker_line(dot, interval=10, offset_along=5))


@entry(TRA, FILL, "Runway / airfield", ["airport", "runway", "airfield", "apron"],
       "Grey paved surface with dashed centre-line.")
def tra_runway():
    return fill_symbol(simple_fill("#c7cbd1", "#6b7280", 0.3),
                       hatch("#ffffff", 0, 6, 0.3, alpha=200))


@entry(TRA, FILL, "Car park", ["parking", "car park", "lot"],
       "Light grey with parking-bay stripes.")
def tra_parking():
    return fill_symbol(simple_fill("#eceef1", "#9ca3af", 0.25),
                       hatch("#c3c7cd", 90, 1.6, 0.2))


@entry(TRA, MARKER, "Airport", ["airport", "aerodrome", "aviation"],
       "Dark round badge with an aircraft.")
def tra_airport():
    return marker_symbol(svg_marker(icons.AIRPORT, 5.5, "#334155", "#ffffff", 0.4))


@entry(TRA, MARKER, "Port / harbour", ["port", "harbour", "harbor", "anchor"],
       "Navy round badge with an anchor.")
def tra_port():
    return marker_symbol(svg_marker(icons.PORT, 5.5, "#1e3a8a", "#ffffff", 0.4))


@entry(TRA, MARKER, "Fuel station", ["fuel", "petrol", "gas station"],
       "Rounded badge with a fuel pump.")
def tra_fuel():
    return marker_symbol(svg_marker(icons.FUEL, 5, "#0f766e", "#ffffff", 0.4))


@entry(TRA, RAMP, "Traffic speed", ["traffic", "speed", "congestion"],
       "Red (jam) → amber → green (free flow).")
def ramp_traffic():
    return ramp(["#7f1d1d", "#dc2626", "#f59e0b", "#facc15", "#84cc16", "#16a34a"])


# ---- Boundaries -----------------------------------------------------------
@entry(BND, LINE, "Maritime boundary / EEZ", ["maritime", "eez", "sea boundary", "territorial"],
       "Blue long-dash maritime limit.")
def bnd_maritime():
    return line_symbol(line("#1d4ed8", 0.45, dash=[5, 1.5, 1, 1.5]))


@entry(BND, LINE, "Disputed boundary", ["disputed", "claimed", "boundary", "border"],
       "Red short-dash border over a pale halo.")
def bnd_disputed():
    return line_symbol(line("#fee2e2", 1.8, alpha=220), line("#b91c1c", 0.45, dash=[1.6, 1.0]))


@entry(BND, LINE, "Municipal / city limit", ["municipal", "city limit", "urban boundary"],
       "Dark grey dash-dot-dot city limit.")
def bnd_municipal():
    return line_symbol(line("#374151", 0.4, dash=[3, 0.8, 0.5, 0.8, 0.5, 0.8]))


@entry(BND, LINE, "Commune / village boundary", ["commune", "village", "sangkat", "boundary"],
       "Light dotted line for the finest admin level.")
def bnd_commune():
    return line_symbol(line("#9ca3af", 0.25, dash=[0.4, 0.6]))


@entry(BND, FILL, "Country – pastel", ["country", "nation", "admin", "pastel"],
       "Soft cream country fill with a crisp violet-grey border.")
def bnd_country():
    return fill_symbol(simple_fill("#fbf5e6", "#6b5b95", 0.45))


@entry(BND, FILL, "Province – pastel green", ["province", "state", "admin", "pastel"],
       "Pale green admin unit – pair with the other pastels for a political map.")
def bnd_prov_green():
    return fill_symbol(simple_fill("#dff0d8", "#6b7280", 0.3))


@entry(BND, FILL, "Province – pastel blue", ["province", "state", "admin", "pastel"],
       "Pale blue admin unit.")
def bnd_prov_blue():
    return fill_symbol(simple_fill("#dbe8f6", "#6b7280", 0.3))


@entry(BND, FILL, "Province – pastel pink", ["province", "state", "admin", "pastel"],
       "Pale pink admin unit.")
def bnd_prov_pink():
    return fill_symbol(simple_fill("#f8e1e7", "#6b7280", 0.3))


@entry(BND, FILL, "Province – pastel yellow", ["province", "state", "admin", "pastel"],
       "Pale yellow admin unit.")
def bnd_prov_yellow():
    return fill_symbol(simple_fill("#fbf3c9", "#6b7280", 0.3))


# ---- Land Cover ----------------------------------------------------------
@entry(LND, FILL, "Shrubland", ["shrub", "scrub", "bush", "vegetation"],
       "Olive with small scattered bush dots.")
def lnd_shrub():
    bush = marker_symbol(simple_marker("circle", 1.0, "#8aa64a", "#5f7a2a", 0.15))
    return fill_symbol(simple_fill("#eef2d6", "#8aa64a", 0.25),
                       random_markers(bush, density=7, seed=6))


@entry(LND, FILL, "Mixed forest", ["forest", "mixed", "woodland"],
       "Broadleaf crowns and conifers together.")
def lnd_mixed():
    a = marker_symbol(svg_marker(icons.TREE, 3.0, "#3f8f3a"))
    b = marker_symbol(svg_marker(icons.CONIFER, 3.0, "#1f5f3a"))
    return fill_symbol(simple_fill("#d2e6c8", "#4d7a3a", 0.25),
                       point_pattern(a, 6, 5), point_pattern(b, 6, 5, offset_x=0, offset_y=2.5))


@entry(LND, FILL, "Savanna", ["savanna", "grassland", "woodland", "open forest"],
       "Straw-yellow with sparse trees.")
def lnd_savanna():
    tree = marker_symbol(svg_marker(icons.TREE, 2.6, "#6b8f2a"))
    return fill_symbol(simple_fill("#f5ecc2", "#a89a4a", 0.25),
                       random_markers(tree, density=60, seed=12))


@entry(LND, FILL, "Bamboo", ["bamboo", "forest", "vegetation"],
       "Light green with paired vertical stems.")
def lnd_bamboo():
    return fill_symbol(simple_fill("#e3f3c9", "#5f8f2a", 0.25),
                       hatch("#7cb342", 90, 2.4, 0.25), hatch("#7cb342", 90, 2.4, 0.25, offset=0.6))


@entry(LND, FILL, "Snow / ice", ["snow", "ice", "glacier", "cryosphere"],
       "White with very light blue dots.")
def lnd_snow():
    dot = marker_symbol(simple_marker("circle", 0.5, "#9cc7e6", "#9cc7e6", 0, no_outline=True))
    return fill_symbol(simple_fill("#ffffff", "#9cc7e6", 0.3), point_pattern(dot, 2.6, 2.6))


@entry(LND, FILL, "Quarry / mine", ["quarry", "mine", "extraction", "pit"],
       "Grey with hachure ring along the pit edge.")
def lnd_quarry():
    return fill_symbol(simple_fill("#e5e1dc", "#6b625a", 0.35),
                       hash_line("#6b625a", 0.25, 1.4, 1.0, offset=0.7))


@entry(LND, FILL, "Cemetery", ["cemetery", "graveyard"],
       "Pale green with a grid of small crosses.")
def lnd_cemetery():
    x = marker_symbol(simple_marker("cross", 1.4, "#4b5563", "#4b5563", 0.25))
    return fill_symbol(simple_fill("#e8f0e3", "#6b7280", 0.25), point_pattern(x, 3, 3))


@entry(LND, FILL, "Bare soil", ["bare soil", "barren", "exposed"],
       "Tan with fine brown stipple.")
def lnd_bare():
    dot = marker_symbol(simple_marker("circle", 0.4, "#8b6b43", "#8b6b43", 0, no_outline=True))
    return fill_symbol(simple_fill("#eadcc5", "#a8875f", 0.25),
                       random_markers(dot, density=2.2, seed=31))


# ---- Terrain & Geology -------------------------------------------------
@entry(GEO, LINE, "Anticline axis", ["anticline", "fold", "axis", "geology"],
       "Fold axis with outward-pointing arrow pairs.")
def geo_anticline():
    a1 = marker_symbol(simple_marker("arrowhead", 1.8, "#111111", "#111111", 0.3, angle=-90))
    a2 = marker_symbol(simple_marker("arrowhead", 1.8, "#111111", "#111111", 0.3, angle=90))
    return line_symbol(line("#111111", 0.45),
                       marker_line(a1, placement="centralpoint", offset=1.4),
                       marker_line(a2, placement="centralpoint", offset=-1.4))


@entry(GEO, LINE, "Syncline axis", ["syncline", "fold", "axis", "geology"],
       "Fold axis with inward-pointing arrow pairs.")
def geo_syncline():
    a1 = marker_symbol(simple_marker("arrowhead", 1.8, "#111111", "#111111", 0.3, angle=90))
    a2 = marker_symbol(simple_marker("arrowhead", 1.8, "#111111", "#111111", 0.3, angle=-90))
    return line_symbol(line("#111111", 0.45),
                       marker_line(a1, placement="centralpoint", offset=1.4),
                       marker_line(a2, placement="centralpoint", offset=-1.4))


@entry(GEO, LINE, "Lineament", ["lineament", "fracture", "remote sensing", "structure"],
       "Red long-dash lineament interpreted from imagery.")
def geo_lineament():
    return line_symbol(line("#dc2626", 0.4, dash=[4, 1.5]))


@entry(GEO, FILL, "Limestone (brick)", ["limestone", "lithology", "carbonate", "geology"],
       "Classic brick pattern for limestone.")
def geo_limestone():
    tick = marker_symbol(simple_marker("line", 1.6, "#3b82c4", "#3b82c4", 0.2))
    return fill_symbol(simple_fill("#dbeafe", "#1e40af", 0.3),
                       hatch("#3b82c4", 0, 1.6, 0.2),
                       point_pattern(tick, 3.2, 1.6, offset_x=1.6))


@entry(GEO, FILL, "Sandstone (dots)", ["sandstone", "lithology", "clastic", "geology"],
       "Yellow with regular dot pattern for sandstone.")
def geo_sandstone():
    dot = marker_symbol(simple_marker("circle", 0.5, "#92400e", "#92400e", 0, no_outline=True))
    return fill_symbol(simple_fill("#fde68a", "#92400e", 0.3), point_pattern(dot, 1.6, 1.6))


@entry(GEO, FILL, "Shale (dashes)", ["shale", "mudstone", "lithology", "geology"],
       "Grey with short horizontal dashes.")
def geo_shale():
    d = marker_symbol(simple_marker("line", 1.2, "#374151", "#374151", 0.25, angle=90))
    return fill_symbol(simple_fill("#e5e7eb", "#374151", 0.3), point_pattern(d, 2.4, 1.4))


@entry(GEO, FILL, "Granite / igneous", ["granite", "igneous", "intrusive", "lithology"],
       "Pink with random small crosses.")
def geo_granite():
    x = marker_symbol(simple_marker("cross", 1.0, "#9d174d", "#9d174d", 0.2))
    return fill_symbol(simple_fill("#fbcfe8", "#9d174d", 0.3),
                       random_markers(x, density=5, seed=23))


@entry(GEO, FILL, "Alluvium", ["alluvium", "quaternary", "sediment", "floodplain"],
       "Pale yellow with open circles – unconsolidated sediment.")
def geo_alluvium():
    o = marker_symbol(simple_marker("circle", 0.9, "#ffffff", "#a16207", 0.15, alpha=0))
    return fill_symbol(simple_fill("#fef9c3", "#a16207", 0.3),
                       random_markers(o, density=4, seed=29))


@entry(GEO, MARKER, "Volcano", ["volcano", "volcanic", "crater"],
       "Volcano glyph with crater glow.")
def geo_volcano():
    return marker_symbol(svg_marker(icons.VOLCANO, 5.5, "#57534e"))


@entry(GEO, MARKER, "Cave", ["cave", "karst", "sinkhole"],
       "Dark badge with a cave entrance.")
def geo_cave():
    return marker_symbol(svg_marker(icons.CAVE, 5, "#44403c", "#ffffff", 0.4))


@entry(GEO, MARKER, "Strike & dip", ["strike", "dip", "bedding", "structural"],
       "Long strike bar with a short dip tick (rotate by attribute).")
def geo_strike():
    return marker_symbol(simple_marker("line", 4.0, "#111111", "#111111", 0.45, angle=90),
                         simple_marker("line", 1.4, "#111111", "#111111", 0.45,
                                       offset=(0, 0.7)))


@entry(GEO, RAMP, "Slope (degrees)", ["slope", "terrain", "dem", "steepness"],
       "Flat white → yellow → orange → red → dark red (steep).")
def ramp_slope():
    return ramp(["#ffffff", "#fff3b0", "#fdba74", "#f97316", "#dc2626", "#7f1d1d"])


@entry(GEO, RAMP, "Aspect (cyclic)", ["aspect", "orientation", "terrain", "circular"],
       "Cyclic N→E→S→W→N colour wheel.")
def ramp_aspect():
    return ramp(["#e41a1c", "#ff7f00", "#ffff33", "#4daf4a", "#377eb8", "#984ea3", "#e41a1c"])


# ---- Points of Interest ---------------------------------------------------
@entry(POI, MARKER, "Police", ["police", "security", "law"],
       "Navy badge with a shield.")
def poi_police():
    return marker_symbol(svg_marker(icons.POLICE, 5.5, "#1e3a8a", "#ffffff", 0.4))


@entry(POI, MARKER, "Fire station", ["fire station", "emergency", "rescue"],
       "Red badge with a flame.")
def poi_firestation():
    return marker_symbol(svg_marker(icons.FIRE_STATION, 5.5, "#b91c1c", "#ffffff", 0.4))


@entry(POI, MARKER, "Market", ["market", "shop", "shopping", "commerce"],
       "Orange badge with a shopping bag.")
def poi_market():
    return marker_symbol(svg_marker(icons.MARKET, 5.5, "#ea580c", "#ffffff", 0.4))


@entry(POI, MARKER, "Hotel", ["hotel", "accommodation", "guesthouse", "lodging"],
       "Indigo badge with a bed.")
def poi_hotel():
    return marker_symbol(svg_marker(icons.HOTEL, 5.5, "#4338ca", "#ffffff", 0.4))


@entry(POI, MARKER, "Information", ["information", "tourist info", "visitor centre"],
       "Teal round badge with an i.")
def poi_info():
    return marker_symbol(svg_marker(icons.INFO, 5, "#0f766e", "#ffffff", 0.4))


@entry(POI, MARKER, "Bank / ATM", ["bank", "atm", "finance"],
       "Slate badge with classical columns.")
def poi_bank():
    return marker_symbol(svg_marker(icons.BANK, 5.5, "#475569", "#ffffff", 0.4))


@entry(POI, MARKER, "Café / restaurant", ["cafe", "restaurant", "food", "drink"],
       "Brown round badge with a cup.")
def poi_cafe():
    return marker_symbol(svg_marker(icons.CAFE, 5, "#92400e", "#ffffff", 0.4))


@entry(POI, MARKER, "Museum / heritage", ["museum", "heritage", "monument", "culture"],
       "Purple badge with a classical building.")
def poi_museum():
    return marker_symbol(svg_marker(icons.MUSEUM, 5.5, "#7e22ce", "#ffffff", 0.4))


# ---- Dark & Neon ----------------------------------------------------------
@entry(NEO, LINE, "Neon green", ["neon", "glow", "dark", "green"],
       "Electric green glowing line.")
def neo_green():
    return line_symbol(with_glow(line("#bbf7d0", 0.5), "#22c55e", spread=1.8, blur=3))


@entry(NEO, LINE, "Night road casing", ["dark", "road", "night", "basemap"],
       "Muted slate road for dark basemaps.")
def neo_road():
    return line_symbol(line("#0f172a", 1.6), line("#334155", 1.1))


@entry(NEO, FILL, "Dark water", ["dark", "water", "night", "basemap"],
       "Deep navy water fill for dark basemaps.")
def neo_water():
    return fill_symbol(simple_fill("#0b1d33", "#1e3a5f", 0.3))


@entry(NEO, FILL, "Dark land", ["dark", "land", "night", "basemap", "background"],
       "Charcoal land fill with a subtle edge.")
def neo_land():
    return fill_symbol(simple_fill("#1f2430", "#2f3645", 0.3))


@entry(NEO, MARKER, "Glow point – lime", ["glow", "neon", "dark", "lime"],
       "Lime point with soft halo.")
def neo_pt_lime():
    return marker_symbol(with_glow(simple_marker("circle", 1.8, "#ecfccb", "#84cc16", 0.3),
                                   "#84cc16", spread=2.5, blur=4))


# ---- Remote Sensing -------------------------------------------------------
@entry(RS, RAMP, "EVI / SAVI", ["evi", "savi", "vegetation index"],
       "Bare tan → light green → deep green.")
def ramp_evi():
    return ramp(["#a6611a", "#e9d8a6", "#c7e9b4", "#7fcdbb", "#41ab5d", "#006d2c", "#00441b"])


@entry(RS, RAMP, "NBR (burn ratio)", ["nbr", "burn", "fire", "index"],
       "Burned dark red → orange → green (healthy).")
def ramp_nbr():
    return ramp(["#67001f", "#d6604d", "#fddbc7", "#d9f0d3", "#5aae61", "#00441b"])


@entry(RS, RAMP, "NDBI / built-up index", ["ndbi", "urban", "built-up", "index"],
       "Low (green) → grey → high (deep red-violet) built-up.")
def ramp_ndbi():
    return ramp(["#1b7837", "#a6dba0", "#f0f0f0", "#c2a5cf", "#9970ab", "#40004b"])


@entry(RS, RAMP, "NDSI / snow index", ["ndsi", "snow", "ice", "index"],
       "Land brown → white snow → ice blue.")
def ramp_ndsi():
    return ramp(["#8c6d31", "#d9c89e", "#ffffff", "#c6e2f5", "#6baed6"])


@entry(RS, RAMP, "SAR backscatter (dB)", ["sar", "radar", "backscatter", "sentinel-1"],
       "Black → grey → white for radar intensity.")
def ramp_sar():
    return ramp(["#000000", "#4d4d4d", "#9a9a9a", "#d9d9d9", "#ffffff"])


@entry(RS, RAMP, "Chlorophyll-a", ["chlorophyll", "ocean colour", "algae", "water quality"],
       "Oligotrophic blue → green → yellow-brown bloom.")
def ramp_chl():
    return ramp(["#081d58", "#225ea8", "#41b6c4", "#7fcdbb", "#c7e9b4", "#fdd49e", "#b35806"])


@entry(RS, RAMP, "Sea surface temperature", ["sst", "sea surface temperature", "ocean"],
       "Cold purple-blue → cyan → yellow → red.")
def ramp_sst():
    return ramp(["#2c0f5e", "#2a5ba8", "#24b0c8", "#a6e3a1", "#f9e45b", "#f28b2c", "#b0171f"])


@entry(RS, RAMP, "Air quality (PM2.5)", ["pm2.5", "air quality", "aqi", "pollution"],
       "AQI-style green → yellow → orange → red → purple → maroon.")
def ramp_aqi():
    return ramp(["#00e400", "#ffff00", "#ff7e00", "#ff0000", "#8f3f97", "#7e0023"])


@entry(RS, RAMP, "Cloud probability", ["cloud", "mask", "probability", "qa"],
       "Clear transparent-blue → white (cloudy).")
def ramp_cloud():
    return ramp(["#08306b", "#4292c6", "#c6dbef", "#ffffff"])


@entry(RS, RAMP, "Elevation greyscale", ["dem", "elevation", "greyscale", "hillshade"],
       "Black low → white high, ideal under hillshade.")
def ramp_grey():
    return ramp(["#0d0d0d", "#5a5a5a", "#a8a8a8", "#f2f2f2"])


def _lulc(name, color, tags, desc):
    @entry(RS, FILL, "LULC – " + name, ["lulc", "classification", "land cover"] + tags, desc)
    def _f():
        edge = "#9ca3af" if color.lower() in ("#ffffff", "#f0f0f0") else "#ffffff"
        return fill_symbol(simple_fill(color, edge, 0.2 if edge != "#ffffff" else 0.15,
                                       outline_alpha=255 if edge != "#ffffff" else 120))
    _f.__name__ = "rs_lulc_" + name.lower().replace(" ", "_").replace("/", "_")
    ENTRIES[-1].id = _f.__name__
    return _f


for _n, _c, _t, _d in [
        ("Tree cover", "#006400", ["forest", "trees"], "Dark green class colour for trees."),
        ("Shrubland", "#ffbb22", ["shrub"], "Amber class colour for shrubland."),
        ("Grassland", "#ffff4c", ["grass"], "Yellow class colour for grassland."),
        ("Cropland", "#f096ff", ["crop", "agriculture"], "Pink class colour for cropland."),
        ("Built-up", "#fa0000", ["urban", "built-up"], "Red class colour for built-up area."),
        ("Bare / sparse", "#b4b4b4", ["bare", "sparse"], "Grey class colour for bare land."),
        ("Water", "#0064c8", ["water"], "Blue class colour for open water."),
        ("Wetland", "#0096a0", ["wetland"], "Teal class colour for herbaceous wetland."),
        ("Mangroves", "#00cf75", ["mangrove"], "Bright green class colour for mangroves."),
        ("Snow / ice", "#f0f0f0", ["snow", "ice"], "Near-white class colour for snow and ice."),
        ("Cloud", "#ffffff", ["cloud", "mask"], "White with a grey edge for cloud masks."),
        ("Shadow", "#3c3c3c", ["shadow", "mask"], "Dark grey for cloud/terrain shadow."),
]:
    _lulc(_n, _c, _t, _d)


# ---- Utilities -----------------------------------------------------------
@entry(UTL, LINE, "Storm drain", ["storm water", "drainage", "pipe"],
       "Teal dashed storm-water pipe.")
def utl_storm():
    return line_symbol(line("#0e7490", 0.55, dash=[3, 1]))


@entry(UTL, LINE, "Irrigation pipe (utility)", ["irrigation", "reclaimed", "pipe"],
       "Purple line (reclaimed / non-potable water convention).")
def utl_reclaimed():
    return line_symbol(line("#7e22ce", 0.55))


@entry(UTL, FILL, "Solar farm", ["solar", "pv", "renewable", "energy"],
       "Dark blue panel grid.")
def utl_solar():
    return fill_symbol(simple_fill("#1e3a8a", "#0f172a", 0.35),
                       hatch("#93c5fd", 0, 1.4, 0.15), hatch("#93c5fd", 90, 2.8, 0.15))


@entry(UTL, FILL, "Utility corridor", ["corridor", "right of way", "utility", "wayleave"],
       "Yellow hatched corridor with dashed edge.")
def utl_corridor():
    return fill_symbol(hatch("#eab308", -45, 1.6, 0.22),
                       simple_fill("#000000", "#a16207", 0.35, style="no", alpha=0,
                                   outline_style="dash"))


@entry(UTL, FILL, "Treatment plant", ["treatment plant", "wastewater", "water works"],
       "Light teal fill with circle tanks pattern.")
def utl_plant():
    tank = marker_symbol(simple_marker("circle", 2.0, "#a5f3fc", "#0e7490", 0.3))
    return fill_symbol(simple_fill("#ecfeff", "#0e7490", 0.4), point_pattern(tank, 4, 4))


@entry(UTL, MARKER, "Wind turbine", ["wind turbine", "renewable", "energy"],
       "Three-blade turbine glyph.")
def utl_wind():
    return marker_symbol(svg_marker(icons.WIND_TURBINE, 5.5, "#475569"))


@entry(UTL, MARKER, "Cell tower", ["cell tower", "telecom", "antenna", "mobile"],
       "Mast with radio waves.")
def utl_cell():
    return marker_symbol(svg_marker(icons.CELL_TOWER, 5, "#475569"))


@entry(UTL, MARKER, "Water tank", ["water tank", "storage", "tower", "supply"],
       "Blue badge with an elevated tank.")
def utl_tank():
    return marker_symbol(svg_marker(icons.WATER_TANK, 5, "#0369a1", "#ffffff", 0.4))


@entry(UTL, MARKER, "Street light", ["street light", "lamp", "lighting"],
       "Lamp post glyph with lit bulb.")
def utl_light():
    return marker_symbol(svg_marker(icons.STREET_LIGHT, 4.5, "#475569"))


# ---- Weather & Climate -----------------------------------------------
@entry(WTH, LINE, "Trough", ["trough", "weather", "synoptic"],
       "Brown dashed trough axis.")
def wth_trough():
    return line_symbol(line("#92400e", 0.6, dash=[3, 1.5]))


@entry(WTH, LINE, "Squall line", ["squall", "thunderstorm", "weather"],
       "Red dash-dot-dot squall line.")
def wth_squall():
    return line_symbol(line("#dc2626", 0.6, dash=[3, 0.8, 0.6, 0.8, 0.6, 0.8]))


@entry(WTH, LINE, "Isotherm", ["isotherm", "temperature", "isoline"],
       "Thin red dashed isotherm.")
def wth_isotherm():
    return line_symbol(line("#dc2626", 0.3, dash=[2, 1]))


@entry(WTH, LINE, "Cyclone track", ["cyclone", "typhoon", "hurricane", "track"],
       "Purple track with regularly spaced position dots.")
def wth_track():
    dot = marker_symbol(simple_marker("circle", 1.4, "#ffffff", "#7e22ce", 0.35))
    return line_symbol(line("#7e22ce", 0.5), marker_line(dot, interval=6, offset_along=1))


@entry(WTH, FILL, "Rain area", ["rain", "precipitation", "weather"],
       "Light green area with diagonal rain streaks.")
def wth_rain():
    return fill_symbol(simple_fill("#d9f99d", "#4d7c0f", 0.3, alpha=150),
                       hatch("#4d7c0f", 70, 2.0, 0.2))


@entry(WTH, RAMP, "Radar reflectivity (dBZ)", ["radar", "dbz", "reflectivity", "rain"],
       "Classic radar palette: cyan → green → yellow → red → magenta.")
def ramp_dbz():
    return ramp(["#04e9e7", "#019ff4", "#02fd02", "#008e00", "#fdf802", "#fd9500",
                 "#fd0000", "#bc0000", "#f800fd"])


@entry(WTH, RAMP, "Drought index (SPI)", ["spi", "drought", "spei", "dryness"],
       "Extreme dry dark brown → normal white → extreme wet dark blue.")
def ramp_spi():
    return ramp(["#730000", "#e69800", "#ffd37f", "#ffffff", "#9ecae1", "#3182bd", "#08306b"])


@entry(WTH, RAMP, "Relative humidity", ["humidity", "moisture", "weather"],
       "Dry tan → green → deep blue humid.")
def ramp_rh():
    return ramp(["#d8b365", "#f5f5f5", "#a6dba0", "#5aae61", "#2b8cbe", "#084081"])


# ---- Environment & Hazards -----------------------------------------
@entry(ENV, FILL, "Flood-prone area", ["flood prone", "floodplain", "hazard"],
       "Light blue with horizontal hazard lines.")
def env_floodprone():
    return fill_symbol(simple_fill("#dbeafe", "#1d4ed8", 0.35, alpha=160),
                       hatch("#3b82f6", 0, 2.0, 0.22))


@entry(ENV, FILL, "Wildlife habitat", ["habitat", "wildlife", "biodiversity", "species"],
       "Green tint band with dotted edge.")
def env_habitat():
    return fill_symbol(line("#22c55e", 3.0, alpha=70, cap="flat", join="miter", inside=True),
                       line("#15803d", 0.45, dash=[0.6, 0.8]))


@entry(ENV, FILL, "Noise / impact zone", ["noise", "impact", "buffer", "contour"],
       "Orange translucent zone with a firm edge.")
def env_noise():
    return fill_symbol(simple_fill("#fed7aa", "#c2410c", 0.4, alpha=130))


@entry(ENV, MARKER, "Waste / dump site", ["waste", "dump", "landfill", "garbage"],
       "Brown badge with a bin.")
def env_waste():
    return marker_symbol(svg_marker(icons.WASTE, 5, "#78350f", "#ffffff", 0.4))


# ---- Land Parcels -------------------------------------------------------
@entry(CAD, FILL, "Road reserve", ["road reserve", "right of way", "public"],
       "Light grey parcel for road reserves.")
def cad_road_reserve():
    return fill_symbol(simple_fill("#eceff3", "#6b7280", 0.25))


@entry(CAD, FILL, "Building footprint (cadastral)", ["building", "footprint", "structure"],
       "Grey building with black outline, cadastral style.")
def cad_building():
    return fill_symbol(simple_fill("#bfbfbf", "#000000", 0.3))


@entry(CAD, FILL, "Parcel – registered", ["registered", "titled", "parcel"],
       "Pale green parcel for titled / registered land.")
def cad_registered():
    return fill_symbol(simple_fill("#e6f4ea", "#2f6f3e", 0.3))


@entry(CAD, FILL, "Parcel – pending registration", ["pending", "unregistered", "parcel"],
       "Pale amber parcel with dashed boundary.")
def cad_pending():
    return fill_symbol(simple_fill("#fff4d6", "#b45309", 0.35, outline_style="dash"))


# ==========================================================================
# v1.2 – gap filling against field reference standards
# (HEC-RAS geometry layers, USGS topographic symbols, FAO irrigation
#  structures, CORINE land cover, APWA utility colours, FGDC geologic
#  symbolization, NOAA/WPC surface analysis, APP-6 style control measures)
# ==========================================================================


def _struct(shape, color, size=3.0, inner=None, inner_color="#ffffff", angle=0):
    """Clean engineering-style structure marker (white ring + coloured shape)."""
    layers = [simple_marker(shape, size, color, "#111111", 0.25, angle=angle)]
    if inner:
        layers.append(simple_marker(inner, size * 0.5, inner_color, inner_color, 0.35,
                                    angle=angle))
    return marker_symbol(*layers)


def _letter(ch, color, ring="#111111", size=4.0, text="#111111", fill="#ffffff"):
    """Circle with a bold letter (font marker – uses the system sans font)."""
    from qgis.core import QgsFontMarkerSymbolLayer
    from .builders import enc
    fm = QgsFontMarkerSymbolLayer.create({
        "font": "Arial", "font_style": "Bold", "chr": ch,
        "size": str(size * 0.62), "size_unit": "MM", "color": enc(text),
        "outline_width": "0", "joinstyle": "round"})
    return marker_symbol(simple_marker("circle", size, fill, ring, 0.35), fm)


# ---- Hydraulics & Flood (HEC-RAS geometry layers) -------------------------
@entry(HYD, LINE, "Flow path – channel", ["hec-ras", "flow path", "channel", "ras mapper"],
       "Blue dashed main-channel flow path.")
def hyd_fp_channel():
    return line_symbol(line("#1d4ed8", 0.35, dash=[3, 1.2]))


@entry(HYD, LINE, "Flow path – overbank", ["hec-ras", "flow path", "overbank", "ras mapper"],
       "Red dashed left/right overbank flow path.")
def hyd_fp_overbank():
    return line_symbol(line("#dc2626", 0.35, dash=[3, 1.2]))


@entry(HYD, LINE, "River edge line", ["hec-ras", "edge line", "ras mapper"],
       "Thin black edge line of the 1D river geometry.")
def hyd_edge():
    return line_symbol(line("#111111", 0.3, dash=[1.2, 0.8]))


@entry(HYD, LINE, "Lateral structure", ["hec-ras", "lateral structure", "weir", "embankment"],
       "Brown line with short perpendicular ticks.")
def hyd_lateral():
    return line_symbol(line("#92400e", 0.6), hash_line("#92400e", 0.3, 1.6, 2.0))


@entry(HYD, LINE, "Inline structure / bridge", ["hec-ras", "inline structure", "bridge",
                                                 "culvert", "weir"],
       "Bold black line with end bars – structure crossing the channel.")
def hyd_inline():
    bar = marker_symbol(simple_marker("line", 2.4, "#111111", "#111111", 0.5))
    return line_symbol(line("#111111", 0.9, cap="flat"),
                       marker_line(bar, placement="firstvertex"),
                       marker_line(bar, placement="lastvertex"))


@entry(HYD, LINE, "SA/2D connection", ["hec-ras", "sa/2d connection", "connection", "2d"],
       "Purple line with diamonds – storage area / 2D connection.")
def hyd_sa_conn():
    d = marker_symbol(simple_marker("diamond", 1.6, "#7e22ce", "#7e22ce", 0))
    return line_symbol(line("#7e22ce", 0.5), marker_line(d, interval=5, offset_along=2.5))


@entry(HYD, LINE, "Boundary condition line", ["hec-ras", "bc line", "boundary condition", "2d"],
       "Thick orange line marking a 2D inflow/outflow boundary.")
def hyd_bc():
    return line_symbol(line("#f97316", 1.0, cap="flat"))


@entry(HYD, LINE, "Reference line", ["hec-ras", "reference line", "output"],
       "Green dash-dot reference line for flow/stage output.")
def hyd_refline():
    return line_symbol(line("#059669", 0.45, dash=[3, 1, 0.6, 1]))


@entry(HYD, LINE, "Pipe conduit", ["hec-ras", "pipe", "conduit", "storm network"],
       "Dark teal pipe with node rings.")
def hyd_pipe():
    n = marker_symbol(simple_marker("circle", 1.2, "#ffffff", "#115e59", 0.3))
    return line_symbol(line("#115e59", 0.55), marker_line(n, interval=8, offset_along=1))


@entry(HYD, LINE, "Depth contour (flood)", ["flood", "depth contour", "isoline"],
       "Thin navy isoline for flood depth contours.")
def hyd_depth_contour():
    return line_symbol(line("#1e3a8a", 0.25))


@entry(HYD, FILL, "Storage area", ["hec-ras", "storage area", "pond", "reservoir"],
       "Light blue fill with dark blue edge and diagonal hatch.")
def hyd_storage():
    return fill_symbol(simple_fill("#dbeafe", "#1e40af", 0.5), hatch("#93c5fd", 45, 2.0, 0.2))


@entry(HYD, FILL, "Refinement region", ["hec-ras", "refinement region", "mesh", "2d"],
       "Transparent with a magenta dashed outline – local mesh refinement.")
def hyd_refine():
    return fill_symbol(simple_fill("#000000", "#c026d3", 0.45, style="no", alpha=0,
                                   outline_style="dash"))


@entry(HYD, FILL, "Manning's n region", ["hec-ras", "manning", "roughness", "land cover"],
       "Olive outline with sparse dots – roughness override region.")
def hyd_manning():
    dot = marker_symbol(simple_marker("circle", 0.6, "#65a30d", "#65a30d", 0, no_outline=True))
    return fill_symbol(simple_fill("#f7fee7", "#4d7c0f", 0.4, alpha=120),
                       point_pattern(dot, 3, 3))


@entry(HYD, FILL, "2D mesh cells", ["hec-ras", "mesh", "cells", "2d"],
       "Hairline grey cell outlines, no fill.")
def hyd_mesh():
    return fill_symbol(simple_fill("#000000", "#9ca3af", 0.12, style="no", alpha=0))


@entry(HYD, FILL, "Flood hazard – medium", ["flood", "hazard", "risk"],
       "Orange tint with orange hatch.")
def hyd_hazard_med():
    return fill_symbol(simple_fill("#fed7aa", "#c2410c", 0.4, alpha=110),
                       hatch("#ea580c", 45, 2.6, 0.25))


@entry(HYD, FILL, "Flood hazard – low", ["flood", "hazard", "risk"],
       "Yellow tint with light hatch.")
def hyd_hazard_low():
    return fill_symbol(simple_fill("#fef9c3", "#ca8a04", 0.4, alpha=120),
                       hatch("#eab308", 45, 3.0, 0.2))


@entry(HYD, MARKER, "Reference point", ["hec-ras", "reference point", "output", "gauge"],
       "Green triangle reference/observation point.")
def hyd_refpoint():
    return _struct("triangle", "#059669", 2.8)


@entry(HYD, MARKER, "Pipe node / manhole", ["hec-ras", "pipe node", "manhole", "junction"],
       "Teal ring junction node.")
def hyd_node():
    return _struct("circle", "#ffffff", 2.4, "circle", "#115e59")


# ---- Water & Hydrology (USGS topo water features) ---------------------
@entry(WAT, FILL, "Intermittent lake", ["lake", "intermittent", "seasonal", "pond"],
       "Pale blue fill with dashed shoreline and horizontal water lines.")
def wat_int_lake():
    return fill_symbol(simple_fill("#e0f2fe", "#2f6db5", 0.35, outline_style="dash"),
                       hatch("#7fb6ea", 0, 1.8, 0.18))


@entry(WAT, FILL, "Wooded marsh / swamp", ["swamp", "wooded marsh", "wetland", "forest"],
       "Marsh tufts mixed with tree crowns.")
def wat_swamp():
    tuft = marker_symbol(svg_marker(icons.MARSH_TUFT, 3.0, "#2f7d6d"))
    tree = marker_symbol(svg_marker(icons.TREE, 2.6, "#3f8f3a"))
    return fill_symbol(simple_fill("#e3f3ea", "#2f7d6d", 0.25),
                       point_pattern(tuft, 6, 5), point_pattern(tree, 6, 5, offset_x=0,
                                                                offset_y=2.5))


@entry(WAT, FILL, "Coastal lagoon", ["lagoon", "coastal", "estuary"],
       "Turquoise water with fine horizontal lines.")
def wat_lagoon():
    return fill_symbol(simple_fill("#a7f3d0", "#0f766e", 0.3), hatch("#5eead4", 0, 2.0, 0.2))


@entry(WAT, FILL, "Reef / rocks awash", ["reef", "rocks", "coral", "hazard"],
       "Blue-grey with scattered small crosses.")
def wat_reef():
    x = marker_symbol(simple_marker("cross2", 1.0, "#1e3a8a", "#1e3a8a", 0.25))
    return fill_symbol(simple_fill("#dbeafe", "#1e3a8a", 0.3, outline_style="dot"),
                       random_markers(x, density=6, seed=41))


@entry(WAT, LINE, "Dry wash / ephemeral stream", ["wash", "wadi", "ephemeral", "dry stream"],
       "Brown dashed bed line.")
def wat_wash():
    return line_symbol(line("#a16207", 0.4, dash=[2, 1.2]))


@entry(WAT, LINE, "Aqueduct / flume", ["aqueduct", "flume", "elevated channel"],
       "Blue line with perpendicular support ticks.")
def wat_aqueduct():
    return line_symbol(line("#1d4ed8", 0.6), hash_line("#1d4ed8", 0.3, 1.4, 3.0))


@entry(WAT, LINE, "Depth contour (bathymetric)", ["bathymetry", "depth contour", "sounding"],
       "Light blue isobath.")
def wat_isobath():
    return line_symbol(line("#60a5fa", 0.3))


@entry(WAT, LINE, "Ditch", ["ditch", "drain", "small channel"],
       "Thin blue dash-dot ditch line.")
def wat_ditch():
    return line_symbol(line("#3b82c4", 0.3, dash=[1.6, 0.6, 0.4, 0.6]))


@entry(WAT, MARKER, "Canal lock", ["lock", "canal", "navigation"],
       "Pair of blue gate triangles across a canal.")
def wat_lock():
    return marker_symbol(simple_marker("triangle", 2.0, "#1d4ed8", "#1d4ed8", 0, angle=90,
                                       offset=(-0.9, 0)),
                         simple_marker("triangle", 2.0, "#1d4ed8", "#1d4ed8", 0, angle=-90,
                                       offset=(0.9, 0)))


@entry(WAT, MARKER, "Ford", ["ford", "crossing", "river"],
       "Pair of brown parallel bars.")
def wat_ford():
    return marker_symbol(simple_marker("line", 3.0, "#92400e", "#92400e", 0.5, offset=(0.6, 0)),
                         simple_marker("line", 3.0, "#92400e", "#92400e", 0.5,
                                       offset=(-0.6, 0)))


# ---- Irrigation & Drainage (FAO / scheme-map structures) -----------------
@entry(IRR, LINE, "Escape / wasteway", ["escape", "wasteway", "spill channel"],
       "Blue-grey dash-dot escape channel.")
def irr_escape():
    return line_symbol(line("#475569", 0.6, dash=[3, 0.8, 0.6, 0.8]))


@entry(IRR, LINE, "Aqueduct / flume (irrigation)", ["aqueduct", "flume", "crossing"],
       "Canal blue with support ticks on both sides.")
def irr_flume():
    return line_symbol(line("#1e5aa8", 1.0, cap="flat"),
                       hash_line("#1e5aa8", 0.3, 2.4, 2.4))


@entry(IRR, LINE, "Chute", ["chute", "steep channel", "drop"],
       "Canal with closely spaced cross bars.")
def irr_chute():
    return line_symbol(line("#1e5aa8", 0.8, cap="flat"), hash_line("#0b3d91", 0.25, 1.8, 0.8))


@entry(IRR, LINE, "Sub-surface drain pipe", ["subsurface drain", "tile drain", "pipe"],
       "Teal dotted pipe.")
def irr_tile():
    return line_symbol(line("#0f766e", 0.45, dash=[0.5, 0.8]))


@entry(IRR, MARKER, "Head regulator", ["head regulator", "offtake", "gate", "structure"],
       "Navy square with white bar.")
def irr_head_reg():
    return _struct("square", "#1e3a8a", 3.0, "line", angle=90)


@entry(IRR, MARKER, "Cross regulator / check", ["cross regulator", "check", "structure"],
       "Red square with white bar.")
def irr_cross_reg():
    return _struct("square", "#dc2626", 3.0, "line", angle=90)


@entry(IRR, MARKER, "Drop structure", ["drop", "fall", "structure"],
       "Blue triangle pointing downstream.")
def irr_drop():
    return _struct("triangle", "#0284c7", 3.0, angle=180)


@entry(IRR, MARKER, "Division box", ["division box", "bifurcation", "structure"],
       "Square with a white cross – splits flow.")
def irr_divbox():
    return _struct("square", "#0f766e", 3.0, "cross")


@entry(IRR, MARKER, "Turnout / offtake", ["turnout", "offtake", "outlet", "structure"],
       "Small green circle with white dot.")
def irr_turnout():
    return _struct("circle", "#16a34a", 2.6, "circle")


@entry(IRR, MARKER, "Field outlet", ["field outlet", "farm turnout", "outlet"],
       "Light green diamond.")
def irr_field_outlet():
    return _struct("diamond", "#86efac", 2.6)


@entry(IRR, MARKER, "Measuring flume / weir", ["parshall", "flume", "measuring", "gauging"],
       "Orange square with a V notch.")
def irr_measure():
    return _struct("square", "#f59e0b", 3.0, "triangle", angle=180)


@entry(IRR, MARKER, "Valve", ["valve", "pipe", "control"],
       "Bow-tie valve symbol.")
def irr_valve():
    return marker_symbol(simple_marker("triangle", 2.2, "#111111", "#111111", 0, angle=90,
                                       offset=(-0.55, 0)),
                         simple_marker("triangle", 2.2, "#111111", "#111111", 0, angle=-90,
                                       offset=(0.55, 0)))


@entry(IRR, MARKER, "Water meter", ["meter", "flow meter", "measurement"],
       "Teal round badge with a gauge needle.")
def irr_meter():
    return marker_symbol(svg_marker(icons.METER, 4.5, "#0f766e", "#ffffff", 0.4))


@entry(IRR, MARKER, "Tube well / borehole", ["tube well", "borehole", "groundwater", "well"],
       "Blue well with cross-hair.")
def irr_tubewell():
    return marker_symbol(svg_marker(icons.WELL_BORE, 4.5, "#0369a1"))


@entry(IRR, MARKER, "Observation bore / piezometer", ["piezometer", "observation bore",
                                                       "groundwater", "monitoring"],
       "Hollow blue circle with centre dot.")
def irr_piezo():
    return _struct("circle", "#ffffff", 2.6, "circle", "#0369a1")


@entry(IRR, MARKER, "Trash rack", ["trash rack", "screen", "intake"],
       "Square with three vertical bars.")
def irr_trash():
    return marker_symbol(svg_marker(icons.GATE, 4.5, "#475569", "#ffffff", 0.3))


@entry(IRR, FILL, "Center-pivot field", ["pivot", "center pivot", "sprinkler"],
       "Circle-irrigated field (use with circular polygons).")
def irr_pivot():
    return fill_symbol(simple_fill("#c9eab3", "#4d7c0f", 0.35),
                       line("#86c46a", 0.25, offset=2.0), line("#86c46a", 0.25, offset=4.0))


@entry(IRR, FILL, "Night storage reservoir", ["night storage", "reservoir", "tank"],
       "Blue with bold embankment outline.")
def irr_night_storage():
    return fill_symbol(simple_fill("#93c5fd", "#7c4a1e", 0.9))


@entry(IRR, FILL, "Drip / sprinkler area", ["drip", "sprinkler", "pressurised irrigation"],
       "Green with small blue dots in rows.")
def irr_drip():
    dot = marker_symbol(simple_marker("circle", 0.6, "#2563eb", "#2563eb", 0, no_outline=True))
    return fill_symbol(simple_fill("#e6f4d7", "#4d7c0f", 0.3),
                       point_pattern(dot, 1.6, 2.4, offset_x=0))


@entry(IRR, MARKER, "Center pivot (point)", ["pivot", "center pivot", "sprinkler"],
       "Pivot circle with arm.")
def irr_pivot_pt():
    return marker_symbol(svg_marker(icons.PIVOT, 5, "#9fd88a"))


# ---- Agriculture ---------------------------------------------------------
@entry(AGR, FILL, "Vineyard", ["vineyard", "grapes", "crop"],
       "Purple-tinted rows of small dots.")
def agr_vine():
    dot = marker_symbol(simple_marker("circle", 0.8, "#7e22ce", "#7e22ce", 0, no_outline=True))
    return fill_symbol(simple_fill("#f3e8ff", "#7e22ce", 0.25), point_pattern(dot, 1.6, 2.6,
                                                                              offset_x=0))


@entry(AGR, FILL, "Tea / coffee", ["tea", "coffee", "plantation", "crop"],
       "Dark green bushes in contour-like rows.")
def agr_tea():
    b = marker_symbol(simple_marker("circle", 1.1, "#166534", "#14532d", 0.1))
    return fill_symbol(simple_fill("#dcfce7", "#166534", 0.25), point_pattern(b, 1.8, 2.2))


@entry(AGR, FILL, "Oil palm", ["oil palm", "palm", "plantation"],
       "Grid of star-shaped palm crowns.")
def agr_palm():
    s = marker_symbol(simple_marker("star", 2.0, "#15803d", "#14532d", 0.1))
    return fill_symbol(simple_fill("#ecfccb", "#3f6212", 0.25), point_pattern(s, 3.8, 3.8))


@entry(AGR, FILL, "Rubber plantation", ["rubber", "plantation", "trees"],
       "Regular rows of small dark crowns.")
def agr_rubber():
    t = marker_symbol(svg_marker(icons.TREE, 2.2, "#2f6b2a"))
    return fill_symbol(simple_fill("#e2f0d9", "#2f6b2a", 0.25), point_pattern(t, 3.0, 3.0,
                                                                              offset_x=0))


@entry(AGR, FILL, "Shifting cultivation", ["shifting cultivation", "swidden", "slash and burn"],
       "Light brown with green and brown random patches.")
def agr_swidden():
    a = marker_symbol(simple_marker("circle", 1.2, "#a3be8c", "#a3be8c", 0, no_outline=True))
    b = marker_symbol(simple_marker("circle", 0.9, "#a16207", "#a16207", 0, no_outline=True))
    return fill_symbol(simple_fill("#f5ecd7", "#a16207", 0.25),
                       random_markers(a, density=8, seed=3), random_markers(b, density=8, seed=9))


@entry(AGR, FILL, "Complex cultivation", ["complex cultivation", "mosaic", "mixed farming"],
       "Yellow with mixed hatch directions – small mixed plots.")
def agr_complex():
    return fill_symbol(simple_fill("#fef3c7", "#a16207", 0.25),
                       hatch("#d4a72c", 0, 2.4, 0.2), hatch("#86c46a", 90, 3.6, 0.2))


@entry(AGR, MARKER, "Livestock farm", ["livestock", "cattle", "poultry", "farm"],
       "Brown square badge with white dot pattern.")
def agr_livestock():
    return _struct("square", "#92400e", 3.0, "circle")


# ---- Land Cover (CORINE level-3 gaps) -----------------------------------
@entry(LND, FILL, "Discontinuous urban", ["urban", "discontinuous", "suburban"],
       "Light red-grey with sparse building squares.")
def lnd_disc_urban():
    b = marker_symbol(simple_marker("square", 0.9, "#b45353", "#b45353", 0, no_outline=True))
    return fill_symbol(simple_fill("#f6dfdf", "#b45353", 0.25), point_pattern(b, 2.6, 2.6))


@entry(LND, FILL, "Industrial / commercial units", ["industrial", "commercial", "corine"],
       "Purple-grey flat fill.")
def lnd_industrial():
    return fill_symbol(simple_fill("#d8c6e6", "#6b4f8a", 0.25))


@entry(LND, FILL, "Construction site", ["construction", "building site"],
       "Grey with orange diagonal stripes.")
def lnd_construction():
    return fill_symbol(simple_fill("#f1f1f1", "#c2410c", 0.3), hatch("#f97316", 45, 2.4, 0.3))


@entry(LND, FILL, "Dump site / landfill", ["dump", "landfill", "waste"],
       "Brown-grey with random small triangles.")
def lnd_dump():
    t = marker_symbol(simple_marker("triangle", 0.9, "#78350f", "#78350f", 0))
    return fill_symbol(simple_fill("#e7dfd4", "#78350f", 0.3), random_markers(t, density=5,
                                                                              seed=19))


@entry(LND, FILL, "Green urban area", ["green urban", "park", "garden"],
       "Fresh green with small round trees.")
def lnd_green_urban():
    t = marker_symbol(simple_marker("circle", 1.0, "#4ade80", "#15803d", 0.15))
    return fill_symbol(simple_fill("#dcfce7", "#15803d", 0.25), random_markers(t, density=14,
                                                                               seed=5))


@entry(LND, FILL, "Sport & leisure", ["sport", "leisure", "golf", "stadium"],
       "Light green with wide mowing stripes.")
def lnd_sport():
    return fill_symbol(simple_fill("#d9f99d", "#4d7c0f", 0.25),
                       hatch("#bef264", 0, 3.0, 1.2))


@entry(LND, FILL, "Moors & heathland", ["heath", "moor", "heathland"],
       "Mauve-brown with small tufts.")
def lnd_heath():
    tick = marker_symbol(simple_marker("line", 1.0, "#9d4a6b", "#9d4a6b", 0.25, angle=15))
    return fill_symbol(simple_fill("#f3e3e9", "#9d4a6b", 0.25),
                       random_markers(tick, density=5, seed=27))


@entry(LND, FILL, "Transitional woodland-shrub", ["regrowth", "woodland", "shrub",
                                                   "transitional"],
       "Yellow-green with small shrubs and saplings.")
def lnd_transitional():
    a = marker_symbol(simple_marker("circle", 0.9, "#84cc16", "#4d7c0f", 0.1))
    t = marker_symbol(svg_marker(icons.TREE, 2.2, "#4d7c0f"))
    return fill_symbol(simple_fill("#f0f7d4", "#65a30d", 0.25),
                       random_markers(a, density=8, seed=31), random_markers(t, density=40,
                                                                             seed=33))


@entry(LND, FILL, "Sparsely vegetated", ["sparse", "sparsely vegetated", "semi-arid"],
       "Pale sand with very sparse green dots.")
def lnd_sparse():
    d = marker_symbol(simple_marker("circle", 0.7, "#65a30d", "#65a30d", 0, no_outline=True))
    return fill_symbol(simple_fill("#f5efe0", "#a8a29e", 0.25), random_markers(d, density=10,
                                                                               seed=37))


@entry(LND, FILL, "Peat bog", ["peat", "bog", "wetland"],
       "Brown-violet with horizontal dashes.")
def lnd_peat():
    d = marker_symbol(simple_marker("line", 1.4, "#5b3a29", "#5b3a29", 0.25, angle=90))
    return fill_symbol(simple_fill("#e9dfe6", "#5b3a29", 0.25), point_pattern(d, 2.6, 1.8))


@entry(LND, FILL, "Salt marsh", ["salt marsh", "coastal wetland"],
       "Light teal with blue lines and marsh tufts.")
def lnd_saltmarsh():
    tuft = marker_symbol(svg_marker(icons.MARSH_TUFT, 2.6, "#0f766e"))
    return fill_symbol(simple_fill("#e0f2f1", "#0f766e", 0.25), hatch("#93c5fd", 0, 2.4, 0.15),
                       point_pattern(tuft, 5, 4.8))


@entry(LND, FILL, "Salines / salt pans", ["salines", "salt production", "salt"],
       "White-violet grid of evaporation ponds.")
def lnd_salines():
    return fill_symbol(simple_fill("#f5f3ff", "#7c3aed", 0.3),
                       hatch("#a78bfa", 0, 2.4, 0.2), hatch("#a78bfa", 90, 2.4, 0.2))


# ---- Urban Planning --------------------------------------------------------
@entry(URB, FILL, "Heritage / historic district", ["heritage", "historic", "conservation area",
                                                   "overlay"],
       "Overlay: brown diagonal hatch with dashed brown edge.")
def urb_heritage():
    return fill_symbol(hatch("#92400e", -45, 2.2, 0.25),
                       simple_fill("#000000", "#92400e", 0.5, style="no", alpha=0,
                                   outline_style="dash"))


@entry(URB, FILL, "Flood overlay zone", ["overlay", "flood", "planning control"],
       "Overlay: blue horizontal hatch over any zoning.")
def urb_flood_overlay():
    return fill_symbol(hatch("#2563eb", 0, 2.0, 0.25),
                       simple_fill("#000000", "#1d4ed8", 0.4, style="no", alpha=0))


@entry(URB, FILL, "Special economic zone", ["sez", "special economic zone", "industrial park"],
       "Violet with diamond grid.")
def urb_sez():
    d = marker_symbol(simple_marker("diamond", 1.0, "#6d28d9", "#6d28d9", 0))
    return fill_symbol(simple_fill("#ede9fe", "#5b21b6", 0.35), point_pattern(d, 3, 3))


@entry(URB, FILL, "Tourism / hospitality", ["tourism", "hospitality", "resort", "zoning"],
       "Pink-magenta zoning colour.")
def urb_tourism():
    return _zone("#f9a8d4", "#9d174d")


@entry(URB, FILL, "Government / military reserve", ["government reserve", "military land",
                                                    "zoning"],
       "Slate with white cross-hatch.")
def urb_gov():
    return fill_symbol(simple_fill("#94a3b8", "#334155", 0.35),
                       hatch("#e2e8f0", 45, 2.0, 0.18), hatch("#e2e8f0", -45, 2.0, 0.18))


@entry(URB, FILL, "Waterfront / riverside", ["waterfront", "riverside", "promenade", "zoning"],
       "Aqua zoning colour.")
def urb_waterfront():
    return _zone("#99f6e4", "#0f766e")


@entry(URB, LINE, "Zoning boundary", ["zoning boundary", "zone line", "planning"],
       "Black long-dash zone limit.")
def urb_zone_line():
    return line_symbol(line("#111111", 0.45, dash=[4, 1.2]))


@entry(URB, LINE, "Pedestrian street", ["pedestrian", "walkway", "footway", "street"],
       "Pale pink paving with dotted edge.")
def urb_ped():
    return line_symbol(line("#d1d5db", 1.4, dash=[0.5, 0.5], cap="flat"),
                       line("#fce7f3", 1.0, cap="flat"))


@entry(URB, LINE, "Planned cycle lane", ["cycle lane", "bike", "planned"],
       "Green dashed lane line.")
def urb_cycle():
    return line_symbol(line("#16a34a", 0.6, dash=[1.6, 1.0]))


# ---- Land Parcels & Cadastre ------------------------------------------
@entry(CAD, LINE, "Proposed subdivision line", ["subdivision", "proposed", "parcel"],
       "Red dashed proposed lot line.")
def cad_subdiv():
    return line_symbol(line("#dc2626", 0.35, dash=[2.4, 1.0]))


@entry(CAD, LINE, "Natural boundary", ["natural boundary", "river boundary", "ambulatory"],
       "Blue dotted boundary following a natural feature.")
def cad_natural():
    return line_symbol(line("#2563eb", 0.35, dash=[0.5, 0.7]))


@entry(CAD, LINE, "Historic / superseded boundary", ["historic", "superseded", "old boundary"],
       "Faint grey dash-dot – no longer current.")
def cad_old():
    return line_symbol(line("#9ca3af", 0.25, dash=[3, 1, 0.5, 1]))


@entry(CAD, LINE, "Fence", ["fence", "physical boundary"],
       "Black line with small x marks.")
def cad_fence():
    x = marker_symbol(simple_marker("cross2", 1.0, "#111111", "#111111", 0.2))
    return line_symbol(line("#111111", 0.25), marker_line(x, interval=3, offset_along=1.5))


@entry(CAD, LINE, "Wall", ["wall", "physical boundary"],
       "Heavy dark-grey wall line.")
def cad_wall():
    return line_symbol(line("#374151", 0.8, cap="flat"))


@entry(CAD, LINE, "Survey traverse", ["traverse", "survey", "measurement"],
       "Thin magenta line with station ticks.")
def cad_traverse():
    t = marker_symbol(simple_marker("triangle", 1.2, "#c026d3", "#c026d3", 0))
    return line_symbol(line("#c026d3", 0.25), marker_line(t, interval=7, offset_along=1))


@entry(CAD, FILL, "Encroachment area", ["encroachment", "illegal occupation", "overlap"],
       "Red cross-hatched overlap area.")
def cad_encroach():
    return fill_symbol(hatch("#ef4444", 45, 1.2, 0.2), hatch("#ef4444", -45, 1.2, 0.2),
                       simple_fill("#000000", "#b91c1c", 0.4, style="no", alpha=0))


@entry(CAD, FILL, "Condominium / strata unit", ["condominium", "strata", "apartment", "unit"],
       "Light blue with fine grid.")
def cad_condo():
    return fill_symbol(simple_fill("#e0f2fe", "#0369a1", 0.3),
                       hatch("#7dd3fc", 0, 1.6, 0.12), hatch("#7dd3fc", 90, 1.6, 0.12))


@entry(CAD, FILL, "Water-body parcel", ["water parcel", "public water", "parcel"],
       "Light blue parcel for rivers and lakes in the register.")
def cad_water():
    return fill_symbol(simple_fill("#dbeafe", "#2563eb", 0.3))


@entry(CAD, MARKER, "Parcel label point", ["label", "centroid", "parcel number"],
       "Small red dot for parcel centroid / label anchors.")
def cad_label():
    return _struct("circle", "#dc2626", 1.4)


# ---- Transport -------------------------------------------------------------
@entry(TRA, LINE, "Motorway link / ramp", ["ramp", "link", "interchange", "slip road"],
       "Thinner orange link with casing.", color_layer=1)
def tra_link():
    return line_symbol(line("#8a3b12", 1.2), line("#f59e42", 0.8))


@entry(TRA, LINE, "Road under construction", ["construction", "road", "proposed"],
       "White road with yellow-black dashed casing.", color_layer=1)
def tra_construction():
    return line_symbol(line("#eab308", 1.4, dash=[1.6, 1.0], cap="flat"),
                       line("#ffffff", 0.9, cap="flat"))


@entry(TRA, LINE, "Steps / stairs", ["steps", "stairs", "path"],
       "Brown line with dense cross ticks.")
def tra_steps():
    return line_symbol(hash_line("#92400e", 0.3, 1.4, 0.6))


@entry(TRA, LINE, "Cable car / gondola", ["cable car", "gondola", "aerial tramway", "ropeway"],
       "Black line with small squares (cabins).")
def tra_cable():
    s = marker_symbol(simple_marker("square", 1.2, "#111111", "#111111", 0))
    return line_symbol(line("#111111", 0.3), marker_line(s, interval=5, offset_along=2.5))


@entry(TRA, LINE, "Abandoned / disused railway", ["abandoned", "disused", "railway"],
       "Grey dashed rail with sparse ties.")
def tra_disused():
    return line_symbol(line("#9ca3af", 0.4, dash=[2, 1]), hash_line("#9ca3af", 0.25, 1.2, 4))


@entry(TRA, LINE, "Narrow-gauge railway", ["narrow gauge", "railway", "light rail"],
       "Thin black/white rail.")
def tra_narrow():
    return line_symbol(line("#1f2937", 0.7, cap="flat"),
                       line("#ffffff", 0.35, dash=[2, 2], cap="flat"))


@entry(TRA, MARKER, "Railway station", ["railway station", "train", "station"],
       "Black square with white centre.")
def tra_station():
    return _struct("square", "#111111", 2.8, "square")


@entry(TRA, MARKER, "Toll gate", ["toll", "toll plaza", "gate"],
       "Amber badge with toll booth.")
def tra_toll():
    return marker_symbol(svg_marker(icons.TOLL, 4.5, "#b45309", "#ffffff", 0.4))


@entry(TRA, MARKER, "Traffic signal", ["traffic light", "signal", "junction"],
       "Three-lamp signal head.")
def tra_signal():
    return marker_symbol(svg_marker(icons.SIGNAL, 4.5, "#222222"))


@entry(TRA, MARKER, "Helipad", ["helipad", "heliport", "helicopter"],
       "Round badge with H.")
def tra_helipad():
    return marker_symbol(svg_marker(icons.HELIPAD, 5, "#334155", "#ffffff", 0.4))


# ---- Utilities & Infrastructure (APWA colours) --------------------------
@entry(UTL, LINE, "Electric – underground (APWA red)", ["electric", "underground", "apwa",
                                                        "cable"],
       "Red dashed line – buried power cable.")
def utl_elec_ug():
    return line_symbol(line("#dc2626", 0.55, dash=[2.4, 1.0]))


@entry(UTL, LINE, "Electric – overhead (APWA red)", ["electric", "overhead", "apwa"],
       "Red line with pole ticks.")
def utl_elec_oh():
    t = marker_symbol(simple_marker("line", 1.8, "#dc2626", "#dc2626", 0.3))
    return line_symbol(line("#dc2626", 0.45), marker_line(t, interval=5, offset_along=2.5))


@entry(UTL, LINE, "Steam / hot water", ["steam", "district heating", "hot water"],
       "Yellow double line.")
def utl_steam():
    return line_symbol(line("#ca8a04", 1.0, cap="flat"), line("#fef3c7", 0.4, cap="flat"))


@entry(UTL, LINE, "Survey marking (APWA pink)", ["survey", "temporary", "apwa", "pink"],
       "Pink dashed temporary survey line.")
def utl_survey():
    return line_symbol(line("#ec4899", 0.4, dash=[1.6, 0.8]))


@entry(UTL, LINE, "Proposed excavation (APWA white)", ["excavation", "proposed", "apwa"],
       "White dashed line with black edge.")
def utl_excav():
    return line_symbol(line("#111111", 0.9, cap="flat"),
                       line("#ffffff", 0.6, dash=[2, 1], cap="flat"))


@entry(UTL, MARKER, "Fire hydrant", ["hydrant", "fire", "water"],
       "Red hydrant glyph.")
def utl_hydrant():
    return marker_symbol(svg_marker(icons.HYDRANT, 4.5, "#dc2626", "#ffffff", 0.3))


@entry(UTL, MARKER, "Utility pole", ["pole", "power pole", "telecom pole"],
       "Small black dot with white ring.")
def utl_pole():
    return _struct("circle", "#111111", 1.6)


@entry(UTL, MARKER, "Transformer", ["transformer", "electric"],
       "Red badge with twin coils.")
def utl_transformer():
    return marker_symbol(svg_marker(icons.TRANSFORMER, 4.5, "#dc2626", "#ffffff", 0.4))


@entry(UTL, MARKER, "Water meter / connection", ["water meter", "service connection"],
       "Blue meter badge.")
def utl_meter():
    return marker_symbol(svg_marker(icons.METER, 4.5, "#1d4ed8", "#ffffff", 0.4))


# ---- Boundaries ------------------------------------------------------------
@entry(BND, LINE, "Territorial sea (12 nm)", ["territorial sea", "maritime", "12 nm"],
       "Blue dashed maritime limit with fine halo.")
def bnd_terr_sea():
    return line_symbol(line("#dbeafe", 1.2), line("#1d4ed8", 0.35, dash=[2.4, 1.0]))


@entry(BND, LINE, "Electoral district", ["electoral", "constituency", "voting"],
       "Teal long-dash electoral boundary.")
def bnd_electoral():
    return line_symbol(line("#0f766e", 0.45, dash=[5, 1.5]))


@entry(BND, LINE, "Census / enumeration area", ["census", "enumeration area", "statistical"],
       "Thin grey dash-dot statistical boundary.")
def bnd_census():
    return line_symbol(line("#6b7280", 0.25, dash=[2, 0.8, 0.4, 0.8]))


@entry(BND, FILL, "Indigenous / tribal territory", ["indigenous", "tribal", "reserve",
                                                    "territory"],
       "Orange inner tint band with solid edge.")
def bnd_indigenous():
    return fill_symbol(line("#f97316", 3.0, alpha=70, cap="flat", join="miter", inside=True),
                       simple_fill("#000000", "#c2410c", 0.5, style="no", alpha=0))


# ---- Terrain & Geology (FGDC themes) ------------------------------------
@entry(GEO, LINE, "Fault – inferred", ["fault", "inferred", "approximate", "fgdc"],
       "Black dashed fault – location approximate or inferred.")
def geo_fault_inf():
    return line_symbol(line("#111111", 0.5, dash=[3, 1.2]))


@entry(GEO, LINE, "Fault – concealed", ["fault", "concealed", "buried", "fgdc"],
       "Black dotted fault – concealed beneath cover.")
def geo_fault_conc():
    return line_symbol(line("#111111", 0.5, dash=[0.4, 0.9]))


@entry(GEO, LINE, "Strike-slip fault", ["strike-slip", "fault", "lateral", "fgdc"],
       "Fault with opposing half-arrows at the centre.")
def geo_strike_slip():
    a = marker_symbol(simple_marker("filled_arrowhead", 1.8, "#111111", "#111111", 0))
    b = marker_symbol(simple_marker("filled_arrowhead", 1.8, "#111111", "#111111", 0,
                                    angle=180))
    return line_symbol(line("#111111", 0.55),
                       marker_line(a, interval=16, offset=1.0, offset_along=7),
                       marker_line(b, interval=16, offset=-1.0, offset_along=9))


@entry(GEO, LINE, "Dike (geologic)", ["dike", "dyke", "intrusion", "fgdc"],
       "Red-brown bold line for igneous dikes.")
def geo_dike():
    return line_symbol(line("#9f1239", 0.8))


@entry(GEO, LINE, "Landslide scarp", ["landslide", "scarp", "mass wasting", "fgdc"],
       "Red line with downslope hachures.")
def geo_scarp():
    return line_symbol(line("#b91c1c", 0.5), hash_line("#b91c1c", 0.3, 1.2, 1.0, offset=-0.6))


@entry(GEO, LINE, "Moraine crest", ["moraine", "glacial", "fgdc"],
       "Green line with small circles.")
def geo_moraine():
    o = marker_symbol(simple_marker("circle", 1.0, "#ffffff", "#15803d", 0.3))
    return line_symbol(line("#15803d", 0.4), marker_line(o, interval=2.4, offset_along=1.2))


@entry(GEO, LINE, "Depression contour", ["depression", "contour", "closed depression"],
       "Brown contour with inward ticks.")
def geo_depression():
    return line_symbol(line("#8b5a2b", 0.35), hash_line("#8b5a2b", 0.25, 1.0, 2.4,
                                                        offset=-0.5))


@entry(GEO, LINE, "Supplementary contour", ["contour", "supplementary", "auxiliary"],
       "Thin dashed brown contour.")
def geo_supp_contour():
    return line_symbol(line("#b08060", 0.18, dash=[2, 1]))


@entry(GEO, FILL, "Conglomerate", ["conglomerate", "lithology", "clastic"],
       "Ochre with random open pebbles.")
def geo_conglomerate():
    o = marker_symbol(simple_marker("circle", 1.4, "#ffffff", "#78350f", 0.2, alpha=0))
    return fill_symbol(simple_fill("#fde68a", "#78350f", 0.3), random_markers(o, density=4,
                                                                              seed=43))


@entry(GEO, FILL, "Schist / metamorphic", ["schist", "metamorphic", "gneiss", "lithology"],
       "Green-grey with wavy-like short dashes in offset rows.")
def geo_schist():
    d = marker_symbol(simple_marker("line", 1.6, "#3f6212", "#3f6212", 0.25, angle=75))
    return fill_symbol(simple_fill("#e2ead3", "#3f6212", 0.3), point_pattern(d, 2.0, 1.6))


@entry(GEO, FILL, "Basalt / volcanic", ["basalt", "volcanic", "extrusive", "lithology"],
       "Dark olive with random small v marks.")
def geo_basalt():
    v = marker_symbol(simple_marker("arrowhead", 1.0, "#365314", "#365314", 0.25, angle=-90))
    return fill_symbol(simple_fill("#c7d2a6", "#365314", 0.3), random_markers(v, density=4,
                                                                              seed=47))


@entry(GEO, FILL, "Coal / carbonaceous", ["coal", "lignite", "lithology"],
       "Near-black fill with grey horizontal lines.")
def geo_coal():
    return fill_symbol(simple_fill("#27272a", "#000000", 0.3), hatch("#71717a", 0, 1.4, 0.2))


@entry(GEO, FILL, "Karst area", ["karst", "limestone", "sinkholes"],
       "Light blue-grey with small depression marks.")
def geo_karst():
    d = marker_symbol(simple_marker("semi_circle", 1.2, "#ffffff", "#475569", 0.25, alpha=0,
                                    angle=180))
    return fill_symbol(simple_fill("#eef2f7", "#475569", 0.3), random_markers(d, density=8,
                                                                              seed=51))


@entry(GEO, MARKER, "Sinkhole", ["sinkhole", "doline", "karst"],
       "Circle with inward ticks.")
def geo_sinkhole():
    return marker_symbol(simple_marker("circle", 3.0, "#ffffff", "#475569", 0.4, alpha=0),
                         simple_marker("cross", 3.0, "#475569", "#475569", 0.25))


@entry(GEO, MARKER, "Spot height", ["spot height", "elevation point", "spot elevation"],
       "Small black cross for spot elevations.")
def geo_spot():
    return marker_symbol(simple_marker("cross2", 1.6, "#111111", "#111111", 0.35))


@entry(GEO, MARKER, "Mine shaft / adit", ["mine", "shaft", "adit", "natural resources"],
       "Black square with white crossed hammers-like cross.")
def geo_mine():
    return _struct("square", "#111111", 2.8, "cross2")


@entry(GEO, MARKER, "Earthquake epicentre", ["earthquake", "epicentre", "epicenter", "seismic"],
       "Concentric red rings.")
def geo_epicentre():
    return marker_symbol(svg_marker(icons.EPICENTER, 5, "#b91c1c"))


# ---- Environment & Hazards ---------------------------------------------
@entry(ENV, FILL, "Oil spill", ["oil spill", "pollution", "marine"],
       "Dark iridescent violet-black slick.")
def env_oil():
    return fill_symbol(simple_fill("#1f1b2e", "#6d28d9", 0.4, alpha=170),
                       shapeburst("#6d28d9", distance=1.4, alpha=150))


@entry(ENV, FILL, "Drought-affected area", ["drought", "dry", "agricultural drought"],
       "Tan with cracked-earth crosshatch.")
def env_drought():
    return fill_symbol(simple_fill("#f5deb3", "#a16207", 0.35),
                       hatch("#c08a3e", 30, 2.6, 0.2), hatch("#c08a3e", -60, 3.4, 0.2))


@entry(ENV, FILL, "Tsunami inundation zone", ["tsunami", "coastal hazard", "inundation"],
       "Blue-violet hatch with dashed edge.")
def env_tsunami():
    return fill_symbol(simple_fill("#e0e7ff", "#4338ca", 0.45, alpha=150,
                                   outline_style="dash"), hatch("#6366f1", -30, 2.2, 0.22))


@entry(ENV, FILL, "Invasive species area", ["invasive", "alien species", "biodiversity"],
       "Lime fill with dark green dots.")
def env_invasive():
    d = marker_symbol(simple_marker("circle", 0.8, "#166534", "#166534", 0, no_outline=True))
    return fill_symbol(simple_fill("#ecfccb", "#4d7c0f", 0.35), random_markers(d, density=3,
                                                                               seed=53))


@entry(ENV, FILL, "Seismic hazard zone", ["seismic", "earthquake", "fault zone"],
       "Red-orange tint with zig-zag-like double hatch.")
def env_seismic():
    return fill_symbol(simple_fill("#fee2e2", "#b91c1c", 0.4, alpha=150),
                       hatch("#ef4444", 60, 2.6, 0.2), hatch("#ef4444", -60, 2.6, 0.2))


@entry(ENV, LINE, "Evacuation route", ["evacuation", "emergency route", "escape"],
       "Green arrowed route.")
def env_evac():
    a = marker_symbol(simple_marker("filled_arrowhead", 2.0, "#16a34a", "#16a34a", 0))
    return line_symbol(line("#16a34a", 0.8), marker_line(a, interval=8, offset_along=4))


@entry(ENV, MARKER, "Evacuation shelter", ["shelter", "evacuation", "refuge"],
       "Green badge with a house.")
def env_shelter():
    return marker_symbol(svg_marker(icons.SHELTER, 5, "#16a34a", "#ffffff", 0.4))


@entry(ENV, MARKER, "Air quality monitor", ["air quality", "monitor", "sensor"],
       "Teal circle with white ring.")
def env_aq():
    return _struct("circle", "#0d9488", 2.8, "circle")


# ---- Weather & Climate (NOAA/WPC surface analysis) ----------------------
@entry(WTH, LINE, "Dry line", ["dry line", "dryline", "synoptic"],
       "Brown line with open semicircles.")
def wth_dryline():
    s = marker_symbol(simple_marker("semi_circle", 2.4, "#ffffff", "#92400e", 0.4, alpha=0))
    return line_symbol(line("#92400e", 0.6), marker_line(s, interval=6, offset=-0.2))


@entry(WTH, LINE, "Tropical wave", ["tropical wave", "easterly wave", "synoptic"],
       "Brown line – tropical wave axis.")
def wth_twave():
    return line_symbol(line("#92400e", 0.9))


@entry(WTH, LINE, "Outflow boundary", ["outflow boundary", "gust front", "synoptic"],
       "Short-dashed orange-brown trough-style line.")
def wth_outflow():
    return line_symbol(line("#c2410c", 0.5, dash=[1.2, 1.0]))


@entry(WTH, LINE, "Ridge axis", ["ridge", "high pressure ridge", "synoptic"],
       "Orange zig-zag-like line with chevrons.")
def wth_ridge():
    c = marker_symbol(simple_marker("arrowhead", 1.4, "#ea580c", "#ea580c", 0.4, angle=-90))
    return line_symbol(line("#ea580c", 0.5), marker_line(c, interval=2.2))


@entry(WTH, LINE, "Cold front – forming", ["frontogenesis", "cold front", "synoptic"],
       "Dashed blue front with triangles on each segment.")
def wth_cold_gen():
    tri = marker_symbol(simple_marker("triangle", 2.0, "#1d4ed8", "#1d4ed8", 0))
    return line_symbol(line("#1d4ed8", 0.6, dash=[4, 2]),
                       marker_line(tri, interval=6, offset=-0.8, offset_along=2))


@entry(WTH, LINE, "Jet stream", ["jet stream", "upper air", "wind"],
       "Bold magenta arrow-line.")
def wth_jet():
    a = marker_symbol(simple_marker("filled_arrowhead", 2.6, "#c026d3", "#c026d3", 0))
    return line_symbol(line("#c026d3", 1.0), marker_line(a, placement="lastvertex"))


@entry(WTH, FILL, "Snow area", ["snow", "precipitation", "winter weather"],
       "Light blue-white with small asterisks.")
def wth_snow():
    s = marker_symbol(simple_marker("star", 1.4, "#60a5fa", "#60a5fa", 0))
    return fill_symbol(simple_fill("#eff6ff", "#3b82f6", 0.3, alpha=200),
                       point_pattern(s, 3, 3))


@entry(WTH, FILL, "Fog area", ["fog", "mist", "visibility"],
       "Grey with horizontal fog lines.")
def wth_fog():
    return fill_symbol(simple_fill("#f3f4f6", "#6b7280", 0.3, alpha=200),
                       hatch("#9ca3af", 0, 1.6, 0.35))


@entry(WTH, FILL, "Thunderstorm area", ["thunderstorm", "convection", "storm"],
       "Red outline with yellow tint and red hatch.")
def wth_tstorm():
    return fill_symbol(simple_fill("#fef3c7", "#dc2626", 0.5, alpha=170),
                       hatch("#ef4444", 60, 2.4, 0.25))


@entry(WTH, MARKER, "High pressure (H)", ["high pressure", "anticyclone", "h"],
       "Blue bold H in a white ring.")
def wth_high():
    return _letter("H", "#1d4ed8", ring="#1d4ed8", text="#1d4ed8")


@entry(WTH, MARKER, "Low pressure (L)", ["low pressure", "depression", "l"],
       "Red bold L in a white ring.")
def wth_low():
    return _letter("L", "#dc2626", ring="#dc2626", text="#dc2626")


@entry(WTH, MARKER, "Tropical cyclone", ["cyclone", "typhoon", "hurricane"],
       "Classic red tropical-cyclone symbol with two spiral arms.")
def wth_tc():
    return marker_symbol(svg_marker(icons.CYCLONE, 5, "#dc2626"))


@entry(WTH, MARKER, "Weather station", ["weather station", "met station", "synop"],
       "Sky-blue triangle with flag.")
def wth_station():
    return marker_symbol(svg_marker(icons.WEATHER_STATION, 4.5, "#0ea5e9"))


# ---- Remote Sensing ------------------------------------------------------
@entry(RS, FILL, "Scene footprint", ["footprint", "scene", "tile", "image extent"],
       "Transparent with blue outline – image or tile extent.")
def rs_footprint():
    return fill_symbol(simple_fill("#000000", "#2563eb", 0.5, style="no", alpha=0))


@entry(RS, FILL, "Training area", ["training", "sample", "roi", "classification"],
       "Transparent with bold yellow outline – ROI / training polygon.")
def rs_training():
    return fill_symbol(simple_fill("#000000", "#facc15", 0.8, style="no", alpha=0))


@entry(RS, FILL, "Cloud mask (hatched)", ["cloud mask", "qa", "mask"],
       "White diagonal hatch with grey edge – keeps imagery visible.")
def rs_cloud_mask():
    return fill_symbol(hatch("#ffffff", 45, 1.4, 0.35),
                       simple_fill("#000000", "#9ca3af", 0.4, style="no", alpha=0))


@entry(RS, FILL, "Change – gain", ["change detection", "gain", "increase"],
       "Green change class.")
def rs_gain():
    return fill_symbol(simple_fill("#22c55e", "#ffffff", 0.15, outline_alpha=120))


@entry(RS, FILL, "Change – loss", ["change detection", "loss", "decrease"],
       "Red change class.")
def rs_loss():
    return fill_symbol(simple_fill("#ef4444", "#ffffff", 0.15, outline_alpha=120))


@entry(RS, FILL, "Change – no change", ["change detection", "stable", "no change"],
       "Neutral light grey class.")
def rs_nochange():
    return fill_symbol(simple_fill("#e5e7eb", "#ffffff", 0.15, outline_alpha=120))


@entry(RS, MARKER, "Ground control point (GCP)", ["gcp", "ground control", "georeferencing"],
       "Magenta crosshair target.")
def rs_gcp():
    return marker_symbol(svg_marker(icons.SAMPLE, 4.5, "#c026d3"))


@entry(RS, MARKER, "Validation / accuracy point", ["validation", "accuracy assessment",
                                                   "reference point"],
       "Yellow circle with black ring.")
def rs_valid():
    return _struct("circle", "#facc15", 2.4)


@entry(RS, RAMP, "NDMI / moisture index", ["ndmi", "moisture", "index"],
       "Dry brown → white → wet blue.")
def ramp_ndmi():
    return ramp(["#a6611a", "#dfc27d", "#f5f5f5", "#80cdc1", "#018571", "#08306b"])


@entry(RS, RAMP, "LAI (leaf area index)", ["lai", "leaf area index", "vegetation"],
       "Pale yellow → green → deep green.")
def ramp_lai():
    return ramp(["#ffffe5", "#d9f0a3", "#78c679", "#238443", "#004529"])


# ---- Military & Tactical (simplified APP-6 style) -----------------------
@entry(MIL, MARKER, "Hostile – armour", ["military", "hostile", "armour", "app-6"],
       "Red diamond frame with track oval." + _MIL_NOTE)
def mil_hostile_armor():
    return _mil_marker(icons._mil(icons.FRAME_HOSTILE,
                                  '<rect x="11" y="14.5" width="14" height="7" rx="3.5" '
                                  'fill="none" stroke="#000000" stroke-width="1.1"/>'),
                       "#ff8080")


@entry(MIL, MARKER, "Hostile – artillery", ["military", "hostile", "artillery", "app-6"],
       "Red diamond frame with solid dot." + _MIL_NOTE)
def mil_hostile_arty():
    return _mil_marker(icons._mil(icons.FRAME_HOSTILE, icons.ICON_ARTY), "#ff8080")


@entry(MIL, MARKER, "Friendly – headquarters", ["military", "friendly", "hq", "headquarters"],
       "Blue frame on a staff." + _MIL_NOTE)
def mil_hq():
    return _mil_marker(icons._mil('<path d="M3 8.5v26" stroke="#000000" stroke-width="1.3"/>'
                                  + icons.FRAME_FRIEND), "#80e0ff")


@entry(MIL, LINE, "Line of departure (LD)", ["military", "line of departure", "ld", "lc"],
       "Black line with LD-style end bars.")
def mil_ld():
    bar = marker_symbol(simple_marker("line", 3.0, "#000000", "#000000", 0.5))
    return line_symbol(line("#000000", 0.6), marker_line(bar, placement="firstvertex"),
                       marker_line(bar, placement="lastvertex"))


@entry(MIL, LINE, "FEBA", ["military", "feba", "forward edge"],
       "Black line with paired diamonds.")
def mil_feba():
    d = marker_symbol(simple_marker("diamond", 1.6, "#000000", "#000000", 0))
    return line_symbol(line("#000000", 0.5), marker_line(d, interval=8, offset_along=3),
                       marker_line(d, interval=8, offset_along=4.4))


@entry(MIL, LINE, "Main supply route (MSR)", ["military", "msr", "supply route", "logistics"],
       "Black route with direction chevrons.")
def mil_msr():
    c = marker_symbol(simple_marker("arrowhead", 1.6, "#000000", "#000000", 0.4))
    return line_symbol(line("#000000", 0.7), marker_line(c, interval=8, offset_along=4))


@entry(MIL, LINE, "Limit of advance", ["military", "limit of advance", "loa"],
       "Black dash-dot control line.")
def mil_loa():
    return line_symbol(line("#000000", 0.6, dash=[4, 1, 0.6, 1]))


@entry(MIL, FILL, "Assembly area", ["military", "assembly area", "aa"],
       "Rounded-feel area with black outline and light tint.")
def mil_aa():
    return fill_symbol(simple_fill("#e5e7eb", "#000000", 0.6, alpha=110))


@entry(MIL, FILL, "Engagement area", ["military", "engagement area", "ea", "fires"],
       "Red outline with light red tint.")
def mil_ea():
    return fill_symbol(simple_fill("#fecaca", "#b91c1c", 0.6, alpha=110))


@entry(MIL, FILL, "Battle position", ["military", "battle position", "bp", "defence"],
       "Black outline with inner tick band (defensive side).")
def mil_bp():
    return fill_symbol(hash_line("#000000", 0.3, 1.0, 1.6, offset=0.5),
                       simple_fill("#000000", "#000000", 0.6, style="no", alpha=0))


@entry(MIL, MARKER, "Checkpoint", ["military", "checkpoint", "control point"],
       "Black-outlined white circle with centre dot.")
def mil_cp():
    return marker_symbol(simple_marker("circle", 3.2, "#ffffff", "#000000", 0.45),
                         simple_marker("circle", 0.8, "#000000", "#000000", 0))


@entry(MIL, MARKER, "Contact point", ["military", "contact point"],
       "Black-outlined white square.")
def mil_contact():
    return marker_symbol(simple_marker("square", 3.0, "#ffffff", "#000000", 0.45,
                                       angle=45))


@entry(MIL, MARKER, "Target reference point", ["military", "trp", "target reference point"],
       "Black cross in a ring.")
def mil_trp():
    return marker_symbol(simple_marker("circle", 3.4, "#ffffff", "#000000", 0.4, alpha=0),
                         simple_marker("cross", 3.4, "#000000", "#000000", 0.35))


# ---- Points of Interest ---------------------------------------------------
@entry(POI, MARKER, "Pharmacy", ["pharmacy", "drugstore", "health"],
       "Green round badge with cross.")
def poi_pharmacy():
    return marker_symbol(svg_marker(icons.PHARMACY, 5, "#16a34a", "#ffffff", 0.4))


@entry(POI, MARKER, "Post office", ["post office", "mail"],
       "Red badge with envelope.")
def poi_post():
    return marker_symbol(svg_marker(icons.POST, 5, "#b91c1c", "#ffffff", 0.4))


@entry(POI, MARKER, "Library", ["library", "books", "education"],
       "Brown badge with open book.")
def poi_library():
    return marker_symbol(svg_marker(icons.LIBRARY, 5, "#92400e", "#ffffff", 0.4))


@entry(POI, MARKER, "Mosque", ["mosque", "religion", "worship"],
       "Green badge with dome.")
def poi_mosque():
    return marker_symbol(svg_marker(icons.MOSQUE, 5, "#047857", "#ffffff", 0.4))


@entry(POI, MARKER, "Church", ["church", "religion", "worship"],
       "Indigo badge with church.")
def poi_church():
    return marker_symbol(svg_marker(icons.CHURCH, 5, "#3730a3", "#ffffff", 0.4))


@entry(POI, MARKER, "Stadium / sports", ["stadium", "sports", "arena"],
       "Round badge with stadium oval.")
def poi_stadium():
    return marker_symbol(svg_marker(icons.STADIUM, 5, "#0f766e", "#ffffff", 0.4))


@entry(POI, MARKER, "Playground / park", ["playground", "park", "children"],
       "Green round badge with swing.")
def poi_playground():
    return marker_symbol(svg_marker(icons.PLAYGROUND, 5, "#65a30d", "#ffffff", 0.4))


@entry(POI, MARKER, "Toilets", ["toilet", "wc", "restroom"],
       "Slate badge with figures.")
def poi_toilet():
    return marker_symbol(svg_marker(icons.TOILET, 5, "#475569", "#ffffff", 0.4))


# ---- Dark & Neon -----------------------------------------------------------
@entry(NEO, LINE, "Dark contour", ["dark", "contour", "night", "terrain"],
       "Subtle blue-grey contour for dark basemaps.")
def neo_contour():
    return line_symbol(line("#475569", 0.25))


@entry(NEO, FILL, "Neon cyan polygon", ["neon", "glow", "dark", "polygon"],
       "Translucent cyan fill with glowing edge.")
def neo_poly_cyan():
    return fill_symbol(simple_fill("#06b6d4", "#000000", 0, alpha=45, outline_style="no"),
                       with_glow(line("#cffafe", 0.4), "#06b6d4", spread=1.6, blur=3))


@entry(NEO, MARKER, "Glow point – magenta", ["glow", "neon", "dark", "magenta"],
       "Magenta point with soft halo.")
def neo_pt_mag():
    return marker_symbol(with_glow(simple_marker("circle", 1.8, "#fce7f3", "#ec4899", 0.3),
                                   "#ec4899", spread=2.5, blur=4))


# ==========================================================================
# helpers
# ==========================================================================
CATEGORIES = []
for _e in ENTRIES:
    if _e.category not in CATEGORIES:
        CATEGORIES.append(_e.category)
# order categories by field so related sets sit together in the picker
_ORDER = [CLS, HYD, WAT, IRR, AGR, LND, URB, CAD, TRA, UTL, BND, GEO, ENV, WTH, RS,
          MIL, POI, "Amenities & Services", "Shops & Commerce",
          "Recreation, Sports & Tourism", NEO]
CATEGORIES.sort(key=lambda c: _ORDER.index(c) if c in _ORDER else len(_ORDER))
ENTRIES.sort(key=lambda e: CATEGORIES.index(e.category))  # stable: keeps order within


# ---- DEM / terrain colour ramps ------------------------------------------
# Stops are (position 0..1, colour).  Positions are relative to the raster's
# min/max, so the same ramp fits a 0–40 m floodplain or a 0–4,000 m range.
_DEM_RAMPS = [
    ("dem_rasmapper", "RAS Mapper style terrain",
     ["hec-ras", "ras mapper", "dem", "terrain", "elevation", "hillshade"],
     "Soft teal-green lowlands → light green → straw → tan → rust → mauve-grey → "
     "white peaks; looks best with ⋯ ▸ Add hillshade.",
     [(0, "#2f7f6f"), (.10, "#4fa36b"), (.25, "#9bcd6e"), (.40, "#e9e48a"),
      (.55, "#e7b866"), (.70, "#c67f4c"), (.83, "#9a6a6e"), (.93, "#c9bfc6"), (1, "#ffffff")]),
    ("dem_rasmapper_low", "RAS Mapper style – floodplain detail",
     ["hec-ras", "floodplain", "dem", "terrain", "lowland", "elevation"],
     "Same palette, with most colours packed into the lowest 30 % – ideal when a "
     "floodplain DEM also contains a few high hills.",
     [(0, "#2f7f6f"), (.03, "#4fa36b"), (.08, "#9bcd6e"), (.14, "#e9e48a"),
      (.22, "#e7b866"), (.32, "#c67f4c"), (.55, "#9a6a6e"), (.8, "#c9bfc6"), (1, "#ffffff")]),
    ("dem_atlas", "Atlas hypsometric",
     ["atlas", "hypsometric", "dem", "elevation", "classic", "school atlas"],
     "Classic school-atlas tints: deep green → light green → yellow → orange → "
     "brown → purple-grey → white.",
     [(0, "#0b6b3a"), (.12, "#3f9a4b"), (.25, "#a3cd6a"), (.38, "#f3ec8c"),
      (.52, "#f2b65a"), (.66, "#c8743a"), (.8, "#8b4a2d"), (.92, "#9d8d9a"), (1, "#ffffff")]),
    ("dem_swiss", "Swiss relief (pastel)",
     ["swiss", "imhof", "relief", "dem", "pastel", "elevation"],
     "Muted, airy greens → sand → pale rock → white, in the spirit of Swiss relief maps.",
     [(0, "#9fbf96"), (.2, "#c3d6a3"), (.4, "#e3e2b6"), (.6, "#e8d9b8"),
      (.8, "#d9d1c9"), (1, "#fbfbfb")]),
    ("dem_globe", "Global relief",
     ["global", "relief", "globe", "dem", "elevation", "world"],
     "Rich lowland green → olive → ochre → chocolate → snow.",
     [(0, "#339966"), (.15, "#66b36b"), (.3, "#b3cc66"), (.45, "#e6cc80"),
      (.6, "#cc9966"), (.75, "#99664d"), (.88, "#b3a199"), (1, "#ffffff")]),
    ("dem_green_brown", "Green–yellow–brown DEM",
     ["dem", "elevation", "green", "brown", "simple"],
     "The familiar DEM look: green valleys, yellow slopes, brown highlands.",
     [(0, "#006837"), (.25, "#31a354"), (.5, "#ffffbf"), (.75, "#a6611a"), (1, "#543005")]),
    ("dem_terrain_classic", "Terrain (blue base)",
     ["terrain", "dem", "elevation", "coast", "blue"],
     "Blue at the lowest values → green → yellow → brown → white; good for "
     "coastal DEMs with sea level near the minimum.",
     [(0, "#333399"), (.15, "#0099ff"), (.25, "#00cc66"), (.5, "#ffff99"),
      (.75, "#80604d"), (1, "#ffffff")]),
    ("dem_earth", "Earth tones",
     ["earth", "dem", "elevation", "natural", "brown"],
     "Natural earth palette: deep blue-green → forest → olive → khaki → light stone.",
     [(0, "#0d2a4a"), (.05, "#1a3d6b"), (.15, "#2e7a7a"), (.3, "#3d8f4a"),
      (.5, "#8ca05a"), (.7, "#bfa874"), (.85, "#d9c3a3"), (1, "#fdfbfb")]),
    ("dem_desert", "Desert sun",
     ["desert", "arid", "dem", "elevation", "sand", "warm"],
     "Pale sand → apricot → terracotta → rust → deep red-brown, for arid terrain.",
     [(0, "#fff5d6"), (.25, "#f9d39a"), (.5, "#e9a060"), (.75, "#b75a33"), (1, "#5e2414")]),
    ("dem_sepia", "Sepia relief",
     ["sepia", "vintage", "dem", "elevation", "old map"],
     "Vintage cream → tan → sepia brown, for old-map style relief.",
     [(0, "#fbf6ea"), (.35, "#e6d2a8"), (.7, "#b38a5a"), (1, "#5b3a1e")]),
    ("dem_alpine", "Alpine",
     ["alpine", "mountain", "dem", "elevation", "rock", "snow"],
     "Meadow green → dark forest → grey rock → white snow.",
     [(0, "#a7d17a"), (.25, "#5d9b48"), (.45, "#2f6b3a"), (.62, "#7f7a6e"),
      (.8, "#b9b5ad"), (.9, "#e8e8e8"), (1, "#ffffff")]),
    ("dem_tropical", "Tropical lowland",
     ["tropical", "lowland", "mekong", "dem", "elevation", "cambodia", "delta"],
     "Lush emerald → lime → gold → clay → laterite red, tuned for wide tropical plains.",
     [(0, "#00704a"), (.06, "#1f9a55"), (.15, "#7cc243"), (.3, "#d9e36b"),
      (.48, "#f2c45a"), (.68, "#d7843d"), (.85, "#a64b2a"), (1, "#6b2a1a")]),
    ("dem_rainbow_soft", "Elevation spectrum (soft)",
     ["rainbow", "spectrum", "dem", "elevation", "spectral"],
     "A gentler rainbow than the default: blue → cyan → green → yellow → orange → red.",
     [(0, "#3b4cc0"), (.2, "#4fa3d9"), (.4, "#66c28a"), (.6, "#e6e36b"),
      (.8, "#f59d4c"), (1, "#c8312f")]),
    ("dem_spectral", "Spectral terrain",
     ["spectral", "dem", "elevation", "diverging"],
     "Purple-blue → teal → green → yellow → orange → red.",
     [(0, "#5e4fa2"), (.15, "#3288bd"), (.3, "#66c2a5"), (.45, "#abdda4"),
      (.55, "#e6f598"), (.7, "#fee08b"), (.85, "#f46d43"), (1, "#9e0142")]),
    ("dem_viridis", "Viridis (perceptual)",
     ["viridis", "perceptual", "colour-blind", "dem", "elevation", "scientific"],
     "Perceptually uniform, colour-blind safe: purple → blue → green → yellow.",
     [(0, "#440154"), (.25, "#3b528b"), (.5, "#21918c"), (.75, "#5ec962"), (1, "#fde725")]),
    ("dem_magma", "Magma (perceptual)",
     ["magma", "perceptual", "dem", "elevation", "scientific", "dark"],
     "Black → purple → coral → pale yellow; dramatic on dark backgrounds.",
     [(0, "#000004"), (.25, "#51127c"), (.5, "#b73779"), (.75, "#fc8961"), (1, "#fcfdbf")]),
    ("dem_cividis", "Cividis (colour-blind safe)",
     ["cividis", "colour-blind", "accessible", "dem", "elevation"],
     "Navy → grey → yellow, readable for every type of colour vision.",
     [(0, "#00224e"), (.25, "#35456c"), (.5, "#666970"), (.75, "#a59c74"), (1, "#fee838")]),
    ("dem_turbo", "Turbo (high contrast)",
     ["turbo", "rainbow", "high contrast", "dem", "elevation"],
     "Smooth high-contrast rainbow: dark blue → cyan → green → yellow → red → dark red.",
     [(0, "#30123b"), (.15, "#4686fb"), (.3, "#1ae4b6"), (.45, "#a2fc3c"),
      (.6, "#f9ba38"), (.8, "#e4460a"), (1, "#7a0403")]),
    ("dem_dark", "Night terrain",
     ["dark", "night", "dem", "elevation", "neon"],
     "Dark navy lowlands → teal → ice-blue peaks, for dark-theme maps.",
     [(0, "#0b1020"), (.3, "#123a5a"), (.6, "#1f7a8c"), (.85, "#7fd1d9"), (1, "#e8fbff")]),
    ("dem_grey_inv", "Greyscale relief (inverted)",
     ["grey", "greyscale", "inverted", "dem", "elevation", "print"],
     "White lowlands → black peaks, for print or under hillshade.",
     [(0, "#ffffff"), (1, "#111111")]),
    ("dem_grey_soft", "Soft grey relief",
     ["grey", "soft", "dem", "elevation", "base map"],
     "Light grey → mid grey; a quiet backdrop for other data.",
     [(0, "#f2f2f2"), (1, "#7a7a7a")]),
    ("dem_ice", "Ice & snow",
     ["ice", "snow", "polar", "glacier", "dem", "elevation"],
     "Glacier blue → pale cyan → white.",
     [(0, "#1d4f7a"), (.35, "#5f9ccb"), (.7, "#bfe3f2"), (1, "#ffffff")]),
    ("dem_topo_bathy", "Topo-bathy (sea level at middle)",
     ["topobathy", "bathymetry", "coast", "dem", "elevation", "sea level"],
     "Ocean blues below the middle value, land tints above – set Min/Max "
     "symmetric around 0 (e.g. −50…50) so the break falls at sea level.",
     [(0, "#08306b"), (.25, "#2171b5"), (.45, "#6baed6"), (.4999, "#c6e6f5"),
      (.5, "#4f9a5b"), (.62, "#a8cf6f"), (.75, "#f0e48a"), (.88, "#c78a4f"), (1, "#f5f2ee")]),
    ("dem_gebco", "Ocean depth (GEBCO style)",
     ["bathymetry", "ocean", "depth", "gebco", "sea"],
     "Deep indigo → ocean blue → shelf cyan → pale shallows.",
     [(0, "#1a1446"), (.25, "#253494"), (.5, "#2c7fb8"), (.75, "#41b6c4"),
      (.9, "#a1dab4"), (1, "#e8f6f3")]),
    ("dem_lagoon", "Shallow lagoon",
     ["bathymetry", "lagoon", "shallow", "lake", "reef", "depth"],
     "Turquoise shallows → teal → deep blue (shallow water at the minimum).",
     [(0, "#d6f5ef"), (.3, "#7fd8cf"), (.6, "#2aa6b8"), (1, "#0d3f73")]),
    ("dem_hand", "Height above drainage (HAND)",
     ["hand", "flood", "drainage", "dem", "floodplain", "hydrology"],
     "Dark blue near the drainage network → white high above it (flood-prone areas stand out).",
     [(0, "#08306b"), (.05, "#2171b5"), (.15, "#6baed6"), (.35, "#c6dbef"), (1, "#ffffff")]),
    ("dem_slope_classes", "Slope classes (stepped)",
     ["slope", "classes", "stepped", "dem", "terrain", "fao"],
     "Stepped flat → gentle → moderate → steep → very steep (green → red).",
     [(0, "#1a9850"), (.2, "#91cf60"), (.4, "#d9ef8b"), (.6, "#fee08b"),
      (.8, "#fc8d59"), (1, "#d73027")], True),
    ("dem_curvature", "Curvature (concave ↔ convex)",
     ["curvature", "diverging", "dem", "terrain", "geomorphology"],
     "Diverging blue (concave) → white (flat) → red-brown (convex).",
     [(0, "#2166ac"), (.25, "#92c5de"), (.5, "#f7f7f7"), (.75, "#f4a582"), (1, "#8c2d04")]),
    ("dem_tpi", "Topographic position (valley ↔ ridge)",
     ["tpi", "valley", "ridge", "diverging", "dem", "landform"],
     "Valleys teal → flats cream → ridges brown.",
     [(0, "#01665e"), (.25, "#80cdc1"), (.5, "#f5f5dc"), (.75, "#dfc27d"), (1, "#8c510a")]),
    ("dem_roughness", "Roughness / ruggedness",
     ["roughness", "ruggedness", "tri", "dem", "terrain"],
     "Smooth pale yellow → orange → rugged dark purple.",
     [(0, "#ffffcc"), (.3, "#fed976"), (.55, "#fd8d3c"), (.8, "#bd0026"), (1, "#49006a")]),
    ("dem_cut_fill", "Cut / fill (elevation change)",
     ["cut", "fill", "difference", "dod", "erosion", "deposition", "dem"],
     "Red cut (loss) → white (no change) → blue fill (gain); set Min/Max symmetric.",
     [(0, "#b2182b"), (.25, "#ef8a62"), (.45, "#fddbc7"), (.5, "#f7f7f7"),
      (.55, "#d1e5f0"), (.75, "#67a9cf"), (1, "#2166ac")]),
]


_DERIVATIVE = {"dem_slope_classes", "dem_curvature", "dem_tpi", "dem_roughness",
               "dem_cut_fill", "dem_hand"}


def _dem_factory(stops, discrete):
    def build():
        cols = [c for _p, c in stops]
        if discrete:      # equal-width steps; the last colour gets its own step too
            n = len(cols)
            return ramp(cols[:-1] + [cols[-1], cols[-1]],
                        [i / float(n) for i in range(n)] + [1.0], True)
        return ramp(cols, [p for p, _c in stops], discrete)
    return build


for _row in _DEM_RAMPS:
    _id, _name, _tags, _desc, _stops = _row[:5]
    _fn = _dem_factory(_stops, len(_row) > 5 and _row[5])
    _fn.__name__ = _id
    _extra = [] if _id in _DERIVATIVE else ["relief"]   # "relief" -> offers hillshade
    entry(GEO, RAMP, _name, _tags + ["dem ramp"] + _extra, _desc)(_fn)


BUILTIN_COUNT = len(ENTRIES)
_BY_ID = {e.id: e for e in ENTRIES}


def get(entry_id):
    return _BY_ID[entry_id]


def has(entry_id):
    return entry_id in _BY_ID


def set_pack_entries(entries):
    """Replace all style-pack entries (keeps the built-in catalog intact)."""
    del ENTRIES[BUILTIN_COUNT:]
    for cat in list(CATEGORIES):
        if cat not in {e.category for e in ENTRIES}:
            CATEGORIES.remove(cat)
    for k in [k for k, e in _BY_ID.items() if k.startswith("pack:")]:
        del _BY_ID[k]
    order = {c: i for i, c in enumerate(_ORDER)}
    entries = sorted(entries, key=lambda e: order.get(e.category, len(order)))
    for e in entries:
        ENTRIES.append(e)
        _BY_ID[e.id] = e
        if e.category not in CATEGORIES:
            CATEGORIES.append(e.category)
    CATEGORIES.sort(key=lambda c: _ORDER.index(c) if c in _ORDER else len(_ORDER))

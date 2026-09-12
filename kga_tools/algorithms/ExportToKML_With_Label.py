# -*- coding: utf-8 -*-

import os
import re
import math
import shutil
import hashlib
import zipfile
import tempfile

from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterFolderDestination,
    QgsProcessingParameterEnum,
    QgsProcessingParameterString,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterNumber,
    QgsProcessingParameterColor,
    QgsProcessing,
    QgsProcessingException,
    QgsProcessingUtils,
    QgsVectorLayer,
    QgsGeometry,
    QgsWkbTypes,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsExpression,
    QgsExpressionContext,
    QgsExpressionContextUtils,
    QgsRenderContext,
)
from qgis.PyQt.QtCore import QCoreApplication, Qt
from qgis.PyQt.QtGui import QImage, QPainter, QColor, QFont, QFontMetrics

from ..core.compat import mark_advanced
from ..branding import docs_url

try:
    from qgis.core import Qgis
except ImportError:                                    # pragma: no cover
    Qgis = None


# ----------------------------------------------- Qt5 / Qt6 enum compatibility
def _enum(owner, scoped, flat):
    obj = owner
    for part in scoped.split("."):
        obj = getattr(obj, part, None)
        if obj is None:
            break
    if obj is not None:
        return obj
    return getattr(owner, flat)


FMT_ARGB32   = _enum(QImage, "Format.Format_ARGB32", "Format_ARGB32")
HINT_AA      = _enum(QPainter, "RenderHint.Antialiasing", "Antialiasing")
HINT_TEXT_AA = _enum(QPainter, "RenderHint.TextAntialiasing", "TextAntialiasing")
# Used only to build the label-image cache key, so two different
# QColor objects with the same value hit the same cached PNG.
HEX_ARGB     = _enum(QColor, "NameFormat.HexArgb", "HexArgb")


try:
    GEOM_POINT   = Qgis.GeometryType.Point
    GEOM_LINE    = Qgis.GeometryType.Line
    GEOM_POLYGON = Qgis.GeometryType.Polygon
except AttributeError:                                 # QGIS < 3.30
    GEOM_POINT   = QgsWkbTypes.PointGeometry
    GEOM_LINE    = QgsWkbTypes.LineGeometry
    GEOM_POLYGON = QgsWkbTypes.PolygonGeometry


WGS84         = "EPSG:4326"
M_PER_DEG_LAT = 111320.0

# LOD tier stepping. Each tier is LOD_MULT x the ground size of the
# one below it and is shown when its box spans LOD_MIN..LOD_MAX screen
# pixels. Setting MULT == MAX/MIN gives a clean handoff with no gap.
LOD_MULT = 4.0
LOD_MIN  = 64
LOD_MAX  = 256

SUPERSCRIPT = {
    "0": "\u2070", "1": "\u00b9", "2": "\u00b2", "3": "\u00b3",
    "4": "\u2074", "5": "\u2075", "6": "\u2076", "7": "\u2077",
    "8": "\u2078", "9": "\u2079", "+": "\u207a", "-": "\u207b",
}
SUBSCRIPT = {
    "0": "\u2080", "1": "\u2081", "2": "\u2082", "3": "\u2083",
    "4": "\u2084", "5": "\u2085", "6": "\u2086", "7": "\u2087",
    "8": "\u2088", "9": "\u2089", "+": "\u208a", "-": "\u208b",
}


class ExportToKML(QgsProcessingAlgorithm):

    INPUT_LAYERS  = "INPUT_LAYERS"
    OUTPUT_FOLDER = "OUTPUT_FOLDER"
    EXPORT_MODE   = "EXPORT_MODE"
    COMBINED_NAME = "COMBINED_NAME"
    KEEP_LABELS   = "KEEP_LABELS"
    LABEL_MODE    = "LABEL_MODE"
    LABEL_SCALE   = "LABEL_SCALE"
    LABEL_HEIGHT  = "LABEL_HEIGHT"
    LABEL_OFFSET  = "LABEL_OFFSET"
    LOD_TIERS     = "LOD_TIERS"
    FONT_SIZE     = "FONT_SIZE"
    TEXT_COLOR    = "TEXT_COLOR"
    HALO_COLOR    = "HALO_COLOR"
    FLIP_UPRIGHT  = "FLIP_UPRIGHT"
    FONT_FAMILY   = "FONT_FAMILY"
    FONT_BOLD     = "FONT_BOLD"
    SELECTED_ONLY = "SELECTED_ONLY"
    INCLUDE_ATTRS = "INCLUDE_ATTRS"
    LINE_WIDTH    = "LINE_WIDTH"

    EXPORT_MODES = [
        "One file per layer",
        "Single combined file",
    ]

    LABEL_MODES = [
        "Placemark labels — horizontal, constant screen size  (.kml)",
        "Rotated ground overlays along lines  (.kmz)",
    ]

    def flags(self):
        f = super().flags()
        try:
            f |= Qgis.ProcessingAlgorithmFlag.NoThreading
        except AttributeError:
            f |= QgsProcessingAlgorithm.FlagNoThreading
        return f

    # ----------------------------------------------------------------- Inputs
    def initAlgorithm(self, config=None):

        # ------------------------------------------------- What gets exported
        self._add(QgsProcessingParameterMultipleLayers(
            self.INPUT_LAYERS, "Input Layers",
            layerType=QgsProcessing.TypeVectorAnyGeometry),
            "Point, line and polygon layers in any CRS. Everything is "
            "reprojected to WGS 84 on the way out, since that is the only "
            "CRS KML understands - the layers themselves are left alone. "
            "Each layer becomes its own folder in Google Earth, in the order "
            "listed here.")

        self._add(QgsProcessingParameterBoolean(
            self.SELECTED_ONLY, "Export Selected Features Only",
            defaultValue=False),
            "Exports only what is currently selected. A layer with nothing "
            "selected is exported in full, and the log says which ones "
            "those were.")

        self._add(QgsProcessingParameterEnum(
            self.EXPORT_MODE, "Export Mode",
            options=self.EXPORT_MODES, defaultValue=0),
            "One file per layer - a separate file named after each layer.\n"
            "Single combined file - every layer in one file, one folder "
            "each, which is what you want when the whole lot is going to "
            "somebody as an attachment.")

        self._add(QgsProcessingParameterString(
            self.COMBINED_NAME, "Combined File Name",
            defaultValue="Combined_Export", optional=True),
            "Name for the single file, without an extension - the tool "
            "appends .kml or .kmz itself. Characters that Windows rejects "
            "in a filename are replaced with underscores. Ignored unless "
            "the mode above is Single combined file.")

        # ------------------------------------------------------------- Labels
        self._add(QgsProcessingParameterBoolean(
            self.KEEP_LABELS, "Carry Layer Labels Across",
            defaultValue=True),
            "Reads whatever the layer Labels tab is set to - a field, an "
            "expression, or the first labelled rule of a rule-based setup - "
            "and writes it as the placemark name. Untick to export geometry "
            "and attributes only; every label setting below is then "
            "ignored.")

        self._add(QgsProcessingParameterEnum(
            self.LABEL_MODE, "Label Style",
            options=self.LABEL_MODES, defaultValue=0),
            "Placemark labels - ordinary KML text. Google Earth always "
            "draws it horizontally at a constant screen size, so it stays "
            "legible at any zoom but never follows a line.\n"
            "Rotated ground overlays - line labels are drawn to transparent "
            "PNGs and laid on the ground turned to match the line, so the "
            "text runs along the feature. This forces a .kmz, because the "
            "images have to travel inside the file. Point and polygon "
            "labels stay as placemarks either way.")

        self._add(QgsProcessingParameterNumber(
            self.LABEL_SCALE, "Placemark Labels \u00b7 Text Scale",
            type=QgsProcessingParameterNumber.Double,
            defaultValue=1.0, minValue=0.0, maxValue=10.0),
            "The KML LabelStyle scale: 1.0 is normal, 0.8 is noticeably "
            "smaller, 0 hides the text and leaves the geometry showing. "
            "Applies to point and polygon labels always, and to line labels "
            "in Placemark mode.")

        # -------------------------------------- Rotated ground-overlay labels
        self._add(QgsProcessingParameterNumber(
            self.LABEL_HEIGHT,
            "Rotated Labels \u00b7 Text Height on Ground (m)",
            type=QgsProcessingParameterNumber.Double,
            defaultValue=4.0, minValue=0.05, maxValue=10000.0),
            "Height of the glyphs measured on the ground in metres, not on "
            "screen, for the closest zoom tier. Pick roughly what the text "
            "should span when you are zoomed right in: 4 m suits parcel "
            "boundaries, 50 m suits a canal seen from a few kilometres up.")

        self._add(QgsProcessingParameterNumber(
            self.LABEL_OFFSET,
            "Rotated Labels \u00b7 Perpendicular Offset (m)",
            type=QgsProcessingParameterNumber.Double,
            defaultValue=0.0, minValue=-10000.0, maxValue=10000.0),
            "Pushes the text off the line so it does not sit on top of it. "
            "Positive is to the left of the direction the line runs, "
            "negative to the right, 0 centres it on the line. Half the text "
            "height is usually enough to clear the stroke. The offset grows "
            "with each zoom tier, so the gap looks the same at every zoom.")

        self._add(QgsProcessingParameterNumber(
            self.LOD_TIERS, "Rotated Labels \u00b7 Zoom Tiers",
            type=QgsProcessingParameterNumber.Integer,
            defaultValue=3, minValue=1, maxValue=6),
            "A ground overlay has a fixed size in metres, so one copy only "
            "looks right at one zoom. Each extra tier adds a copy 4x larger "
            "that switches on as you zoom out, keeping the text roughly the "
            "same size on screen. 3 tiers covers a 64x zoom range - 4 m "
            "through 64 m at the default height. Set 1 for a single fixed "
            "ground size. The tiers all reference the same PNG, so they "
            "cost XML, not image weight.")

        self._add(QgsProcessingParameterColor(
            self.TEXT_COLOR, "Rotated Labels \u00b7 Text Colour",
            defaultValue=QColor(255, 255, 255), opacityEnabled=True),
            "Colour of the rendered glyphs. White over a dark halo reads "
            "well against both imagery and bare terrain.")

        self._add(QgsProcessingParameterColor(
            self.HALO_COLOR, "Rotated Labels \u00b7 Halo Colour",
            defaultValue=QColor(0, 0, 0), opacityEnabled=True),
            "Outline drawn behind the text, about 8% of the font size wide. "
            "Drop the opacity to 0 for no halo at all - though plain text "
            "tends to vanish over busy imagery.")

        self._add(QgsProcessingParameterBoolean(
            self.FLIP_UPRIGHT,
            "Rotated Labels \u00b7 Keep Text Right-Way-Up",
            defaultValue=True),
            "Lines running east to west would otherwise carry upside-down "
            "text. Ticked, any label turned past vertical is spun 180 "
            "degrees so it always reads left to right. Untick only when the "
            "text has to follow the direction the line runs, such as flow "
            "labels.")

        # ------------------------------------------------ Advanced: rendering
        self._add(QgsProcessingParameterNumber(
            self.FONT_SIZE, "Rotated Labels \u00b7 Render Resolution (px)",
            type=QgsProcessingParameterNumber.Integer,
            defaultValue=48, minValue=8, maxValue=256),
            "Pixel height the label image is drawn at. This is sharpness, "
            "not size - how big the text looks on the ground is set by Text "
            "Height on Ground. Raise it if labels go soft when you zoom "
            "right in; each step up makes the .kmz larger.", True)

        self._add(QgsProcessingParameterString(
            self.FONT_FAMILY, "Rotated Labels \u00b7 Font Family",
            defaultValue="Arial", optional=True),
            "Font for the rendered labels. It only has to be installed on "
            "this machine - the glyphs are baked into the PNG, so whoever "
            "opens the file needs nothing. Khmer text needs a font that "
            "covers it, such as Khmer OS or Noto Sans Khmer. An unknown "
            "name silently falls back to a system default.", True)

        self._add(QgsProcessingParameterBoolean(
            self.FONT_BOLD, "Rotated Labels \u00b7 Bold Text",
            defaultValue=True),
            "Bold holds up better against aerial imagery. Untick for "
            "lighter text over plain terrain.", True)

        # ---------------------------------------- Advanced: style and content
        self._add(QgsProcessingParameterBoolean(
            self.INCLUDE_ATTRS, "Include Attribute Balloon",
            defaultValue=True),
            "Writes every non-empty field into the description balloon that "
            "opens when a feature is clicked in Google Earth. Untick for a "
            "much smaller file when the attributes are not meant to travel "
            "with it.", True)

        self._add(QgsProcessingParameterNumber(
            self.LINE_WIDTH, "Line Width (px)",
            type=QgsProcessingParameterNumber.Integer,
            defaultValue=2, minValue=1, maxValue=10),
            "Stroke width for lines and polygon outlines in Google Earth. "
            "The colours come from each layer own symbology; only the width "
            "is set here.", True)

        # ------------------------------------------------------------- Output
        self._add(QgsProcessingParameterFolderDestination(
            self.OUTPUT_FOLDER, "Output Folder"),
            "Where the .kml and .kmz files are written. Files of the same "
            "name are overwritten without asking. Left empty, the results "
            "go to the Processing temp folder and the log says where.")

    def _add(self, param, help_text, advanced=False):
        """Add a parameter, with its help shown as the widget tooltip."""
        param.setHelp(help_text)
        if advanced:
            mark_advanced(param)
        self.addParameter(param)

    def checkParameterValues(self, parameters, context):
        ok, msg = super().checkParameterValues(parameters, context)
        if not ok:
            return ok, msg

        layers = self.parameterAsLayerList(
            parameters, self.INPUT_LAYERS, context)
        if not layers:
            return False, self.tr(
                "No input layers selected. Pick at least one point, line or "
                "polygon layer.")

        if self.parameterAsEnum(parameters, self.EXPORT_MODE, context) == 1:
            name = (self.parameterAsString(
                parameters, self.COMBINED_NAME, context) or "").strip()
            if name and not self._safe_name(name).strip("_"):
                return False, self.tr(
                    "The combined file name is nothing but characters that "
                    "are illegal in a filename. Use letters, digits, spaces, "
                    "hyphens or underscores.")

        return True, ""

    # --------------------------------------------------------------- Labeling
    def _get_label_def(self, layer):
        """(label source, is_expression), or None if the layer has none.

        Rule-based labelling exposes one sub-provider per rule, and the
        first rule is often an unlabelled catch-all, so take the first
        sub-provider that actually carries a label source rather than
        simply the first one.
        """
        try:
            if not layer.labelsEnabled():
                return None
            labeling = layer.labeling()
            if labeling is None:
                return None

            candidates = []
            try:
                for sub in (labeling.subProviders() or []):
                    settings = labeling.settings(sub)
                    if settings is not None:
                        candidates.append(settings)
            except Exception:
                candidates = []
            if not candidates:
                try:
                    settings = labeling.settings()
                    if settings is not None:
                        candidates.append(settings)
                except Exception:
                    pass

            for settings in candidates:
                text = settings.fieldName
                if text:
                    return (text, bool(settings.isExpression))
            return None
        except Exception:
            return None

    def _label_text(self, feat, layer, label_def, expr_ctx):
        if not label_def:
            return ""
        text, is_expr = label_def
        try:
            if is_expr:
                expr = QgsExpression(text)
                if expr.hasParserError():
                    return ""
                expr_ctx.setFeature(feat)
                val = expr.evaluate(expr_ctx)
            else:
                idx = layer.fields().lookupField(text)
                if idx < 0:
                    return ""
                val = feat[idx]
            return self._clean_label(self._to_text(val))
        except Exception:
            return ""

    @staticmethod
    def _to_text(val):
        if val is None:
            return ""
        try:
            if hasattr(val, "isNull") and val.isNull():
                return ""
        except Exception:
            pass
        s = str(val)
        return "" if s in ("NULL", "None") else s

    @staticmethod
    def _clean_label(text):
        """KML <name> renders HTML literally; convert or strip it."""
        if not text:
            return ""
        s = str(text)

        def _sup(m):
            return "".join(SUPERSCRIPT.get(c, c) for c in m.group(1))

        def _sub(m):
            return "".join(SUBSCRIPT.get(c, c) for c in m.group(1))

        s = re.sub(r"<\s*sup\s*>(.*?)<\s*/\s*sup\s*>", _sup, s,
                   flags=re.IGNORECASE | re.DOTALL)
        s = re.sub(r"<\s*sub\s*>(.*?)<\s*/\s*sub\s*>", _sub, s,
                   flags=re.IGNORECASE | re.DOTALL)
        s = re.sub(r"<\s*br\s*/?\s*>", " ", s, flags=re.IGNORECASE)
        s = re.sub(r"<[^>]+>", "", s)
        for ent, ch in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                        ("&quot;", '"'), ("&apos;", "'"), ("&nbsp;", " ")):
            s = s.replace(ent, ch)
        return s.strip()

    # ------------------------------------------------------------ XML helpers
    def _xml_escape(self, text):
        text = str(text)
        for a, b in (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"),
                     ('"', "&quot;"), ("'", "&apos;")):
            text = text.replace(a, b)
        return text

    def _cdata_safe(self, text):
        return str(text).replace("]]>", "]]&gt;")

    # ---------------------------------------------------------------- Colours
    def _qgis_color_to_kml(self, color):
        try:
            return "{:02x}{:02x}{:02x}{:02x}".format(
                color.alpha(), color.blue(), color.green(), color.red())
        except Exception:
            return "ff0000ff"

    def _get_layer_colors(self, layer):
        line_color = "ff0000ff"
        fill_color = "660000ff"
        try:
            renderer = layer.renderer()
            if renderer is None:
                return line_color, fill_color
            sym = None
            if hasattr(renderer, "symbol"):
                sym = renderer.symbol()
            if sym is None and hasattr(renderer, "symbols"):
                syms = renderer.symbols(QgsRenderContext())
                sym = syms[0] if syms else None
            if sym is None:
                return line_color, fill_color
            for i in range(sym.symbolLayerCount()):
                sl = sym.symbolLayer(i)
                t = sl.__class__.__name__
                if "SimpleFill" in t:
                    fill_color = self._qgis_color_to_kml(sl.fillColor())
                    line_color = self._qgis_color_to_kml(sl.strokeColor())
                elif "SimpleLine" in t or "MarkerLine" in t:
                    line_color = self._qgis_color_to_kml(sl.color())
                elif "SimpleMarker" in t:
                    line_color = self._qgis_color_to_kml(sl.color())
                    fill_color = line_color
        except Exception:
            pass
        return line_color, fill_color

    # -------------------------------------- Label PNG, cropped to the ink box
    def _render_label_png(self, text, opts):
        """
        Renders text to a transparent PNG cropped to the glyph ink
        box, so the image centre IS the visual centre of the text and
        image height IS the text height. Returns
        (disk_path, arcname, width_px, height_px).
        """
        font_px    = opts["font_px"]
        text_color = opts["text_color"]        # QColor
        halo_color = opts["halo_color"]        # QColor, or None for no halo

        # Key on the colour values, not the QColor objects - two objects
        # holding the same colour have to hit the same cached PNG.
        key = "{}|{}|{}|{}|{}|{}".format(
            text, font_px, opts["font_family"],
            int(bool(opts["font_bold"])),
            text_color.name(HEX_ARGB),
            halo_color.name(HEX_ARGB) if halo_color else "none")
        if key in self._png_cache:
            return self._png_cache[key]

        font = QFont()
        font.setFamily(opts["font_family"] or "Arial")
        font.setPixelSize(int(font_px))
        font.setBold(bool(opts["font_bold"]))

        fm = QFontMetrics(font)
        try:
            ink = fm.tightBoundingRect(text)
        except Exception:
            ink = fm.boundingRect(text)

        halo_r = max(1, int(round(font_px * 0.08))) if halo_color else 0
        pad    = halo_r + 2

        w = max(4, ink.width() + pad * 2)
        h = max(4, ink.height() + pad * 2)

        # Baseline placement so the ink box lands exactly at (pad, pad).
        bx = pad - ink.x()
        by = pad - ink.y()

        img = QImage(w, h, FMT_ARGB32)
        img.fill(QColor(0, 0, 0, 0))

        p = QPainter(img)
        p.setRenderHint(HINT_AA, True)
        p.setRenderHint(HINT_TEXT_AA, True)
        p.setFont(font)

        if halo_r:
            hc = QColor(halo_color)
            if hc.isValid():
                p.setPen(hc)
                for dx in range(-halo_r, halo_r + 1):
                    for dy in range(-halo_r, halo_r + 1):
                        if (dx or dy) and (dx * dx + dy * dy
                                           <= halo_r * halo_r + 1):
                            p.drawText(bx + dx, by + dy, text)

        tc = QColor(text_color)
        if not tc.isValid():
            tc = QColor(255, 255, 255)
        p.setPen(tc)
        p.drawText(bx, by, text)
        p.end()

        digest  = hashlib.md5(key.encode("utf-8")).hexdigest()[:12]
        arcname = "files/lbl_{}.png".format(digest)
        disk    = os.path.join(opts["tmp_dir"], "lbl_{}.png".format(digest))
        img.save(disk, "PNG")

        out = (disk, arcname, w, h)
        self._png_cache[key] = out
        return out

    # ------------------------------------------------------- Geometry helpers
    def _coords(self, pts):
        return " ".join("{:.8f},{:.8f},0".format(p.x(), p.y()) for p in pts)

    def _segmentize(self, geom):
        try:
            if QgsWkbTypes.isCurvedType(geom.wkbType()):
                abstract = geom.constGet()
                if abstract is not None:
                    seg = abstract.segmentize()
                    if seg is not None:
                        return QgsGeometry(seg)
        except Exception:
            pass
        return geom

    def _line_anchor(self, g, flip_upright):
        """
        Returns (QgsPointXY midpoint, rotation_deg_ccw).
        Rotation is CCW from east, which is what KML <rotation> uses.
        The bearing comes from the segment that actually contains the
        midpoint, not from the overall start-to-end direction.
        """
        try:
            length = g.length()
            if length <= 0:
                return None
            mid = g.interpolate(length / 2.0)
            if mid is None or mid.isEmpty():
                return None
            mid_pt = mid.asPoint()

            res = g.closestSegmentWithContext(mid_pt)
            after = res[2] if len(res) > 2 else 0
            if after < 1:
                after = 1
            p1 = g.vertexAt(after - 1)
            p2 = g.vertexAt(after)
            if p1 is None or p2 is None:
                return (mid_pt, 0.0)

            lat_mid = math.radians((p1.y() + p2.y()) / 2.0)
            dx = (p2.x() - p1.x()) * math.cos(lat_mid)
            dy = (p2.y() - p1.y())
            if dx == 0 and dy == 0:
                return (mid_pt, 0.0)

            ang = math.degrees(math.atan2(dy, dx))
            if flip_upright:
                if ang > 90.0:
                    ang -= 180.0
                elif ang < -90.0:
                    ang += 180.0
            return (mid_pt, ang)
        except Exception:
            return None

    # --------------------------------------- Rotated ground overlays with LOD
    def _ground_overlays(self, text, pt, rotation, iw, ih, arcname, opts):
        """
        One GroundOverlay per zoom tier. Every tier points at the same
        PNG; only the ground footprint and the Region LOD band differ,
        so extra tiers cost bytes of XML, not extra images.
        """
        out    = []
        tiers  = max(1, int(opts["lod_tiers"]))
        aspect = float(iw) / float(ih)
        th     = math.radians(rotation)
        esc    = self._xml_escape(text)

        for k in range(tiers):
            f    = LOD_MULT ** k
            h_m  = opts["height_m"] * f
            off  = opts["offset_m"] * f
            w_m  = h_m * aspect

            # Perpendicular shift, +ve to the left of the text direction.
            dxm = -math.sin(th) * off
            dym = math.cos(th) * off

            lat = pt.y() + dym / M_PER_DEG_LAT
            cos_lat = max(math.cos(math.radians(lat)), 1e-6)
            lon = pt.x() + dxm / (M_PER_DEG_LAT * cos_lat)

            dlat = (h_m / 2.0) / M_PER_DEG_LAT
            dlon = (w_m / 2.0) / (M_PER_DEG_LAT * cos_lat)
            n, s = lat + dlat, lat - dlat
            e, w = lon + dlon, lon - dlon

            region = ""
            if tiers > 1:
                # Smallest tier has no upper bound so it stays on when
                # zoomed right in; largest has no lower bound so it
                # stays on when zoomed right out.
                lo = 0 if k == tiers - 1 else LOD_MIN
                hi = -1 if k == 0 else LOD_MAX
                region = (
                    '      <Region>\n'
                    '        <LatLonAltBox>\n'
                    '          <north>{n:.10f}</north>\n'
                    '          <south>{s:.10f}</south>\n'
                    '          <east>{e:.10f}</east>\n'
                    '          <west>{w:.10f}</west>\n'
                    '        </LatLonAltBox>\n'
                    '        <Lod>\n'
                    '          <minLodPixels>{lo}</minLodPixels>\n'
                    '          <maxLodPixels>{hi}</maxLodPixels>\n'
                    '        </Lod>\n'
                    '      </Region>\n'
                ).format(n=n, s=s, e=e, w=w, lo=lo, hi=hi)

            out.append(
                '    <GroundOverlay>\n'
                '      <name>{name}</name>\n'
                '{region}'
                '      <drawOrder>{order}</drawOrder>\n'
                '      <Icon><href>{href}</href></Icon>\n'
                '      <altitudeMode>clampToGround</altitudeMode>\n'
                '      <LatLonBox>\n'
                '        <north>{n:.10f}</north>\n'
                '        <south>{s:.10f}</south>\n'
                '        <east>{e:.10f}</east>\n'
                '        <west>{w:.10f}</west>\n'
                '        <rotation>{r:.4f}</rotation>\n'
                '      </LatLonBox>\n'
                '    </GroundOverlay>\n'.format(
                    name=esc, region=region, order=50 + k, href=arcname,
                    n=n, s=s, e=e, w=w, r=rotation)
            )
        return out

    # ------------------------------------------- One feature -> KML fragments
    def _feature_to_kml(self, feat, layer, label_def, expr_ctx, geom_type,
                        xform, style_id, keep_labels, opts):
        placemarks, overlays, images = [], [], {}

        label_text = self._label_text(feat, layer, label_def, expr_ctx) \
            if keep_labels else ""

        geom = feat.geometry()
        if geom is None or geom.isNull() or geom.isEmpty():
            return placemarks, overlays, images

        g = QgsGeometry(geom)
        if xform is not None:
            try:
                g.transform(xform)
            except Exception:
                return placemarks, overlays, images
        g = self._segmentize(g)

        desc_parts = []
        if opts.get("include_attrs", True):
            for field in layer.fields():
                val = self._to_text(feat[field.name()])
                if val.strip():
                    desc_parts.append("<b>{}:</b> {}".format(
                        self._xml_escape(field.name()),
                        self._xml_escape(val)))
        description = self._cdata_safe("<br/>".join(desc_parts))
        name = self._xml_escape(label_text) if label_text else ""

        def placemark(inner):
            return (
                '    <Placemark>\n'
                '      <name>{}</name>\n'
                '      <description><![CDATA[{}]]></description>\n'
                '      <styleUrl>#{}</styleUrl>\n'
                '{}'
                '    </Placemark>\n'
            ).format(name, description, style_id, inner)

        def label_placemark(pt):
            return (
                '    <Placemark>\n'
                '      <name>{}</name>\n'
                '      <styleUrl>#labelStyle</styleUrl>\n'
                '      <Point><coordinates>{:.8f},{:.8f},0'
                '</coordinates></Point>\n'
                '    </Placemark>\n'
            ).format(name, pt.x(), pt.y())

        # -------------------------------------------------------------- POINT
        if geom_type == GEOM_POINT:
            pts = g.asMultiPoint() if g.isMultipart() else [g.asPoint()]
            for p in pts:
                inner = ('      <Point>\n'
                         '        <coordinates>{:.8f},{:.8f},0</coordinates>\n'
                         '      </Point>\n').format(p.x(), p.y())
                placemarks.append(placemark(inner))

        # --------------------------------------------------------------- LINE
        elif geom_type == GEOM_LINE:
            parts = g.asMultiPolyline() if g.isMultipart() else [g.asPolyline()]
            parts = [p for p in parts if p and len(p) >= 2]
            if not parts:
                return placemarks, overlays, images

            def linestring(pts):
                return ('        <LineString>\n'
                        '          <tessellate>1</tessellate>\n'
                        '          <coordinates>{}</coordinates>\n'
                        '        </LineString>\n').format(self._coords(pts))

            if len(parts) == 1:
                inner = linestring(parts[0])
            else:
                inner = ('      <MultiGeometry>\n'
                         + "".join(linestring(p) for p in parts)
                         + '      </MultiGeometry>\n')
            placemarks.append(placemark(inner))

            if label_text:
                anchor = self._line_anchor(g, opts["flip_upright"])
                if anchor:
                    pt, rot = anchor
                    if opts["rotated"]:
                        disk, arcname, iw, ih = self._render_label_png(
                            label_text, opts)
                        images[arcname] = disk
                        overlays.extend(self._ground_overlays(
                            label_text, pt, rot, iw, ih, arcname, opts))
                    else:
                        placemarks.append(label_placemark(pt))

        # ------------------------------------------------------------ POLYGON
        elif geom_type == GEOM_POLYGON:
            polys = g.asMultiPolygon() if g.isMultipart() else [g.asPolygon()]
            polys = [p for p in polys if p]
            if not polys:
                return placemarks, overlays, images

            def polygon(rings):
                out = ('        <Polygon>\n'
                       '          <tessellate>1</tessellate>\n')
                for idx, ring in enumerate(rings):
                    tag = "outerBoundaryIs" if idx == 0 else "innerBoundaryIs"
                    out += ('          <{0}><LinearRing><coordinates>{1}'
                            '</coordinates></LinearRing></{0}>\n').format(
                        tag, self._coords(ring))
                out += '        </Polygon>\n'
                return out

            if len(polys) == 1:
                inner = polygon(polys[0])
            else:
                inner = ('      <MultiGeometry>\n'
                         + "".join(polygon(p) for p in polys)
                         + '      </MultiGeometry>\n')
            placemarks.append(placemark(inner))

            if label_text:
                try:
                    c = g.pointOnSurface()
                    if c is None or c.isEmpty():
                        c = g.centroid()
                    if c and not c.isEmpty():
                        placemarks.append(label_placemark(c.asPoint()))
                except Exception:
                    pass

        return placemarks, overlays, images

    # ------------------------------------------------ One layer -> KML folder
    def _layer_to_kml_content(self, layer, keep_labels, index,
                              context, feedback, opts):
        if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
            raise ValueError("Not a valid vector layer")

        geom_type = QgsWkbTypes.geometryType(layer.wkbType())
        label_def = self._get_label_def(layer) if keep_labels else None
        line_color, fill_color = self._get_layer_colors(layer)
        style_id = "layerStyle{}".format(index)

        xform = None
        src = layer.crs()
        if src.isValid() and src.authid() != WGS84:
            xform = QgsCoordinateTransform(
                src, QgsCoordinateReferenceSystem(WGS84),
                context.transformContext())

        expr_ctx = QgsExpressionContext()
        expr_ctx.appendScopes(
            QgsExpressionContextUtils.globalProjectLayerScopes(layer))

        suppress = opts["rotated"] and geom_type == GEOM_LINE
        placemark_label_scale = "0" if suppress else opts["label_scale"]

        selected_only = opts.get("selected_only", False)
        use_selection = selected_only and layer.selectedFeatureCount() > 0
        total = (layer.selectedFeatureCount() if use_selection
                 else layer.featureCount())

        feedback.pushInfo("   Geometry  : {}".format(
            QgsWkbTypes.displayString(layer.wkbType())))
        feedback.pushInfo("   CRS       : {}{}".format(
            src.authid() or "unknown",
            "" if xform is None else "  ->  " + WGS84))
        feedback.pushInfo("   Features  : {}  ({})".format(
            total, "selection" if use_selection else "whole layer"))
        if selected_only and not use_selection:
            feedback.pushWarning(
                "   Nothing is selected in this layer, so all {} feature(s) "
                "are being exported.".format(total))
        feedback.pushInfo("   Label     : {}{}".format(
            label_def[0] if label_def else "none",
            "  (expression)" if label_def and label_def[1] else ""))
        if keep_labels and label_def is None:
            feedback.pushInfo("   Note      : this layer has no labelling "
                              "set, so its features are exported unnamed.")
        feedback.pushInfo("   Colour    : line={} fill={}".format(
            line_color, fill_color))
        if suppress:
            feedback.pushInfo("   Labels    : rotated ground overlays, "
                              "{} zoom tier(s)".format(opts["lod_tiers"]))

        placemarks, overlays, images = [], [], {}
        count = 0

        features = (layer.getSelectedFeatures() if use_selection
                    else layer.getFeatures())

        for i, feat in enumerate(features):
            if feedback.isCanceled():
                break
            if total and total > 0:
                feedback.setProgress(int(i / float(total) * 90))
            pms, ovs, imgs = self._feature_to_kml(
                feat, layer, label_def, expr_ctx, geom_type,
                xform, style_id, keep_labels, opts)
            placemarks.extend(pms)
            overlays.extend(ovs)
            images.update(imgs)
            count += 1

        feedback.pushInfo("   Exported  : {} feature(s)".format(count))
        if overlays:
            feedback.pushInfo("   Overlays  : {} overlay(s) from {} "
                              "unique image(s)".format(
                                  len(overlays), len(images)))

        style = (
            '    <Style id="{sid}">\n'
            '      <LineStyle><color>{line}</color><width>{lw}</width></LineStyle>\n'
            '      <PolyStyle><color>{fill}</color></PolyStyle>\n'
            '      <IconStyle><color>{line}</color><scale>0.9</scale></IconStyle>\n'
            '      <LabelStyle><scale>{lsc}</scale></LabelStyle>\n'
            '    </Style>\n'
        ).format(sid=style_id, line=line_color, fill=fill_color,
                 lsc=placemark_label_scale, lw=opts.get("line_width", 2))

        folder = (
            '  <Folder>\n'
            '    <name>{}</name>\n'
            '    <visibility>1</visibility>\n'.format(
                self._xml_escape(layer.name()))
            + style
            + "".join(placemarks)
            + "".join(overlays)
            + '  </Folder>\n'
        )
        return folder, images

    # ---------------------------------------------- Header / footer / writers
    def _kml_header(self, doc_name, label_scale):
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<kml xmlns="http://www.opengis.net/kml/2.2">\n'
            '<Document>\n'
            '  <name>{}</name>\n'
            '  <Style id="labelStyle">\n'
            '    <IconStyle>\n'
            '      <scale>0</scale>\n'
            '      <Icon><href></href></Icon>\n'
            '    </IconStyle>\n'
            '    <LabelStyle><scale>{}</scale></LabelStyle>\n'
            '  </Style>\n'
        ).format(self._xml_escape(doc_name), label_scale or "1.0")

    def _kml_footer(self):
        return '</Document>\n</kml>\n'

    def _safe_name(self, name):
        name = re.sub(r'[\\/:*?"<>|]', "_", str(name)).strip().rstrip(".")
        return name or "layer"

    def _write_output(self, folder, base_name, kml_text, images):
        if images:
            path = os.path.join(folder, "{}.kmz".format(base_name))
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("doc.kml", kml_text.encode("utf-8"))
                for arcname, disk in sorted(images.items()):
                    if os.path.exists(disk):
                        z.write(disk, arcname)
        else:
            path = os.path.join(folder, "{}.kml".format(base_name))
            with open(path, "w", encoding="utf-8") as f:
                f.write(kml_text)
        return path

    # ------------------------------------------------------------------- Main
    def processAlgorithm(self, parameters, context, feedback):

        self._png_cache = {}

        layers = self.parameterAsLayerList(
            parameters, self.INPUT_LAYERS, context)
        export_mode = self.parameterAsEnum(
            parameters, self.EXPORT_MODE, context)
        label_mode = self.parameterAsEnum(
            parameters, self.LABEL_MODE, context)
        combined_name = (self.parameterAsString(
            parameters, self.COMBINED_NAME, context) or "").strip() \
            or "Combined_Export"
        keep_labels = self.parameterAsBoolean(
            parameters, self.KEEP_LABELS, context)
        label_scale = "{:g}".format(max(0.0, self.parameterAsDouble(
            parameters, self.LABEL_SCALE, context)))
        height_m = self.parameterAsDouble(
            parameters, self.LABEL_HEIGHT, context)
        offset_m = self.parameterAsDouble(
            parameters, self.LABEL_OFFSET, context)
        lod_tiers = self.parameterAsInt(
            parameters, self.LOD_TIERS, context)
        font_px = self.parameterAsInt(
            parameters, self.FONT_SIZE, context)
        font_family = (self.parameterAsString(
            parameters, self.FONT_FAMILY, context) or "").strip() or "Arial"
        font_bold = self.parameterAsBoolean(
            parameters, self.FONT_BOLD, context)

        text_color = self.parameterAsColor(
            parameters, self.TEXT_COLOR, context)
        if text_color is None or not text_color.isValid():
            text_color = QColor(255, 255, 255)

        halo_color = self.parameterAsColor(
            parameters, self.HALO_COLOR, context)
        # A fully transparent halo is how the form says "no halo".
        if (halo_color is None or not halo_color.isValid()
                or halo_color.alpha() == 0):
            halo_color = None

        flip_upright = self.parameterAsBoolean(
            parameters, self.FLIP_UPRIGHT, context)
        selected_only = self.parameterAsBoolean(
            parameters, self.SELECTED_ONLY, context)
        include_attrs = self.parameterAsBoolean(
            parameters, self.INCLUDE_ATTRS, context)
        line_width = self.parameterAsInt(
            parameters, self.LINE_WIDTH, context)
        output_folder = (self.parameterAsString(
            parameters, self.OUTPUT_FOLDER, context) or "").strip()

        if not output_folder or output_folder == "TEMPORARY_OUTPUT":
            output_folder = QgsProcessingUtils.tempFolder()
            feedback.pushWarning(
                "No output folder given - using temp folder: {}".format(
                    output_folder))

        output_folder = os.path.normpath(output_folder)
        os.makedirs(output_folder, exist_ok=True)

        if not layers:
            raise QgsProcessingException(
                "No input layers selected. Pick at least one point, line or "
                "polygon layer.")

        tmp_dir = tempfile.mkdtemp(prefix="kml_labels_")
        opts = {
            "rotated":       (label_mode == 1) and keep_labels,
            "label_scale":   label_scale,
            "height_m":      float(height_m),
            "offset_m":      float(offset_m),
            "lod_tiers":     int(lod_tiers),
            "font_px":       int(font_px),
            "font_family":   font_family,
            "font_bold":     bool(font_bold),
            "text_color":    text_color,
            "halo_color":    halo_color,
            "flip_upright":  flip_upright,
            "selected_only": bool(selected_only),
            "include_attrs": bool(include_attrs),
            "line_width":    int(line_width),
            "tmp_dir":       tmp_dir,
        }

        feedback.pushInfo("=" * 60)
        feedback.pushInfo(" Export Layers to KML / KMZ")
        feedback.pushInfo("=" * 60)
        feedback.pushInfo("Layers       : {}{}".format(
            len(layers), "  (selected features only)" if selected_only else ""))
        feedback.pushInfo("Export mode  : {}".format(
            self.EXPORT_MODES[export_mode]))
        feedback.pushInfo("Labels       : {}".format(
            self.LABEL_MODES[label_mode] if keep_labels
            else "not carried across"))
        feedback.pushInfo("Placemarks   : label scale {}, line width {} px, "
                          "balloon {}".format(
                              label_scale, opts["line_width"],
                              "on" if opts["include_attrs"] else "off"))
        if opts["rotated"]:
            n_lines = sum(
                1 for lyr in layers
                if isinstance(lyr, QgsVectorLayer) and lyr.isValid()
                and QgsWkbTypes.geometryType(lyr.wkbType()) == GEOM_LINE)
            feedback.pushInfo("Text height  : {:g} m on ground".format(
                opts["height_m"]))
            feedback.pushInfo("Offset       : {:g} m perpendicular".format(
                opts["offset_m"]))
            feedback.pushInfo("Zoom tiers   : {}  ({:g} m up to {:g} m)".format(
                opts["lod_tiers"], opts["height_m"],
                opts["height_m"] * LOD_MULT ** (opts["lod_tiers"] - 1)))
            feedback.pushInfo("Rendered as  : {} {}px{}, {}{}".format(
                opts["font_family"], opts["font_px"],
                " bold" if opts["font_bold"] else "",
                opts["text_color"].name(),
                " on {} halo".format(opts["halo_color"].name())
                if opts["halo_color"] else ", no halo"))
            if n_lines == 0:
                feedback.pushWarning(
                    "Rotated ground overlays only apply to line layers, and "
                    "none were selected. Point and polygon labels stay as "
                    "ordinary placemarks, so the output will be .kml.")
        feedback.pushInfo("Output       : {}".format(output_folder))

        results, failed, folders = [], [], []
        combined_images = {}

        try:
            for i, layer in enumerate(layers):
                if feedback.isCanceled():
                    break

                feedback.pushInfo("\n" + "-" * 55)
                feedback.pushInfo("[{}/{}] {}".format(
                    i + 1, len(layers), layer.name()))
                feedback.pushInfo("-" * 55)

                try:
                    folder_kml, images = self._layer_to_kml_content(
                        layer, keep_labels, i, context, feedback, opts)
                except Exception as e:
                    feedback.reportError("   FAILED: {}: {}".format(
                        type(e).__name__, e))
                    failed.append(layer.name())
                    continue

                if export_mode == 0:
                    kml = (self._kml_header(layer.name(), label_scale)
                           + folder_kml + self._kml_footer())
                    path = self._write_output(
                        output_folder, self._safe_name(layer.name()),
                        kml, images)
                    feedback.pushInfo("   Saved: {}".format(path))
                    results.append(path)
                else:
                    folders.append(folder_kml)
                    combined_images.update(images)
                    feedback.pushInfo("   Added to combined file")

            if export_mode == 1 and folders:
                kml = (self._kml_header(combined_name, label_scale)
                       + "".join(folders) + self._kml_footer())
                path = self._write_output(
                    output_folder, self._safe_name(combined_name),
                    kml, combined_images)
                feedback.pushInfo("\nCombined file saved: {}".format(path))
                results.append(path)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        feedback.pushInfo("\n" + "=" * 60)
        feedback.pushInfo("COMPLETED : {} file(s) written".format(len(results)))
        for path in results:
            feedback.pushInfo("            {}".format(os.path.basename(path)))
        if failed:
            feedback.reportError("FAILED    : {}".format(", ".join(failed)))
        feedback.pushInfo("Output    : {}".format(output_folder))
        feedback.pushInfo("=" * 60)
        feedback.setProgress(100)

        return {"OUTPUT_FOLDER": output_folder}

    # --------------------------------------------------------------- Metadata
    def name(self):
        return "export_layers_to_kml"

    def displayName(self):
        return "Export Layers to KML"

    def group(self):
        return "KGA Data Conversion"

    def groupId(self):
        return "kgadataconversion"

    def helpUrl(self):
        return docs_url('export_layers_to_kml')

    def shortDescription(self):
        return ("Write vector layers to KML or KMZ for Google Earth, with "
                "labels carried across - optionally rendered along lines.")

    def shortHelpString(self):
        return (
            "<p>Writes point, line and polygon layers to <b>KML</b> or "
            "<b>KMZ</b> for Google Earth. Layer colours, the label the layer "
            "already shows, and the attribute table all come across. Any CRS "
            "is reprojected to WGS 84 on the way out &mdash; the layers "
            "themselves are untouched.</p>"

            "<h3>Two ways to label</h3>"
            "<table border='1' cellpadding='4' cellspacing='0'>"
            "<tr><th></th><th>Placemark labels</th>"
            "<th>Rotated ground overlays</th></tr>"
            "<tr><td><b>Drawn as</b></td><td>KML text</td>"
            "<td>Transparent PNG on the ground</td></tr>"
            "<tr><td><b>Angle</b></td><td>Always horizontal</td>"
            "<td>Turned to match the line</td></tr>"
            "<tr><td><b>Size</b></td><td>Constant on screen</td>"
            "<td>Fixed on the ground, in metres</td></tr>"
            "<tr><td><b>File</b></td><td>.kml</td>"
            "<td>.kmz &mdash; the images ride inside</td></tr>"
            "<tr><td><b>Applies to</b></td><td>Everything</td>"
            "<td>Lines; points and polygons still get placemarks</td></tr>"
            "</table>"
            "<p>Placemark labels are the safe choice and always legible. "
            "Reach for rotated overlays when the text has to read along a "
            "canal, a road or a parcel boundary the way it does on the map "
            "&mdash; something plain KML cannot do at all.</p>"

            "<h3>Sizing rotated labels</h3>"
            "<p>A ground overlay is measured in metres, so a single copy "
            "only looks right at one zoom: too small when you pull back, "
            "absurd when you push in. <b>Zoom tiers</b> is the way out. Each "
            "tier is a second copy 4&times; larger that switches on as you "
            "zoom out, so the text keeps roughly the same size on screen "
            "across the whole range.</p>"
            "<table border='1' cellpadding='4' cellspacing='0'>"
            "<tr><th>Tiers</th><th>Ground sizes at 4 m</th>"
            "<th>Good for</th></tr>"
            "<tr><td>1</td><td>4 m</td>"
            "<td>One fixed scale, printed-map look</td></tr>"
            "<tr><td>3</td><td>4, 16, 64 m</td>"
            "<td>Default &mdash; parcel to district</td></tr>"
            "<tr><td>5</td><td>4 m to 1024 m</td>"
            "<td>Province-wide browsing</td></tr>"
            "</table>"
            "<p>All tiers point at the same PNG, so they cost XML, not image "
            "weight. <b>Render resolution</b> is sharpness only &mdash; how "
            "big the text looks is set entirely by text height on ground.</p>"

            "<h3>Placing rotated labels</h3>"
            "<p>Each label sits on the midpoint of its line, turned to the "
            "bearing of the segment that contains that midpoint. "
            "<b>Perpendicular offset</b> pushes it clear of the stroke &mdash; "
            "positive to the left of the direction the line runs, negative to "
            "the right. The offset scales with each tier, so the gap looks "
            "the same at every zoom. <b>Keep text right-way-up</b> spins any "
            "label turned past vertical, so westward lines do not come out "
            "upside down.</p>"

            "<h3>Where the labels come from</h3>"
            "<p>Whatever the layer&rsquo;s Labels tab is set to: a field, an "
            "expression, or the first labelled rule of a rule-based setup. "
            "KML prints HTML literally, so tags are stripped and "
            "<code>&lt;sup&gt;2&lt;/sup&gt;</code> becomes a real "
            "&sup2;.</p>"

            "<h3>What else travels</h3>"
            "<p>Line and fill colours are read from the layer&rsquo;s "
            "symbology, and every non-empty field goes into the balloon that "
            "opens when a feature is clicked &mdash; turn that off in "
            "<i>Advanced</i> for a much smaller file.</p>"

            "<h3>Watch out for</h3>"
            "<p><b>One colour per layer.</b> A categorised or graduated "
            "renderer collapses to its first symbol; split the layer if the "
            "classes have to stay apart in Google Earth.</p>"
            "<p><b>Everything is clamped to the ground</b> at altitude 0. "
            "Z values are not carried across.</p>"
            "<p><b>Rotated labels are images.</b> Thousands of distinct "
            "label texts mean thousands of PNGs inside the .kmz. Repeated "
            "text is rendered once and shared, so it is the number of "
            "<i>different</i> strings that matters.</p>"
            "<p><b>Files are overwritten</b> without asking when a name "
            "already exists in the output folder.</p>"
        )

    def createInstance(self):
        return ExportToKML()

    def tr(self, string):
        return QCoreApplication.translate("Processing", string)

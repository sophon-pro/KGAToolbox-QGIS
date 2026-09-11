# -*- coding: utf-8 -*-
"""
The pieces the *Modify Features* tools work from.

Geometry that has to happen somewhere real - a distance measured in metres on
a layer stored in degrees, a click turned into the features under it, values
carried from one layer into another - written once, as plain functions over a
canvas and a layer. `gui/modify_dialog.py` builds the tools on top of these,
and the algorithms reach past it for the two or three they need directly.

This was a `QDockWidget` base class until those tools stopped being panes.
The pane read a selection somebody else had made with the QGIS select tool,
which meant it depended on a chain of things it did not own, and when a link in
that chain broke it went on looking correct while doing nothing at all. The
tools carry their own map tool now; what is left here is the part that was
never the problem.
"""

from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QMessageBox

from qgis.core import (
    QgsCoordinateTransform,
    QgsFeatureRequest,
    QgsGeometry,
    QgsProject,
    QgsVectorLayerUtils,
)

from ..core import schema
from ..core.modify_features import Workspace

#: What the traceback from a failed apply is logged under.
LOG_TAG = 'KGA Toolbox'

#: The Pro preview colour: a bright cyan outline over a barely-there fill.
PREVIEW_COLOUR = QColor(0, 190, 220)
PREVIEW_FILL = QColor(0, 190, 220, 45)


def workspace_hint_for(canvas, layer):
    """A point in `layer`'s own CRS for `Workspace` to pick a UTM zone by.

    The canvas centre is the right place to work around - it is what the user
    is looking at - but it is a point in the *canvas* CRS, and `Workspace`
    reads its hint as a point in the *layer* CRS. Handing it over untransformed
    picks the zone for the wrong longitude: a layer in degrees under a Web
    Mercator project (which is what adding any web basemap does) sends an
    easting of eleven million through as a longitude and lands on zone 60, so a
    50 m offset comes out 18 m long.

    Falls back to the layer's own extent when the canvas has no extent yet or
    the transform will not go through, and to None - which leaves `Workspace`
    to ask the layer itself - when there is nothing to go on.
    """
    if layer is None:
        return None

    try:
        extent = canvas.extent()
        centre = None if extent.isEmpty() else extent.center()
    except Exception:                       # pragma: no cover - no canvas
        centre = None

    if centre is not None:
        canvas_crs = canvas.mapSettings().destinationCrs()
        if not canvas_crs.isValid() or canvas_crs == layer.crs():
            return centre
        try:
            return QgsCoordinateTransform(
                canvas_crs, layer.crs(), QgsProject.instance()).transform(centre)
        except Exception:                   # pragma: no cover - out of domain
            pass

    layer_extent = layer.extent()
    return None if layer_extent.isEmpty() else layer_extent.center()


def workspace_for(canvas, layer, unit_name):
    """The `Workspace` a typed distance is measured in, built around `canvas`.

    Every tool that takes a distance or an area goes through here, so the
    centre it is built around is worked out once and correctly. Callers are
    expected to look at `fell_back` and say so: if a real-world unit was asked
    for on a layer stored in degrees and no projected CRS could be found, the
    typed number is quietly read as degrees.
    """
    return Workspace(layer, unit_name, workspace_hint_for(canvas, layer))


def default_unit_index(units, layer):
    """Meters for a layer in degrees, map units for anything projected.

    A distance typed against a layer stored in degrees is almost never meant to
    be degrees, and starting on "Map units" there would offset the first
    feature clean off the map.
    """
    if layer is not None and layer.crs().isValid() and layer.crs().isGeographic():
        index = units.findData('Meters')
        if index >= 0:
            return index
    return 0


def canvas_to_layer(canvas, layer):
    """Canvas CRS -> layer CRS, or None when they are the same."""
    canvas_crs = canvas.mapSettings().destinationCrs()
    if not canvas_crs.isValid() or canvas_crs == layer.crs():
        return None
    return QgsCoordinateTransform(canvas_crs, layer.crs(),
                                  QgsProject.instance())


def layer_to_canvas(canvas, layer):
    """Layer CRS -> canvas CRS, or None when they are the same.

    The way back, for a tool that reads something off a layer and then has to
    draw it: a rubber band is a canvas object and takes canvas coordinates,
    whichever layer the geometry in it came out of.
    """
    canvas_crs = canvas.mapSettings().destinationCrs()
    if not canvas_crs.isValid() or canvas_crs == layer.crs():
        return None
    return QgsCoordinateTransform(layer.crs(), canvas_crs,
                                  QgsProject.instance())


def pick_order(feature):
    """Sort key that puts saved features ahead of ones added this session.

    A feature added to a layer that is being edited is given a *negative* id,
    and they run downwards - the first is -2, the next -3 - so plain ascending
    id order hands back the most recently added feature first. That is what
    turned Buffer into a ratchet: the buffer written by one click was the first
    thing the next hover found, so it was buffered again, and again, each ring
    a distance wider than the last.

    Saved features come first, in id order, and the unsaved ones after them,
    oldest first. Deterministic either way, which is the other thing this is
    for: a set comes back in whatever order the provider likes, and two runs
    over the same click have to agree.
    """
    fid = feature.id()
    return (0, fid) if fid >= 0 else (1, -fid)


def features_touching(canvas, layer, geometry, with_attributes=False, skip=()):
    """Features of `layer` that `geometry` - drawn in canvas CRS - touches.

    `skip` is a set of feature ids to leave out - what the tool has written
    into this layer itself, which it must not then read back as input.
    """
    if layer is None or geometry is None or geometry.isEmpty():
        return []

    search = QgsGeometry(geometry)
    transform = canvas_to_layer(canvas, layer)
    if transform is not None:
        try:
            search.transform(transform)
        except Exception:                   # pragma: no cover - out of domain
            raise ValueError('That part of the map cannot be projected into '
                             '"{}".'.format(layer.name()))

    engine = QgsGeometry.createGeometryEngine(search.constGet())
    engine.prepareGeometry()

    request = QgsFeatureRequest().setFilterRect(search.boundingBox())
    if not with_attributes:
        request.setNoAttributes()

    hits = []
    for feature in layer.getFeatures(request):
        if feature.id() in skip:
            continue
        candidate = feature.geometry()
        if candidate is None or candidate.isNull() or candidate.isEmpty():
            continue
        if engine.intersects(candidate.constGet()):
            hits.append(feature)
    return sorted(hits, key=pick_order)


def make_feature(layer, geometry, attributes=None):
    """A feature `layer` will accept, with its own defaults filled in.

    `QgsVectorLayerUtils.createFeature` rather than a bare `QgsFeature` so that
    provider-side defaults, unique-value constraints and the primary key are
    handled the way they are when the feature is digitized by hand.
    """
    context = layer.createExpressionContext()
    return QgsVectorLayerUtils.createFeature(
        layer, geometry if geometry is not None else QgsGeometry(),
        attributes or {}, context)


def carry_attributes(feature, source_layer, target_layer):
    """`feature`'s values as a {field index: value} map for `target_layer`.

    Same layer: everything but the primary key travels. Different layer: fields
    are matched by name and values coerced, and anything that will not fit is
    left for the target's own default rather than forced in.
    """
    if target_layer is None:
        return {}
    target_fields = target_layer.fields()
    source_fields = source_layer.fields()

    try:
        owned = set(target_layer.primaryKeyAttributes() or [])
    except Exception:                       # pragma: no cover
        owned = set()

    values = {}
    for index, field in enumerate(target_fields):
        if index in owned:
            continue
        source_index = source_fields.lookupField(field.name())
        if source_index < 0:
            continue
        try:
            # `coerce` answers (value, note); the note is for a report, and
            # these tools carry values across rather than import them.
            value, _note = schema.coerce(feature.attribute(source_index), field)
        except Exception:
            continue                        # the target default is safer
        values[index] = value
    return values


def offer_to_edit(parent, title, layer):
    """Offer to start editing `layer`, the way every KGA editing tool does.

    Returns (started, message): the message is '' when the layer is editable
    and says what happened otherwise, so the caller can put it where its own
    user is looking.
    """
    if layer is None:
        return False, 'Choose a layer first.'
    if layer.isEditable():
        return True, ''
    answer = QMessageBox.question(
        parent, title,
        'The layer "{}" is not in edit mode.\n\nStart editing now?'.format(
            layer.name()),
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.Yes)
    if answer != QMessageBox.StandardButton.Yes:
        return False, 'Nothing written: "{}" is not being edited.'.format(
            layer.name())
    if not layer.startEditing():
        return False, 'Could not start editing "{}".'.format(layer.name())
    return True, ''


def layer_is_shown(layer):
    """Whether `layer` is ticked in the layer tree.

    Clicking works in a layer that is switched off - these tools read the layer
    directly - but nothing about it can be seen, so it is worth saying rather
    than leaving the user clicking at features that are not drawn.
    """
    try:
        node = QgsProject.instance().layerTreeRoot().findLayer(layer.id())
    except Exception:                       # pragma: no cover
        return True
    return True if node is None else node.isVisible()


def sip_module():
    """The sip module, under whichever of its names this build has it.

    `import sip` works on the QGIS 3 installers, which put PyQt5's copy on the
    path, and fails on a build that only has it as `PyQt5.sip` - where the
    plain import raises and everything hanging off it quietly stops working:
    `is_deleted` says a dead object is alive, and the band sweep compares
    wrapper identities instead of the objects behind them and takes back the
    preview a live tool is drawing with. `qgis.PyQt.sip` is the name that is
    right on both, so it is asked first.
    """
    try:
        from qgis.PyQt import sip
        return sip
    except Exception:                       # pragma: no cover - older shim
        pass
    try:
        import sip
        return sip
    except Exception:                       # pragma: no cover - no sip at all
        return None


def is_deleted(obj):
    """True once Qt has taken the C++ side of `obj` away."""
    sip = sip_module()
    if sip is None:                         # pragma: no cover
        return False
    try:
        return sip.isdeleted(obj)
    except Exception:
        return False


def object_address(obj):
    """The C++ object behind a wrapper, to tell two wrappers of one item apart.

    Qt hands back its own wrapper each time it is asked for an item, so `is`
    and `id()` compare the wrappers rather than the objects.
    """
    sip = sip_module()
    if sip is not None:
        try:
            return sip.unwrapinstance(obj)
        except Exception:                   # pragma: no cover - not a wrapper
            pass
    return id(obj)                          # pragma: no cover

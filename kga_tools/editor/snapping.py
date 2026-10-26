"""
Snapping for the KGA Editor: switch it on, with every snapping type.

Edit Session turns this on whenever the session is On, so a feature can always be snapped to
another one: vertices, segments, areas, centroids, middles of segments and line endpoints.
QGIS keeps these settings in the project, so they are written to the current project.
"""

from qgis.core import Qgis, QgsProject, QgsVectorLayer
from qgis.PyQt import sip


def all_types():
    """Every snapping type QGIS offers (Vertex, Segment, Area, Centroid, Middle of Segments, Line Endpoints)."""
    t = Qgis.SnappingType
    return t.Vertex | t.Segment | t.Area | t.Centroid | t.MiddleOfSegment | t.LineEndpoint


def enable_all_snapping(project=None):
    """
    Turn snapping on and tick every snapping type. In 'Advanced Configuration' mode (a setting per
    layer) every vector layer is switched on with every type as well.
    Returns True if anything had to be changed.
    """
    project = project or QgsProject.instance()
    config = project.snappingConfig()
    flags = all_types()
    changed = False
    if not config.enabled():
        config.setEnabled(True)
        changed = True
    if config.typeFlag() != flags:
        config.setTypeFlag(flags)
        changed = True
    if config.mode() == Qgis.SnappingMode.AdvancedConfiguration:
        for node in project.layerTreeRoot().findLayers():
            layer = node.layer()
            if not isinstance(layer, QgsVectorLayer) or sip.isdeleted(layer) or not layer.isSpatial():
                continue
            settings = config.individualLayerSettings(layer)
            if not settings.enabled() or settings.typeFlag() != flags:
                settings.setEnabled(True)
                settings.setTypeFlag(flags)
                config.setIndividualLayerSettings(layer, settings)
                changed = True
    if changed:
        project.setSnappingConfig(config)
    return changed

"""
The Modify Features tool registry.

Laid out like ArcGIS Pro's Modify Features pane: categories (Alignment, Reshape, Divide ...) each
holding tools, in Pro's order. Clicking a tile opens the tool inside the pane (see controller.py):
`map_tool=True` arms the map tool of that key and shows its page, `command=True` shows the page of a
command that acts on the selection, `algorithm=` shows that KGA algorithm's own window in the pane; a
tile with none of them is a placeholder - clicking it says it is not built yet. `run` is called with the
controller (`ctl.iface`, `ctl.start_tool(key)`, `ctl.open_command(key)`, `ctl.open_algorithm(key)`).
"""

import os
from collections import namedtuple

HERE = os.path.dirname(os.path.abspath(__file__))
OWN_ICONS = os.path.join(HERE, "icons")
KGA_ICONS = os.path.join(HERE, "..", "..", "icons")

# id: unique key; label: text under the icon (\n marks where it wraps); icon: path to an svg/png;
# tip: tooltip; run: callable(controller) that starts the tool
ModifyTool = namedtuple("ModifyTool", "id label icon tip run")


def _not_built(name):
    def run(ctl):
        ctl.iface.messageBar().pushInfo("Modify Features", f"{name} is not built yet.")
    return run


def _map_tool(key):
    """Arms one of the map tools and opens its page (the pane's controller owns them)."""
    def run(ctl):
        ctl.start_tool(key)
    return run


def _command(key):
    """Opens the page of a command (Generalize, Explode ...) that acts on the selection."""
    def run(ctl):
        ctl.open_command(key)
    return run


def _embedded(key):
    """Opens a KGA algorithm's own window inside the pane."""
    def run(ctl):
        ctl.open_algorithm(key)
    return run


def _tool(key, label, tip, icon=None, algorithm=None, map_tool=False, command=False):
    """A tool tile. `icon` is a KGA toolbar icon file to reuse, else icons/<key>.svg; `algorithm` is the
    name of the KGA algorithm it opens; `map_tool` arms the map tool of the same key; `command` runs the
    command of the same key on the selection. With none of them the tile is a placeholder."""
    name = label.replace("\n", " ")
    path = os.path.normpath(os.path.join(KGA_ICONS, icon)) if icon else os.path.join(OWN_ICONS, key + ".svg")
    if map_tool:
        run = _map_tool(key)
    elif command:
        run = _command(key)
    else:
        run = _embedded(key) if algorithm else _not_built(name)
    return ModifyTool(key, label, path, f"{name} - {tip}", run)


CATEGORIES = [
    ("Alignment", [
        _tool("move", "Move", "drag to move the selected features, or type an offset in its pane",
              map_tool=True),
        _tool("rotate", "Rotate", "drag to rotate the selected features about an anchor, or type an angle "
              "in its pane", map_tool=True),
        _tool("scale", "Scale", "drag to scale the selected features about an anchor, or type a factor "
              "in its pane", map_tool=True),
        _tool("move_to", "Move To", "click to move the selected features to a location, or type coordinates "
              "in its pane", map_tool=True),
    ]),
    ("Reshape", [
        _tool("edit_vertices", "Edit\nVertices", "click a feature, then drag its vertices, double-click a segment to add "
              "one, Delete to remove one; the table lists and edits them", map_tool=True),
        _tool("reshape", "Reshape", "draw a line over features to reshape them",
              map_tool=True),
        _tool("replace_geometry", "Replace\nGeometry", "with features selected, click the feature whose "
              "shape they should take", map_tool=True),
        _tool("extend_trim", "Extend or\nTrim", "click a boundary, then lines to trim back to it or extend up "
              "to it", map_tool=True),
        _tool("fillet", "Fillet", "click two lines to join them with an arc of a chosen radius", map_tool=True),
        _tool("generalize", "Generalize", "remove vertices from the selected features within a tolerance",
              command=True),
        _tool("line_intersection", "Line\nIntersection", "click two lines to trim or extend both to their "
              "corner", map_tool=True),
        _tool("reverse_direction", "Reverse\nDirection", "hover a line to see its direction, click to reverse it",
              map_tool=True),
        _tool("change_line_length", "Change\nLine Length", "set the length of the selected lines, keeping the "
              "first or last vertex", command=True),
    ]),
    ("Divide", [
        _tool("split", "Split", "draw a line across features, or split target features by input features",
              map_tool=True),
        _tool("divide", "Divide", "divide polygons into parts by area, proportion or width", "divideFeatures.png",
              map_tool=True),
        _tool("clip", "Clip", "clip lines and polygons with other features", "clipFeatures.png", map_tool=True),
        _tool("explode", "Explode", "break multi-part features into single parts, or pull single parts out of them",
              map_tool=True),
        _tool("planarize", "Planarize", "cut the selected lines where they cross each other", "planarizeTool.png",
              map_tool=True),
    ]),
    ("Construct", [
        _tool("merge", "Merge", "merge features into one", "mergeFeatures.png", map_tool=True),
        _tool("buffer", "Buffer", "buffer the selected features into polygons or outlines", "bufferFeature.png",
              map_tool=True),
        _tool("copy_parallel", "Copy\nParallel", "copy the selected lines at a distance, on one or both sides",
              "copyParallel.png", map_tool=True),
        _tool("construct_polygons", "Construct\nPolygons", "build polygons from the selected lines that enclose an area",
              "constructPolygon.png", map_tool=True),
        _tool("offset", "Offset", "create points along a reference line at distances and offsets you type",
              map_tool=True),
        _tool("copy_paste", "Copy\nPaste", "copy features onto the KGA clipboard and paste them into any layer, "
              "choosing how the attributes carry across", "copyPaste.png", map_tool=True),
    ]),
    ("Attributes", [
        _tool("sequential_numbering", "Sequential\nNumbering", "draw a line across features to number them in order",
              "seqNumbering.png", map_tool=True),
        _tool("transfer_attributes", "Transfer\nAttributes", "with features selected, click the feature to copy "
              "attribute values from (fields matched by name)", map_tool=True),
    ]),
    ("COGO", [
        _tool("traverse", "Traverse", "click a start point, then type directions and distances (radius for a "
              "curve) to draw COGO lines", map_tool=True),
        _tool("update_cogo", "Update\nCOGO", "rewrite the COGO fields (Direction, Distance, Radius, ArcLength) of "
              "the selected lines from their geometry", map_tool=True),
        _tool("cul_de_sac", "Cul-de-sac", "click near the end of a line and give a radius to add a turnaround "
              "circle", map_tool=True),
        _tool("split_cogo_lines", "Split Into\nCOGO Lines", "split lines into COGO segments", "splitCogoLines.png",
              map_tool=True),
    ]),
]


def all_tools():
    return [tool for _name, tools in CATEGORIES for tool in tools]

# -*- coding: utf-8 -*-
"""Point to Geometry Converter: the engine (no GUI).

Ported from the stand-alone "Point to Geometry Converter" plugin (1.7.0).
Points are read once, grouped and ordered in memory, built into lines or
polygons, validated, and written in bulk from a ``QgsTask``:

    config      settings and result containers
    sequencer   pure-Python grouping / ordering / attribute rules
    builder     geometry construction, validation, repair
    engine      read -> group -> order -> build (no GUI, thread safe)
    writer      output planning and bulk writing
    task        the QgsTask: main-thread capture, worker run, main-thread load
    compat      QGIS 3.28+ / 4 enum shims used only here

The window is ``gui/point_geometry_dialog.py``; the Toolbox entry is
``algorithms/point_to_geometry_alg.py``.
"""

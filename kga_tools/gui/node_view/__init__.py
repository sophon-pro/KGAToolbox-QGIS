# -*- coding: utf-8 -*-
"""Vertices and nodes view: ArcGIS Pro's "show vertices and nodes" for the map canvas.

* ``engine``     - numpy: reads line / polygon geometries and finds vertices, nodes and midpoints;
* ``overlay``    - the canvas item that stamps the markers;
* ``controller`` - the switch (toolbar button, right-click entry, Settings > Editor), the background
  worker and the scheduling.
"""

from .controller import NodeViewController

__all__ = ['NodeViewController']

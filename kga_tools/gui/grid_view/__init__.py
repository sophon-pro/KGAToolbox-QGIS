# -*- coding: utf-8 -*-
"""Grid: ArcGIS Pro's map grid. Lines at a fixed spacing (and rotation) over the map.

* ``engine``     - spacing units, dynamic scaling and the line geometry (no Qt);
* ``overlay``    - the canvas item that draws the lines;
* ``popup``      - the small spacing / rotation panel under the button's arrow;
* ``controller`` - the switch (status-bar button and right-click menu), settings and panel.
"""

from .controller import GridController

__all__ = ['GridController']

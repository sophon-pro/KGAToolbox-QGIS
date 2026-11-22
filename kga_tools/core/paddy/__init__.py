# -*- coding: utf-8 -*-
"""Detect Paddy Field Plots from Imagery: the engine.

``models``  the two Delineate Anything models: where they live, checking them.
``detect``  one 512 x 512 chip through the YOLO segmentation model, decoded
            into field instances (boxes, scores, masks) with numpy alone.
``task``    the background job: imagery -> instances -> plots -> GeoPackage.
``style``   symbology for the plot and boundary layers.

The AI runtime (onnxruntime), the imagery sampler and the polygon helpers are
the road tool's (``core/roads``): the same install, the same metric grid.
"""

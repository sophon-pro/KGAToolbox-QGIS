# -*- coding: utf-8 -*-
"""Detect Roads from Imagery: the engine.

``runtime``   onnxruntime and the model: where they live, installing, loading.
``sampler``   turning any raster layer into RGB blocks on a fixed metric grid.
``infer``     running the model chip by chip and stitching the probabilities.
``skeleton``  thinning a mask and tracing it into a road graph.
``vectorize`` surface polygons, edges and widths from the mask.
``task``      the background job and what it writes.
"""

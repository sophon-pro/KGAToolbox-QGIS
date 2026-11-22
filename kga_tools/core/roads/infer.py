# -*- coding: utf-8 -*-
"""The model, chip by chip, stitched back into one probability block.

The model takes a fixed 256 x 256 chip. A block is covered by chips that
overlap by a quarter; each chip's answer is weighted down towards its edges,
where it sees least, and the weighted answers are averaged. That removes the
seams a plain grid of chips leaves.
"""

import numpy as np

#: Share of a chip that overlaps its neighbour on each side.
OVERLAP = 0.25


def _ramp(size, edge):
    """1-D weights: low at both ends, 1 in the middle."""
    weights = np.ones(size, dtype=np.float32)
    ramp = np.linspace(0.05, 1.0, edge, dtype=np.float32)
    weights[:edge] = ramp
    weights[-edge:] = ramp[::-1]
    return weights


def chip_weights(chip):
    edge = max(1, int(chip * OVERLAP))
    line = _ramp(chip, edge)
    return np.outer(line, line)


def positions(length, chip, stride):
    """Chip starts along one axis, the last one flush with the end."""
    if length <= chip:
        return [0]
    starts = list(range(0, length - chip, stride))
    starts.append(length - chip)
    return starts


def sigmoid(values):
    return 1.0 / (1.0 + np.exp(-np.clip(values, -30.0, 30.0)))


class Predictor(object):
    """Runs one ONNX session over RGB blocks."""

    def __init__(self, session, model):
        self.session = session
        self.input = model['input']
        self.chip = int(model['chip'])
        self.stride = max(1, int(self.chip * (1.0 - OVERLAP)))
        self.weights = chip_weights(self.chip)

    def predict(self, rgb, valid, stop=None):
        """Road probability, float32 0..1, for an RGB block.

        A block smaller than a chip is padded. Chips with no valid pixel are
        skipped and stay 0. `stop()` is asked between chips; when it answers
        True, None is returned.
        """
        rows, cols = valid.shape
        chip = self.chip
        if rows < chip or cols < chip:
            pad_r, pad_c = max(0, chip - rows), max(0, chip - cols)
            rgb = np.pad(rgb, ((0, pad_r), (0, pad_c), (0, 0)))
            valid = np.pad(valid, ((0, pad_r), (0, pad_c)))
        height, width = valid.shape
        total = np.zeros((height, width), dtype=np.float32)
        weight = np.zeros((height, width), dtype=np.float32)
        image = rgb.astype(np.float32) / 255.0
        for top in positions(height, chip, self.stride):
            if stop is not None and stop():
                return None
            for left in positions(width, chip, self.stride):
                window = valid[top:top + chip, left:left + chip]
                if not window.any():
                    continue
                tile = image[top:top + chip, left:left + chip]
                batch = np.ascontiguousarray(
                    tile.transpose(2, 0, 1)[np.newaxis])
                logits = self.session.run(None, {self.input: batch})[0]
                prob = sigmoid(logits[0, 0])
                total[top:top + chip, left:left + chip] += prob * self.weights
                weight[top:top + chip, left:left + chip] += self.weights
        out = np.where(weight > 0, total / np.maximum(weight, 1e-6), 0.0)
        out[~valid] = 0.0
        return out[:rows, :cols].astype(np.float32)

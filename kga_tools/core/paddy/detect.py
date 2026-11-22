# -*- coding: utf-8 -*-
"""One chip through the field model, decoded into field instances.

The model is a YOLO instance-segmentation network exported to ONNX. For a
1 x 3 x 512 x 512 RGB chip (0..1) it answers two arrays:

* ``preds``  1 x (4 + classes + 32) x anchors: a box (centre x, centre y,
  width, height, in chip pixels), a score per class, and 32 mask
  coefficients per anchor;
* ``protos`` 1 x 32 x 128 x 128: mask prototypes. An instance's mask is its
  coefficients times the prototypes, enlarged to the chip and cut to its box.

The decoding follows the model authors' own ONNX code (``openeo_udp/udf/
delineate_onnx.py`` in their repository) and Ultralytics' ``process_mask``:
class-agnostic NMS on the boxes, then the mask logits enlarged bilinearly
and cut at 0 (a probability of one half). Only each box's window is enlarged,
not the whole chip, which keeps a chip with 300 fields fast.
"""

import numpy as np

#: Ultralytics' defaults, kept so the model behaves as its authors ran it.
IOU_THRESHOLD = 0.7
MAX_NMS = 30000
MAX_DET = 300
#: Grey the model was trained to see as "nothing" (letterbox padding).
PAD_GREY = 114


class Instance(object):
    """One field found in a chip: score, box and a mask inside the box."""

    __slots__ = ('score', 'top', 'left', 'mask')

    def __init__(self, score, top, left, mask):
        self.score = float(score)
        self.top = int(top)
        self.left = int(left)
        #: bool array, the box's window of the chip.
        self.mask = mask

    def centre(self):
        rows, cols = np.nonzero(self.mask)
        return self.top + rows.mean(), self.left + cols.mean()


def nms(boxes, scores, iou):
    """Indices of the boxes kept, best first (class-agnostic)."""
    if not len(boxes):
        return np.empty((0,), dtype=np.int64)
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
    order = np.argsort(-scores)
    keep = []
    while order.size:
        first = order[0]
        keep.append(int(first))
        rest = order[1:]
        if not rest.size:
            break
        width = np.maximum(0.0, np.minimum(x2[first], x2[rest])
                           - np.maximum(x1[first], x1[rest]))
        height = np.maximum(0.0, np.minimum(y2[first], y2[rest])
                            - np.maximum(y1[first], y1[rest]))
        inter = width * height
        union = areas[first] + areas[rest] - inter
        overlap = np.where(union > 0.0, inter / np.maximum(union, 1e-9), 0.0)
        order = rest[overlap <= iou]
    return np.asarray(keep, dtype=np.int64)


def _axis(start, stop, size_out, size_in):
    """Bilinear sample positions along one axis for output pixels
    start..stop-1 of `size_out`, from an input of `size_in` (align_corners
    False, as torch.nn.functional.interpolate)."""
    pos = (np.arange(start, stop, dtype=np.float32) + 0.5) * \
        (size_in / float(size_out)) - 0.5
    pos = np.clip(pos, 0.0, size_in - 1)
    low = np.floor(pos).astype(np.int32)
    high = np.minimum(low + 1, size_in - 1)
    return low, high, pos - low


def window_logits(logits, top, bottom, left, right, size):
    """Bilinear enlargement of a low-resolution logit map to `size` x `size`,
    computed for the window [top:bottom, left:right] only."""
    rows_in, cols_in = logits.shape
    y0, y1, wy = _axis(top, bottom, size, rows_in)
    x0, x1, wx = _axis(left, right, size, cols_in)
    wy = wy[:, None]
    wx = wx[None, :]
    a = logits[y0[:, None], x0[None, :]]
    b = logits[y0[:, None], x1[None, :]]
    c = logits[y1[:, None], x0[None, :]]
    d = logits[y1[:, None], x1[None, :]]
    upper = a * (1.0 - wx) + b * wx
    lower = c * (1.0 - wx) + d * wx
    return upper * (1.0 - wy) + lower * wy


class FieldDetector(object):
    """Runs one ONNX session over 512 x 512 RGB chips."""

    def __init__(self, session, chip=512, confidence=0.15, iou=IOU_THRESHOLD):
        self.session = session
        self.input = session.get_inputs()[0].name
        self.chip = int(chip)
        self.confidence = float(confidence)
        self.iou = float(iou)

    def run(self, rgb, valid):
        """Field instances in one chip.

        `rgb` is uint8 (chip, chip, 3) and `valid` a bool (chip, chip) array.
        Pixels without imagery are shown to the model as its padding grey, and
        cut out of every mask afterwards.
        """
        size = self.chip
        image = rgb.astype(np.float32)
        if not valid.all():
            image[~valid] = PAD_GREY
        batch = np.ascontiguousarray(
            (image / 255.0).transpose(2, 0, 1)[np.newaxis])
        outputs = self.session.run(None, {self.input: batch})
        preds = protos = None
        for out in outputs:
            if out.ndim == 4:
                protos = out
            elif out.ndim == 3:
                preds = out
        if preds is None or protos is None:
            raise ValueError('The model answered in an unexpected shape: '
                             '{0}'.format([o.shape for o in outputs]))
        protos = protos[0].astype(np.float32)
        count, rows_in, cols_in = protos.shape
        preds = preds[0].astype(np.float32).T             # anchors x values
        classes = preds.shape[1] - 4 - count
        if classes <= 0:
            raise ValueError('The model answered {0} values per anchor; a '
                             'segmentation model gives more.'.format(
                                 preds.shape[1]))
        scores = preds[:, 4:4 + classes].max(axis=1)
        keep = scores >= self.confidence
        if not keep.any():
            return []
        preds = preds[keep]
        scores = scores[keep]
        order = np.argsort(-scores)[:MAX_NMS]
        preds, scores = preds[order], scores[order]
        centre_x, centre_y = preds[:, 0], preds[:, 1]
        half_w, half_h = preds[:, 2] / 2.0, preds[:, 3] / 2.0
        boxes = np.stack([centre_x - half_w, centre_y - half_h,
                          centre_x + half_w, centre_y + half_h], axis=1)
        chosen = nms(boxes, scores, self.iou)[:MAX_DET]
        if not chosen.size:
            return []
        coefs = preds[chosen, 4 + classes:]
        logits = (coefs @ protos.reshape(count, rows_in * cols_in)).reshape(
            -1, rows_in, cols_in)
        found = []
        for index, number in enumerate(chosen):
            x1, y1, x2, y2 = boxes[number]
            left = int(np.clip(np.floor(x1), 0, size - 1))
            top = int(np.clip(np.floor(y1), 0, size - 1))
            right = int(np.clip(np.ceil(x2), 0, size))
            bottom = int(np.clip(np.ceil(y2), 0, size))
            if right <= left or bottom <= top:
                continue
            mask = window_logits(logits[index], top, bottom, left, right,
                                 size) > 0.0
            mask &= valid[top:bottom, left:right]
            if mask.any():
                found.append(Instance(scores[number], top, left, mask))
        return found

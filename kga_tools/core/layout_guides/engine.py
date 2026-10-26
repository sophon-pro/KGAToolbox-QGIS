# -*- coding: utf-8 -*-
"""Layout Guides - geometry (pure Python, no QGIS needed).

All values are millimetres from the page's top-left corner.
A guide is (orientation, position, kind):
    "v" = vertical line (X position), "h" = horizontal line (Y position)
    kind = "edge", "divide", "gap" or "center"
"""

EPS = 0.005


def default_settings():
    return {
        "left": 10.0, "right": 10.0, "top": 10.0, "bottom": 10.0,
        "same": True,       # one value for all four edges
        "cols": 1,          # vertical parts (1 = not divided)
        "rows": 1,          # horizontal parts
        "gap": 0.0,         # space between parts
        "center": False,    # center cross of the area inside the edges
        "auto": True,       # update the page while typing
        "mode": "equal",    # "equal" parts or "exact" distances
        # exact distances measured from each margin line, e.g. "50" or "50, 120"
        "from_left": "", "from_right": "", "from_top": "", "from_bottom": "",
    }


def parse_distances(text):
    """'50, 120' -> [50.0, 120.0]. Accepts commas, semicolons or spaces.
    Returns (values, bad_tokens)."""
    vals, bad = [], []
    for tok in str(text or "").replace(";", ",").replace(" ", ",").split(","):
        tok = tok.strip().lower().replace("mm", "")
        if not tok:
            continue
        try:
            v = float(tok)
        except ValueError:
            bad.append(tok)
            continue
        if v > 0:
            vals.append(v)
    return vals, bad


def clean_settings(s):
    out = default_settings()
    if isinstance(s, dict):
        for k in out:
            if k in s:
                out[k] = s[k]
    return out


def _split(start, length, parts, gap):
    """Return the boundaries between `parts` cells (and cell size)."""
    parts = max(1, int(parts))
    gap = max(0.0, float(gap))
    cell = (length - gap * (parts - 1)) / parts
    if cell <= 0:
        return None, cell
    lines = []
    for i in range(1, parts):
        end_prev = start + i * cell + (i - 1) * gap
        lines.append((end_prev, "divide"))
        if gap > 0:
            lines.append((end_prev + gap, "gap"))
    return lines, cell


def compute(page_w, page_h, settings):
    """Returns dict: guides, cell_w, cell_h, area (x, y, w, h), warning."""
    s = clean_settings(settings)
    out = {"guides": [], "cell_w": None, "cell_h": None, "area": None, "warning": ""}
    if page_w <= 0 or page_h <= 0:
        out["warning"] = "This page has no size."
        return out

    if s["same"]:
        left = right = top = bottom = max(0.0, float(s["left"]))
    else:
        left, right = max(0.0, float(s["left"])), max(0.0, float(s["right"]))
        top, bottom = max(0.0, float(s["top"])), max(0.0, float(s["bottom"]))

    aw, ah = page_w - left - right, page_h - top - bottom
    if aw <= 0 or ah <= 0:
        out["warning"] = "Edges are bigger than the page."
        return out
    out["area"] = (left, top, aw, ah)

    raw = [("v", left, "edge"), ("v", page_w - right, "edge"),
           ("h", top, "edge"), ("h", page_h - bottom, "edge")]

    if s["mode"] == "exact":
        gap = max(0.0, float(s["gap"]))
        problems = []
        # (key, orientation, start, direction, length, label)
        sides = (("from_left", "v", left, 1, aw, "left"),
                 ("from_right", "v", page_w - right, -1, aw, "right"),
                 ("from_top", "h", top, 1, ah, "top"),
                 ("from_bottom", "h", page_h - bottom, -1, ah, "bottom"))
        for key, o, start, sign, length, label in sides:
            vals, bad = parse_distances(s[key])
            if bad:
                problems.append("'%s' is not a number" % bad[0])
            for d in vals:
                if d >= length:
                    problems.append("%s mm from %s is outside the area" % (_n(d), label))
                    continue
                raw.append((o, start + sign * d, "divide"))
                if gap > 0 and d + gap < length:
                    raw.append((o, start + sign * (d + gap), "gap"))
        if problems:
            out["warning"] = "; ".join(problems[:2])
    else:
        vlines, cw = _split(left, aw, s["cols"], s["gap"])
        hlines, ch = _split(top, ah, s["rows"], s["gap"])
        if vlines is None or hlines is None:
            out["warning"] = "Gap is too big for this many parts."
        raw += [("v", p, k) for p, k in (vlines or [])]
        raw += [("h", p, k) for p, k in (hlines or [])]
        out["cell_w"] = cw if vlines is not None else None
        out["cell_h"] = ch if hlines is not None else None

    if s["center"]:
        raw += [("v", left + aw / 2.0, "center"), ("h", top + ah / 2.0, "center")]

    # keep guides strictly inside the page, remove duplicates
    final = []
    for o, p, k in sorted(raw, key=lambda g: (g[0], g[1])):
        limit = page_w if o == "v" else page_h
        if p <= EPS or p >= limit - EPS:
            continue  # on the page edge: QGIS already snaps there
        if final and final[-1][0] == o and abs(final[-1][1] - p) < EPS:
            continue
        final.append((o, round(p, 4), k))
    out["guides"] = final

    # sizes of the spaces between guides inside the margin (for the summary)
    def spans(o, a, b):
        cuts = sorted({a, b} | {p for oo, p, k in final if oo == o and a + EPS < p < b - EPS})
        return [round(y - x, 2) for x, y in zip(cuts, cuts[1:])]
    out["across"] = spans("v", left, page_w - right)
    out["down"] = spans("h", top, page_h - bottom)
    return out


def _n(v):
    return ("%.1f" % v).rstrip("0").rstrip(".")

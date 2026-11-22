"""
metadata_check.py - Indicator 1: product registry + naming conventions.

Text evidence is gathered from the file path, layer name, band description
and GDAL metadata, then matched against a registry of known global
elevation products and generic keywords.

Important: the word "DEM" on its own is WEAK evidence.  Several of the
most widely used "DEMs" (SRTM, Copernicus GLO-30, ALOS AW3D30, ASTER GDEM)
are, physically, surface models that include buildings and canopy.
"""

import re

# (pattern, score, label, note)   score > 0 => DSM-like, < 0 => bare earth
# Patterns are matched against a normalised lower-case string where every
# non-alphanumeric run is replaced by a single space.
PRODUCT_REGISTRY = [
    # ---- bare-earth products (strong) ---------------------------------
    (r"\bfabdem\b", -0.95, "FABDEM",
     "Forest And Buildings removed Copernicus DEM (bare earth)."),
    (r"\bdeltadtm\b", -0.90, "DeltaDTM", "Global coastal bare-earth DTM."),
    (r"\b3dep\b|\busgs ned\b|\bned\b", -0.80, "USGS 3DEP/NED",
     "USGS bare-earth elevation."),
    (r"\bcoastaldem\b", -0.75, "CoastalDEM", "Bias-corrected bare-earth model."),
    (r"\bmerit ?dem\b|\bmerit\b", -0.55, "MERIT DEM",
     "SRTM/AW3D with tree-height bias reduced (approximately bare earth)."),
    (r"\bedtm\b|\bopengeohub dtm\b", -0.80, "Ensemble DTM", "Bare-earth ensemble DTM."),
    # ---- surface products (strong / medium) ---------------------------
    (r"\bcopernicus\b|\bcop ?dem\b|\bcop ?30\b|\bcop ?90\b|\bglo ?30\b|\bglo ?90\b"
     r"|\bdem glo\b", 0.85, "Copernicus GLO-30/90",
     "Copernicus DEM is an X-band InSAR DSM (includes buildings/canopy)."),
    (r"\baw3d ?30\b|\baw3d\b|\balos world\b|\balos dsm\b|\balpsmlc\d*\b", 0.85, "ALOS AW3D30",
     "Optical stereo DSM."),
    (r"\bastgtm\b|\baster ?gdem\b|\baster\b", 0.65, "ASTER GDEM",
     "Optical stereo surface model."),
    (r"\btandem ?x\b|\btdx\b|\bworlddem\b", 0.85, "TanDEM-X / WorldDEM",
     "X-band InSAR DSM."),
    (r"\bnasadem\b", 0.55, "NASADEM",
     "Reprocessed SRTM; C-band partly penetrates canopy (DSM-like)."),
    (r"\bsrtm\b|\bsrtmgl\d\b|\bhgt\b|\b[13] ?arc v[1-3]\b", 0.55, "SRTM",
     "C-band InSAR; elevation lies between ground and canopy top (DSM-like)."),
]

GENERIC_KEYWORDS = [
    (r"\bdsm\b|\bsurface model\b|\bdigital surface\b|\bfirst return\b"
     r"|\bfirst ?ret\b|\bmax ?return\b", 0.75, "DSM keyword"),
    (r"\bdtm\b|\bterrain model\b|\bbare ?earth\b|\bground ?model\b"
     r"|\bground ?points?\b|\bclass ?2\b|\blast ?return\b", -0.75, "DTM/bare-earth keyword"),
    (r"\bdem\b|\belevation model\b", -0.15, "Generic 'DEM' keyword"),
]

# Derived products that are neither DEM nor DSM
NOT_ELEVATION = [
    (r"\bndsm\b|\bchm\b|\bcanopy height\b|\bheight above ground\b|\bhag\b",
     "Name suggests a normalised height model (nDSM/CHM), not an elevation model."),
]


def _normalise(text):
    return " " + re.sub(r"[^a-z0-9]+", " ", text.lower()) + " "


def evaluate_metadata(texts):
    """Score the textual evidence.

    Parameters
    ----------
    texts : dict  {source_name: text}

    Returns
    -------
    dict with keys: score (-1..1), weight (0..1), hits (list of str),
    warnings (list of str), product (str or None)
    """
    hits, warnings = [], []
    best_product, product_score = None, 0.0
    generic_scores = []

    for source, text in texts.items():
        if not text:
            continue
        norm = _normalise(str(text))
        for pat, score, label, note in PRODUCT_REGISTRY:
            if re.search(pat, norm):
                hits.append("{}: '{}' -> {} ({})".format(source, label,
                            "DSM" if score > 0 else "DEM", note))
                if abs(score) > abs(product_score):
                    best_product, product_score = label, score
        for pat, score, label in GENERIC_KEYWORDS:
            if re.search(pat, norm):
                hits.append("{}: {}".format(source, label))
                generic_scores.append(score)
        for pat, msg in NOT_ELEVATION:
            if re.search(pat, norm):
                warnings.append(msg)

    # A named product outranks generic keywords.  FABDEM file names often
    # also contain "copernicus"; FABDEM's larger |score| makes it win.
    if best_product is not None:
        score = product_score
        weight = 0.6 if abs(score) >= 0.8 else 0.45
    elif generic_scores:
        # Use the strongest generic keyword (DSM / DTM beat plain 'DEM')
        score = max(generic_scores, key=abs)
        weight = 0.35 if abs(score) >= 0.5 else 0.10
    else:
        score, weight = 0.0, 0.0

    return {
        "score": float(score),
        "weight": float(weight),
        "hits": sorted(set(hits)),
        "warnings": sorted(set(warnings)),
        "product": best_product,
    }

"""
report.py - Professional summary report (HTML / JSON / CSV).

The HTML deliberately uses table-based layout and inline colours only, so
it renders identically in QGIS (QTextBrowser, Qt rich-text subset), in any
web browser, and in the PDF export.
"""

import csv
import datetime
import html
import json


PALETTE = {
    "DSM": ("#92400E", "#FEF3C7", "#D97706"),          # text, background, accent
    "DEM": ("#065F46", "#D1FAE5", "#059669"),
    "INCONCLUSIVE": ("#374151", "#E5E7EB", "#6B7280"),
    "PARTIAL": ("#5B21B6", "#EDE9FE", "#7C3AED"),
    "Partial": ("#5B21B6", "#EDE9FE", "#7C3AED"),
    "Neutral": ("#374151", "#F3F4F6", "#9CA3AF"),
    "N/A": ("#6B7280", "#F9FAFB", "#D1D5DB"),
}
INK, MUTED, RULE, PAPER = "#111827", "#6B7280", "#E5E7EB", "#FFFFFF"


def _e(x):
    return html.escape(str(x))


def _badge(label):
    fg, bg, _ = PALETTE.get(label, PALETTE["Neutral"])
    return ('<table cellspacing="0" cellpadding="3"><tr><td bgcolor="{bg}" style="color:{fg};'
            'font-weight:bold;font-size:8pt;">&nbsp;{t}&nbsp;</td></tr></table>'
            .format(bg=bg, fg=fg, t=_e(label)))


def _score_bar(score, width=110):
    """Diverging bar: left half = DEM evidence, right half = DSM evidence."""
    half = width // 2
    n = int(round(abs(score) * (half - 2)))
    dem_c, dsm_c = PALETTE["DEM"][2], PALETTE["DSM"][2]
    if score < 0:
        cells = [(half - n, "#F3F4F6"), (n, dem_c), (2, "#9CA3AF"), (half - 2, "#F3F4F6")]
    else:
        cells = [(half - 2, "#F3F4F6"), (2, "#9CA3AF"), (n, dsm_c), (half - n - 2, "#F3F4F6")]
    tds = "".join('<td width="{}" bgcolor="{}" height="8"></td>'.format(w, c)
                  for w, c in cells if w > 0)
    return ('<table cellspacing="0" cellpadding="0" width="{}"><tr>{}</tr></table>'
            '<span style="color:{};font-size:8pt;">{:+.2f}</span>'
            .format(width, tds, MUTED, score))


def _conf_bar(conf, colour):
    filled = max(1, int(round(conf)))
    return ('<table cellspacing="0" cellpadding="0" width="100%"><tr>'
            '<td width="{f}%" height="10" bgcolor="{c}"></td>'
            '<td width="{r}%" height="10" bgcolor="#E5E7EB"></td></tr></table>'
            .format(f=filled, r=100 - filled, c=colour))


def build_html(result, title="DEM / DSM Diagnostic Report", include_card=True, compact=False):
    """Return a complete HTML document for an engine.Result.

    include_card=False omits the verdict card (the dock shows its own).
    compact=True stacks each indicator vertically for narrow panels."""
    r = result
    info = r.info
    cls = r.classification
    fg, bg, accent = PALETTE[cls]
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    full = {"DSM": "Digital Surface Model - includes buildings / vegetation",
            "DEM": "Digital Elevation Model - bare-earth terrain",
            "INCONCLUSIVE": "Evidence too weak for a reliable verdict - leans {}".format(r.lean),
            "PARTIAL": "Bare-earth model with residual vegetation / buildings (objects only "
                       "partly removed)"}[cls]

    out = []
    w = out.append
    w('<html><head><meta charset="utf-8"><title>{}</title></head>'.format(_e(title)))
    w('<body style="background:{p};color:{i};font-family:\'Segoe UI\',Arial,sans-serif;'
      'font-size:9pt;">'.format(p=PAPER, i=INK))

    # ---- header -------------------------------------------------------
    w('<table width="100%" cellspacing="0" cellpadding="0"><tr><td>'
      '<span style="font-size:15pt;font-weight:bold;">{}</span><br>'
      '<span style="color:{m};">{} &middot; {}</span></td>'
      '<td align="right" style="color:{m};font-size:8pt;">{}</td></tr></table>'
      .format(_e(title), _e(info.get("layer", "")), _e(info.get("scope", "")), now,
              m=MUTED))
    w('<hr style="border:0;border-top:1px solid {};">'.format(RULE))

    # ---- verdict card (stacked rows: reads well at any width) ----------
    if include_card:
        w('<table width="100%" cellspacing="0" cellpadding="10" bgcolor="{bg}" '
          'style="border:1px solid {ac};">'
          '<tr><td><span style="color:{fg};font-size:8pt;font-weight:bold;">CLASSIFICATION</span><br>'
          '<span style="color:{fg};font-size:22pt;font-weight:bold;">{head}</span><br>'
          '<span style="color:{fg};">{full}</span></td></tr>'
          '<tr><td><span style="color:{fg};font-size:8pt;font-weight:bold;">CONFIDENCE</span>'
          '&nbsp;&nbsp;<span style="color:{fg};font-size:16pt;font-weight:bold;">{conf:.0f}%</span>'
          '{bar}<span style="color:{fg};font-size:8pt;">Combined evidence score {s:+.2f} '
          '(-1 = DEM, +1 = DSM)</span></td></tr></table>'
          .format(bg=bg, ac=accent, fg=fg, head=_e(r.headline), full=_e(full),
                  conf=r.confidence, bar=_conf_bar(r.confidence, accent), s=r.combined_score))

    if r.reference:
        ref = r.reference
        rf = PALETTE.get(ref["classification"], PALETTE["Neutral"])
        w('<p style="margin-top:8px;">Reference layer <b>{}</b> &rarr; '
          '<span style="color:{};font-weight:bold;">{}</span> &nbsp;'
          '<span style="color:{};">(reference higher on {}% of pixels, lower on {}%; '
          'open-ground offset {:+.2f} m)</span></p>'
          .format(_e(info.get("reference_layer", "")), rf[0], _e(ref["classification"]),
                  MUTED, ref["higher_pct"], ref["lower_pct"], ref["offset_m"]))

    # ---- resampled-data banner -----------------------------------------
    if info.get("resampling_factor") and info.get("interpolated_surface"):
        w('<table width="100%" cellspacing="0" cellpadding="8" bgcolor="#E0F2FE"><tr><td '
          'style="color:#075985;"><b>Interpolated surface.</b> File pixels are {:.2f} m; the '
          'elevations vary linearly between points ~{:.0f} m apart (typical of a DTM built from '
          'ground points). Analysed at ~{:.0f} m; buildings and crowns remain resolvable.'
          '</td></tr></table>'.format(info["nominal_pixel_m"], info["pixel_size_m"],
                                      info["pixel_size_m"]))
    elif info.get("resampling_factor"):
        w('<table width="100%" cellspacing="0" cellpadding="8" bgcolor="#FEF9C3"><tr><td '
          'style="color:#713F12;"><b>Resampled data detected.</b> File pixels are {:.2f} m, '
          'but the real detail is only ~{:.0f} m (one measurement spans ~{} pixels). The '
          'layer was analysed at ~{:.0f} m; objects smaller than that cannot be resolved.'
          '</td></tr></table>'.format(info["nominal_pixel_m"], info["pixel_size_m"],
                                      info["resampling_factor"], info["pixel_size_m"]))

    # ---- indicator breakdown ----------------------------------------------
    w('<h3 style="margin-top:14px;">Indicator breakdown</h3>')
    if compact:
        for k, ind in enumerate(r.indicators, 1):
            w('<table width="100%" cellspacing="0" cellpadding="4" '
              'style="border-bottom:1px solid {rule};"><tr>'
              '<td><b>{k}. {name}</b></td><td align="right">{badge}</td></tr>'
              '<tr><td colspan="2"><span style="color:{m};font-size:8pt;">{q}</span><br>'
              '<b>{val}</b> &nbsp;<span style="color:{m};font-size:8pt;">weight {wt:.2f}</span>'
              '<br>{bar}<br>{find}</td></tr></table>'
              .format(rule=RULE, m=MUTED, k=k, name=_e(ind.name), badge=_badge(ind.verdict),
                      q=_e(ind.question), val=_e(ind.value_text), wt=ind.weight,
                      bar=_score_bar(ind.score), find=_e(ind.finding)))
    else:
        w('<table width="100%" cellspacing="0" cellpadding="6" style="border-collapse:collapse;">')
        w('<tr bgcolor="#F9FAFB">' + "".join(
            '<th align="left" style="color:{};font-size:8pt;border-bottom:1px solid {};">{}</th>'
            .format(MUTED, RULE, h) for h in
            ("#", "Indicator", "Measured value", "Finding", "Evidence", "Weight", "Verdict")) + '</tr>')
        for k, ind in enumerate(r.indicators, 1):
            w('<tr>'
              '<td valign="top" style="border-bottom:1px solid {rule};color:{m};">{k}</td>'
              '<td valign="top" style="border-bottom:1px solid {rule};"><b>{name}</b><br>'
              '<span style="color:{m};font-size:8pt;">{q}</span></td>'
              '<td valign="top" style="border-bottom:1px solid {rule};">{val}</td>'
              '<td valign="top" style="border-bottom:1px solid {rule};">{find}</td>'
              '<td valign="top" style="border-bottom:1px solid {rule};">{bar}</td>'
              '<td valign="top" style="border-bottom:1px solid {rule};color:{m};">{wt:.2f}</td>'
              '<td valign="top" style="border-bottom:1px solid {rule};">{badge}</td></tr>'
              .format(rule=RULE, m=MUTED, k=k, name=_e(ind.name), q=_e(ind.question),
                      val=_e(ind.value_text), find=_e(ind.finding), bar=_score_bar(ind.score),
                      wt=ind.weight, badge=_badge(ind.verdict)))
        w('</table>')

    # ---- multi-scale filter test ------------------------------------------
    ms = info.get("multiscale")
    if ms:
        w('<h3 style="margin-top:14px;">Multi-scale filter test</h3>')
        w('<p style="color:{};font-size:8pt;">The surface is opened (erosion + dilation) with '
          'growing windows. "Removed" = compact raised features lower than the window, as a '
          'share of flat terrain. A DSM loses its trees and houses abruptly at 5-16 m; a DEM '
          'changes little. TPI, focal SD and focal range are neighbourhood statistics at the '
          'same windows (reported, not scored separately).</p>'.format(MUTED))
        heads = ("Window (m)", "Removed &gt; 1 m", "Removed &gt; 4.5 m", "dZ p99.5 (m)",
                 "TPI p99.5 (m)", "Focal SD p99 (m)", "Focal range p99 (m)")
        w('<table width="100%" cellspacing="0" cellpadding="4" style="border-collapse:collapse;">'
          '<tr bgcolor="#F9FAFB">' + "".join(
              '<th align="left" style="color:{};font-size:8pt;border-bottom:1px solid {};">{}</th>'
              .format(MUTED, RULE, h) for h in heads) + '</tr>')
        for c in ms["curve"]:
            hot = c["removed_full_pct"] >= 0.1
            w('<tr{}>'.format(' bgcolor="#FEF3C7"' if hot else '') + "".join(
                '<td style="border-bottom:1px solid {};">{}</td>'.format(RULE, v) for v in (
                    "{:g}".format(c["scale_m"]), "{:.2f}%".format(c["removed_pct"]),
                    "{:.2f}%".format(c["removed_full_pct"]), c["p995_dz"], c["tpi_p995"],
                    c["focal_sd_p99"], c["focal_range_p99"])) + '</tr>')
        w('</table><p>Highest points: {:.0f}% collapse like trees / houses{}, {:.0f}% drop '
          'only slightly (small compact bumps), {:.0f}% stay in place ({} points).</p>'.format(
              ms["full_pct"], " (at ~{:g} m)".format(ms["median_collapse_scale_m"])
              if ms["median_collapse_scale_m"] else "", ms["low_pct"], ms["stable_pct"],
              ms["n_peaks"]))

    # ---- block grid ---------------------------------------------------------
    bs = info.get("block_summary")
    if bs:
        g = info.get("block_grid", {})
        c = bs["counts"]
        w('<h3 style="margin-top:14px;">Block grid ({} x {} blocks of ~{:.0f} m)</h3>'.format(
            g.get("nx", "?"), g.get("ny", "?"), (g.get("block_m") or [0])[0]))
        w('<table cellspacing="0" cellpadding="4"><tr>')
        for lab in ("OBJECTS", "MIXED", "BARE", "N/A"):
            col = {"OBJECTS": "#FEF3C7", "MIXED": "#EDE9FE", "BARE": "#D1FAE5", "N/A": "#F3F4F6"}[lab]
            w('<td bgcolor="{}">&nbsp;<b>{}</b> {}&nbsp;</td><td width="6"></td>'.format(col, c[lab], lab))
        w('</tr></table>')
        w('<table width="100%" cellspacing="0" cellpadding="5" style="border-collapse:collapse;'
          'margin-top:6px;"><tr bgcolor="#F9FAFB">' + "".join(
              '<th align="left" style="color:{};font-size:8pt;border-bottom:1px solid {};">{}</th>'
              .format(MUTED, RULE, h) for h in
              ("Criterion", "Median", "90th pct", "Blocks DSM-like", "Blocks DEM-like")) + '</tr>')
        for key, cr in bs["criteria"].items():
            w('<tr><td style="border-bottom:1px solid {r};">{n}</td>'
              '<td style="border-bottom:1px solid {r};">{m}</td>'
              '<td style="border-bottom:1px solid {r};">{p}</td>'
              '<td style="border-bottom:1px solid {r};">{a:.0f}%</td>'
              '<td style="border-bottom:1px solid {r};">{b:.0f}%</td></tr>'.format(
                  r=RULE, n=_e(cr["name"]), m=cr["median"], p=cr["p90"],
                  a=cr["dsm_like_pct"], b=cr["dem_like_pct"]))
        w('</table>')
        pk = bs.get("peaks") or {}
        if pk.get("n"):
            cov = pk.get("cover_pct")
            sp = pk.get("shape_pct") or {}
            w('<p><b>Highest cells:</b> in {} blocks the maximum cell stands a median {} m above '
              'the ground within 15 m; {:.0f}% stand more than 4.5 m above it (tree / roof '
              'height). They sit on {:.0f}% linear ridges (dikes / bunds / embankments), '
              '{:.0f}% broad tops (plots / mounds), {:.0f}% compact peaks.{}</p>'.format(
                  pk["n"], pk["median_dz"], pk["tall_pct"], sp.get("linear", 0),
                  sp.get("broad", 0), sp.get("compact", 0),
                                      " Orthophoto under them: {:.0f}% trees, {:.0f}% building "
                                      "footprints, {:.0f}% crops/grass, {:.0f}% roofs / bare / other."
                                      .format(cov["tree"], cov["building"], cov["crop/grass"],
                                              cov["roof/bare/other"]) if cov else ""))
        top = sorted([b for b in info.get("blocks", []) if b["label"] == "OBJECTS"],
                     key=lambda b: -b.get("score", 0))[:8]
        if top:
            w('<p style="color:{};font-size:8pt;">Strongest object blocks: {}</p>'.format(
                MUTED, ", ".join("r{} c{} (dZ {:.1f} m)".format(b["row"], b["col"], b["values"]["dz"])
                                 for b in top)))

    # ---- notes and caps ---------------------------------------------------
    if r.caps or r.notes:
        w('<h3 style="margin-top:14px;">Why the confidence is not higher</h3><ul>')
        for c in r.caps:
            w('<li>{}</li>'.format(_e(c)))
        for n in r.notes:
            w('<li>{}</li>'.format(_e(n)))
        w('</ul>')

    # ---- technical details -----------------------------------------------
    keys = [("layer", "Layer"), ("path", "Source"), ("size", "Raster size"), ("crs", "CRS"),
            ("nominal_pixel_m", "File pixel size (m)"), ("pixel_size_m", "Analysed pixel size (m)"),
            ("resampling_factor", "Resampled data: factor"), ("data_type", "Data type"),
            ("vertical_units", "Vertical units"), ("vertical_datum", "Vertical datum"),
            ("scope", "Analysis scope"), ("reference_layer", "Reference layer"),
            ("orthophoto", "Orthophoto"), ("building_footprints", "Building footprints"), ("tiles_used", "Tiles used"),
            ("pixels_analysed", "Pixels analysed"), ("flat_context_pct", "Flat context (%)"),
            ("noise_sigma_m", "Noise sigma (m)"), ("object_height_threshold_m", "Object height threshold (m)"),
            ("steep_slope_threshold_deg", "Edge-steepness threshold (deg)"),
            ("object_window_px", "Object window (px)"), ("runtime_s", "Runtime (s)")]
    w('<h3 style="margin-top:14px;">Technical details</h3>'
      '<table cellspacing="0" cellpadding="3">')
    for key, label in keys:
        if key in info and info[key] not in ("", None):
            w('<tr><td style="color:{};">{}</td><td>{}</td></tr>'.format(MUTED, label, _e(info[key])))
    for pth in info.get("diagnostic_rasters", []):
        w('<tr><td style="color:{};">Diagnostic raster</td><td>{}</td></tr>'.format(MUTED, _e(pth)))
    w('</table>')

    w('<p style="color:{};font-size:8pt;margin-top:14px;">How to read this report: each indicator '
      'scores from -1 (bare-earth evidence) to +1 (surface-object evidence) and is weighted by how '
      'reliable it is for this pixel size, noise level and terrain. Confidence is capped where the '
      'data cannot physically prove the answer (for example, a DSM of bare land is identical to a '
      'DEM). For decisive proof, compare against a second layer of the same area.</p>'
      .format(MUTED))
    w('</body></html>')
    return "".join(out)


def write_html(result, path):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(build_html(result))
    return path


def write_json(result, path):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(result.to_dict(), fh, indent=2, default=str)
    return path


def write_csv(result, path):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        wr = csv.writer(fh)
        wr.writerow(["classification", result.headline, "confidence_pct",
                     round(result.confidence, 1), "combined_score", round(result.combined_score, 3)])
        wr.writerow([])
        wr.writerow(["indicator", "measured_value", "raw_value", "score", "weight", "verdict", "finding"])
        for i in result.indicators:
            wr.writerow([i.name, i.value_text, round(i.value, 5), round(i.score, 3),
                         round(i.weight, 3), i.verdict, i.finding])
    return path


def text_summary(result):
    """Plain-text table for the command line / QGIS log."""
    lines = ["", "RESULT: {}   confidence {:.0f}%   (score {:+.2f})".format(
        result.headline, result.confidence, result.combined_score), ""]
    lines.append("{:<36} {:<34} {:>7} {:>6}  {}".format("Indicator", "Measured", "Score", "Wt", "Verdict"))
    lines.append("-" * 96)
    for i in result.indicators:
        lines.append("{:<36} {:<34} {:>+7.2f} {:>6.2f}  {}".format(
            i.name[:36], i.value_text[:34], i.score, i.weight, i.verdict))
    for c in result.caps + result.notes:
        lines.append("  * " + c)
    return "\n".join(lines)

# Reservoir Rating Curve

|  |  |
|---|---|
| **Algorithm ID** | `kga:reservoir_rating_curve` |
| **Group** | KGA Irrigation Tools |
| **Source** | `kga_tools/algorithms/ReservoirRatingCurve.py` |
| **Icon** | group icon (`icons/irrigation.png`) |
| **Type** | Batch algorithm |

## Overview

Tabulates the **total outflow** from a reservoir at every water level between the
lowest and highest DEM cell, and draws the stage–discharge curve. One CSV and one
PNG per DEM.

The DEM only sets the elevation range, and the boundary clips it. Discharge
itself comes entirely from the outlet definitions — no storage routing is
involved, so the curve is static. Pair it with
**[Storage Capacity Curve](storage_capacity_curve.md)** for the volume side.

### Formulas

- **Weir:** `Q = C × L × H^1.5`
- **Orifice and pipe:** `Q = Cd × A × √(2gH)`, with g = 9.81 m/s²

`H` = water level − activation elevation. An outlet contributes nothing while
`H ≤ 0`, which is what puts the kinks in the total curve.

### Typical coefficients

| Outlet | Coefficient |
|---|---|
| Sharp-crested weir | C = 1.84 |
| Broad-crested weir | C = 1.70 |
| Ogee spillway | C = 2.0–2.2 |
| Sharp-edged orifice or pipe | Cd = 0.61 |
| Rounded entry | Cd = 0.80–0.90 |

## Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| **DEM Raster Layer(s)** | Multiple raster layers | — | One curve per DEM. |
| **Reservoir Boundary (optional — leave blank for full DEM)** | Vector layer, optional | empty | |
| **Elevation Step (m)** | Number (min 0.001) | `0.1` | |
| **Outlet Definition Mode** | Enum | `Fixed slots — up to 3 outlets, defined below` | Or `CSV file — any number of outlets, from a table`. In CSV mode the three slots below are ignored. |
| **CSV Mode · Outlet Table (.csv)** | File (`*.csv`), optional | empty | Columns: `outlet_id`, `type`, `act_elev`, `LA`, `C_Cd`. |
| **Output Folder** | Folder destination | — | |

### Outlet slots 1, 2 and 3

Each slot repeats the same five controls. Slot 1 is enabled by default as a weir;
slots 2 and 3 are off, pre-filled as an orifice and a pipe.

| Parameter | Type | Slot 1 / 2 / 3 default | Description |
|---|---|---|---|
| **Outlet N · Enable** | Boolean | `True` / `False` / `False` | |
| **Outlet N · Type** | Enum | `Weir` / `Orifice` / `Pipe` | |
| **Outlet N · Activation Elevation (m)** | Number | `0.0` | The crest or centreline level. |
| **Outlet N · Size — L (m) or A (m²)** | Number (min 0.001) | `1.0` / `0.5` / `0.3` | Crest width **L** in metres for a weir, or opening area **A** in m² for an orifice or pipe. **Areas are not derived for you** — enter the computed value. |
| **Outlet N · Coefficient — C or Cd** | Number (min 0.001) | `1.84` / `0.61` / `0.61` | |

### Advanced

| Parameter | Type | Default | Description |
|---|---|---|---|
| **Plot · Elevation Axis Minimum / Maximum (m)** | Number | `0.0` (auto) | |
| **Plot · Discharge Axis Maximum (m³/s)** | Number | `0.0` (auto) | |
| **Plot · Elevation Tick Interval (m)** | Number | `0.0` (auto) | |
| **Plot · Discharge Tick Interval (m³/s)** | Number | `0.0` (auto) | |
| **Plot · Resolution (DPI)** | Integer (72–600) | `150` | |

### CSV table format

| outlet_id | type | act_elev | LA | C_Cd |
|---|---|---|---|---|
| 1 | Weir | 40.0 | 5.00 | 1.84 |
| 2 | Orifice | 35.0 | 0.20 | 0.61 |
| 3 | Pipe | 33.5 | 0.07 | 0.61 |

`outlet_id` must be unique — it names the output column. `type` must be exactly
`Weir`, `Orifice` or `Pipe`.

## How to use

1. Load the DEM and the reservoir boundary.
2. Open **KGA Irrigation Tools > Reservoir Rating Curve**.
3. Define the outlets. For up to three, fill in the slots on the form; for more,
   switch **Outlet Definition Mode** to CSV and supply a table.
4. Check that the **activation elevations are in the same vertical datum as the
   DEM.** This is the mistake that produces a flat zero curve.
5. Press **Run**.
6. To compare several reservoirs on identical axes, pin the axis ranges and tick
   intervals under *Advanced* and run again.

## Outputs

Per DEM, in the output folder:

- `rating_{DEM}_{boundary}.csv` — elevation, one column per outlet, and
  `Q_total`.
- `rating_{DEM}_{boundary}.png` — the curve, with each outlet's contribution
  shaded, its activation level marked, and `Q_total` crossed at every grid line
  for reading off.

## Notes and limits

- **Activation elevations must be in the same vertical datum as the DEM.** If
  they all sit above the DEM's highest cell the curve comes out flat at zero, and
  the log warns about it.
- **Areas are not derived for you.** For an orifice or pipe, `LA` is the opening
  area in m² — compute it from the diameter yourself.
- **No storage routing.** This is a static stage–discharge relationship, not a
  flood-routing result. It does not tell you how long the reservoir takes to draw
  down.
- The curve's kinks are real: they are where each outlet activates.

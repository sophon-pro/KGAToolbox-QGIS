# Create Points From Table

|  |  |
|---|---|
| **Algorithm ID** | `kga:create_points_from_table` |
| **Group** | KGA Data Management |
| **Source** | `kga_tools/algorithms/CreatePointsFromTable.py` |
| **Icon** | `icons/PointFromTable.png` |
| **Type** | Batch algorithm |

## Overview

Turns the coordinate columns of a table into a point layer. It reads `.csv`,
`.txt`, `.tsv`, `.xlsx`, `.xlsm`, `.xls` and `.ods` straight from disk — the
table does not have to be added to the project first, and nothing is written
back to it.

It exists because the QGIS "Add Delimited Text Layer" route falls over on real
survey exports: thousand separators, comma decimals, a title block above the
header, a sheet that is not the first one, columns that come out as text. This
tool handles those and says in the log what it did.

## Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| **Input Table** | File | — | The table to read: `.csv`, `.txt`, `.tsv`, `.xlsx`, `.xlsm`, `.xls` or `.ods`. Multi-sheet workbooks use the first sheet unless you name another under Advanced. |
| **X Column  (Easting or Longitude)** | String, optional | `X` | Header of the X column; matching ignores case. Not found, or left blank, and the tool looks for `x`, `easting`, `east`, `lon`, `longitude`, `xcoord`, `coord_x`, `point_x`, `utm_e`, then logs which it used. |
| **Y Column  (Northing or Latitude)** | String, optional | `Y` | Same, with fallbacks `y`, `northing`, `north`, `lat`, `latitude`, `ycoord`, `coord_y`, `point_y`, `utm_n`. |
| **Z Column  (Elevation, optional)** | String, optional | empty | Fill it in to build a 3D PointZ layer; blank leaves the output 2D. Fallbacks are `z`, `elev`, `elevation`, `height`, `altitude`, `level`, `rl`. Rows whose Z will not parse get a Z of 0 and are counted in the log rather than dropped. |
| **Coordinate Reference System** | CRS | `EPSG:32648` | The CRS the numbers **are already in**, not the one you want to end up in. `EPSG:32648` is WGS 84 / UTM zone 48N, which covers most of Cambodia; use `EPSG:4326` for degrees. |
| **Label Points With  (optional)** | String, optional | empty | Column to label the points with. The loaded layer arrives with labelling already on: 9 pt black, white buffer, placed around the point. |
| **Detect Numeric Columns** | Boolean | `True` | Types each column as Integer, Real or Text from what is in it, so graduated symbology, statistics and the field calculator work straight away. Leading-zero codes such as `007` stay Text on purpose. Untick to make every column Text. |
| **Skip Rows With Unusable Coordinates** | Boolean | `True` | On, unparseable rows are counted, left out, and the run finishes. Off, the first such row stops the run and names the offending value — which is what you want when the table is meant to be complete. |

### Advanced

| Parameter | Type | Default | Description |
|---|---|---|---|
| **Workbook · Worksheet** | String, optional | empty | Which sheet of an Excel/ODS workbook: its name, or its position starting at 1. Blank means the first sheet. Ignored for text files. |
| **Workbook · Header Row** | Integer (1–1000) | `1` | Row the column headers are on, counting from 1. Raise it for tables carrying a title block, project name or units above the real header; everything above that row is discarded. |
| **Text Files · Column Separator** | Enum | `Auto-detect` | `Auto-detect`, `Comma  ,`, `Semicolon  ;`, `Tab`, `Pipe  \|`, `Space`. Auto-detect samples the first few kilobytes; set it explicitly when a one-column file or quoted text full of commas fools it. Ignored for Excel and ODS. |
| **Text Files · Character Encoding** | Enum | `Auto-detect` | `Auto-detect`, `UTF-8`, `Windows-1252 (Western)`, `Windows-874 (Thai)`, `UTF-16`. Auto-detect tries UTF-8, then Windows-1252, then Latin-1, and logs which worked. Set it explicitly if Khmer or accented text comes out as mojibake — CSVs saved from Excel on Windows are usually Windows-1252. |
| **Output Point Layer** | Feature sink | temporary layer | Leave it temporary to check the result first, or write straight to GeoPackage or Shapefile. |

## How to use

1. Open **KGA Data Management > Create Points From Table**.
2. Choose the **Input Table**. Nothing needs to be loaded in QGIS first.
3. Leave **X**/**Y** at their defaults unless your headers are unusual — the
   tool finds the common names by itself, and the log names the column it chose.
4. Set the **Coordinate Reference System** to the system the numbers are already
   recorded in. This is the single most common source of a wrong result.
5. If the table carries a title block above the real header, raise
   **Workbook · Header Row** under *Advanced*.
6. Press **Run**, then check the log's `Format / Encoding / Separator /
   Worksheet` block and any warnings before trusting the points.

## Outputs

- A point layer (PointZ when a Z column is given) in the chosen sink.
- Every column of the table carried across as an attribute, typed per column when
  *Detect Numeric Columns* is on.
- Labelling already switched on when a label column was named — but only for a
  layer added to the project; writing straight to a file carries geometry and
  attributes, never style.
- A log block naming the format, encoding, separator and worksheet actually used,
  plus counts of skipped rows.

## Notes and limits

- **Messy numbers are handled, with one ambiguity.** `1,407,186.000`,
  `1 407 186,25` and a padded ` 556120 ` all read correctly. `1407186,000` is the
  awkward case — 1407186.0 in Europe, 1,407,186,000 elsewhere. The decision is
  made once per column from whichever value in it can only be read one way, and
  the log names any column read as comma-decimal. Glance at that when a
  coordinate looks a thousand times too big.
- **The tool warns when the numbers do not suit the CRS** — degrees-sized values
  under a projected CRS, or metre-sized values under a geographic one. Read the
  warning; it usually means the CRS parameter is wrong.
- **Shapefile output truncates field names** to 10 characters and caps the layer
  at 255 columns. GeoPackage does neither.
- `.xls` and `.ods` are read through **pandas**; `.xlsx`/`.xlsm` prefer
  **openpyxl** and fall back to pandas. If neither is installed in your QGIS, save
  the table as `.csv` instead — the error says so.
- Skipped-row messages are capped at 25 lines, so a badly broken file does not
  flood the log; the total count is still reported.
- A failed run raises an error instead of reporting success with no output.

# Add Grid to Layout

|  |  |
|---|---|
| **Algorithm ID** | `kga:add_grid` |
| **Group** | KGA Mapping |
| **Source** | `kga_tools/algorithms/add_grid_alg.py` (dialog in `kga_tools/gui/add_grid_dialog.py`, settings model in `kga_tools/core/layout_grid.py`) |
| **Icon** | `icons/addGrid.png` |
| **Type** | Interactive dialog — modeless, needs the QGIS window and a print layout |

## Overview

Put a **coordinate grid** on a print layout map from one window.

QGIS can already do all of this. What it cannot do is do it quickly: the grid
lives in the layout designer's item properties panel, behind *Grids*, a **+**
button, a *Modify grid…* button and five collapsible sections, and the interval,
the frame, the label format and each side's label orientation are in four
different places. This is the same settings in one form, with a **Suggest**
button that works the interval out from what the map is actually showing.

It also edits the grids a map already has, not only new ones, so it is the
faster way back in when an interval turns out wrong at print size.

And it draws an **MGRS** grid, which QGIS cannot do at all: there is no MGRS
anywhere in the QGIS API. See *The MGRS preset* below.

## Parameters

The algorithm takes no Processing parameters — running it opens the window. The
controls below are the window's, and rows it does not need are hidden: the
interval boxes disappear when the units are set to fit the page, the cross and
marker sizes appear only for those grid types, and the frame rows go when the
frame style is *No frame*.

### Which map

| Control | Type | Default | Description |
|---|---|---|---|
| **Layout** | Combo box | The layout whose map is selected | Every print layout in the project. The **Open** button beside it opens that layout in the designer, or brings its designer to the front. |
| **Map item** | Combo box | The selected map | The map items of that layout, by name. |
| **Grid** | Combo box | `<Add a new grid>` | Also lists the grids the map already has, as `Edit: <name>`. Picking one loads its settings into the form. |
| **Reload** | Button | — | Read the layouts, maps and grids again, after adding or removing one in the designer. |
| **Grid name** | Text | `Grid` | What the grid is called in the layout designer's grid list. A name already on that map gets ` 2` appended rather than overwriting. |
| **Grid interval** | Combo box | Automatic | Only shown while the MGRS set is armed. `Automatic` works the spacing out from the map extent; `10 km`, `1 km`, `100 m`, `10 m` and `1 m` name it instead, which is what a sheet drawn to a stated scale needs — a 1:50,000 map carries a 1 km grid whatever its extent happens to suggest. |
| **Also add** | Two check boxes | Both off | Only shown while the MGRS set is armed. **1 km mesh** adds the fine mesh of a 1:50,000 sheet; **Labels inside too** repeats the numbers against the inside of the frame. |
| **Positioning** | Three toggle buttons | Two strips per axis, at 1/4 and 3/4 | Where the interior number strips go: `One strip per axis, centred`, `Two strips per axis, at 1/3 and 2/3`, or `Two strips per axis, at 1/4 and 3/4`. Shown while the MGRS set is armed or the map already has strips, and enabled once **Labels inside too** is ticked. Changing it re-places the strips already on the map. |
| **100 km letters** | Combo box | In the margin | Only shown while the MGRS set is armed. `In the margin, outside the numbers` or `Inside the map, against the frame`. |
| **Preset** | Combo box | `Start from...` | Fills the whole form in one pick: *Thin black graticule*, *Exterior ticks, labels outside*, *Zebra frame, labels outside*, *Lat/long graticule in degrees*, *Crosses, labels inside*. It also re-suggests the interval, because a preset can change the CRS. Below the separator sits **MGRS grid**, which is not a preset but a *set* — see *The MGRS preset*. |

### Grid tab

| Control | Type | Default | Description |
|---|---|---|---|
| **Grid type** | Combo box | Solid lines | `Solid lines`, `Crosses`, `Markers`, `Frame and labels only`. |
| **Coordinate system** | CRS selector | Not set | Not set grids the map in its own CRS. Set it to EPSG:4326 for a latitude/longitude graticule over a projected map. Changing it re-suggests the interval. |
| **Interval units** | Combo box | Map units | `Map units`, `Millimetres on the page`, `Centimetres on the page`, `Fit the page automatically`. |
| **Interval** | Two numbers + **Suggest** | 1000 | X and Y spacing, in the chosen units. **Suggest** fills both with a round number — 1, 2, 2.5 or 5 times a power of ten — that cuts the map into about five columns and rows. |
| **Offset** | Two numbers | 0 | Shifts the grid origin, for lines that have to land on a particular coordinate. |
| **Spacing on the page** | Two numbers | 50–100 mm | *Fit the page automatically* only. QGIS recomputes the interval at each render so the spacing stays between these two widths. |
| **Line colour** | Colour button | Black | Opacity included. Also the marker colour. |
| **Line width** | Number | 0.2 mm | Solid lines and crosses. |
| **Cross size** | Number | 3 mm | Crosses only. |
| **Marker size** | Number | 2 mm | Markers only. |

### Frame tab

| Control | Type | Default | Description |
|---|---|---|---|
| **Frame style** | Combo box | Exterior ticks | `No frame`, `Zebra`, `Zebra (nautical)`, `Interior ticks`, `Exterior ticks`, `Interior and exterior ticks`, `Line border`, `Line border (nautical)`. |
| **Frame size** | Number | 2 mm | How far the frame or the ticks reach out from the map. |
| **Frame line thickness** | Number | 0.3 mm | |
| **Frame line colour** | Colour button | Black | |
| **Fill colour 1 / 2** | Colour buttons | Black / white | The two zebra bands. Shown for the zebra styles only. |
| **Draw on** | Four check boxes | All ticked | Left, Right, Top, Bottom. A frame on three sides is normal on a sheet where the fourth carries the legend. |

### Labels tab

| Control | Type | Default | Description |
|---|---|---|---|
| **Show coordinate labels** | Group check box | On | Off leaves the lines and the frame with no numbers. |
| **Number format** | Combo box | Decimal | `Decimal`, `Decimal with N/S/E/W`, `Degree, minute`, `Degree, minute (padded)`, `Degree, minute, second`, `Degree, minute, second (padded)`, plus four that label from an expression: `MGRS zone and band letter`, `MGRS 100 km square letter`, `MGRS principal digits` (two digits minimum, more on a grid finer than 1 km, with a raised 100 km prefix on every tenth line), `MGRS corner value` (the first line of each margin, written out in full) and `Custom expression`. |
| **UTM zone** | Integer (0–60) | `from the coordinate system` | *MGRS 100 km square letter* only. Which zone the letters belong to. At 0 it is read from the grid's own CRS, which is what the MGRS preset sets. |
| **Expression** | Text | — | *Custom expression* only. A QGIS expression, with `@grid_number` for the value of the line being labelled and `@grid_axis` for `'x'` or `'y'`. |
| **Label rule** | Read-only text | — | Shown for the four expression formats: exactly what will be written onto the grid. Change the interval and watch the divisor change with it. |
| **Decimals** | Integer (0–12) | 0 | On a degree/minute/second format, the decimals on the last part shown. **Hidden for the four expression formats** — QGIS does not consult it under a custom format, so leaving it on screen would be a control that does nothing. |
| **Distance from the frame** | Number | 1 mm | |
| **Font** | Font button | The QGIS default | Family, size and style. |
| **Label colour** | Colour button | Black | |
| **Each side** | Four rows | Show all, outside, horizontal | One row per side — Left, Right, Top, Bottom — with **Show** (`Show all`, `Latitude / Y only`, `Longitude / X only`, `Hide`), **Placement** (`Outside the frame`, `Inside the frame`) and **Orientation** (`Horizontal`, `Vertical, reading up`, `Vertical, reading down`, `Along the frame`, `Above the tick`, `On the tick`, `Under the tick`). |
| **Copy the Left row to every side** | Button | — | The common case — every side the same — in one press. |

### Buttons

| Button | Description |
|---|---|
| **Add MGRS Grid (4 grids)** | What **Add Grid** becomes once the MGRS set is picked. One press writes every grid in the set as one undo step, then leaves the tool editing the finest of them. Picking a different layout, map or grid puts the button back before it can fire at the wrong target. |
| **Add Grid** / **Apply** | Writes the form to the map. On a new grid it adds one and then selects it, so the next press changes that grid instead of stacking a second one beside it — which is why the button changes its name after the first press. |
| **Remove Grid** | Deletes the selected grid, after asking. |
| **Reset** | Every setting back to its default, then a fresh interval suggestion. |
| **Close** | The grid stays; nothing is undone. |
| **Help** | This page. |

## How to use

The tool needs a print layout with a map item on it. Run it without one and it
says so and stops — see *Notes and limits*.

1. Open the layout in **Project > Layouts**, and click the map item to select it.
2. Back in the main window, run **KGA Mapping > Add Grid to Layout**. The
   window opens on that layout and that map, **in front of the layout
   designer** rather than behind it, so the form and the sheet are on screen
   together.
3. Pick a **Preset** if one is close to what you want.
4. Press **Suggest** to get a round interval for the scale the map is at.
5. Set the frame and the label sides, then press **Add Grid**.
6. Look at the sheet, change a number, press **Apply** again. Repeat until it
   reads right at print size.

### The MGRS preset

A grid reference such as **48P VT 92 77** is four things: the zone number, the
latitude band letter, the 100 km square letters and the numeric location. They
are read off different grids, so the preset lays down four, plus up to three more you
can tick on (the 1 km mesh is one grid; the interior labels are two, one for
northings and one for eastings):

| Grid | What it draws | What it labels |
|---|---|---|
| `MGRS zone 48P` | A 6° x 8° graticule in EPSG:4326 | The zone number along the top and bottom, the band letter up the sides — **inside** the frame, since the margin is already carrying numbers |
| `MGRS 100 km squares` | A 100,000 m grid in the map's UTM zone | The square's column letter on the vertical lines, its row letter on the horizontal ones. **In the margin or inside the map**, your choice |
| `MGRS corner values` | Nothing — no lines, no frame | One label per margin: the first line's coordinate written out in full, so the short labels along the rest of the margin have something to be read against. Three parts, only the middle one full size — on a 10 km grid `¹26⁰⁰⁰⁰m.N.`, on a 1 km grid `¹²60⁰⁰⁰m.N.` for the same line. **It follows the map** — pan or zoom and it re-finds the first line by itself |
| `MGRS 1 km mesh` *(optional)* | A 1 km grid, lighter than the one above it | Nothing — see *Notes and limits* |
| `MGRS interior northing labels` and `MGRS interior easting labels` *(optional)* | Nothing — no lines, no frame | The same principal digits as the fine grid, repeated against the **inside** of the frame: northings on vertical strips, eastings along horizontal ones |
| `MGRS 1 km` (or 10 km, 100 m…) | The fine grid | The principal digits, **never fewer than two** — `12` on a 1 km grid, `51` on a 10 km grid — with the 100 km digits raised and small on every tenth line: `78 79 ⁴80 81` |

1. Select the map and run the tool as above.
2. Pick **MGRS grid** from the bottom of the **Preset** list. Nothing is written
   yet: the button changes to **Add MGRS Grid (4 grids)**, the status line
   names them, and a **100 km letters** row and an **Also add** row appear
   beside the preset. The button's count follows what you tick.
3. Set the font, colours and frame if you want them different — everything the
   set does not fix for itself is taken from the form when you press Add, so a
   colour picked now still reaches every grid in the set.
4. Choose whether the **100 km letters** go in the margin or inside the map.
   Inside keeps the margin to the numbers alone, which is how a printed sheet
   does it.
5. Set the **Grid interval** if `Automatic` is not what the sheet needs.
   Tick **1 km mesh** for the fine mesh a 1:50,000 sheet carries, and
   **Labels inside too** to repeat the numbers against the inside of the
   frame. Nothing is remembered between sessions; the window opens on the defaults.
6. Press **Add MGRS Grid**. One undo step; the tool is left editing the fine
   grid, and the others are in the **Grid** list.

The zone comes from the middle of the map. The interval comes from the extent
unless you name one, snapped down to 10 km / 1 km / 100 m / 10 m / 1 m — name
it when the sheet is drawn to a stated scale, because the extent of a
1:50,000 sheet does not always suggest the 1 km grid such a sheet should
carry. Everything else follows: the margin digits, the corner label's split,
and how many lines land on the page. The three margin
rows are stacked by hand — digits innermost, corner values outside them,
letters outside those — because QGIS measures every annotation distance from
the map frame rather than from the row before it.

For a latitude/longitude graticule over a projected map, set **Coordinate
system** to EPSG:4326 on the Grid tab — the interval re-suggests itself in
degrees — and set **Number format** to *Degree, minute* on the Labels tab. The
*Lat/long graticule in degrees* preset does all three.

## Outputs

- A grid on the chosen map item of the chosen print layout. Nothing is written
  to disk; the grid is part of the layout and is saved with the project.
- Adding, changing and removing are each **one undo step** in the layout
  designer, so Ctrl+Z there takes back exactly one press of Apply.

## Notes and limits

- **It checks there is something to grid before it opens.** A grid lives on a
  map item inside a print layout, so running the tool in a project with no
  layout — or with layouts that carry no map item — puts up a message saying
  which of the two is missing and how to fix it, and opens nothing. The message
  names the layout when there is only one.
- **MGRS is written in this plugin, because QGIS has none.** There is no MGRS
  in the QGIS C++ or Python API, `QgsCoordinateFormatter` has no
  grid-reference format, and the `mgrs` Python package is not part of a QGIS
  install. The maths is `kga_tools/core/mgrs.py`: WGS84 only, no UPS, and
  checked against PROJ (the projection agrees to well under a millimetre,
  and every 100 km square letter matches an independent derivation) and
  against the standard `4QFJ 12345 67890` example.
- **The GZD graticule draws nothing on most sheets.** Its lines are 6° apart
  across and 8° up, so on any map smaller than a zone cell — nearly all of
  them — there is no line to draw and no label to hang on it. The grid is
  still added, and is *named* for the designator it stands for, because that
  name is what the Grid list shows. On a single-zone sheet, put `48P` in a
  layout label instead.
- **Above 84° N and below 80° S there is no MGRS grid.** Those latitudes use
  UPS, which has its own letters and is not something this tool draws. The
  preset refuses and says so.
- **One sheet, one zone.** All the grids use the zone at the middle of the
  map. A map crossing a zone boundary is gridded anyway and warned about, in
  the window and in the Processing log, but the numbers and the 100 km letters
  are right only inside that centre zone. That is a property of MGRS, not of
  this tool.
- **The Norway and Svalbard zones are handled for the letters, not for the
  graticule.** The reference and the 100 km letters know that south-west
  Norway is zone 32 and that Svalbard runs 31/33/35/37. The GZD *graticule*
  still draws plain 6° cells, because a label expression on the x axis is
  handed only a longitude and can never know which band it is being drawn at.
- **The labels are an expression, so they survive without the plugin.** They
  are written as QGIS's own *Custom* annotation format with self-contained
  expression text — there is no registered function to go missing when the
  project is opened elsewhere. The cost is that QGIS's **Decimals** setting
  stops applying, which is why that row is hidden for these formats.
- **The margin never prints a single digit.** A 10 km grid only needs one digit
  to be unambiguous inside a 100 km square, but a margin of bare single digits
  reads as a ruler rather than as coordinates, and no printed sheet does it —
  so the labels are floored at two. Two digits span 1000 km, which is wider
  than a UTM zone, so the second one costs nothing. A 1 km grid reads
  `58 59 60 61 62` and a 10 km grid `44 45 46 47`, which is the convention on
  a 1:50,000 sheet.
- **The raised 100 km digits are Unicode superscripts.** `⁴80` is a superscript
  four followed by an ordinary 80 — QGIS gives a grid one font per label, so
  there is no way to set part of it smaller. ArcGIS Pro can, because its
  dynamic text takes `<FNT size>` runs; this is the closest QGIS gets, and it
  carries the same information. Nearly every font has these
  glyphs, but a font that does not will draw boxes; change the label font if
  you see them.
- **The corner label is split by the interval, not by the hundred kilometre.**
  Its large digits are exactly what the margin prints beside every other line,
  so the two read as the same number; the small digits in front are everything
  above them and the small digits behind everything below. Change the interval
  and the split moves with it — `¹26⁰⁰⁰⁰m.N.` on a 10 km grid becomes
  `¹²60⁰⁰⁰m.N.` on a 1 km one, for the very same line.
- **The corner label follows the map.** It draws no lines and no frame; it
  exists only to put one full coordinate at the start of each margin, and it
  finds that line by reading the map's own extent every time the layout is
  drawn. Pan or zoom and it re-finds the first line on its own, with no need
  to press Apply — the same behaviour as ArcGIS Pro's *Corner Grid Labels*.
  The one exception is a grid whose coordinate system differs from the map's:
  the extent is then in the wrong units to compare a grid line against, so the
  first line is worked out when the grid is written and *does* go stale until
  the next Apply. Keeping the map in the same UTM zone as the grid avoids it,
  and the MGRS preset already does.
- **The 1 km mesh carries no numbers.** A second row of labels would land in
  the same margin row as the first and overlap it, and at 1 km spacing on
  anything wider than a 1:50,000 sheet there are far too many of them to read
  — a whole country comes out as a solid tint, which the tool warns about.
  The mesh is there to measure against, not to name. When the automatic
  interval is already 1 km the mesh would be a duplicate, so it is skipped and
  the status line says so.
- **Interior labels are a grid of their own.** QGIS gives one grid a single
  label position per side, so repeating the numbers inside the frame needs a
  second grid rather than a second setting — which is also how Pro does it,
  with its separate *InteriorLabels* component.
- **Finer grids keep their full precision.** Below 1 km the digits go up with
  the step — three on a 100 m grid, four on 10 m, five on 1 m — so a reference
  read off the sheet is still unique within its 100 km square.
- **The principal digits follow the interval and are rebuilt on every Apply.**
  Change a 1 km grid to 100 m and the labels go from two digits to three by
  themselves. An interval that is not an MGRS step is rounded **down** for the
  labels — a 2.5 km grid is labelled to the kilometre — because rounding up
  would print the same number on two neighbouring lines.
- **Fit the page automatically cannot carry MGRS digits.** QGIS recomputes
  that interval at every render and the expression cannot follow it, so the
  combination is refused.
- **The letters and the digits are two rows in the margin**, because they are
  two grids and QGIS gives each grid one label per line. The letters are
  pushed further out than the digits by about a line of text; a large font may
  need the *Distance from the frame* on the 100 km grid nudged by hand.
- **An MGRS format with no UTM zone falls back to plain numbers** rather than
  printing a blank margin, and the status line says it has. Set the grid's
  **Coordinate system** to a UTM zone, or type the zone in yourself.
- **The interval is in the grid's CRS, not the map's.** Switching the
  coordinate system to EPSG:4326 with an interval of 1000 left over from a
  metre grid means 1000 degrees, and draws nothing at all. The tool re-suggests
  the interval whenever the CRS changes, so this only bites if you then type a
  metre value back in. An interval of zero is refused with a message.
- **Labels outside the frame draw beyond the map item's rectangle.** They can
  overrun a neighbouring item or the edge of the page; the map item itself does
  not grow to make room. Move the map in a little, or set **Placement** to
  inside.
- **Latitude and longitude are QGIS's words** for the two families of line.
  On a projected grid they mean northing and easting, and *Latitude / Y only*
  on the left side is the usual choice either way.
- **The window is modeless and reads the project when it opens.** Add a layout
  or a map while it is open and press **Reload** to see it. Selecting a
  different map in the designer and re-running the tool also re-reads it.
- **The window follows the layout it is working on.** It parents itself to the
  designer showing that layout, so it stays in front of the sheet; picking a
  different layout in the combo moves it to that layout's designer, and a
  layout with no designer open leaves it on the QGIS main window. **Closing
  that designer closes this window with it** — re-run the tool to get it back.
  Nothing is lost: the grid is already on the map. Pick it under **Grid** to
  load its settings again; the form itself opens on the defaults, because
  nothing is remembered between sessions.
- **Fit the page automatically** ignores the interval boxes entirely: QGIS
  computes a fresh interval at every render so the spacing on paper stays
  between the two widths given. That means the numbers on the sheet change when
  the map's scale does, which is what makes it useful for an atlas.
- Only **print layouts** are offered. A report is not a layout the tool can put
  a grid on.

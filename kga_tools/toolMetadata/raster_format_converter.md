# Raster Format Converter

|  |  |
|---|---|
| **Algorithm ID** | `kga:raster_format_converter` |
| **Group** | KGA Data Conversion |
| **Source** | `kga_tools/algorithms/raster_format_converter.py` |
| **Icon** | `icons/rasterFormatConverter.png` |
| **Type** | Batch algorithm |

## Overview

Converts a raster from **any format GDAL can read** into **any format it can
write** — ECW to GeoTIFF, GeoTIFF to JPEG 2000, IMG to GeoTIFF, and back.

It exists for the orthophoto that is too big to convert in one go: the aerial
survey that arrives as a 200 GB ECW and has to become a GeoTIFF before anything
else can touch it.

**How it survives a long run.** The source is read in square blocks. Each block
is written straight to disk as a temporary GeoTIFF — GeoTIFF whatever the final
format is, because it is the one format that takes reliable windowed writes —
and a small `<output>.checkpoint.json` beside the output records how many blocks
are down. Cancel the run, lose QGIS, lose the machine: **run it again with the
same output path and it carries on from the next unfinished block.** When the
last block lands they are mosaicked and written once into the chosen format,
which is the fast part, because it reads local uncompressed blocks instead of
decoding the original again.

Small rasters skip all of that. Anything that fits inside one block is converted
in a single pass.

### How the pyramid is written

Overviews are **built on the throwaway mosaic and copied into the output as it
is created** (`COPY_SRC_OVERVIEWS`), not built into the finished file
afterwards. Re-opening a freshly written multi-gigabyte raster for update puts
hours of work under a write that can still run out of room; the mosaic is
disposable, so a failure there costs a rebuild and nothing else.

Disk space is checked three times — before the merge, before the pyramid, and
again after it, when there is still the option of dropping the pyramid instead
of crowding out the final write. **The pyramid and the output have to fit on the
drive at the same time**, because nothing is cleaned up until the output is
written. If they do not:

1. The pyramid is skipped and the raster is written without it.
2. Once the blocks are deleted and their space is back, it is tried again as a
   **sidecar `.ovr`**, with the raster opened **read-only** — so that attempt
   cannot touch the data even if it fails.
3. If there is still no room, the log says so and tells you the raster is
   complete and the pyramids can be built later.

Erdas IMAGINE always takes the sidecar route: `COPY_SRC_OVERVIEWS` is a GeoTIFF
creation option and does not apply to it.

## Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| **Source \| Raster to convert** | Raster layer | — | A layer in the project or a file on disk: ECW, GeoTIFF, JPEG 2000, IMG, SID, PNG, JPEG — anything GDAL reads. It must be a file: a WMS or XYZ layer has nothing to convert. |
| **Output \| Format** | Enum | `GeoTIFF (.tif)` | GeoTIFF, ECW, JPEG 2000, Erdas IMAGINE, PNG or JPEG. |
| **Output \| Converted raster** | File destination | — | Where to write. The extension is corrected to match the format, so the two can never disagree. **Re-run with this same path to resume.** |
| **Options \| Compression (GeoTIFF, Erdas IMAGINE)** | Enum | `LZW` | `LZW`, `DEFLATE` and `ZSTD` are lossless, `JPEG` is lossy but small for photography, `NONE` is uncompressed. Erdas IMAGINE reads anything but `NONE` as "compressed". Ignored by the other formats. |
| **Options \| Quality (ECW, JPEG 2000, JPEG)** | Number 1–100 | `75` | JPEG and JPEG 2000: higher is better quality and a bigger file. **ECW reads it as a target compression ratio, where higher means a smaller file** — the opposite direction. Ignored by the other formats. |
| **Options \| Build overviews (pyramids)** | Boolean | `True` | Pyramids down to roughly a 256 px thumbnail, so the raster draws quickly when zoomed out. Only GeoTIFF and Erdas IMAGINE can carry them; ignored for the rest. See *How the pyramid is written* below — it is never edited into the finished file. |
| **Options \| Add the result to the project** | Boolean | `False` | Load the converted raster when the run finishes. |

### Advanced

| Parameter | Type | Default | Description |
|---|---|---|---|
| **Processing block size (px)** | Number 512–32768 | `4096` | How much of the source is converted between checkpoints. Smaller blocks lose less work when a run is interrupted but cost more overhead; larger blocks are faster and want more memory. Changing it invalidates an existing checkpoint. |
| **GDAL threads** | Enum | `ALL_CPUS` | Threads GDAL may use for decoding and compression. Drop it below `ALL_CPUS` to leave the machine usable while a long conversion runs. |
| **Resume from an existing checkpoint** | Boolean | `True` | Off, the blocks already on disk are thrown away and the conversion starts over. |

### Outputs declared

| Output | Type | Description |
|---|---|---|
| **Converted raster** | File | The written file. |
| **Converted raster path** | String | Its path, after the extension was corrected. |
| **Blocks converted** | Number | Blocks written this run. `1` for a one-pass conversion, `0` when the run was cancelled with a checkpoint left behind. |

## How to use

1. Open **KGA Data Conversion > Raster Format Converter**.
2. Pick the source raster and the output format, and name the output file.
3. Leave the defaults unless you have a reason: LZW is lossless, overviews are
   worth having, and 4096 px blocks suit most orthophotos.
4. Press **Run** and read the header the log prints first — size, band count,
   data type, creation options and **how much temporary disk space the blocks
   will need**. Stop there if that number does not fit on the drive.
5. Leave it running. The progress bar moves per block, then again through the
   merge and the overviews.
6. **If you have to stop it, press Cancel.** The log says which block it reached.
   Run it again later with the same source and output path, and it says
   *"Resuming: N of M blocks were already converted"*.

## Outputs

- The converted raster, with its georeferencing, CRS, band structure and nodata
  carried across by GDAL.
- Internal overviews when the format can hold them and the box is ticked.
- The raster added to the project when that box is ticked.
- On a clean finish, the block folder, the VRT and the checkpoint are deleted.
  **After a cancel they are deliberately kept** — they are what resuming reads.

## Notes and limits

- **Writing ECW needs a Hexagon licence.** QGIS reads ECW out of the box, and
  the driver advertises write support, but the encoder refuses unless
  `ECW_ENCODE_KEY` and `ECW_ENCODE_COMPANY` are set with the key bought from
  Hexagon (*Settings > Options > System > Environment*, then restart QGIS). The
  tool checks the licence, not just the driver, when you press **Run** — GDAL
  would otherwise refuse at the final write, hours in. **JPEG 2000 goes through
  the open-source JP2OpenJPEG driver and needs no licence.**
- **Budget the temporary disk space.** The blocks are LZW GeoTIFF, so roughly
  the uncompressed size of the source sits beside the output until the merge is
  done. The log prints the estimate before it starts, and checks the free space
  again before the final write and before the pyramid. This is the cost of being
  able to resume. Running out during the final write is the one failure that
  wastes the whole run, which is why it is checked rather than assumed.
- **A pyramid is never worth risking the raster for.** Whatever happens to the
  overviews — skipped for space, dropped after being built, failed outright —
  the converted raster is complete and correct, and the log says which happened.
  Missing pyramids can be built any time from *Raster > Build Overviews*.
- **Changing the block size or the output path starts over.** The blocks no
  longer line up, and the tool says so rather than mixing them. It also notices
  when the source file itself has changed since the checkpoint was written.
- **JPEG and PNG have format limits**: JPEG takes 8-bit data, 1, 3 or 4 bands
  and no side over 65535 pixels; PNG takes 8- or 16-bit data and up to 4 bands.
  Both are checked before the run, against what the driver in *this* QGIS says
  it will accept.
- **JPEG, JPEG 2000 and a JPEG-compressed GeoTIFF are lossy.** Converting an
  archive ortho into one of them throws away detail that no later conversion
  brings back. GeoTIFF with LZW or DEFLATE is the lossless choice.
- **A source with no georeferencing** — a plain PNG or JPEG with no world file —
  cannot be mosaicked back from blocks, so it is converted in one pass and that
  run cannot be resumed. The log says so.
- **Cancelling during the merge** keeps every block, so the next run only redoes
  the merge, which is the cheap half. The half-written output is removed, so
  there is never a truncated file that looks finished. A merge that fails for
  another reason (a full disk, say) also keeps the blocks and says so.
- **A one-pass conversion cannot be resumed.** Cancelling one removes the
  unfinished file.
- Resuming is the same idea the **[DEM and Contour Tool](demcontourtool.md)** and
  **[DEM Elevation Correction](demelevationcorrectionandpointsamplereport.md)**
  use, so a long job in this toolbox behaves the same way wherever you meet one.

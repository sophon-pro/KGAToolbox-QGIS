# Cambodia administrative data: cleaning and release

Two scripts take the hand-maintained GeoPackage to the files the plugin's
**KGA Database (Cambodia)** source reads from a GitHub release. Run both with
QGIS's own Python (they need GDAL only):

```
python-qgis-ltr.bat standardize.py   <raw.gpkg>      <standard.gpkg> <log.csv>
python-qgis-ltr.bat build_release.py <standard.gpkg> <out_dir>       <data_version>
```

## 1. `standardize.py`

Gives every layer the same schema (codes as zero-padded text, `name_en`,
`name_kh`, `type_kh`, `type_en`, `pcode`, parent names, `area_km2`, `qa_note`),
in EPSG:4326, and repairs what can be repaired safely. Every change is written
to the log CSV, one row each. The rules, and why:

* The 2024 **centre** layers are authoritative for codes, names and types. The
  commune polygons are an older numbering that was later reorganised, so a
  polygon is re-linked to a centre **by name inside the same district** (then
  across the province for communes that moved district), never by code alone —
  the same code can name two different communes in the two layers.
* A district whose polygon and centre disagree on the code takes the code the
  commune and village layers actually use.
* Village codes follow the 2024 numbering except in Kampong Speu district 0505,
  where they follow the old polygon numbering; they are re-coded to match,
  judged by whether the villages sit inside the polygon that carried the old
  code.
* Not administrative units, dropped: the Tonle Sap water body (once as a
  "province", once as a district). Seven nameless island pieces coded like
  communes are merged into their district.
* Anything it cannot resolve is kept and flagged in `qa_note` rather than
  guessed.

Open questions it cannot answer, listed in the log: communes that have a centre
but no boundary yet (`no boundary`), commune polygons with no 2024 counterpart
(`unmatched`), and villages whose code was missing (`codes assigned by
location`, `vil_code` left empty).

## 2. `build_release.py`

Cuts the standard file into one GeoPackage per province (every level inside
it), zips each, and writes `khm-admin-manifest.json`: the province, district
and commune index (names, codes, which geometries exist), the pack list with
sizes and SHA-256 checksums, and the layout. The plugin fills every drop-down
from the manifest and downloads only the packs a selection touches.

## 3. Publishing

Attach **every file in `out_dir`** to one GitHub release of the private
repository. The release must be an ordinary release — not a draft and not a
pre-release — because the plugin asks GitHub for the *latest* one.

```
gh release create cam-admin-2024.08 <out_dir>/* --repo OWNER/REPO ^
   --title "Cambodia admin 2024.08" --notes "..."
```

To publish a new data version, fix the source data, rerun both scripts with a
new `data_version`, and create a new release. Users pick it up within a day (or
at once with **Refresh**); nothing in the plugin changes.

## 4. Access

The repository is private, so the plugin needs a token. Create a **fine-grained
personal access token** limited to this one repository with **Contents:
Read-only** and a fixed expiry, and give it to the people who should have the
data. Each user pastes it once into the **Access key** box in the Add Open Data
window; it is stored encrypted in the QGIS password store (or, where QGIS
authentication is disabled, in the QGIS profile settings, which the window
says) and is sent to `api.github.com` only. Revoking or letting the token expire cuts access; the
window then asks for a new one.

Do **not** put a token in the plugin: anything inside a distributed plugin can
be read by whoever installs it.

The plugin is pointed at the repository in `kga_tools/core/kga_release.py`
(`DEFAULT_REPO = 'owner/name'`); until that is set the source is listed but
greyed out.

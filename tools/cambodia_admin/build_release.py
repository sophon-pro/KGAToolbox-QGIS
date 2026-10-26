"""Cut the standardised Cambodia GeoPackage into release assets.

    python-qgis-ltr.bat build_release.py <standard.gpkg> <out_dir> <data_version>

Writes, into ``out_dir``, the files to attach to one GitHub release:

    khm-admin-manifest.json      what exists, how big, and the unit index
    khm-admin-p01.gpkg.zip ...   one per province, every level inside it:
                                 province, district, commune (boundary and
                                 centre) and village (centre)

Why this split. A province pack is about 0.6 MB zipped, so a user who wants one
district, one commune or a village list downloads one province, not the 24 MB
country. Every unit lives in exactly one asset, so nothing can disagree with
itself. The manifest carries the whole province/district/commune index (names,
codes, which geometries exist), so every drop-down in the dialog fills without
downloading any geometry at all.

The plugin never hard-codes this layout: it reads ``packs`` from the manifest,
so the split can change later without a plugin update.
"""
import hashlib
import json
import os
import sqlite3
import sys
import zipfile
from datetime import datetime, timezone

from osgeo import gdal, ogr

gdal.UseExceptions()
ogr.UseExceptions()

SCHEMA_VERSION = 1
PREFIX = 'khm-admin'

LEVELS = (
    # layer, level, geometry, key column
    ('province_boundary', 'province', 'boundary', 'pro_code'),
    ('province_center', 'province', 'center', 'pro_code'),
    ('district_boundary', 'district', 'boundary', 'dist_code'),
    ('district_center', 'district', 'center', 'dist_code'),
    ('commune_boundary', 'commune', 'boundary', 'com_code'),
    ('commune_center', 'commune', 'center', 'com_code'),
    ('village_center', 'village', 'center', 'vil_code'),
)
ALL_LAYERS = tuple(row[0] for row in LEVELS)


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as fh:
        for block in iter(lambda: fh.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def cut(src, layers, where, dst):
    """One GeoPackage holding `layers` of `src`, each filtered by `where`."""
    if os.path.exists(dst):
        os.remove(dst)
    first = True
    for name in layers:
        opts = gdal.VectorTranslateOptions(
            format='GPKG', layers=[name], layerName=name,
            where=where.get(name) if isinstance(where, dict) else where,
            accessMode=None if first else 'update',
            layerCreationOptions=['GEOMETRY_NAME=geom', 'FID=fid',
                                  'SPATIAL_INDEX=YES', 'IDENTIFIER=' + name])
        gdal.VectorTranslate(dst, src, options=opts).FlushCache()
        first = False
    con = sqlite3.connect(dst)
    con.execute('VACUUM')
    con.close()


def zipped(path):
    out = path + '.zip'
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        zf.write(path, os.path.basename(path))
    os.remove(path)
    return out


def main(src, out_dir, data_version):
    os.makedirs(out_dir, exist_ok=True)
    ds = ogr.Open(src)

    # --------------------------------------------------------------- index
    def rows(layer, cols):
        lyr = ds.GetLayerByName(layer)
        return [{c: f.GetField(c) for c in cols} for f in lyr]

    prov_b = {r['pro_code'] for r in rows('province_boundary', ['pro_code'])}
    dist_b = {r['dist_code'] for r in rows('district_boundary', ['dist_code'])}

    vill_n = {}
    for r in rows('village_center', ['com_code']):
        vill_n[r['com_code']] = vill_n.get(r['com_code'], 0) + 1

    provinces = [dict(code=r['pro_code'], name_en=r['name_en'], name_kh=r['name_kh'],
                      type_en=r['type_en'], boundary=r['pro_code'] in prov_b)
                 for r in sorted(rows('province_center', [
                     'pro_code', 'name_en', 'name_kh', 'type_en']),
                     key=lambda r: r['pro_code'])]
    districts = [dict(code=r['dist_code'], parent=r['pro_code'], name_en=r['name_en'],
                      name_kh=r['name_kh'], type_en=r['type_en'],
                      boundary=r['dist_code'] in dist_b)
                 for r in sorted(rows('district_center', [
                     'pro_code', 'dist_code', 'name_en', 'name_kh', 'type_en']),
                     key=lambda r: r['dist_code'])]

    # A commune is listed once whether it has a centre, a boundary or both.
    communes = {}
    for layer, flag in (('commune_center', 'center'), ('commune_boundary', 'boundary')):
        for r in rows(layer, ['dist_code', 'com_code', 'name_en', 'name_kh', 'type_en']):
            if not r['com_code']:
                continue
            c = communes.setdefault(r['com_code'], dict(
                code=r['com_code'], parent=r['dist_code'], name_en=r['name_en'],
                name_kh=r['name_kh'], type_en=r['type_en'], boundary=False,
                center=False, villages=vill_n.get(r['com_code'], 0)))
            c[flag] = True
            c['name_en'] = c['name_en'] or r['name_en']
            c['name_kh'] = c['name_kh'] or r['name_kh']
            c['type_en'] = c['type_en'] or r['type_en']
    communes = [communes[k] for k in sorted(communes)]

    # --------------------------------------------------------------- packs
    packs = []
    for p in provinces:
        pc = p['code']
        where = "pro_code = '%s'" % pc
        final = os.path.join(out_dir, '%s-p%s.gpkg' % (PREFIX, pc))
        cut(src, ALL_LAYERS, where, final)
        z = zipped(final)
        packs.append(dict(id='p' + pc, file=os.path.basename(z), layers=list(ALL_LAYERS),
                          scope=dict(pro_code=pc),
                          size=os.path.getsize(z), sha256=sha256(z)))
        print('%s %-22s %8.0f KB' % (pc, p['name_en'], os.path.getsize(z) / 1024))

    counts = {n: ds.GetLayerByName(n).GetFeatureCount() for n, *_ in LEVELS}
    manifest = dict(
        schema_version=SCHEMA_VERSION,
        dataset='kga-cambodia-admin',
        country='KHM',
        data_version=data_version,
        generated=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        crs='EPSG:4326',
        layers=[dict(name=n, level=lv, geometry=g, key=k, count=counts[n])
                for n, lv, g, k in LEVELS],
        packs=packs,
        index=dict(provinces=provinces, districts=districts, communes=communes),
    )
    path = os.path.join(out_dir, '%s-manifest.json' % PREFIX)
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(manifest, fh, ensure_ascii=False, separators=(',', ':'))
    total = sum(p['size'] for p in packs)
    print('manifest %.0f KB; %d packs, %.1f MB in all' % (
        os.path.getsize(path) / 1024, len(packs), total / 1e6))
    print('largest pack: %.2f MB' % (max(p['size'] for p in packs) / 1e6))


if __name__ == '__main__':
    main(*sys.argv[1:4])

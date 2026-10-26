"""Standardise the Cambodia administrative GeoPackage.

Reads the raw hand-maintained GeoPackage and writes one with a single schema
across every level, which is what the "KGA database" source in Add Open Data
relies on. Run it with QGIS's own interpreter (it needs GDAL/OGR only):

    python-qgis-ltr.bat standardize.py <source.gpkg> <output.gpkg> <log.csv>

Standard schema, every layer, in this order (a level only has the columns down
to its own level):

    pro_code TEXT(2)   dist_code TEXT(4)   com_code TEXT(6)   vil_code TEXT(8)
    name_en  name_kh   type_kh  type_en    pcode ('KH' + the deepest code)
    pro_name_en / dist_name_en / com_name_en   the ancestors' English names
    area_km2 (polygons only)                   qa_note (null when clean)

Codes are zero padded TEXT: the raw file stores them as numbers, which loses
the leading zero (``10201`` for commune ``010201``) and breaks parent/child
string prefixes. EPSG:4326 out; areas are measured in the source UTM 48N first.

The 2024 centre layers are treated as authoritative for codes, names and
types: they carry the current 1,652 communes, while the commune polygons are an
older numbering that was later reorganised. Polygons are re-linked to a centre
by name inside the same district, never by code alone.
"""
import csv
import difflib
import re
import sys
from collections import defaultdict

from osgeo import gdal, ogr, osr

gdal.UseExceptions()
ogr.UseExceptions()

TYPE_EN = {
    'ខេត្ត': 'Province', 'រាជធានី': 'Capital',
    'ស្រុក': 'District', 'ក្រុង': 'Municipality', 'ខណ្ឌ': 'Khan',
    'ឃុំ': 'Commune', 'សង្កាត់': 'Sangkat', 'ភូមិ': 'Village',
}

LOG = []


def log(layer, ident, action, detail=''):
    LOG.append((layer, ident, action, detail))


def clean(text):
    text = re.sub(r'\s+', ' ', str(text or '')).strip()
    return text or None


def key(text):
    return re.sub(r'[^a-z0-9]', '', (text or '').lower())


def code(value, width):
    if value is None or value == '':
        return None
    return str(int(float(value))).zfill(width)


def read(ds, name):
    out = []
    for feat in ds.GetLayerByName(name):
        row = dict(feat.items())
        row['_fid'] = feat.GetFID()
        geom = feat.GetGeometryRef()
        row['_g'] = geom.Clone() if geom is not None else None
        out.append(row)
    return out


def polygons_of(geom):
    """Valid MultiPolygon from whatever MakeValid hands back."""
    if not geom.IsValid():
        geom = geom.MakeValid()
    multi = ogr.Geometry(ogr.wkbMultiPolygon)

    def walk(g):
        kind = ogr.GT_Flatten(g.GetGeometryType())
        if kind == ogr.wkbPolygon:
            multi.AddGeometry(g)
        elif g.GetGeometryCount():
            for i in range(g.GetGeometryCount()):
                walk(g.GetGeometryRef(i))
    walk(geom)
    multi.FlattenTo2D()
    return multi


def main(src, dst, log_path):
    ds = ogr.Open(src)
    P, PC = read(ds, 'Province'), read(ds, 'Province_Center')
    D, DC = read(ds, 'District'), read(ds, 'District_Center')
    C, CC = read(ds, 'Commune'), read(ds, 'Commune_Center')
    V = read(ds, 'Village_Center')

    # ------------------------------------------------------------ provinces
    centre_p = {code(x['Pro_Code'], 2): x for x in PC}
    prov = {}
    for x in P:
        pc = code(x['Pro_Code'], 2)
        if pc == '00' or pc not in centre_p:
            log('Province', x['_fid'], 'dropped',
                '%s - not an administrative province (water body)' % x['Name_En'])
            continue
        prov[pc] = dict(
            pro_code=pc, name_en=clean(x['Name_En']), name_kh=clean(x['Name_Kh']),
            type_kh=clean(x['Class']) or clean(centre_p[pc]['Class']),
            g=polygons_of(x['_g']))
        if not x['_g'].IsValid():
            log('Province', x['_fid'], 'geometry repaired', prov[pc]['name_en'])
    pname = {k: v['name_en'] for k, v in prov.items()}

    # ------------------------------------------------------------ districts
    centre_d = {code(x['Dist_Code'], 4): x for x in DC}
    # The commune and village layers say which district code is actually in
    # use; when polygon and centre disagree the code those layers use wins.
    in_use = {cc[:4] for cc in (code(x['Com_Code'], 6) for x in CC)}
    poly_codes = {code(y['Dist_Code'], 4) for y in D
                  if y['Dist_Code'] is not None and int(y['Dist_Code']) <= 9999}
    dist = {}
    slivers = []
    alias = {}                       # centre code -> the code the district ends up with
    for x in D:
        dc = code(x['Dist_Code'], 4) if x['Dist_Code'] is not None else None
        raw = x['Dist_Code']
        if raw is not None and int(raw) > 9999:        # a commune-sized code
            slivers.append(x)
            continue
        if not x['Name_En'] and dc is None and x['Pro_Code'] is None:
            log('District', x['_fid'], 'dropped',
                'no code, no name - the Tonle Sap water body again')
            continue
        if dc is None or dc not in centre_d:
            # a polygon whose code the centres do not know: the centre
            # inside it names the district.
            inside = [(k, c) for k, c in centre_d.items()
                      if k not in poly_codes and x['_g'].Contains(c['_g'])]
            if len(inside) != 1:
                log('District', x['_fid'], 'dropped', 'cannot be identified: %r' % dc)
                continue
            centre_code, c = inside[0]
            final = dc if dc in in_use else centre_code
            alias[centre_code] = final
            log('District', x['_fid'], 'recoded',
                'polygon %s / centre %s (%s) -> %s, the code the commune and '
                'village layers use' % (dc, centre_code, clean(c['Name_En']), final))
            dc = final
        else:
            c = centre_d[dc]
        dist[dc] = dict(
            dist_code=dc, pro_code=dc[:2], name_en=clean(c['Name_En']),
            name_kh=clean(c['Name_Kh']), type_kh=clean(c['Class']),
            g=polygons_of(x['_g']))
        if not x['_g'].IsValid():
            log('District', x['_fid'], 'geometry repaired', dist[dc]['name_en'])

    for x in slivers:
        parent = code(int(x['Dist_Code']) // 100, 4)
        if parent in dist:
            dist[parent]['g'] = dist[parent]['g'].Union(polygons_of(x['_g']))
            dist[parent]['g'] = polygons_of(dist[parent]['g'])
            log('District', x['_fid'], 'merged',
                'island piece coded %s merged into district %s (%s)' % (
                    x['Dist_Code'], parent, dist[parent]['name_en']))
        else:
            log('District', x['_fid'], 'dropped',
                'island piece coded %s, no district %s' % (x['Dist_Code'], parent))
    centre_d = {alias.get(k, k): v for k, v in centre_d.items()}
    for dc in centre_d:
        if dc not in dist:
            log('District', dc, 'no boundary',
                '%s has a centre but no polygon' % clean(centre_d[dc]['Name_En']))
    dname = {k: v['name_en'] for k, v in dist.items()}
    for dc, c in centre_d.items():
        dname.setdefault(dc, clean(c['Name_En']))

    # ------------------------------------------------------------- communes
    centre_c = {code(x['Com_Code'], 6): x for x in CC}
    by_dist = defaultdict(list)
    for cc in centre_c:
        by_dist[cc[:4]].append(cc)

    polys = []
    for x in C:
        polys.append(dict(row=x, old=code(x['COM_CODE'], 6),
                          dist=code(x['DIS_CODE'], 4), name=clean(x['HRName'])))

    # Pass 1 strict, pass 2 loose; a centre can be claimed by one polygon only.
    by_prov = defaultdict(list)
    for cc in centre_c:
        by_prov[cc[:2]].append(cc)

    match, used = {}, set()
    # Communes also moved between districts in the reorganisation, so the last
    # pass looks across the whole province and only accepts a near-exact name.
    for threshold, scope in ((0.80, 'dist'), (0.55, 'dist'), (0.85, 'prov')):
        pairs = []
        for i, p in enumerate(polys):
            if i in match:
                continue
            pool = by_dist.get(p['dist'], []) if scope == 'dist' \
                else by_prov.get(p['dist'][:2], [])
            for cc in pool:
                if cc in used:
                    continue
                s = difflib.SequenceMatcher(
                    None, key(p['name']), key(centre_c[cc]['Name_En'])).ratio()
                if cc == p['old']:
                    s += 0.05
                pairs.append((s, i, cc))
        pairs.sort(reverse=True)
        for s, i, cc in pairs:
            if s >= threshold and i not in match and cc not in used:
                match[i] = cc
                used.add(cc)

    recode_map = {}
    com = {}
    for i, p in enumerate(polys):
        x = p['row']
        geom = polygons_of(x['_g'])
        if not x['_g'].IsValid():
            log('Commune', x['_fid'], 'geometry repaired', p['name'])
        if i in match:
            cc = match[i]
            c = centre_c[cc]
            note = None
            if cc != p['old']:
                note = 'boundary was coded %s in the older numbering' % p['old']
                recode_map[p['old']] = cc
                log('Commune', x['_fid'], 'recoded', '%s -> %s %s' % (
                    p['old'], cc, clean(c['Name_En'])))
            com[cc] = dict(com_code=cc, name_en=clean(c['Name_En']),
                           name_kh=clean(c['Name_Kh']), type_kh=clean(c['Class']),
                           g=geom, qa_note=note)
        else:
            taken = p['old'] in centre_c
            note = ('no matching 2024 commune centre; code %s is %s' % (
                p['old'], 'used by a different commune - left blank' if taken
                else 'from the older numbering, unverified'))
            cc = None if taken else p['old']
            log('Commune', x['_fid'], 'unmatched', '%s %s: %s' % (
                p['old'], p['name'], note))
            ident = cc or ('old-' + p['old'])
            com[ident] = dict(com_code=cc, name_en=p['name'], name_kh=clean(x['Name_Kh']),
                              type_kh=None, g=geom, qa_note=note, _dist=p['dist'])
    for cc in centre_c:
        if cc not in used:
            log('Commune', cc, 'no boundary', '%s has a centre but no polygon' % (
                clean(centre_c[cc]['Name_En'])))
    cname = {cc: clean(c['Name_En']) for cc, c in centre_c.items()}
    for k, v in com.items():
        if v['com_code']:
            cname.setdefault(v['com_code'], v['name_en'])

    # ------------------------------------------------------------- villages
    comm_polys = [(v['com_code'], v['g']) for v in com.values() if v['com_code']]
    # Villages follow the 2024 commune numbering except where the old polygon
    # numbering was kept (Kampong Speu 505). A village is on the old numbering
    # only when its point lies inside the polygon that used that old code.
    # Which old-numbered communes the village layer really uses: those where
    # most villages sit inside the polygon that carried the old code.
    inside_n, total_n = defaultdict(int), defaultdict(int)
    owner_geom = {p['old']: p['row']['_g'] for p in polys}
    for x in V:
        if x['Vil_Code'] is None:
            continue
        cc = code(x['Vil_Code'], 8)[:6]
        if cc in recode_map and cc in owner_geom:
            total_n[cc] += 1
            if owner_geom[cc].Contains(x['_g']):
                inside_n[cc] += 1
    old_numbered = {cc for cc in total_n if inside_n[cc] * 2 >= total_n[cc]}

    vill = []
    for x in V:
        vc = code(x['Vil_Code'], 8)
        note = None
        if vc is None:
            cc = None
            for code6, g in comm_polys:
                if g.Contains(x['_g']):
                    cc = code6
                    break
            note = 'village code missing in source; commune taken from its location'
            log('Village', x['_fid'], 'codes assigned by location',
                '%s -> commune %s' % (clean(x['Name_Kh']), cc))
        else:
            cc = vc[:6]
            new = recode_map.get(cc)
            if new:
                if cc not in centre_c or cc in old_numbered:
                    nv = new + vc[6:]
                    log('Village', x['_fid'], 'recoded',
                        '%s -> %s (commune %s -> %s)' % (vc, nv, cc, new))
                    note = 'village was coded %s in the older numbering' % vc
                    vc, cc = nv, new
        if cc is None:
            log('Village', x['_fid'], 'dropped', 'no commune found for %s' % x['Name_Kh'])
            continue
        vill.append(dict(vil_code=vc, com_code=cc, name_en=clean(x['Name_En']),
                         name_kh=clean(x['Name_Kh']), type_kh=clean(x['Class']) or 'ភូមិ',
                         g=x['_g'], qa_note=note))
    codes_seen = defaultdict(int)
    for v in vill:
        if v['vil_code']:
            codes_seen[v['vil_code']] += 1
    for vcode, n in codes_seen.items():
        if n > 1:
            log('Village', vcode, 'duplicate code', '%d villages share it' % n)
    vc_with = {v['com_code'] for v in vill}
    for cc in centre_c:
        if cc not in vc_with:
            log('Commune', cc, 'no villages', clean(centre_c[cc]['Name_En']))

    write(dst, prov, centre_p, dist, centre_d, com, centre_c, vill, pname, dname, cname)
    with open(log_path, 'w', newline='', encoding='utf-8-sig') as fh:
        writer = csv.writer(fh)
        writer.writerow(['layer', 'id', 'action', 'detail'])
        writer.writerows(LOG)
    counts = defaultdict(int)
    for layer, _i, action, _d in LOG:
        counts[(layer, action)] += 1
    for k in sorted(counts):
        print('%-10s %-28s %d' % (k[0], k[1], counts[k]))


# --------------------------------------------------------------------- output

def write(dst, prov, centre_p, dist, centre_d, com, centre_c, vill, pname, dname, cname):
    src_srs = osr.SpatialReference()
    src_srs.ImportFromEPSG(32648)
    out_srs = osr.SpatialReference()
    out_srs.ImportFromEPSG(4326)
    out_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    src_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    tr = osr.CoordinateTransformation(src_srs, out_srs)

    drv = ogr.GetDriverByName('GPKG')
    try:
        drv.DeleteDataSource(dst)
    except RuntimeError:
        pass
    out = drv.CreateDataSource(dst)

    def layer(name, gtype, level, polygon):
        lyr = out.CreateLayer(name, out_srs, gtype, [
            'GEOMETRY_NAME=geom', 'FID=fid', 'SPATIAL_INDEX=YES',
            'IDENTIFIER=' + name])
        cols = ['pro_code', 'dist_code', 'com_code', 'vil_code'][:level + 1]
        cols += ['name_en', 'name_kh', 'type_kh', 'type_en', 'pcode']
        cols += ['pro_name_en', 'dist_name_en', 'com_name_en'][:level]
        for c in cols:
            lyr.CreateField(ogr.FieldDefn(c, ogr.OFTString))
        if polygon:
            lyr.CreateField(ogr.FieldDefn('area_km2', ogr.OFTReal))
        lyr.CreateField(ogr.FieldDefn('qa_note', ogr.OFTString))
        return lyr

    def put(lyr, level, attrs, geom, area=None):
        feat = ogr.Feature(lyr.GetLayerDefn())
        attrs = dict(attrs)
        attrs['type_en'] = TYPE_EN.get(attrs.get('type_kh'))
        deepest = [attrs.get(k) for k in ('pro_code', 'dist_code', 'com_code', 'vil_code')][level]
        attrs['pcode'] = ('KH' + deepest) if deepest else None
        for k, v in attrs.items():
            if lyr.GetLayerDefn().GetFieldIndex(k) >= 0 and v is not None:
                feat.SetField(k, v)
        if area is not None:
            feat.SetField('area_km2', round(area, 4))
        geom = geom.Clone()
        geom.Transform(tr)
        feat.SetGeometry(geom)
        lyr.CreateFeature(feat)

    def parents(level, pc, dc=None, cc=None):
        d = {}
        if level >= 1:
            d['pro_name_en'] = pname.get(pc)
        if level >= 2:
            d['dist_name_en'] = dname.get(dc)
        if level >= 3:
            d['com_name_en'] = cname.get(cc)
        return d

    def sort_key(a):
        return tuple(a.get(k) or '~' for k in ('pro_code', 'dist_code', 'com_code', 'vil_code'))

    # province
    lb, lc = layer('province_boundary', ogr.wkbMultiPolygon, 0, True), \
        layer('province_center', ogr.wkbPoint, 0, False)
    for pc in sorted(prov):
        p = prov[pc]
        attrs = dict(pro_code=pc, name_en=p['name_en'], name_kh=p['name_kh'],
                     type_kh=p['type_kh'])
        put(lb, 0, attrs, p['g'], area_m2(p['g'], True) / 1e6)
    for pc in sorted(centre_p):
        c = centre_p[pc]
        base = prov.get(pc, {})
        attrs = dict(pro_code=pc, name_en=base.get('name_en') or clean(c['Name_En']),
                     name_kh=clean(c['Name_Kh']), type_kh=clean(c['Class']))
        put(lc, 0, attrs, c['_g'])

    # district
    lb, lc = layer('district_boundary', ogr.wkbMultiPolygon, 1, True), \
        layer('district_center', ogr.wkbPoint, 1, False)
    for dc in sorted(dist):
        d = dist[dc]
        attrs = dict(pro_code=d['pro_code'], dist_code=dc, name_en=d['name_en'],
                     name_kh=d['name_kh'], type_kh=d['type_kh'])
        attrs.update(parents(1, d['pro_code']))
        put(lb, 1, attrs, d['g'], area_m2(d['g'], True) / 1e6)
    for dc in sorted(centre_d):
        c = centre_d[dc]
        attrs = dict(pro_code=dc[:2], dist_code=dc, name_en=clean(c['Name_En']),
                     name_kh=clean(c['Name_Kh']), type_kh=clean(c['Class']))
        attrs.update(parents(1, dc[:2]))
        put(lc, 1, attrs, c['_g'])

    # commune
    lb, lc = layer('commune_boundary', ogr.wkbMultiPolygon, 2, True), \
        layer('commune_center', ogr.wkbPoint, 2, False)
    rows = []
    for ident, c in com.items():
        cc = c['com_code']
        dc = cc[:4] if cc else c['_dist']
        attrs = dict(pro_code=dc[:2], dist_code=dc, com_code=cc, name_en=c['name_en'],
                     name_kh=c['name_kh'], type_kh=c['type_kh'], qa_note=c['qa_note'])
        attrs.update(parents(2, dc[:2], dc))
        rows.append((attrs, c['g']))
    for attrs, g in sorted(rows, key=lambda r: sort_key(r[0]) + (r[0]['name_en'] or '',)):
        put(lb, 2, attrs, g, area_m2(g, True) / 1e6)
    for cc in sorted(centre_c):
        c = centre_c[cc]
        attrs = dict(pro_code=cc[:2], dist_code=cc[:4], com_code=cc,
                     name_en=clean(c['Name_En']), name_kh=clean(c['Name_Kh']),
                     type_kh=clean(c['Class']))
        attrs.update(parents(2, cc[:2], cc[:4]))
        put(lc, 2, attrs, c['_g'])

    # village (centres only)
    lc = layer('village_center', ogr.wkbPoint, 3, False)
    for v in sorted(vill, key=lambda r: (r['vil_code'] or '~', r['name_kh'] or '')):
        cc = v['com_code']
        attrs = dict(pro_code=cc[:2], dist_code=cc[:4], com_code=cc, vil_code=v['vil_code'],
                     name_en=v['name_en'], name_kh=v['name_kh'], type_kh=v['type_kh'],
                     qa_note=v['qa_note'])
        attrs.update(parents(3, cc[:2], cc[:4], cc))
        put(lc, 3, attrs, v['g'])
    out = None


def area_m2(geom, _utm):
    return geom.GetArea()


if __name__ == '__main__':
    main(*sys.argv[1:4])

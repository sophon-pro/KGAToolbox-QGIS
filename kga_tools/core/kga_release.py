# -*- coding: utf-8 -*-
"""The KGA Cambodia administrative database, served from a GitHub release.

There is no database server yet, so the data lives as files attached to a
release of a (private) GitHub repository and this module is the client. Three
decisions make that practical:

* **A manifest, not a listing.** The release carries ``khm-admin-manifest.json``:
  every province, district and commune (names, codes, which geometries exist)
  plus the list of data packs. All the drop-downs in the dialog fill from it
  without downloading a single geometry, and the pack layout can change later
  without a plugin update because the plugin reads the layout from there.
* **Packs are cut by province.** Each pack holds every level for one province
  (about half a megabyte zipped), so asking for one district or one village list
  costs one small download, and it is cached. Because codes are hierarchical
  (``0102`` sits in ``01``) the pack a unit lives in is known from its code.
* **Checked and cached.** A pack is verified against the manifest's SHA-256
  before it is trusted, kept unzipped in the profile cache under its data
  version, and reused offline. A new release changes the version, which retires
  the old files on its own.

Access to a private repository needs a token. It is stored in the QGIS
authentication database, never in plain settings and never in this file, and it
is sent to ``api.github.com`` only: the signed storage address the API redirects
to is fetched without it (see :func:`kga_tools.core.net.request`).

No Qt widgets here.
"""

import hashlib
import json
import os
import re
import shutil
import zipfile
from contextlib import suppress

from qgis.core import (Qgis, QgsApplication, QgsFeatureRequest, QgsMessageLog,
                       QgsSettings, QgsVectorLayer)

from . import net
from .net import NetError

LOG_TAG = 'KGA Toolbox'

#: The repository holding the release, as ``owner/name``. Overridable in QGIS
#: settings (``KGA/kga_release/repo``) so a test or a staging copy can point
#: elsewhere without touching code.
DEFAULT_REPO = 'sophon-pro/kga-admin_data'
DEFAULT_API = 'https://api.github.com'
SETTING_REPO = 'KGA/kga_release/repo'
SETTING_API = 'KGA/kga_release/api_base'
#: Where the key goes when the QGIS password store cannot be used.
SETTING_PLAIN_KEY = 'KGA/kga_release/access_key'

#: Where the access token lives in the QGIS authentication database.
ACCESS_SETTING_ID = 'kga_tools/kga_release_access_key'

MANIFEST_NAME = 'khm-admin-manifest.json'
SUPPORTED_SCHEMA = 1
COUNTRY = 'KHM'

#: How long the release lookup is trusted before GitHub is asked again. The
#: data changes a few times a year; a day keeps a new release from waiting
#: weeks without making every dialog open cost a request.
RELEASE_TTL = 24 * 3600

CACHE_FOLDER = 'kga_database'

#: level number -> (layer prefix, key column, label)
LEVELS = {
    1: ('province', 'pro_code', 'Province'),
    2: ('district', 'dist_code', 'District'),
    3: ('commune', 'com_code', 'Commune'),
    4: ('village', 'vil_code', 'Village'),
}
BOUNDARY = 'boundary'
CENTER = 'center'

_CODE = re.compile(r'^\d{2,8}$')


# ------------------------------------------------------------------ settings

def repo():
    return str(QgsSettings().value(SETTING_REPO, DEFAULT_REPO) or '').strip()


def api_base():
    return str(QgsSettings().value(SETTING_API, DEFAULT_API)
               or DEFAULT_API).rstrip('/')


def configured():
    """True when there is a repository to read from."""
    return bool(repo())


def _auth_manager():
    """The QGIS authentication manager, or None when it cannot be used.

    Some QGIS installs have the authentication system disabled (its encryption
    component is missing); every call on it then fails.
    """
    try:
        manager = QgsApplication.authManager()
        if manager is None or manager.isDisabled():
            return None
        return manager
    except Exception:                       # pragma: no cover - auth unavailable
        return None


def load_token():
    """The remembered access token, or ''.

    From the QGIS password store when it works - reading may prompt once for
    the master password - and otherwise from the profile settings, where
    :func:`save_token` puts it on a machine without a usable store.
    """
    manager = _auth_manager()
    if manager is not None:
        with suppress(Exception):
            value = manager.authSetting(ACCESS_SETTING_ID, '', True)
            if value:
                return str(value)
    return str(QgsSettings().value(SETTING_PLAIN_KEY, '') or '')


def has_token():
    manager = _auth_manager()
    if manager is not None:
        with suppress(Exception):
            if manager.existsAuthSetting(ACCESS_SETTING_ID):
                return True
    return bool(QgsSettings().value(SETTING_PLAIN_KEY, ''))


def store_name():
    """Where a saved key lives: 'password store', 'profile settings' or ''."""
    manager = _auth_manager()
    if manager is not None:
        with suppress(Exception):
            if manager.existsAuthSetting(ACCESS_SETTING_ID):
                return 'password store'
    return 'profile settings' if QgsSettings().value(SETTING_PLAIN_KEY, '') else ''


def save_token(token):
    """Store `token` ('' forgets it); returns where it went.

    The encrypted QGIS password store is used when it is available. When it is
    not - the authentication system is disabled on some installs - the key is
    kept in the QGIS profile settings instead, which is plain text on this
    user's disk; the caller says so. Returns 'password store',
    'profile settings', or '' when it was forgotten or nothing could be saved.
    """
    settings = QgsSettings()
    manager = _auth_manager()
    if not token:
        settings.remove(SETTING_PLAIN_KEY)
        if manager is not None:
            with suppress(Exception):
                manager.removeAuthSetting(ACCESS_SETTING_ID)
        return ''

    if manager is not None:
        with suppress(Exception):
            if manager.storeAuthSetting(ACCESS_SETTING_ID, token, True):
                settings.remove(SETTING_PLAIN_KEY)
                return 'password store'
    settings.setValue(SETTING_PLAIN_KEY, token)
    return 'profile settings'


# --------------------------------------------------------------------- HTTP

def _headers(accept):
    headers = {'Accept': accept, 'X-GitHub-Api-Version': '2022-11-28'}
    token = load_token()
    if token:
        headers['Authorization'] = 'Bearer ' + token
    return headers


def _explain(reply):
    """One sentence for a non-200 answer from the GitHub API."""
    if reply.status == 429 or (reply.status == 403
                               and b'rate limit' in reply.body.lower()):
        return 'GitHub is limiting requests right now. Try again in a few minutes.'
    if reply.status == 401:
        return ('GitHub refused the KGA access key. It may have expired - '
                'enter a new one.')
    if reply.status in (403, 404):
        if has_token():
            return ('GitHub cannot find the KGA release with the saved access '
                    'key. The key may have expired or lack access to the '
                    'repository - enter a new one.')
        return ('The KGA release is private. Enter the KGA access key to '
                'use it.')
    if reply.status >= 500:
        return 'GitHub is having trouble of its own (HTTP %d). Try again soon.' % \
            reply.status
    return 'GitHub answered with an unexpected status (HTTP %d).' % reply.status


def _api_json(path):
    reply = net.request('%s/repos/%s/%s' % (api_base(), repo(), path),
                        _headers('application/vnd.github+json'))
    if reply.status != 200:
        raise NetError(_explain(reply))
    try:
        return json.loads(reply.body.decode('utf-8'))
    except ValueError:
        raise NetError('GitHub answered with something that is not valid JSON.')


def _asset_bytes(asset_id):
    reply = net.request(
        '%s/repos/%s/releases/assets/%s' % (api_base(), repo(), asset_id),
        _headers('application/octet-stream'))
    if reply.status != 200:
        raise NetError(_explain(reply))
    return reply.body


# ------------------------------------------------------------- the manifest

def _folder(*parts):
    return net.cache_dir(CACHE_FOLDER, *parts)


def _release_path():
    return os.path.join(_folder(), 'release.json')


def _manifest_path():
    return os.path.join(_folder(), MANIFEST_NAME)


def _log(text, level=None):
    QgsMessageLog.logMessage(text, LOG_TAG, level or Qgis.MessageLevel.Info)


def load_manifest(force=False):
    """The current manifest, as a :class:`Manifest`.

    Asks GitHub for the latest release at most once a day. A failed request
    falls back to the manifest already on disk - the dialog keeps working
    offline once it has worked online - and only raises when there is nothing
    cached either.
    """
    if not configured():
        raise NetError('The KGA database has not been set up in this copy of '
                       'the plugin yet.')

    cached_release = net.read_cached_json(_release_path())
    cached_manifest = net.read_cached_json(_manifest_path())
    fresh = (cached_release and cached_manifest and not force
             and net.is_fresh(_release_path(), RELEASE_TTL))
    if fresh:
        return Manifest(cached_manifest, cached_release)

    try:
        release = _api_json('releases/latest')
        assets = {a['name']: {'id': a['id'], 'size': a.get('size', 0)}
                  for a in release.get('assets', [])}
        if MANIFEST_NAME not in assets:
            raise NetError('The latest KGA release has no %s attached.'
                           % MANIFEST_NAME)
        info = {'tag': release.get('tag_name', ''), 'assets': assets}

        same = (cached_release and cached_manifest
                and cached_release.get('assets', {}).get(MANIFEST_NAME)
                == assets[MANIFEST_NAME]
                and cached_release.get('tag') == info['tag'])
        if same:
            data = cached_manifest
        else:
            data = json.loads(_asset_bytes(assets[MANIFEST_NAME]['id'])
                              .decode('utf-8'))
            net.write_json_atomic(_manifest_path(), data)
        net.write_json_atomic(_release_path(), info)
        manifest = Manifest(data, info)
    except NetError as exc:
        if cached_manifest and cached_release:
            _log('KGA release not reachable, using the saved copy: %s' % exc)
            return Manifest(cached_manifest, cached_release)
        raise
    except ValueError:
        raise NetError('The KGA manifest on GitHub is not valid JSON.')

    manifest.check_schema()
    return manifest


class Manifest(object):
    """The index and pack list of one data release."""

    def __init__(self, data, release):
        self.data = data
        self.release = release
        index = data.get('index', {})
        self.provinces = index.get('provinces', [])
        self.districts = index.get('districts', [])
        self.communes = index.get('communes', [])
        self._dist_by_pro = _group(self.districts, 'parent')
        self._com_by_dist = _group(self.communes, 'parent')
        self._names = {}
        for rows in (self.provinces, self.districts, self.communes):
            for row in rows:
                self._names[row['code']] = row

    @property
    def data_version(self):
        return str(self.data.get('data_version', ''))

    @property
    def packs(self):
        return self.data.get('packs', [])

    def check_schema(self):
        if int(self.data.get('schema_version', 0)) > SUPPORTED_SCHEMA:
            raise NetError('The KGA database has moved to a newer layout than '
                           'this plugin understands. Update the KGA Toolbox '
                           'plugin.')

    def districts_of(self, pro_code):
        return self._dist_by_pro.get(pro_code, [])

    def communes_of(self, dist_code):
        return self._com_by_dist.get(dist_code, [])

    def row(self, code):
        return self._names.get(code)

    def name(self, code):
        row = self._names.get(code)
        return row['name_en'] if row and row.get('name_en') else code

    def pack_for(self, pro_code):
        for pack in self.packs:
            if (pack.get('scope') or {}).get('pro_code') == pro_code:
                return pack
        return None


def _group(rows, key):
    out = {}
    for row in rows:
        out.setdefault(row.get(key), []).append(row)
    return out


def label(row):
    """'Banteay Meanchey (បន្ទាយមានជ័យ)' - English first, Khmer beside it."""
    en = row.get('name_en') or row['code']
    kh = row.get('name_kh')
    return '%s (%s)' % (en, kh) if kh else en


# ------------------------------------------------------------------ packs

def pack_path(pack):
    """Where `pack` lives once downloaded.

    The file is named after the pack's checksum, so new content always gets a
    new name and nothing is ever overwritten. That is deliberate: GDAL keeps a
    GeoPackage open for a while after a layer built from it is gone, and on
    Windows a file in that state cannot be replaced.
    """
    return os.path.join(_folder('packs'), '%s-%s.gpkg' % (
        pack['id'], pack['sha256'][:12]))


def pack_cached(pack):
    return os.path.exists(pack_path(pack))


def ensure_pack(manifest, pack, on_progress=None):
    """The local path of `pack`'s GeoPackage, downloading it if needed.

    Returns None when `on_progress` asked to stop.
    """
    path = pack_path(pack)
    if pack_cached(pack):
        return path

    assets = manifest.release.get('assets', {})
    asset = assets.get(pack['file'])
    if asset is None:
        raise NetError('The KGA release has no file %s.' % pack['file'])

    if on_progress is not None and on_progress(0, pack.get('size', 0)) is False:
        return None
    body = _asset_bytes(asset['id'])
    if on_progress is not None:
        on_progress(len(body), pack.get('size', len(body)))

    if hashlib.sha256(body).hexdigest() != pack['sha256']:
        raise NetError('The download of %s did not match its checksum, so it '
                       'was discarded. Try again.' % pack['file'])

    part = path + '.part'
    zip_part = path + '.zip.part'
    try:
        with open(zip_part, 'wb') as fh:
            fh.write(body)
        with zipfile.ZipFile(zip_part) as zf:
            members = [n for n in zf.namelist() if n.lower().endswith('.gpkg')]
            if len(members) != 1:
                raise NetError('%s is not a KGA data pack.' % pack['file'])
            with zf.open(members[0]) as src, open(part, 'wb') as dst:
                shutil.copyfileobj(src, dst)
        os.replace(part, path)
    except zipfile.BadZipFile:
        raise NetError('%s is damaged. Try again.' % pack['file'])
    finally:
        for leftover in (part, zip_part):
            with suppress(OSError):
                os.remove(leftover)

    _retire_old_packs(manifest)
    net.trim_cache()
    return path


def _retire_old_packs(manifest):
    """Remove pack files that no pack of the current release refers to."""
    current = {os.path.basename(pack_path(p)) for p in manifest.packs}
    root = _folder('packs')
    for entry in os.listdir(root):
        if entry not in current:
            with suppress(OSError):
                os.remove(os.path.join(root, entry))


# ------------------------------------------------------------- the selection

class Plan(object):
    """What a selection means: the layer, the filter and the packs it needs."""

    def __init__(self):
        self.level = 0
        self.geometry = CENTER
        self.layer = ''
        self.key = ''
        self.expression = None
        self.pack_ids = []
        self.count = 0
        self.scope_text = ''

    @property
    def level_label(self):
        return LEVELS[self.level][2]


def _quote(code):
    if not _CODE.match(str(code)):
        raise ValueError('not a KGA code: %r' % (code,))
    return "'%s'" % code


def make_plan(manifest, level, geometry, province=None, district=None,
              commune=None, units=()):
    """Resolve a selection into a :class:`Plan`.

    `province`, `district` and `commune` narrow the scope (None means all);
    `units` are specific codes at `level`. With no units, everything inside the
    scope is wanted. Raises ``ValueError`` for a combination that has no data -
    a village boundary, which does not exist.
    """
    if level not in LEVELS:
        raise ValueError('unknown level %r' % (level,))
    if level == 4 and geometry == BOUNDARY:
        raise ValueError('Villages are points only; there is no village '
                         'boundary.')

    plan = Plan()
    plan.level = level
    plan.geometry = geometry
    plan.layer = '%s_%s' % (LEVELS[level][0], geometry)
    plan.key = LEVELS[level][1]

    units = [str(u) for u in units if u]
    if units:
        column = plan.key
        plan.expression = '"%s" IN (%s)' % (column, ', '.join(
            _quote(u) for u in units))
        provinces = sorted({u[:2] for u in units})
    else:
        provinces = [province] if province else None
        scope = None
        for column, code in (('pro_code', province), ('dist_code', district),
                             ('com_code', commune)):
            if code:
                scope = '"%s" = %s' % (column, _quote(code))
        plan.expression = scope

    if provinces is None:
        provinces = [p['code'] for p in manifest.provinces]
    packs = []
    for pro in provinces:
        pack = manifest.pack_for(pro)
        if pack is None:
            raise ValueError('The KGA release has no data pack for province '
                             '%s.' % pro)
        packs.append(pack['id'])
    plan.pack_ids = packs
    plan.count = _count(manifest, plan, units, province, district, commune)
    return plan


def _count(manifest, plan, units, province, district, commune):
    """How many features the plan will add, read from the index."""
    level, geometry = plan.level, plan.geometry

    def has(row):
        return bool(row.get('boundary')) if geometry == BOUNDARY else True

    if level == 1:
        rows = manifest.provinces
        if units:
            return sum(1 for r in rows if r['code'] in units and has(r))
        return sum(1 for r in rows if has(r))
    if level == 2:
        if units:
            rows = [d for d in manifest.districts if d['code'] in units]
        else:
            rows = [d for d in manifest.districts
                    if not province or d['parent'] == province]
        return sum(1 for r in rows if has(r))

    communes = manifest.communes
    if level == 3:
        if units:
            rows = [c for c in communes if c['code'] in units]
        else:
            rows = [c for c in communes if _in_scope(c, province, district)]
        key = 'boundary' if geometry == BOUNDARY else 'center'
        return sum(1 for r in rows if r.get(key))
    # Villages: the index holds a count per commune.
    if units:
        return len(units)
    rows = [c for c in communes if _in_scope(c, province, district, commune)]
    return sum(c.get('villages', 0) for c in rows)


def _in_scope(commune, province=None, district=None, only=None):
    code = commune['code']
    if only:
        return code == only
    if district:
        return commune['parent'] == district
    if province:
        return code[:2] == province
    return True


def download_size(manifest, plan):
    """Bytes still to download for `plan` (packs already cached cost nothing)."""
    total = 0
    for pack_id in plan.pack_ids:
        pack = next(p for p in manifest.packs if p['id'] == pack_id)
        if not pack_cached(pack):
            total += pack.get('size', 0)
    return total


def fetch_plan(manifest, plan, on_progress=None):
    """Make sure every pack `plan` needs is on disk; return their paths.

    `on_progress(received, total)` counts bytes across the packs that actually
    have to be fetched. Returns None when it asked to stop.
    """
    packs = {p['id']: p for p in manifest.packs}
    total = download_size(manifest, plan)
    paths = []
    done = 0
    for pack_id in plan.pack_ids:
        pack = packs[pack_id]
        cached = pack_cached(pack)

        def relay(received, _total, _done=done):
            if on_progress is None:
                return True
            return on_progress(_done + received, total)

        path = ensure_pack(manifest, pack, relay)
        if path is None:
            return None
        paths.append(path)
        if not cached:
            done += pack.get('size', 0)
    return paths


# ---------------------------------------------------------------- the layer

class LoadError(Exception):
    pass


def _open(path, layer):
    source = QgsVectorLayer('%s|layername=%s' % (path, layer), layer, 'ogr')
    if not source.isValid():
        raise LoadError('The KGA data pack could not be opened (layer %s). '
                        'Use Refresh to download it again.' % layer)
    return source


def build_layer(plan, paths, name):
    """One scratch layer holding the plan's features from every pack."""
    request = QgsFeatureRequest()
    if plan.expression:
        request.setFilterExpression(plan.expression)
    request.addOrderBy('"%s"' % plan.key, True, False)

    layer = None
    for path in paths:
        source = _open(path, plan.layer)
        if layer is None:
            layer = source.materialize(request)
            if layer is None or not layer.isValid():
                raise LoadError('The layer could not be copied into memory.')
        else:
            features = list(source.getFeatures(request))
            if features:
                layer.dataProvider().addFeatures(features)
        del source

    if layer is None or layer.featureCount() == 0:
        raise LoadError('The KGA database holds nothing for that selection.')
    layer.setName(name)
    layer.updateExtents()
    return layer


def village_rows(manifest, pro_code, on_progress=None):
    """[(vil_code, name_en, name_kh, com_code)] for one province, by code.

    Villages are not in the manifest (14,000 rows would triple it), so listing
    them downloads that province's pack - which would be needed to add them
    anyway.
    """
    pack = manifest.pack_for(pro_code)
    if pack is None:
        return []
    path = ensure_pack(manifest, pack, on_progress)
    if path is None:
        return []
    source = _open(path, 'village_center')
    rows = []
    for feature in source.getFeatures():
        rows.append((feature['vil_code'] or '', feature['name_en'] or '',
                     feature['name_kh'] or '', feature['com_code'] or ''))
    del source
    rows.sort(key=lambda r: (r[3], r[0], r[2]))
    return rows


def forget_cache():
    """Drop everything cached for the KGA database."""
    shutil.rmtree(_folder(), ignore_errors=True)
    return True

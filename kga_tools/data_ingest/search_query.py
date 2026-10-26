"""Search language for the Data Explorer.

Plain words search everything (file names, layer names inside GeoPackages
and geodatabases, field names, CRS, geometry type, folder path), the way
Premiere Pro searches "All Metadata". On top of that:

  roads river            both words (AND is implicit)
  roads OR streets       either word
  roads -draft           exclude (also: NOT draft)
  "land use"             exact phrase
  (roads OR rivers) 2025 grouping with parentheses
  dem*  *_utm  v?        wildcards anywhere
  name:  layer:  field:  crs:/epsg:  ext:  type:  geom:  in:/path:
  size>100mb  size<2gb   size in b, kb, mb, gb, tb
  modified>2025-01-01    also <, >=, <=, or modified:7d / 4w / 6m / 1y
  features>1000          feature count of any layer

Case, accents, underscores, hyphens and dots are ignored, so
"land_use", "Land-Use" and "landuse" all find each other. If nothing
matches, typo-tolerant (fuzzy) matching is tried automatically.
"""

import datetime
import fnmatch
import functools
import re
import time
import unicodedata
from difflib import SequenceMatcher

FIELD_ALIASES = {
    "name": "name", "file": "name", "title": "name",
    "layer": "layer", "layers": "layer", "fc": "layer", "table": "layer",
    "field": "field", "fields": "field", "column": "field", "col": "field", "attr": "field",
    "crs": "crs", "epsg": "crs", "srs": "crs", "proj": "crs",
    "ext": "ext", "extension": "ext", "format": "ext",
    "type": "type", "kind": "type",
    "geom": "geom", "geometry": "geom",
    "in": "path", "path": "path", "folder": "path", "dir": "path",
}
CMP_KEYS = {"size": "size", "modified": "modified", "date": "modified", "mtime": "modified",
            "features": "features", "count": "features", "feat": "features",
            "after": "after", "before": "before", "newer": "newer"}

WEIGHTS = {"name": 10.0, "layer": 7.0, "ext": 5.0, "type": 5.0, "field": 4.0,
           "crs": 3.0, "geom": 3.0, "path": 2.0}

TYPE_SYNONYMS = {
    "image": "raster", "imagery": "raster", "grid": "raster", "dem": "raster",
    "satellite": "raster", "tif": "raster",
    "shape": "vector", "feature": "vector", "features": "vector", "featureclass": "vector",
    "csv": "table", "excel": "table", "spreadsheet": "table", "attribute": "table",
    "lidar": "pointcloud", "las": "pointcloud", "point cloud": "pointcloud",
    "gdb": "file geodatabase", "geodatabase": "file geodatabase", "gpkg": "geopackage",
}

SIZE_UNITS = {"": 1, "b": 1, "k": 1024, "kb": 1024, "m": 1024 ** 2, "mb": 1024 ** 2,
              "g": 1024 ** 3, "gb": 1024 ** 3, "t": 1024 ** 4, "tb": 1024 ** 4}


class QueryError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

_SEP = re.compile(r"[\s_\-./\\:,;()\[\]]+")


@functools.lru_cache(maxsize=262144)
def _norm_cached(text):
    if all(ord(c) < 0x0250 for c in text):
        # Latin text: fold accents (é -> e) so "Kampong Cham" finds "Kâmpóng Cham"
        t = unicodedata.normalize("NFKD", text)
        t = "".join(c for c in t if not unicodedata.combining(c)) or text
    else:
        # Khmer, Thai, Lao, etc.: vowel signs are combining marks - keep them
        t = unicodedata.normalize("NFC", text)
    return _SEP.sub(" ", t.casefold()).strip()


def norm(text):
    return _norm_cached(str(text or ""))


def squash(text):
    return text.replace(" ", "")


# ---------------------------------------------------------------------------
# Tokenizer / parser
# ---------------------------------------------------------------------------

_TOKEN = re.compile(r'''
    (?P<lpar>\() | (?P<rpar>\)) |
    (?P<neg>-)?(?P<key>[A-Za-z]+)(?P<op>>=|<=|>|<|:|=)(?P<qval>"[^"]*"?)? (?P<val>[^\s()]*) |
    (?P<nphrase>-)?"(?P<phrase>[^"]*)"? |
    (?P<word>[^\s()"]+)
''', re.VERBOSE)


def tokenize(text):
    tokens = []
    pos = 0
    text = text.strip()
    while pos < len(text):
        if text[pos].isspace():
            pos += 1
            continue
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            tokens.append(("word", text[pos]))
            pos += 1
            continue
        pos = m.end()
        if m.group("lpar"):
            tokens.append(("lpar", "("))
        elif m.group("rpar"):
            tokens.append(("rpar", ")"))
        elif m.group("key") is not None and (m.group("key").lower() in FIELD_ALIASES or
                                               m.group("key").lower() in CMP_KEYS):
            value = m.group("qval") or ""
            value = value.strip('"') + (m.group("val") or "") if value else (m.group("val") or "")
            if m.group("neg"):
                tokens.append(("not", "-"))
            tokens.append(("field", (m.group("key").lower(), m.group("op"), value,
                                     bool(m.group("qval")))))
        elif m.group("key") is not None:
            # looks like key:value but key unknown -> treat as plain words
            raw = m.group(0)
            if raw.startswith("-"):
                tokens.append(("not", "-"))
                raw = raw[1:]
            tokens.append(("word", raw))
        elif m.group("phrase") is not None:
            if m.group("nphrase"):
                tokens.append(("not", "-"))
            tokens.append(("phrase", m.group("phrase")))
        else:
            word = m.group("word")
            if word in ("OR", "|", "||"):
                tokens.append(("or", word))
            elif word in ("AND", "&", "&&"):
                tokens.append(("and", word))
            elif word == "NOT":
                tokens.append(("not", word))
            elif word.startswith("-") and len(word) > 1:
                tokens.append(("not", "-"))
                tokens.append(("word", word[1:]))
            else:
                tokens.append(("word", word))
    return tokens


class Parser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.i = 0

    def peek(self):
        return self.tokens[self.i] if self.i < len(self.tokens) else (None, None)

    def take(self):
        tok = self.peek()
        self.i += 1
        return tok

    def parse(self):
        if not self.tokens:
            return None
        node = self.or_expr()
        while self.i < len(self.tokens):  # tolerate stray ')' etc.
            self.take()
            nxt = self.or_expr() if self.i < len(self.tokens) else None
            if nxt is not None:
                node = ("and", [node, nxt])
        return node

    def or_expr(self):
        parts = [self.and_expr()]
        while self.peek()[0] == "or":
            self.take()
            parts.append(self.and_expr())
        parts = [p for p in parts if p is not None]
        return parts[0] if len(parts) == 1 else ("or", parts) if parts else None

    def and_expr(self):
        parts = []
        while True:
            kind, _ = self.peek()
            if kind in (None, "or", "rpar"):
                break
            if kind == "and":
                self.take()
                continue
            node = self.unary()
            if node is not None:
                parts.append(node)
        if not parts:
            return None
        return parts[0] if len(parts) == 1 else ("and", parts)

    def unary(self):
        kind, _ = self.peek()
        if kind == "not":
            self.take()
            inner = self.unary()
            return ("not", inner) if inner is not None else None
        return self.atom()

    def atom(self):
        kind, value = self.take()
        if kind == "lpar":
            node = self.or_expr()
            if self.peek()[0] == "rpar":
                self.take()
            return node
        if kind == "word":
            return ("text", None, value, False)
        if kind == "phrase":
            return ("text", None, value, True) if value.strip() else None
        if kind == "field":
            key, op, val, quoted = value
            if key in CMP_KEYS:
                return make_cmp(CMP_KEYS[key], op, val)
            if not val:
                return None
            return ("text", FIELD_ALIASES[key], val, quoted)
        return None


def parse(text):
    return Parser(tokenize(text or "")).parse()


# ---------------------------------------------------------------------------
# Comparison terms
# ---------------------------------------------------------------------------

def parse_size(text):
    m = re.fullmatch(r"\s*([\d.]+)\s*([kmgt]?b?)\s*", text.lower())
    if not m:
        raise QueryError("Size must look like 500kb, 20mb or 1.5gb (got %r)" % text)
    return float(m.group(1)) * SIZE_UNITS[m.group(2)]


def parse_date(text):
    t = text.strip().lower()
    now = time.time()
    if t in ("today",):
        d = datetime.date.today()
        return time.mktime(d.timetuple())
    if t == "yesterday":
        d = datetime.date.today() - datetime.timedelta(days=1)
        return time.mktime(d.timetuple())
    m = re.fullmatch(r"(\d+)\s*([dwmy])", t)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        days = {"d": 1, "w": 7, "m": 30.44, "y": 365.25}[unit] * n
        return now - days * 86400
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%Y-%m", "%Y"):
        try:
            return time.mktime(datetime.datetime.strptime(t, fmt).timetuple())
        except ValueError:
            continue
    raise QueryError("Date must look like 2025-06-30, 2025-06, 2025, today or 7d / 4w / 6m / 1y (got %r)" % text)


def make_cmp(key, op, value):
    if not value:
        return None
    if key in ("after", "newer"):
        key, op = "modified", ">="
    elif key == "before":
        key, op = "modified", "<"
    if key == "modified" and op in (":", "="):
        # modified:7d -> within the last 7 days; modified:2025 -> that year onwards
        op = ">="
    if op in (":", "="):
        op = "="
    if key == "size":
        return ("cmp", key, op, parse_size(value))
    if key == "modified":
        return ("cmp", key, op, parse_date(value))
    if key == "features":
        try:
            return ("cmp", key, op, float(value.replace(",", "")))
        except ValueError:
            raise QueryError("features needs a number (got %r)" % value)
    return None


def _compare(a, op, b):
    if a is None:
        return False
    return {">": a > b, "<": a < b, ">=": a >= b, "<=": a <= b, "=": a == b}[op]


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def _record_candidates(record):
    """Pre-normalised searchable texts of a record (cached on the record).
    Each entry: (tags, label, text, layer, normed, squashed, words, casefolded)."""
    cands = record.get("_cands")
    if cands is not None:
        return cands
    cands = []

    def add(tags, label, text, layer=None):
        if not text:
            return
        n = norm(text)
        if n:
            cands.append((tags, label, text, layer, n, squash(n), n.split(), str(text).casefold()))

    add(("name",), "name", record["name"])
    base = record["path"].replace("\\", "/").rsplit("/", 1)[-1]
    if base != record["name"]:
        add(("name",), "name", base)
    add(("ext",), "ext", (record.get("ext") or "").lstrip("."))
    add(("type",), "type", record.get("kind", ""))
    add(("type",), "type", record.get("container", ""))
    for lyr in record.get("layers", []):
        lname = lyr.get("name", "")
        add(("layer",), "layer", lname, lname)
        for f in lyr.get("fields", []):
            add(("field",), "field", f, lname)
        add(("crs",), "crs", lyr.get("crs"), lname)
        add(("crs",), "crs", lyr.get("crs_name"), lname)
        add(("geom", "type"), "geom", lyr.get("geom"), lname)
        add(("type",), "type", lyr.get("kind"), lname)
    add(("path",), "path", record.get("folder_display") or record.get("folder", ""))
    record["_cands"] = cands
    return cands


def _compile_term(field, value, phrase):
    if field == "type":
        value = _type_value(value)
    wild = any(c in value for c in "*?")
    v = norm(value)
    term = {"field": field, "value": value, "phrase": phrase, "wild": wild, "norm": v,
            "squash": squash(v), "cf": value.casefold(), "fuzzy_words": None}
    if wild:
        term["re_cf"] = re.compile(fnmatch.translate(value.casefold()))
        term["re_norm"] = re.compile(fnmatch.translate(v))
        term["re_squash"] = re.compile(fnmatch.translate(squash(v)))
    return term


def compile_tree(node):
    if node is None:
        return None
    kind = node[0]
    if kind in ("and", "or"):
        return (kind, [compile_tree(c) for c in node[1]])
    if kind == "not":
        return ("not", compile_tree(node[1]))
    if kind == "text":
        _, field, value, phrase = node
        return ("term", _compile_term(field, value, phrase))
    return node


def _term_score(term, cand, fuzzy):
    """0..1 similarity of a compiled query term to one candidate text."""
    _tags, label, _text, _layer, t, ts, words, cf = cand
    if label == "ext":
        return (1.0, False) if t == term["norm"] else (0.0, False)
    if term["wild"]:
        if term["re_cf"].match(cf) or term["re_norm"].match(t) or term["re_squash"].match(ts):
            return 0.95, False
        rn = term["re_norm"]
        if any(rn.match(w) for w in words):
            return 0.8, False
        return 0.0, False
    v, sv = term["norm"], term["squash"]
    if not v:
        return 0.0, False
    if t == v or ts == sv:
        return 1.0, False
    if term["phrase"]:
        return (0.8, False) if v in t or sv in ts else (0.0, False)
    if v in t:
        if t.startswith(v) or (" " + v) in t:
            return 0.85, False
        return 0.7, False
    if sv in ts:
        return 0.6, False
    fw = term.get("fuzzy_words")
    if fuzzy and fw and label in FUZZY_LABELS:
        best = max([fw.get(w, 0.0) for w in words] + [fw.get(ts, 0.0)])
        if best:
            return 0.4 * best, True
    return 0.0, False


FUZZY_LABELS = ("name", "layer", "field")


def _prepare_fuzzy(tree, records):
    """Typo tolerance: compare each query word once against the vocabulary
    of distinct words in the index (not once per dataset)."""
    terms = []

    def collect(node):
        if node is None:
            return
        if node[0] == "term":
            t = node[1]
            if not t["wild"] and not t["phrase"] and len(t["squash"]) >= 4:
                terms.append(t)
        elif node[0] in ("and", "or"):
            for c in node[1]:
                collect(c)
    collect(tree)
    if not terms:
        return False
    vocab = set()
    for rec in records:
        for cand in _record_candidates(rec):
            if cand[1] in FUZZY_LABELS:
                vocab.update(cand[6])
                if len(cand[6]) > 1:
                    vocab.add(cand[5])
    for t in terms:
        sv, words = t["squash"], {}
        for w in vocab:
            if abs(len(w) - len(sv)) > 3:
                continue
            m = SequenceMatcher(None, sv, w)
            if m.real_quick_ratio() < 0.78 or m.quick_ratio() < 0.78:
                continue
            r = m.ratio()
            if r >= 0.78:
                words[w] = r
        t["fuzzy_words"] = words
    return any(t["fuzzy_words"] for t in terms)


def _type_value(value):
    v = norm(value)
    return TYPE_SYNONYMS.get(v, v)


def _eval(node, record, fuzzy):
    """Return (matched, score, reasons, layers)."""
    kind = node[0]
    if kind == "and":
        total, reasons, layers = 0.0, [], set()
        for child in node[1]:
            ok, s, r, lay_set = _eval(child, record, fuzzy)
            if not ok:
                return False, 0.0, [], set()
            total += s
            reasons += r
            layers |= lay_set
        return True, total, reasons, layers
    if kind == "or":
        best = (False, 0.0, [], set())
        for child in node[1]:
            res = _eval(child, record, fuzzy)
            if res[0] and (not best[0] or res[1] > best[1]):
                best = res
        return best
    if kind == "not":
        ok, _s, _r, _l = _eval(node[1], record, False)
        return (not ok), 0.0, [], set()
    if kind == "cmp":
        _, key, op, value = node
        if key == "size":
            ok = _compare(record.get("size"), op, value)
        elif key == "modified":
            ok = _compare(record.get("mtime"), op, value)
        else:  # features
            counts = [lay_set.get("count", -1) for lay_set in record.get("layers", []) if lay_set.get("count", -1) >= 0]
            ok = any(_compare(c, op, value) for c in counts)
        return ok, (0.5 if ok else 0.0), [], set()
    # compiled text term
    term = node[1]
    field = term["field"]
    best, reason, layers = 0.0, None, set()
    for cand in _record_candidates(record):
        tags, label = cand[0], cand[1]
        if field is not None and field not in tags:
            continue
        if field is None and label == "ext" and term["wild"]:
            continue
        sc, fz = _term_score(term, cand, fuzzy)
        if sc <= 0:
            continue
        score = sc * WEIGHTS.get(label if field is None else field, 1.0)
        if cand[3] is not None:
            layers.add(cand[3])
        if score > best:
            best = score
            reason = (label if field != "type" else "type", cand[2], fz)
    if best <= 0:
        return False, 0.0, [], set()
    return True, best, [reason], layers


def search(records, query_text, kinds=None, bbox=None, limit=2000):
    """Run a query. Returns (results, info) where each result is
    (record, score, reasons, matched_layers) and info has 'fuzzy' and 'error'."""
    info = {"fuzzy": False, "error": "", "total": len(records)}
    try:
        tree = compile_tree(parse(query_text))
    except QueryError as exc:
        info["error"] = str(exc)
        return [], info

    def run(fuzzy):
        out = []
        for rec in records:
            if kinds is not None and not _kind_ok(rec, kinds):
                continue
            if bbox is not None and not _bbox_ok(rec, bbox):
                continue
            if tree is None:
                out.append((rec, 0.0, [], set()))
                continue
            ok, score, reasons, layers = _eval(tree, rec, fuzzy)
            if ok:
                out.append((rec, score, reasons, layers))
        return out

    try:
        results = run(False)
        if not results and tree is not None and _has_text(tree) and _prepare_fuzzy(tree, records):
            results = run(True)
            info["fuzzy"] = bool(results)
    except QueryError as exc:
        info["error"] = str(exc)
        return [], info
    results.sort(key=lambda r: (-r[1], len(r[0]["name"]), -(r[0].get("mtime") or 0)))
    info["count"] = len(results)
    return results[:limit], info


def _has_text(node):
    if node is None:
        return False
    if node[0] in ("text", "term"):
        return True
    if node[0] in ("and", "or"):
        return any(_has_text(c) for c in node[1])
    if node[0] == "not":
        return False
    return False


def _kind_ok(rec, kinds):
    rk = {rec.get("kind")} | {lay.get("kind") for lay in rec.get("layers", [])}
    return bool(rk & set(kinds))


def _bbox_ok(rec, bbox):
    b = rec.get("bbox")
    if not b:
        return False
    return not (b[2] < bbox[0] or b[0] > bbox[2] or b[3] < bbox[1] or b[1] > bbox[3])


def describe_reason(reason):
    if not reason:
        return ""
    field, text, fuzzy = reason
    label = {"name": "name", "layer": "layer", "field": "field", "crs": "CRS", "ext": "format",
             "type": "type", "geom": "geometry", "path": "folder"}.get(field, field)
    return "%s%s: %s" % ("≈ " if fuzzy else "", label, text)


HELP_HTML = """
<h3>Search syntax</h3>
<p>Words search <b>everything</b>: file names, layer names inside GeoPackages
and geodatabases, field (column) names, CRS, geometry type and folder.
Case, underscores, hyphens and accents are ignored. Typos are tolerated when
nothing matches exactly.</p>
<table cellpadding="3">
<tr><td><code>roads river</code></td><td>both words</td></tr>
<tr><td><code>roads OR streets</code></td><td>either word</td></tr>
<tr><td><code>roads -draft</code> / <code>NOT draft</code></td><td>exclude</td></tr>
<tr><td><code>"land use"</code></td><td>exact phrase</td></tr>
<tr><td><code>(roads OR rivers) kandal</code></td><td>grouping</td></tr>
<tr><td><code>dem* &nbsp; *_utm48 &nbsp; s2?_b04</code></td><td>wildcards anywhere</td></tr>
<tr><td><code>layer:villages</code></td><td>layer / feature class name</td></tr>
<tr><td><code>field:NAME_KH</code></td><td>has a field (column) with that name</td></tr>
<tr><td><code>crs:32648</code> / <code>epsg:4326</code> / <code>crs:utm</code></td><td>coordinate system</td></tr>
<tr><td><code>type:raster</code> (vector, table, pointcloud, mesh, polygon, point, geopackage…)</td><td>data type</td></tr>
<tr><td><code>ext:tif</code> / <code>ext:gpkg</code></td><td>file format</td></tr>
<tr><td><code>in:fieldwork</code></td><td>folder path contains</td></tr>
<tr><td><code>size&gt;100mb</code> &nbsp; <code>size&lt;2gb</code></td><td>file size</td></tr>
<tr><td><code>modified&gt;2025-01-01</code> &nbsp; <code>modified:7d</code> &nbsp; <code>before:2024</code></td><td>date modified (d, w, m, y)</td></tr>
<tr><td><code>features&gt;10000</code></td><td>feature count</td></tr>
</table>
<p><b>In map extent</b> keeps only datasets that overlap the current map view.
<b>Save search</b> turns any query into a reusable <i>Search Bin</i>.</p>
"""

# -*- coding: utf-8 -*-
"""
Symbol Bag – persistent SQLite storage.

The library lives in the active QGIS user profile:
    <profile>/symbol_bag/symbol_bag.db
with rolling automatic backups in
    <profile>/symbol_bag/backups/

Everything the user creates (symbols, collections, tags, favourites,
recent history, interface preferences) is written to disk immediately;
nothing is kept only in memory.
"""

import hashlib
import json
import os
import sqlite3
import time
import uuid

from .log import log_info, log_warning

SCHEMA_VERSION = 1

# Symbol "kinds" used throughout the plugin
KIND_MARKER = "marker"
KIND_LINE = "line"
KIND_FILL = "fill"
KIND_RASTER = "raster"
VECTOR_KINDS = (KIND_MARKER, KIND_LINE, KIND_FILL)
ALL_KINDS = VECTOR_KINDS + (KIND_RASTER,)

KIND_LABELS = {
    KIND_MARKER: "Point",
    KIND_LINE: "Line",
    KIND_FILL: "Polygon",
    KIND_RASTER: "Terrain & Raster",
}

DEFAULT_COLLECTIONS = [
    "General",
    "Transportation",
    "Agriculture",
    "Administrative Boundaries",
    "Hydrology",
    "Terrain & Elevation",
    "Remote Sensing",
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS collections (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    parent_id  INTEGER REFERENCES collections(id) ON DELETE CASCADE,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS symbols (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    uid           TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL,
    subtype       TEXT DEFAULT '',
    xml           TEXT NOT NULL,
    hash          TEXT NOT NULL,
    description   TEXT DEFAULT '',
    tags          TEXT DEFAULT '',
    collection_id INTEGER REFERENCES collections(id) ON DELETE SET NULL,
    favorite      INTEGER NOT NULL DEFAULT 0,
    sort_order    INTEGER NOT NULL DEFAULT 0,
    use_count     INTEGER NOT NULL DEFAULT 0,
    last_used     REAL,
    created       REAL NOT NULL,
    modified      REAL NOT NULL,
    builtin       INTEGER NOT NULL DEFAULT 0,
    preview       BLOB
);
CREATE INDEX IF NOT EXISTS idx_symbols_hash ON symbols(hash);
CREATE INDEX IF NOT EXISTS idx_symbols_collection ON symbols(collection_id);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def content_hash(kind, xml):
    """Hash used to detect duplicate symbols (name-independent)."""
    h = hashlib.sha1(usedforsecurity=False)
    h.update(kind.encode("utf-8"))
    h.update(b"\0")
    h.update((xml or "").strip().encode("utf-8"))
    return h.hexdigest()


class SymbolRecord(object):
    """Lightweight in-memory view of a symbol row (no XML / preview)."""

    __slots__ = ("id", "uid", "name", "kind", "subtype", "description", "tags",
                 "collection_id", "favorite", "sort_order", "use_count",
                 "last_used", "created", "modified", "builtin")

    def __init__(self, row):
        for key in self.__slots__:
            setattr(self, key, row[key])

    @property
    def tag_list(self):
        return [t.strip() for t in (self.tags or "").split(",") if t.strip()]

    @property
    def kind_label(self):
        return KIND_LABELS.get(self.kind, self.kind)

    @property
    def is_raster(self):
        return self.kind == KIND_RASTER


class LibraryError(Exception):
    pass


class SymbolStore(object):
    """All database access for Symbol Bag goes through this class."""

    def __init__(self, folder):
        self.folder = folder
        self.path = os.path.join(folder, "symbol_bag.db")
        self.backup_folder = os.path.join(folder, "backups")
        os.makedirs(self.backup_folder, exist_ok=True)
        self.conn = None
        self._listeners = []
        self.open()

    # ------------------------------------------------------------------
    # connection management
    # ------------------------------------------------------------------
    def open(self):
        created = not os.path.exists(self.path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(_SCHEMA)
        self._set_meta("schema_version", str(SCHEMA_VERSION))
        self.conn.commit()
        if created:
            for i, name in enumerate(DEFAULT_COLLECTIONS):
                self.add_collection(name, sort_order=i, notify=False)
        log_info("Library opened: {}".format(self.path))
        return created

    def close(self):
        if self.conn is not None:
            try:
                self.conn.commit()
                self.conn.close()
            finally:
                self.conn = None

    def is_seeded(self):
        return self._get_meta("seeded") == "1"

    def mark_seeded(self):
        self._set_meta("seeded", "1")
        self.conn.commit()

    def _get_meta(self, key):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def _set_meta(self, key, value):
        self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))

    # ------------------------------------------------------------------
    # change notification (UI refresh)
    # ------------------------------------------------------------------
    def add_listener(self, callback):
        if callback not in self._listeners:
            self._listeners.append(callback)

    def remove_listener(self, callback):
        if callback in self._listeners:
            self._listeners.remove(callback)

    def notify(self):
        for cb in list(self._listeners):
            try:
                cb()
            except Exception as e:  # pragma: no cover - defensive
                log_warning("Listener failed: {}".format(e))

    # ------------------------------------------------------------------
    # settings
    # ------------------------------------------------------------------
    def get_setting(self, key, default=None):
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except ValueError:
            return default

    def set_setting(self, key, value):
        self.conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                          (key, json.dumps(value)))
        self.conn.commit()

    # ------------------------------------------------------------------
    # collections
    # ------------------------------------------------------------------
    def collections(self):
        rows = self.conn.execute(
            "SELECT id, name, parent_id, sort_order FROM collections ORDER BY sort_order, name COLLATE NOCASE"
        ).fetchall()
        return [dict(r) for r in rows]

    def collection(self, cid):
        row = self.conn.execute("SELECT id, name, parent_id FROM collections WHERE id=?", (cid,)).fetchone()
        return dict(row) if row else None

    def collection_by_name(self, name, parent_id=None):
        if parent_id is None:
            row = self.conn.execute(
                "SELECT id FROM collections WHERE name=? COLLATE NOCASE AND parent_id IS NULL", (name,)).fetchone()
        else:
            row = self.conn.execute(
                "SELECT id FROM collections WHERE name=? COLLATE NOCASE AND parent_id=?", (name, parent_id)).fetchone()
        return row["id"] if row else None

    def collection_path(self, cid):
        """Return the list of names from root to the collection."""
        names = []
        seen = set()
        while cid is not None and cid not in seen:
            seen.add(cid)
            c = self.collection(cid)
            if not c:
                break
            names.insert(0, c["name"])
            cid = c["parent_id"]
        return names

    def ensure_collection_path(self, names):
        """Find or create a nested collection path, returning the leaf id."""
        parent = None
        for name in names:
            name = (name or "").strip()
            if not name:
                continue
            cid = self.collection_by_name(name, parent)
            if cid is None:
                cid = self.add_collection(name, parent_id=parent, notify=False)
            parent = cid
        return parent

    def add_collection(self, name, parent_id=None, sort_order=None, notify=True):
        name = (name or "").strip()
        if not name:
            raise LibraryError("A collection name cannot be empty.")
        if self.collection_by_name(name, parent_id) is not None:
            raise LibraryError("A collection named '{}' already exists here.".format(name))
        if sort_order is None:
            row = self.conn.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM collections").fetchone()
            sort_order = row["n"]
        cur = self.conn.execute(
            "INSERT INTO collections(name, parent_id, sort_order, created) VALUES (?, ?, ?, ?)",
            (name, parent_id, sort_order, time.time()))
        self.conn.commit()
        if notify:
            self.notify()
        return cur.lastrowid

    def rename_collection(self, cid, name):
        name = (name or "").strip()
        if not name:
            raise LibraryError("A collection name cannot be empty.")
        c = self.collection(cid)
        if c is None:
            raise LibraryError("Collection not found.")
        other = self.collection_by_name(name, c["parent_id"])
        if other is not None and other != cid:
            raise LibraryError("A collection named '{}' already exists here.".format(name))
        self.conn.execute("UPDATE collections SET name=? WHERE id=?", (name, cid))
        self.conn.commit()
        self.notify()

    def move_collection(self, cid, parent_id):
        if parent_id is not None and (parent_id == cid or cid in self._ancestors(parent_id)):
            raise LibraryError("A collection cannot be moved inside itself.")
        self.conn.execute("UPDATE collections SET parent_id=? WHERE id=?", (parent_id, cid))
        self.conn.commit()
        self.notify()

    def _ancestors(self, cid):
        out = []
        seen = set()
        while cid is not None and cid not in seen:
            seen.add(cid)
            c = self.collection(cid)
            if not c:
                break
            cid = c["parent_id"]
            if cid is not None:
                out.append(cid)
        return out

    def descendant_collections(self, cid):
        """cid plus all nested sub-collections."""
        result = [cid]
        stack = [cid]
        while stack:
            current = stack.pop()
            for row in self.conn.execute("SELECT id FROM collections WHERE parent_id=?", (current,)):
                result.append(row["id"])
                stack.append(row["id"])
        return result

    def delete_collection(self, cid, delete_symbols=False):
        rows = [(i,) for i in self.descendant_collections(cid)]
        if delete_symbols:
            self.conn.executemany("DELETE FROM symbols WHERE collection_id=?", rows)
        else:
            self.conn.executemany("UPDATE symbols SET collection_id=NULL WHERE collection_id=?", rows)
        self.conn.executemany("DELETE FROM collections WHERE id=?", rows)
        self.conn.commit()
        self.notify()

    def collection_counts(self):
        rows = self.conn.execute(
            "SELECT collection_id, COUNT(*) AS n FROM symbols GROUP BY collection_id").fetchall()
        return {r["collection_id"]: r["n"] for r in rows}

    # ------------------------------------------------------------------
    # symbols
    # ------------------------------------------------------------------
    def find_duplicate(self, kind, xml):
        row = self.conn.execute("SELECT uid FROM symbols WHERE hash=?", (content_hash(kind, xml),)).fetchone()
        return row["uid"] if row else None

    def add_symbol(self, name, kind, xml, subtype="", description="", tags="",
                   collection_id=None, favorite=False, builtin=False,
                   allow_duplicate=False, notify=True):
        """Insert a symbol. Returns (uid, created_bool).

        When an identical definition already exists and allow_duplicate is
        False, the existing uid is returned and nothing is inserted.
        """
        if kind not in ALL_KINDS:
            raise LibraryError("Unknown symbol kind: {}".format(kind))
        h = content_hash(kind, xml)
        if not allow_duplicate:
            dup = self.conn.execute("SELECT uid FROM symbols WHERE hash=?", (h,)).fetchone()
            if dup:
                return dup["uid"], False
        if isinstance(tags, (list, tuple)):
            tags = ", ".join(t.strip() for t in tags if t.strip())
        now = time.time()
        uid = uuid.uuid4().hex
        row = self.conn.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM symbols").fetchone()
        self.conn.execute(
            """INSERT INTO symbols(uid, name, kind, subtype, xml, hash, description, tags,
                                   collection_id, favorite, sort_order, created, modified, builtin)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (uid, (name or "Untitled").strip() or "Untitled", kind, subtype or "", xml, h,
             description or "", tags or "", collection_id, 1 if favorite else 0, row["n"],
             now, now, 1 if builtin else 0))
        self.conn.commit()
        if notify:
            self.notify()
        return uid, True

    def symbol_records(self):
        rows = self.conn.execute(
            """SELECT id, uid, name, kind, subtype, description, tags, collection_id, favorite,
                      sort_order, use_count, last_used, created, modified, builtin
               FROM symbols ORDER BY sort_order, id""").fetchall()
        return [SymbolRecord(r) for r in rows]

    def record(self, uid):
        row = self.conn.execute(
            """SELECT id, uid, name, kind, subtype, description, tags, collection_id, favorite,
                      sort_order, use_count, last_used, created, modified, builtin
               FROM symbols WHERE uid=?""", (uid,)).fetchone()
        return SymbolRecord(row) if row else None

    def symbol_xml(self, uid):
        row = self.conn.execute("SELECT xml FROM symbols WHERE uid=?", (uid,)).fetchone()
        return row["xml"] if row else None

    def count(self):
        return self.conn.execute("SELECT COUNT(*) AS n FROM symbols").fetchone()["n"]

    # one constant statement per editable column (no SQL is built from text)
    _UPDATE_SQL = {
        "name": "UPDATE symbols SET name=? WHERE uid=?",
        "description": "UPDATE symbols SET description=? WHERE uid=?",
        "tags": "UPDATE symbols SET tags=? WHERE uid=?",
        "collection_id": "UPDATE symbols SET collection_id=? WHERE uid=?",
        "favorite": "UPDATE symbols SET favorite=? WHERE uid=?",
        "subtype": "UPDATE symbols SET subtype=? WHERE uid=?",
        "xml": "UPDATE symbols SET xml=? WHERE uid=?",
        "kind": "UPDATE symbols SET kind=? WHERE uid=?",
        "hash": "UPDATE symbols SET hash=? WHERE uid=?",
        "modified": "UPDATE symbols SET modified=? WHERE uid=?",
    }

    def update_symbol(self, uid, notify=True, **fields):
        pairs = []
        for key, value in fields.items():
            if key not in ("name", "description", "tags", "collection_id", "favorite", "subtype", "xml", "kind"):
                raise LibraryError("Field not editable: {}".format(key))
            if key == "tags" and isinstance(value, (list, tuple)):
                value = ", ".join(t.strip() for t in value if t.strip())
            if key == "favorite":
                value = 1 if value else 0
            if key == "name":
                value = (value or "").strip() or "Untitled"
            pairs.append((key, value))
        if "xml" in fields:
            rec = self.record(uid)
            kind = fields.get("kind", rec.kind if rec else KIND_MARKER)
            pairs.append(("hash", content_hash(kind, fields["xml"])))
        if not pairs:
            return
        pairs.append(("modified", time.time()))
        for key, value in pairs:
            self.conn.execute(self._UPDATE_SQL[key], (value, uid))
        if "xml" in fields:
            self.conn.execute("UPDATE symbols SET preview=NULL WHERE uid=?", (uid,))
        self.conn.commit()
        if notify:
            self.notify()

    def move_symbols(self, uids, collection_id):
        if not uids:
            return
        now = time.time()
        self.conn.executemany("UPDATE symbols SET collection_id=?, modified=? WHERE uid=?",
                              [(collection_id, now, u) for u in uids])
        self.conn.commit()
        self.notify()

    def set_favorite(self, uids, value):
        self.conn.executemany("UPDATE symbols SET favorite=? WHERE uid=?",
                              [(1 if value else 0, u) for u in uids])
        self.conn.commit()
        self.notify()

    def delete_symbols(self, uids):
        if not uids:
            return
        self.conn.executemany("DELETE FROM symbols WHERE uid=?", [(u,) for u in uids])
        self.conn.commit()
        self.notify()

    def duplicate_symbol(self, uid):
        row = self.conn.execute("SELECT * FROM symbols WHERE uid=?", (uid,)).fetchone()
        if row is None:
            raise LibraryError("Symbol not found.")
        new_uid, _ = self.add_symbol(
            self._unique_copy_name(row["name"]), row["kind"], row["xml"], row["subtype"],
            row["description"], row["tags"], row["collection_id"], False, False,
            allow_duplicate=True)
        return new_uid

    def _unique_copy_name(self, name):
        existing = {r["name"] for r in self.conn.execute("SELECT name FROM symbols")}
        base = "{} copy".format(name)
        candidate = base
        n = 2
        while candidate in existing:
            candidate = "{} {}".format(base, n)
            n += 1
        return candidate

    def reorder(self, ordered_uids):
        """Persist a manual order (list of uids, first = top)."""
        for i, uid in enumerate(ordered_uids):
            self.conn.execute("UPDATE symbols SET sort_order=? WHERE uid=?", (i, uid))
        self.conn.commit()
        self.notify()

    def mark_used(self, uid, notify=True):
        self.conn.execute("UPDATE symbols SET use_count=use_count+1, last_used=? WHERE uid=?",
                          (time.time(), uid))
        self.conn.commit()
        if notify:
            self.notify()

    def clear_recent(self):
        self.conn.execute("UPDATE symbols SET last_used=NULL")
        self.conn.commit()
        self.notify()

    # preview cache -----------------------------------------------------
    def preview(self, uid):
        row = self.conn.execute("SELECT preview FROM symbols WHERE uid=?", (uid,)).fetchone()
        return bytes(row["preview"]) if row and row["preview"] is not None else None

    def set_preview(self, uid, data):
        self.conn.execute("UPDATE symbols SET preview=? WHERE uid=?", (sqlite3.Binary(data), uid))
        self.conn.commit()

    def clear_previews(self):
        self.conn.execute("UPDATE symbols SET preview=NULL")
        self.conn.commit()

    # ------------------------------------------------------------------
    # backup / restore
    # ------------------------------------------------------------------
    def backup_to(self, path):
        """Consistent online copy of the whole library (SQLite backup API)."""
        self.conn.commit()
        if os.path.exists(path):
            os.remove(path)
        dest = sqlite3.connect(path)
        try:
            self.conn.backup(dest)
        finally:
            dest.close()
        return path

    def auto_backup(self, keep=5):
        """Rolling backup on startup – protects against accidental data loss."""
        if keep <= 0 or self.count() == 0:
            return None
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = os.path.join(self.backup_folder, "auto-{}.sbbackup".format(stamp))
        self.backup_to(path)
        backups = sorted(f for f in os.listdir(self.backup_folder) if f.startswith("auto-"))
        for old in backups[:-keep]:
            try:
                os.remove(os.path.join(self.backup_folder, old))
            except OSError:
                pass
        return path

    @staticmethod
    def validate_backup(path):
        """Raise LibraryError if path is not a Symbol Bag library."""
        if not os.path.isfile(path):
            raise LibraryError("Backup file not found.")
        try:
            conn = sqlite3.connect("file:{}?mode=ro".format(path), uri=True)
            try:
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {"symbols", "collections", "meta"}.issubset(tables):
                    raise LibraryError("This file is not a Symbol Bag backup.")
                n = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
            finally:
                conn.close()
        except sqlite3.DatabaseError as e:
            raise LibraryError("The backup file is damaged or not a SQLite database ({}).".format(e))
        return n

    def restore_from(self, path):
        """Replace the library with a backup. A safety copy is made first."""
        n = self.validate_backup(path)
        safety = os.path.join(self.backup_folder,
                              "before-restore-{}.sbbackup".format(time.strftime("%Y%m%d-%H%M%S")))
        self.backup_to(safety)
        src = sqlite3.connect(path)
        try:
            src.backup(self.conn)
        finally:
            src.close()
        self.conn.executescript(_SCHEMA)
        self.conn.commit()
        self.notify()
        return n, safety

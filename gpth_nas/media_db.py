"""
SQLite-backed media-file index.

Mirror of `index_db.IndexDB` for media files. Kept in a separate SQLite file
so the JSON sidecar index and the media index can be rebuilt independently
— useful when you change media-extension config but want to keep the JSON
ingest intact, or vice versa.

Schema:
  media_files  — one row per media file (basename, parent, stem, ext,
                 first_seg, size_bytes, mtime). first_seg is the stem up to
                 the first '-', '(' or '~' — the part Google preserves
                 across edit/paren variants. Indexed for cleanup queries.
"""
import re
import sqlite3
import time
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

_SCHEMA = """
CREATE TABLE IF NOT EXISTS media_files (
  path        TEXT PRIMARY KEY,
  basename    TEXT NOT NULL,
  parent      TEXT NOT NULL,
  stem        TEXT NOT NULL,
  ext         TEXT NOT NULL,
  first_seg   TEXT NOT NULL,
  size_bytes  INTEGER,
  mtime       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_media_basename  ON media_files(basename);
CREATE INDEX IF NOT EXISTS idx_media_parent    ON media_files(parent);
CREATE INDEX IF NOT EXISTS idx_media_stem      ON media_files(stem);
CREATE INDEX IF NOT EXISTS idx_media_first_seg ON media_files(first_seg);

CREATE TABLE IF NOT EXISTS meta (
  k TEXT PRIMARY KEY,
  v TEXT
);
"""

_FIRST_SEG_RE = re.compile(r'^([^-(~]+)')


def _first_segment(stem: str) -> str:
    m = _FIRST_SEG_RE.match(stem)
    return m.group(1) if m else stem


class MediaDB:
    def __init__(self, db_path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), isolation_level=None)
        self.conn.executescript(_SCHEMA)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA cache_size=-32000")
        self.conn.execute("PRAGMA temp_store=MEMORY")
        self._in_tx = False

    def close(self):
        if self._in_tx:
            self.commit()
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def begin(self):
        if not self._in_tx:
            self.conn.execute("BEGIN")
            self._in_tx = True

    def commit(self):
        if self._in_tx:
            self.conn.execute("COMMIT")
            self._in_tx = False

    def set_meta(self, k, v):
        self.conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, str(v)))

    def get_meta(self, k):
        r = self.conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r[0] if r else None

    def upsert_media(self, path, size_bytes: Optional[int] = None, mtime: Optional[int] = None):
        p = Path(path)
        basename = p.name
        stem = p.stem
        ext = p.suffix
        self.conn.execute(
            "INSERT OR REPLACE INTO media_files"
            "(path,basename,parent,stem,ext,first_seg,size_bytes,mtime)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (str(p), basename, str(p.parent), stem, ext,
             _first_segment(stem), size_bytes, mtime),
        )

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM media_files").fetchone()[0]

    def iter_all(self) -> Iterator[str]:
        """Yield every media path. Streams so we don't load 250K rows in memory."""
        cur = self.conn.execute("SELECT path FROM media_files ORDER BY path")
        for row in cur:
            yield row[0]

    def iter_by_first_seg(self, seg: str) -> Iterator[str]:
        cur = self.conn.execute(
            "SELECT path FROM media_files WHERE first_seg=?", (seg,)
        )
        for row in cur:
            yield row[0]

    def in_parent_with_basename(self, parent: str, basename: str) -> Optional[str]:
        r = self.conn.execute(
            "SELECT path FROM media_files WHERE parent=? AND basename=?",
            (parent, basename),
        ).fetchone()
        return r[0] if r else None

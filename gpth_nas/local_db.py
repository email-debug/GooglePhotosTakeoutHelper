"""
SQLite index of a LOCAL (non-Google) photo tree being merged into the
archive.

Kept separate from MediaDB because local files carry data the NAS index
doesn't need (EXIF DateTimeOriginal, mvhd creation_time) — and because
re-running the local scan must not touch the NAS-side indexes. One walk
populates everything; the merge then becomes pure DB matching, which
lets you tune dedup thresholds without re-reading every file's EXIF.

Schema:
  local_files
    path        TEXT PK         absolute path on the host running ingest
    basename    TEXT NOT NULL
    parent      TEXT NOT NULL
    stem        TEXT NOT NULL
    ext         TEXT NOT NULL
    first_seg   TEXT NOT NULL   stem before first '-', '(' or '~'
    size_bytes  INTEGER
    mtime       INTEGER
    year_hint   INTEGER         first 19xx/20xx in any path component
    exif_ts     INTEGER         naive-UTC seconds (TZ absorbed at match)
    mp4_ts      INTEGER         QuickTime mvhd creation_time (Unix sec)
    is_junk     INTEGER         1 if path-junk-filter dropped it
    decision    TEXT            populated by merge-local: which strategy
                                claimed it (or 'copied_new' / 'pending')
    nas_match   TEXT            sidecar/media path it deduped against
    dest_path   TEXT            destination path once copied
"""
import sqlite3
import time
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

from ._naming import first_segment

_SCHEMA = """
CREATE TABLE IF NOT EXISTS local_files (
  path        TEXT PRIMARY KEY,
  basename    TEXT NOT NULL,
  parent      TEXT NOT NULL,
  stem        TEXT NOT NULL,
  ext         TEXT NOT NULL,
  first_seg   TEXT NOT NULL,
  size_bytes  INTEGER,
  mtime       INTEGER,
  year_hint   INTEGER,
  exif_ts     INTEGER,
  mp4_ts      INTEGER,
  is_junk     INTEGER DEFAULT 0,
  decision    TEXT,
  nas_match   TEXT,
  dest_path   TEXT
);
CREATE INDEX IF NOT EXISTS idx_local_basename  ON local_files(basename);
CREATE INDEX IF NOT EXISTS idx_local_first_seg ON local_files(first_seg);
CREATE INDEX IF NOT EXISTS idx_local_exif_ts   ON local_files(exif_ts);
CREATE INDEX IF NOT EXISTS idx_local_mp4_ts    ON local_files(mp4_ts);
CREATE INDEX IF NOT EXISTS idx_local_decision  ON local_files(decision);

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


class LocalDB:
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

    def has_path(self, path) -> bool:
        r = self.conn.execute("SELECT 1 FROM local_files WHERE path=?", (str(path),)).fetchone()
        return r is not None

    def upsert(self, path, size_bytes=None, mtime=None, year_hint=None,
               exif_ts=None, mp4_ts=None, is_junk=False):
        p = Path(path)
        self.conn.execute(
            "INSERT OR REPLACE INTO local_files"
            "(path,basename,parent,stem,ext,first_seg,size_bytes,mtime,"
            " year_hint,exif_ts,mp4_ts,is_junk)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(p), p.name, str(p.parent), p.stem, p.suffix,
             first_segment(p.stem), size_bytes, mtime, year_hint,
             exif_ts, mp4_ts, 1 if is_junk else 0),
        )

    def set_decision(self, path, decision, nas_match=None, dest_path=None):
        self.conn.execute(
            "UPDATE local_files SET decision=?, nas_match=?, dest_path=?"
            " WHERE path=?",
            (decision, nas_match, dest_path, str(path)),
        )

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM local_files").fetchone()[0]

    def count_pending(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM local_files WHERE decision IS NULL AND is_junk=0"
        ).fetchone()[0]

    def iter_pending(self) -> Iterator[Tuple]:
        """Yield rows the merge pass still needs to classify. Streams so
        we never load 73K rows into memory."""
        cur = self.conn.execute(
            "SELECT path, basename, year_hint, size_bytes, exif_ts, mp4_ts"
            " FROM local_files WHERE decision IS NULL AND is_junk=0"
            " ORDER BY path"
        )
        for row in cur:
            yield row

    def decision_counts(self) -> dict:
        out = {}
        for k, v in self.conn.execute(
            "SELECT decision, COUNT(*) FROM local_files GROUP BY decision"
        ):
            out[k or 'pending'] = v
        out['junk'] = self.conn.execute(
            "SELECT COUNT(*) FROM local_files WHERE is_junk=1"
        ).fetchone()[0]
        return out

"""
SQLite-backed JSON sidecar index.

v2 holds the JSON index in dicts in memory. On a DS220j (512 MB RAM) with a
143K-file Takeout, that OOMs. This module persists everything to a single
SQLite file on disk and exposes a small query API to the matcher.

Schema:
  json_files  — one row per JSON sidecar (basename, parent, stem, title, taken_ts, raw_json).
  meta        — k/v scratch (source root, scan finish ts, etc).
  processed   — one row per media file (match_type, dest, status). Drives resumability.
"""
import json
import sqlite3
import time
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

_SCHEMA = """
CREATE TABLE IF NOT EXISTS json_files (
  path        TEXT PRIMARY KEY,
  basename    TEXT NOT NULL,
  parent      TEXT NOT NULL,
  stem        TEXT NOT NULL,
  title       TEXT,
  taken_ts    INTEGER,
  geo_lat     REAL,
  geo_lon     REAL,
  geo_alt     REAL,
  raw_json    BLOB
);
CREATE INDEX IF NOT EXISTS idx_json_basename ON json_files(basename);
CREATE INDEX IF NOT EXISTS idx_json_parent   ON json_files(parent);
CREATE INDEX IF NOT EXISTS idx_json_stem     ON json_files(stem);
CREATE INDEX IF NOT EXISTS idx_json_title    ON json_files(title);

CREATE TABLE IF NOT EXISTS meta (
  k TEXT PRIMARY KEY,
  v TEXT
);

CREATE TABLE IF NOT EXISTS processed (
  media_path  TEXT PRIMARY KEY,
  json_path   TEXT,
  match_type  TEXT,
  dest_path   TEXT,
  status      TEXT,
  ts          INTEGER
);
CREATE INDEX IF NOT EXISTS idx_proc_status ON processed(status);
CREATE INDEX IF NOT EXISTS idx_proc_match  ON processed(match_type);
"""


def _as_float(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


class IndexDB:
    def __init__(self, db_path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), isolation_level=None)
        self.conn.executescript(_SCHEMA)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA cache_size=-32000")  # 32 MB page cache
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

    # ── transactions ──────────────────────────────────────────────────────
    def begin(self):
        if not self._in_tx:
            self.conn.execute("BEGIN")
            self._in_tx = True

    def commit(self):
        if self._in_tx:
            self.conn.execute("COMMIT")
            self._in_tx = False

    # ── meta ──────────────────────────────────────────────────────────────
    def set_meta(self, k, v):
        self.conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, str(v)))

    def get_meta(self, k):
        r = self.conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r[0] if r else None

    # ── json sidecar ingest ───────────────────────────────────────────────
    @staticmethod
    def _stem_of_json(basename: str) -> str:
        """Strip .json or .supplemental-metadata.json — returns the media-name stem."""
        bl = basename.lower()
        if bl.endswith('.supplemental-metadata.json'):
            return basename[:-len('.supplemental-metadata.json')]
        if bl.endswith('.json'):
            return basename[:-len('.json')]
        return basename

    def upsert_json(self, path, parsed):
        title = None
        taken_ts = None
        lat = lon = alt = None
        if isinstance(parsed, dict):
            raw_title = parsed.get('title')
            title = raw_title if isinstance(raw_title, str) else None
            t = parsed.get('photoTakenTime') or parsed.get('creationTime') or {}
            if isinstance(t, dict):
                try:
                    taken_ts = int(t.get('timestamp')) if t.get('timestamp') else None
                except (TypeError, ValueError):
                    taken_ts = None
            geo = parsed.get('geoData') or parsed.get('geoDataExif') or {}
            if isinstance(geo, dict):
                lat = _as_float(geo.get('latitude'))
                lon = _as_float(geo.get('longitude'))
                alt = _as_float(geo.get('altitude'))
        p = Path(path)
        basename = p.name
        stem = self._stem_of_json(basename)
        raw = json.dumps(parsed) if parsed is not None else None
        self.conn.execute(
            "INSERT OR REPLACE INTO json_files"
            "(path,basename,parent,stem,title,taken_ts,geo_lat,geo_lon,geo_alt,raw_json)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (str(p), basename, str(p.parent), stem, title, taken_ts, lat, lon, alt, raw),
        )

    def has_json(self, path) -> bool:
        r = self.conn.execute(
            "SELECT 1 FROM json_files WHERE path=?", (str(path),)
        ).fetchone()
        return r is not None

    def count_json(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM json_files").fetchone()[0]

    # ── matcher queries ──────────────────────────────────────────────────
    def by_basename(self, basename: str) -> List[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT path FROM json_files WHERE basename=?", (basename,))]

    def in_parent_with_basename(self, parent: str, basename: str) -> Optional[str]:
        r = self.conn.execute(
            "SELECT path FROM json_files WHERE parent=? AND basename=?",
            (parent, basename),
        ).fetchone()
        return r[0] if r else None

    def in_parent_with_stem(self, parent: str, stem: str) -> Optional[str]:
        r = self.conn.execute(
            "SELECT path FROM json_files WHERE parent=? AND stem=?",
            (parent, stem),
        ).fetchone()
        return r[0] if r else None

    def by_stem(self, stem: str) -> List[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT path FROM json_files WHERE stem=?", (stem,))]

    def by_stem_prefix(self, prefix: str, limit: int = 64) -> List[Tuple[str, str]]:
        rows = self.conn.execute(
            "SELECT path, stem FROM json_files WHERE stem LIKE ? LIMIT ?",
            (prefix + '%', limit),
        ).fetchall()
        return [(p, s) for p, s in rows]

    def in_parent_with_stem_prefix(self, parent: str, prefix: str) -> List[Tuple[str, str]]:
        rows = self.conn.execute(
            "SELECT path, stem FROM json_files WHERE parent=? AND stem LIKE ?",
            (parent, prefix + '%'),
        ).fetchall()
        return [(p, s) for p, s in rows]

    def by_title(self, title: str) -> List[Tuple[str, str]]:
        rows = self.conn.execute(
            "SELECT path, parent FROM json_files WHERE title=?", (title,)
        ).fetchall()
        return [(p, par) for p, par in rows]

    def in_parent_with_title_stem(self, parent: str, stem: str) -> List[str]:
        """JSONs in `parent` whose 'title' field is `{stem}.{ext}` — i.e. a
        sibling media of the same stem but different extension. Used for the
        Live Photo case: a `.MP4` motion clip borrows its `.HEIC` sibling's
        sidecar."""
        rows = self.conn.execute(
            "SELECT path, title FROM json_files WHERE parent=? AND title LIKE ?",
            (parent, stem + '.%'),
        ).fetchall()
        out: List[str] = []
        for path, title in rows:
            if not title or '.' not in title:
                continue
            t_stem, _, _ = title.rpartition('.')
            if t_stem == stem:
                out.append(path)
        return out

    def get_raw_json(self, path: str):
        r = self.conn.execute(
            "SELECT raw_json FROM json_files WHERE path=?", (path,)
        ).fetchone()
        if not r or not r[0]:
            return None
        try:
            return json.loads(r[0])
        except Exception:
            return None

    def get_minimal(self, path: str):
        """Return a dict of the indexed metadata for a JSON path without re-parsing raw_json."""
        r = self.conn.execute(
            "SELECT title, taken_ts, geo_lat, geo_lon, geo_alt FROM json_files WHERE path=?",
            (path,),
        ).fetchone()
        if not r:
            return None
        return {
            'title': r[0],
            'taken_ts': r[1],
            'geo_lat': r[2],
            'geo_lon': r[3],
            'geo_alt': r[4],
        }

    # ── processed (resumability + stats) ─────────────────────────────────
    def mark_processed(self, media_path, json_path, match_type, dest_path, status):
        self.conn.execute(
            "INSERT OR REPLACE INTO processed"
            "(media_path,json_path,match_type,dest_path,status,ts)"
            " VALUES(?,?,?,?,?,?)",
            (
                str(media_path),
                str(json_path) if json_path else None,
                match_type,
                str(dest_path) if dest_path else None,
                status,
                int(time.time()),
            ),
        )

    def get_processed(self, media_path) -> Optional[dict]:
        r = self.conn.execute(
            "SELECT json_path, match_type, dest_path, status, ts"
            " FROM processed WHERE media_path=?",
            (str(media_path),),
        ).fetchone()
        if not r:
            return None
        return {
            'json_path': r[0],
            'match_type': r[1],
            'dest_path': r[2],
            'status': r[3],
            'ts': r[4],
        }

    def iter_processed(self, status: Optional[str] = None) -> Iterator[dict]:
        if status:
            cur = self.conn.execute(
                "SELECT media_path, json_path, match_type, dest_path, status"
                " FROM processed WHERE status=?", (status,))
        else:
            cur = self.conn.execute(
                "SELECT media_path, json_path, match_type, dest_path, status FROM processed")
        for row in cur:
            yield {
                'media_path': row[0],
                'json_path': row[1],
                'match_type': row[2],
                'dest_path': row[3],
                'status': row[4],
            }

    def match_type_counts(self) -> dict:
        rows = self.conn.execute(
            "SELECT COALESCE(match_type,'unmatched'), COUNT(*)"
            " FROM processed GROUP BY match_type"
        ).fetchall()
        return {k: v for k, v in rows}

    def status_counts(self) -> dict:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) FROM processed GROUP BY status"
        ).fetchall()
        return {k: v for k, v in rows}

    def count_processed(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM processed").fetchone()[0]

    def reset_processed(self):
        """Wipe the processed table — used when re-running a clean live pass."""
        self.conn.execute("DELETE FROM processed")

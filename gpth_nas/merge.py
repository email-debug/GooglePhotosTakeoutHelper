"""
Merge a non-Google local photo library into the NAS archive.

The strong dedup signal is content-fingerprint matching, not filename:
local photos uploaded to Google Photos get renamed and recompressed, but
the EXIF DateTimeOriginal (for stills) or the QuickTime mvhd creation_time
(for videos) survives that round-trip. We match those against the NAS
json_files.taken_ts index.

Timezone: EXIF datetimes are naive local time; Google's taken_ts is UTC
seconds. The matcher accepts a whole-hour offset within ±13h to absorb
the camera/upload timezone delta, requiring the residual to be within
5 seconds so random taken_ts values don't false-positive.

Strategy ladder, in confidence order:
  exif_tz       EXIF DateTimeOriginal matches a NAS JSON taken_ts at some
                whole-hour TZ offset, residual ≤5s.
  mp4_atom_tz   QuickTime mvhd creation_time matches the same way (for
                MP4/MOV/M4V where EXIF doesn't exist).
  name_size_yr  Exact basename + year ±1 + size ±15% — survives the
                no-EXIF + no-mvhd case (PNG screenshots, scanned photos).
  no_match      Copy as new, date resolved via the same EXIF/atom/name/
                folder/mtime ladder.
"""
import os
import re
import struct
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

# ── junk filters ────────────────────────────────────────────────────────
JUNK_EXTS = frozenset({
    '.ico', '.db', '.ini', '.mno', '.html', '.thm', '.lrv', '.bni',
    '.info', '.albm', '.download', '.zip', '.spj', '.url', '.lnk',
})
JUNK_NAMES = frozenset({'thumbs.db', 'desktop.ini'})
# Filename substrings that mark a file as a thumbnail or low-res preview.
# Matched case-insensitively against the stem (not the extension).
JUNK_NAME_SUBSTRINGS = ('low res', '-low', '_lowres', 'lowres', 'thumb',
                        '_thumb', '_small', '-preview', '_preview')
# Lowercased path-segment substrings that mark non-photo directories.
JUNK_PATH_TOKENS = frozenset({
    'icons', 'equilliance', 'web sites', 'resize test', '.tmp.drivedownload',
    'screenshots',  # Tim's archive convention is photos-only — skip these
})
# Lowercased basename prefixes that mark stray screenshots living outside
# a Screenshots/ folder.
JUNK_NAME_PREFIXES = ('screenshot ', 'screenshot_')

# ── TZ-aware EXIF match parameters ──────────────────────────────────────
# Whole-hour TZ offsets we'll accept. 13h covers everything from Hawaii
# to NZ + 1h DST drift.
_TZ_RANGE_HOURS = 13
# How close (in seconds) the local↔UTC offset has to be to an integer-hour
# multiple to count as a TZ match. 5s absorbs camera clock drift and the
# odd Samsung second-rounding bug without false-positives.
_TZ_RESIDUAL_S = 5

_YEAR_RE = re.compile(r'\b(19\d{2}|20\d{2})\b')


def is_junk(path: Path) -> bool:
    """Path-level filter that drops obvious non-photo files BEFORE EXIF
    reading or DB queries — both cost real wall time on a NAS."""
    name_lc = path.name.lower()
    if path.suffix.lower() in JUNK_EXTS:
        return True
    if name_lc in JUNK_NAMES:
        return True
    if name_lc.startswith(JUNK_NAME_PREFIXES):
        return True
    stem_lc = path.stem.lower()
    if any(tok in stem_lc for tok in JUNK_NAME_SUBSTRINGS):
        return True
    parts_lc = [p.lower() for p in path.parts]
    for tok in JUNK_PATH_TOKENS:
        for part in parts_lc:
            if tok in part:
                return True
    return False


def infer_year(path: Path) -> Optional[int]:
    """First plausible 19xx/20xx year token in any path component.
    Used as a coarse locality hint for the name+size fallback when EXIF
    is unavailable."""
    for part in path.parts:
        m = _YEAR_RE.search(part)
        if m:
            return int(m.group(1))
    return None


# ── EXIF DateTimeOriginal ───────────────────────────────────────────────

def exif_datetime_ts(path: Path) -> Optional[int]:
    """EXIF DateTimeOriginal as a naive-UTC Unix timestamp. We treat the
    EXIF string as if it were UTC; the matcher absorbs the actual TZ
    offset separately. Returns None for non-JPEG, missing EXIF, or
    malformed timestamps."""
    if path.suffix.lower() not in {'.jpg', '.jpeg'}:
        return None
    try:
        import piexif
    except ImportError:
        return None
    try:
        ex = piexif.load(str(path))
    except Exception:
        return None
    for ifd, tag in (
        ('Exif', piexif.ExifIFD.DateTimeOriginal),
        ('0th', piexif.ImageIFD.DateTime),
    ):
        v = ex.get(ifd, {}).get(tag)
        if not v:
            continue
        s = v.decode('utf-8', errors='replace') if isinstance(v, bytes) else v
        try:
            dt = datetime.strptime(s, '%Y:%m:%d %H:%M:%S')
            return int((dt - datetime(1970, 1, 1)).total_seconds())
        except ValueError:
            continue
    return None


# ── QuickTime mvhd creation_time ────────────────────────────────────────
# MP4/MOV files start with a tree of "boxes". The 'mvhd' box (under
# 'moov') holds the movie creation_time as seconds since 1904-01-01.
# Parsing just enough to find that one field avoids pulling in a video
# library on a 512 MB NAS.

_QT_EPOCH_OFFSET = 2082844800  # seconds between 1904-01-01 and 1970-01-01


def mp4_creation_ts(path: Path) -> Optional[int]:
    """QuickTime mvhd creation_time as Unix seconds, or None.

    We walk the box tree top-level and only into 'moov' — never descend
    into mdat (the actual media). Bounded reads only; bailing on any
    malformed length avoids running away on a corrupt file."""
    if path.suffix.lower() not in {'.mp4', '.mov', '.m4v', '.3gp', '.3g2'}:
        return None
    try:
        with open(path, 'rb') as f:
            return _read_mvhd_ts(f)
    except OSError:
        return None


def _read_box_header(f) -> Optional[Tuple[int, bytes]]:
    """Returns (size, type) or None at EOF / on malformed header."""
    head = f.read(8)
    if len(head) < 8:
        return None
    size = struct.unpack('>I', head[:4])[0]
    typ = head[4:8]
    if size == 1:
        ext = f.read(8)
        if len(ext) < 8:
            return None
        size = struct.unpack('>Q', ext)[0]
        size -= 8
    elif size == 0:
        # Box runs to EOF — caller stops via size==0.
        size = -1
    else:
        size -= 8
    return size, typ


def _read_mvhd_ts(f) -> Optional[int]:
    while True:
        hdr = _read_box_header(f)
        if hdr is None:
            return None
        size, typ = hdr
        if typ == b'moov':
            return _scan_moov_for_mvhd(f, size)
        if size < 0:
            return None
        f.seek(size, 1)


def _scan_moov_for_mvhd(f, moov_size: int) -> Optional[int]:
    end = f.tell() + moov_size if moov_size > 0 else None
    while end is None or f.tell() < end:
        hdr = _read_box_header(f)
        if hdr is None:
            return None
        size, typ = hdr
        if typ == b'mvhd':
            return _parse_mvhd(f, size)
        if size < 0:
            return None
        f.seek(size, 1)
    return None


def _parse_mvhd(f, size: int) -> Optional[int]:
    if size < 16:
        return None
    body = f.read(size)
    if len(body) < 16:
        return None
    version = body[0]
    if version == 1:
        if len(body) < 4 + 8 + 8:
            return None
        creation = struct.unpack('>Q', body[4:12])[0]
    else:
        creation = struct.unpack('>I', body[4:8])[0]
    if creation == 0:
        return None
    ts = creation - _QT_EPOCH_OFFSET
    # Cameras sometimes write 0 (or epoch-1904 with no offset); guard
    # against absurd dates.
    if not (-2208988800 < ts < 4102444800):  # 1900-01-01 .. 2100-01-01
        return None
    return ts


# ── TZ-aware match against NAS JSON taken_ts ────────────────────────────

# How tight a size match has to be to confirm an EXIF/atom dedup. Edited
# files share the original's EXIF DateTimeOriginal — only the size
# tells them apart. 5% absorbs same-format Google recompression but
# flags HEIC→JPG conversions and local edits as distinct content.
_SIZE_TOL_PCT_DEFAULT = 5


def tz_aware_match(
    local_ts: int,
    idx_db,
    local_size: Optional[int] = None,
    media_db=None,
    size_tol_pct: int = _SIZE_TOL_PCT_DEFAULT,
) -> Optional[Tuple[str, int]]:
    """Find a NAS sidecar whose taken_ts differs from local_ts by a
    whole-hour TZ offset (within ±13h, residual ≤5s). Returns
    (json_path, offset_hours) or None.

    When `media_db` and `local_size` are provided, the match additionally
    requires the NAS media file's size to be within `size_tol_pct`% of
    the local file. This is what catches edited variants — they share
    the original photo's EXIF DateTimeOriginal but differ in bytes —
    and treats them as new content rather than dropping the edit on the
    floor.

    The integer-hour gate makes the EXIF half safe: two unrelated photos
    happening to be 6h:00m:03s apart is astronomically unlikely, while
    real TZ deltas always land near an integer hour."""
    window = _TZ_RANGE_HOURS * 3600 + 60
    rows = idx_db.conn.execute(
        "SELECT path, parent, title, taken_ts FROM json_files"
        " WHERE taken_ts BETWEEN ? AND ?",
        (local_ts - window, local_ts + window),
    ).fetchall()
    best = None
    best_residual = _TZ_RESIDUAL_S + 1
    for path, parent, title, nts in rows:
        if nts is None:
            continue
        delta = nts - local_ts
        hours = round(delta / 3600.0)
        if abs(hours) > _TZ_RANGE_HOURS:
            continue
        residual = abs(delta - hours * 3600)
        if residual > _TZ_RESIDUAL_S:
            continue
        if media_db is not None and local_size:
            # The JSON's `title` is the original media filename Google
            # gave it. Looking up that basename in MediaDB (preferring
            # the same parent folder) yields the NAS file we'd be
            # deduping against.
            nas_size = _lookup_media_size(media_db, title, parent)
            if nas_size is None:
                # JSON exists but no NAS media — orphan sidecar, can't
                # confirm the file is in the archive. Skip this row.
                continue
            ratio = abs(nas_size - local_size) / max(nas_size, local_size)
            # Two ways to count as a dup:
            #   1. Sizes within ±size_tol_pct — same content.
            #   2. Local is <10% of NAS size — clearly a thumbnail /
            #      preview / low-res sample, NAS has the real file.
            #      Without this guard, a 200 KB preview of a 10 MB photo
            #      would get treated as a new artifact and copied.
            if ratio <= size_tol_pct / 100.0:
                pass  # close size → dup
            elif local_size < 0.10 * nas_size:
                pass  # thumb/preview → dup
            else:
                # Materially different in either direction — an edit, a
                # crop, a higher-quality original, or a HEIC↔JPG
                # conversion. NOT a dup; the caller copies it.
                continue
        if residual < best_residual:
            best = (path, hours)
            best_residual = residual
    return best


def _lookup_media_size(media_db, title: Optional[str], parent: Optional[str]) -> Optional[int]:
    """NAS media file size for a JSON's title, preferring the same folder
    as the JSON. Returns None when no media row matches (orphan JSON)."""
    if not title:
        return None
    if parent:
        r = media_db.conn.execute(
            "SELECT size_bytes FROM media_files WHERE basename=? AND parent=? LIMIT 1",
            (title, parent),
        ).fetchone()
        if r and r[0]:
            return r[0]
    r = media_db.conn.execute(
        "SELECT size_bytes FROM media_files WHERE basename=? AND size_bytes IS NOT NULL LIMIT 1",
        (title,),
    ).fetchone()
    return r[0] if r else None


# ── name + year + size match against NAS media_files ────────────────────

def name_year_size_match(
    basename: str,
    year: Optional[int],
    size: Optional[int],
    media_db,
    year_tol: int = 1,
    size_tol_pct: int = 15,
) -> Optional[str]:
    """Look up basename in NAS media_files, accept if the candidate's
    inferred year is within year_tol and size differs by ≤ size_tol_pct%.
    Returns the NAS path of the first acceptable match, or None when
    nothing matches OR multiple candidates match (ambiguity is safer to
    skip than to guess)."""
    rows = media_db.conn.execute(
        "SELECT path, parent, size_bytes FROM media_files WHERE basename=?",
        (basename,),
    ).fetchall()
    if not rows:
        return None
    matches = []
    for path, parent, csize in rows:
        ym = _YEAR_RE.search(parent or '')
        cyear = int(ym.group(1)) if ym else None
        if year is None or cyear is None:
            continue
        if abs(cyear - year) > year_tol:
            continue
        if size is not None and csize:
            ratio = abs(csize - size) / max(csize, size)
            if ratio > size_tol_pct / 100.0:
                continue
        matches.append(path)
    if len(matches) >= 1:
        # Multiple matches → still "in the archive" semantically; skip.
        return matches[0]
    return None


# ── resolve target date for a NEW (not-deduped) local file ──────────────

_FILENAME_DATE_PATTERNS = [
    re.compile(r'(?P<y>19\d{2}|20\d{2})[-_]?(?P<m>0[1-9]|1[0-2])[-_]?(?P<d>0[1-9]|[12]\d|3[01])'),
]


def resolve_date(path: Path, exif_ts: Optional[int],
                 mp4_ts: Optional[int]) -> datetime:
    """Pick the best date in confidence order: EXIF → mvhd → filename
    pattern → folder-year (mid-year) → mtime. Always returns a datetime
    so the copy phase can always organise to YYYY/MM/."""
    if exif_ts is not None:
        return datetime.utcfromtimestamp(exif_ts)
    if mp4_ts is not None:
        return datetime.utcfromtimestamp(mp4_ts)
    for part in (path.name, *path.parts):
        for pat in _FILENAME_DATE_PATTERNS:
            m = pat.search(part)
            if m:
                try:
                    return datetime(int(m['y']), int(m['m']), int(m['d']))
                except ValueError:
                    continue
    yr = infer_year(path)
    if yr is not None:
        return datetime(yr, 6, 15)
    try:
        return datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return datetime(1970, 1, 1)


# ── orchestrator ────────────────────────────────────────────────────────

MERGE_TYPES = {
    'exif_tz', 'mp4_atom_tz', 'name_size_yr', 'copied_new',
    'skipped_junk', 'error',
}


def classify_local(path: Path, idx_db, media_db) -> Tuple[str, Optional[str]]:
    """Return (decision, nas_match_path). decision is one of MERGE_TYPES."""
    if is_junk(path):
        return 'skipped_junk', None
    try:
        size = path.stat().st_size
    except OSError:
        return 'error', None

    exif_ts = exif_datetime_ts(path)
    if exif_ts is not None:
        hit = tz_aware_match(exif_ts, idx_db)
        if hit:
            return 'exif_tz', hit[0]

    mp4_ts = mp4_creation_ts(path)
    if mp4_ts is not None:
        hit = tz_aware_match(mp4_ts, idx_db)
        if hit:
            return 'mp4_atom_tz', hit[0]

    year = infer_year(path)
    nas_path = name_year_size_match(path.name, year, size, media_db)
    if nas_path:
        return 'name_size_yr', nas_path

    return 'copied_new', None

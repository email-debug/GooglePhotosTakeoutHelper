"""
Shared utility functions for the v2 takeout helper.

Consolidates the 3 duplicate _walk() definitions, 2 duplicate hash functions,
2 duplicate COMMON_DATETIME_PATTERNS, and scattered timestamp helpers from v1
into single, well-tested implementations.
"""
import hashlib
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterator, Optional

from .config import ALL_MEDIA_FORMATS, EXIF_DATETIME_FORMAT, EXTRA_SUFFIXES

# ── Filename date patterns ────────────────────────────────────────────────────
# Consolidated from v1's two identical copies (lines 1000 and 1998).

COMMON_DATETIME_PATTERNS = (
    # Screenshot_20190919-053857_Camera-edited.jpg
    (re.compile(r'(?P<date>20\d{2}(?:01|02|03|04|05|06|07|08|09|10|11|12)[0-3]\d-\d{6})'),
     lambda m: datetime.strptime(m.group('date'), '%Y%m%d-%H%M%S')),
    # IMG_20190509_154733-edited.jpg, MVIMG_20190215_193501.MP4
    (re.compile(r'(?P<date>20\d{2}(?:01|02|03|04|05|06|07|08|09|10|11|12)[0-3]\d_\d{6})'),
     lambda m: datetime.strptime(m.group('date'), '%Y%m%d_%H%M%S')),
    # Screenshot_2019-04-16-11-19-37-232_com.google.a.jpg
    (re.compile(r'(?P<date>20\d{2}-(?:01|02|03|04|05|06|07|08|09|10|11|12)-[0-3]\d-\d{2}-?\d{2}-?\d{2})'),
     lambda m: datetime.strptime(m.group('date'), '%Y-%m-%d-%H-%M-%S')),
)


# ── Timestamp helpers (Windows-safe) ─────────────────────────────────────────
# Windows crashes on negative timestamps, so we use manual epoch arithmetic.

if os.name == 'nt':
    _EPOCH = datetime(1970, 1, 1)

    def datetime_from_timestamp(t):
        """Convert a Unix timestamp to datetime (Windows-safe)."""
        return _EPOCH + timedelta(seconds=int(t))

    def timestamp_from_datetime(dt):
        """Convert a datetime to Unix timestamp (Windows-safe)."""
        return (dt - _EPOCH).total_seconds()
else:
    def datetime_from_timestamp(t):
        """Convert a Unix timestamp to datetime."""
        return datetime.fromtimestamp(t)

    def timestamp_from_datetime(dt):
        """Convert a datetime to Unix timestamp."""
        return dt.timestamp()


# ── File walking ──────────────────────────────────────────────────────────────
# Single implementation replacing v1's 3 duplicate _walk() functions.
# Iterative (no recursion) for safety over NAS/SMB with deep paths.

def iterative_walk(top, follow_symlinks=False):
    """
    Yield DirEntry objects for all files under `top` using iterative os.scandir.

    This replaces v1's 3 duplicate walker implementations. Uses a stack instead
    of recursion so it's safe on deeply nested network shares.

    Args:
        top: Root directory to walk.
        follow_symlinks: Whether to follow symlinks (default False).

    Yields:
        os.DirEntry objects for files only.
    """
    stack = [str(top)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=follow_symlinks):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=follow_symlinks):
                            yield entry
                    except OSError:
                        continue
        except (PermissionError, OSError):
            continue


def walk_with_dirs(top, file_fn=None, dir_fn=None, filter_fn=None,
                   follow_symlinks=False):
    """
    Walk directory tree, calling file_fn and dir_fn as encountered.

    Replaces v1's for_all_files_recursive with explicit function parameters
    instead of relying on nonlocal state.

    Args:
        top: Root directory to walk.
        file_fn: Called for each file (Path). Return value ignored.
        dir_fn: Called for each directory (Path). Return value ignored.
        filter_fn: If provided, file_fn is only called when this returns True.
        follow_symlinks: Whether to follow symlinks.
    """
    stack = [str(top)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=follow_symlinks):
                            if dir_fn is not None:
                                dir_fn(Path(entry.path))
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=follow_symlinks):
                            f = Path(entry.path)
                            if filter_fn is None or filter_fn(f):
                                if file_fn is not None:
                                    file_fn(f)
                    except OSError:
                        continue
        except (PermissionError, OSError):
            continue


def quick_count_media(photos_dir, media_formats=None):
    """
    Fast walk — count media files only, no JSON parsing.

    Args:
        photos_dir: Root directory to scan.
        media_formats: Set of lowercase extensions including dot (e.g. {'.jpg', '.mp4'}).
                       Defaults to ALL_MEDIA_FORMATS.
    """
    if media_formats is None:
        media_formats = ALL_MEDIA_FORMATS
    count = 0
    for entry in iterative_walk(photos_dir):
        ext = '.' + entry.name.lower().rsplit('.', 1)[-1] if '.' in entry.name else ''
        if ext in media_formats:
            count += 1
    return count


# ── File classification ───────────────────────────────────────────────────────

def is_media(file: Path) -> bool:
    """Check if a file is a recognized photo or video format."""
    return file.suffix.lower() in ALL_MEDIA_FORMATS


def is_photo(file: Path) -> bool:
    """Check if a file is a recognized photo format."""
    from .config import PHOTO_FORMATS
    return file.suffix.lower() in PHOTO_FORMATS


def is_video(file: Path) -> bool:
    """Check if a file is a recognized video format."""
    from .config import VIDEO_FORMATS
    return file.suffix.lower() in VIDEO_FORMATS


def is_extra_file(file: Path) -> bool:
    """Check if a file has an 'edited/effects' suffix (e.g. '-edited', '-effects')."""
    name_lower = file.name.lower()
    return any(extra in name_lower for extra in EXTRA_SUFFIXES)


def has_duplicate_paren(file: Path) -> bool:
    """Check if filename has a (N) duplicate suffix like 'photo(1).jpg'."""
    return bool(re.search(r'\(\d+\)\.', file.name))


# ── Hashing ───────────────────────────────────────────────────────────────────

def get_hash(file: Path, first_chunk_only=False, chunk_size=65536):
    """
    Compute SHA-1 hash of a file.

    SHA-1 is cryptographically weak but perfectly fine for photo deduplication
    where we're detecting identical files, not adversarial collisions.

    Args:
        file: Path to hash.
        first_chunk_only: If True, hash only the first chunk (for quick pre-filter).
        chunk_size: Size of chunks to read (default 64KB).

    Returns:
        bytes: The SHA-1 digest.
    """
    h = hashlib.sha1()
    with open(file, "rb") as f:
        if first_chunk_only:
            h.update(f.read(chunk_size))
        else:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                h.update(chunk)
    return h.digest()


def files_are_identical(path_a: Path, path_b: Path) -> bool:
    """
    Check if two files have identical content by comparing SHA-1 hashes.

    This is the safe verification that v1 was missing in its delete logic.
    """
    try:
        return get_hash(path_a) == get_hash(path_b)
    except OSError:
        return False


# ── Date extraction ───────────────────────────────────────────────────────────

def guess_date_from_filename(file: Path) -> Optional[str]:
    """
    Try to extract a date from the filename using common patterns.

    Returns date string in EXIF format (YYYY:MM:DD HH:MM:SS) or None.
    """
    for regex, extractor in COMMON_DATETIME_PATTERNS:
        m = regex.search(file.name)
        if m:
            try:
                return extractor(m).strftime(EXIF_DATETIME_FORMAT)
            except (ValueError, OverflowError):
                continue
    return None


def extract_folder_year(path: Path) -> Optional[int]:
    """
    Extract year from 'Photos from YYYY' folder names.

    Returns the year as int, or None if not found.
    """
    m = re.search(r'\bfrom\s+((?:19|20)\d{2})\b', str(path), re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


# ── Path helpers ──────────────────────────────────────────────────────────────

def new_name_if_exists(file: Path) -> Path:
    """
    If file exists, generate a new name like 'photo(1).jpg', 'photo(2).jpg', etc.

    Returns:
        Tuple of (final_path, was_renamed: bool).
    """
    if not file.exists():
        return file
    i = 1
    while True:
        new = file.with_name(f"{file.stem}({i}){file.suffix}")
        if not new.exists():
            return new
        i += 1


def sanitize_filename(name: str) -> str:
    """Remove characters that are invalid in Windows filenames."""
    return re.sub(r'[<>:"/\\|?*]', '_', name)


def format_duration(seconds: float) -> str:
    """Format seconds as human-readable duration string."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s"

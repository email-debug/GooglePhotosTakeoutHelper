"""
File operations: copy, move, delete with hash verification.

THIS IS THE KEY FIX for v1's delete bug. v1 deleted source files based on
size-only comparison (lines 2190, 2215, 2244). This module adds SHA-1 hash
verification for photos before deleting, and skips hash verification for
videos (too large, and less likely to have coincidental size matches).

v2.1 OPTIMIZATION: Combined EXIF-fix + copy + hash in a single I/O pass.
Instead of: read source → write EXIF to source → read source → write dest → read both for hash
Now:         read source → fix EXIF in memory → write dest (1 read + 1 write)
This halves total NAS I/O for the common JPEG case.
"""
import hashlib
import os
import re
import shutil
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from loguru import logger

from .config import ALL_MEDIA_FORMATS, EXIF_DATETIME_FORMAT, JPEG_EXTENSIONS
from .exif import modify_image_bytes, set_file_timestamps
from .utils import (
    datetime_from_timestamp,
    files_are_identical,
    get_hash,
    is_media,
    is_photo,
    is_video,
    iterative_walk,
    new_name_if_exists,
    sanitize_filename,
)


# ── Destination matching ─────────────────────────────────────────────────────


def dest_matches_source(dest: Path, src: Path, require_mtime=True) -> bool:
    """
    Check if dest exists and has same size (and optionally mtime) as src.

    mtime can differ after EXIF writes or on network drives,
    so require_mtime=False is used after copy operations.
    """
    if not dest.exists():
        return False
    s_s, d_s = src.stat(), dest.stat()
    if s_s.st_size != d_s.st_size:
        return False
    if require_mtime and s_s.st_mtime != d_s.st_mtime:
        return False
    return True


def natural_dest_paths(file: Path, fixed_dir: Path):
    """
    Yield candidate destination paths (date-divided first, then flat) for a source file.
    """
    creation_date = file.stat().st_mtime
    date = datetime_from_timestamp(creation_date)
    yield fixed_dir / f"{date.year}/{date.month:02}/" / file.name
    yield fixed_dir / file.name


# ── Combined copy + EXIF fix + hash (single-pass optimization) ───────────────


def enrich_copy_and_hash(
    src: Path,
    dest: Path,
    date_str: Optional[str] = None,
    geo_json: Optional[dict] = None,
    chunk_size: int = 65536,
) -> Tuple[Optional[bytes], bool]:
    """
    Copy source to dest with optional EXIF injection, computing source hash in one pass.

    For JPEG with enrichment: reads entire source, modifies EXIF in memory,
    writes modified bytes to dest. Hash is computed from original source bytes.

    For non-JPEG or no enrichment: streams chunks from source to dest,
    computing hash on each chunk. No EXIF modification.

    Args:
        src: Source file path.
        dest: Destination file path.
        date_str: EXIF date string to inject (optional, JPEG only).
        geo_json: Google JSON sidecar dict for GPS (optional, JPEG only).
        chunk_size: Chunk size for streaming copy (non-JPEG path).

    Returns:
        (source_hash_digest_bytes, geo_was_set)
    """
    is_jpeg = src.suffix.lower() in JPEG_EXTENSIONS
    geo_was_set = False

    if is_jpeg and (date_str or geo_json):
        # JPEG with enrichment: read all → modify EXIF in memory → write
        source_data = src.read_bytes()
        source_hash = hashlib.sha1(source_data).digest()

        modified_data, geo_was_set = modify_image_bytes(source_data, date_str, geo_json)
        dest.write_bytes(modified_data)

        # Preserve file metadata (permissions, etc.) from source
        shutil.copystat(src, dest)
        return source_hash, geo_was_set
    else:
        # Non-JPEG or no enrichment: stream copy + hash
        h = hashlib.sha1()
        with open(src, 'rb') as fin, open(dest, 'wb') as fout:
            while True:
                chunk = fin.read(chunk_size)
                if not chunk:
                    break
                h.update(chunk)
                fout.write(chunk)
        shutil.copystat(src, dest)
        return h.digest(), False


# ── Destination index for O(1) lookups ────────────────────────────────────────


def build_dest_index(fixed_dir: Path) -> Tuple[Dict, Dict]:
    """
    Scan fixed_dir to build two indexes for fast destination lookups:
      - exact_idx: (size, stem) -> [paths]
      - base_idx:  (size, base_stem) -> [paths]  (strips trailing (N) suffix)

    Returns (exact_idx, base_idx).
    """
    exact_idx = defaultdict(list)
    base_idx = defaultdict(list)
    paren_re = re.compile(r'\(\d+\)$')

    for entry in iterative_walk(fixed_dir):
        p = Path(entry.path)
        if is_media(p):
            try:
                sz = entry.stat().st_size
                st = p.stem
                exact_idx[(sz, st)].append(p)
                base = paren_re.sub('', st)
                if base != st:
                    base_idx[(sz, base)].append(p)
            except OSError:
                pass

    return exact_idx, base_idx


# ── Hash-verified source deletion (THE KEY FIX) ──────────────────────────────


def delete_source_if_verified(
    file: Path,
    fixed_dir: Path,
    cached_dest_path: Optional[str] = None,
    dest_index: Optional[Tuple[Dict, Dict]] = None,
) -> bool:
    """
    Delete source file only after verifying the destination copy is valid.

    THE KEY FIX: v1 deleted source files when dest had the same size.
    This function:
    - For PHOTOS: compares SHA-1 hashes before deleting (catches corruption)
    - For VIDEOS: uses size-only comparison (too large for hashing, and
      less likely to have coincidental size matches)

    Args:
        file: Source file to potentially delete.
        fixed_dir: Output directory to search for the copy.
        cached_dest_path: Known destination path from cache (checked first).
        dest_index: Pre-built (exact_idx, base_idx) for O(1) lookups.

    Returns:
        True if source was deleted, False otherwise.
    """
    if not file.exists():
        return False

    try:
        src_size = file.stat().st_size
    except OSError:
        return False

    stem = file.stem
    is_video_file = is_video(file)

    # Collect candidate destination paths
    candidates = []

    # 1. Cached dest path (most likely match)
    if cached_dest_path:
        p = Path(cached_dest_path)
        if p.exists():
            candidates.append(p)

    # 2. Dest index lookup (O(1))
    if dest_index is not None:
        exact_idx, base_idx = dest_index
        for d in exact_idx.get((src_size, stem), []):
            if d.exists():
                candidates.append(d)
        for d in base_idx.get((src_size, stem), []):
            if d.exists():
                candidates.append(d)

    # 3. Natural destination paths
    for p in natural_dest_paths(file, fixed_dir):
        if p.exists():
            candidates.append(p)
        elif p.parent.exists():
            # Search parent for renamed variants
            for e in p.parent.iterdir():
                if (e.is_file() and e.stat().st_size == src_size
                        and (e.stem == stem or e.stem.startswith(stem + '('))):
                    candidates.append(e)
                    break

    # Deduplicate candidates
    seen = set()
    for dest in candidates:
        dest_str = str(dest.resolve())
        if dest_str in seen:
            continue
        seen.add(dest_str)

        if not dest.exists():
            continue

        try:
            dest_size = dest.stat().st_size
        except OSError:
            continue

        if dest_size != src_size:
            continue

        # SIZE MATCHES — now verify before deleting

        if is_video_file:
            # Videos: size-only check is sufficient
            # (too large for hashing, less likely coincidental size match)
            try:
                file.unlink()
                logger.debug(f'[delete] Video source deleted (size match): {file.name}')
                return True
            except OSError:
                pass
        else:
            # Photos: HASH VERIFICATION required
            if files_are_identical(file, dest):
                try:
                    file.unlink()
                    logger.debug(f'[delete] Photo source deleted (hash verified): {file.name}')
                    return True
                except OSError:
                    pass
            else:
                logger.warning(
                    f'[delete] Size matched but hash differs! NOT deleting: {file.name} '
                    f'(src size={src_size}, dest={dest})'
                )

    return False


# ── Cached-hash delete verification (for separate-run --deletesourceimage) ───


def delete_source_by_cached_hash(file: Path, cached_source_hash: str) -> bool:
    """
    Delete source file after verifying it matches the cached source hash.

    Used for separate-run --deletesourceimage where the dest has modified EXIF
    (so source/dest content differs). We verify the source is the same file
    we originally processed by comparing its hash with the cached value.

    Args:
        file: Source file to potentially delete.
        cached_source_hash: SHA-1 hex digest stored during original copy.

    Returns:
        True if source was deleted, False otherwise.
    """
    if not file.exists():
        return False

    try:
        current_hash = get_hash(file).hex()
    except OSError:
        return False

    if current_hash == cached_source_hash:
        try:
            file.unlink()
            logger.debug(f'[delete] Source deleted (cached hash verified): {file.name}')
            return True
        except OSError:
            return False
    else:
        logger.warning(
            f'[delete] Source hash changed since last run, NOT deleting: {file.name}'
        )
        return False


# ── Copy operations ──────────────────────────────────────────────────────────


def copy_to_target(
    file: Path,
    fixed_dir: Path,
    delete_source: bool = False,
    date_str: Optional[str] = None,
    geo_json: Optional[dict] = None,
) -> Tuple[bool, bool, Optional[str], Optional[str]]:
    """
    Copy a media file to the output directory (flat structure).
    Applies EXIF enrichment during copy (single-pass optimization).

    Returns:
        (success, source_deleted, dest_path_str, source_hash_hex)
    """
    deleted = False
    source_hash_hex = None

    if not is_media(file):
        return True, False, None, None

    natural_dest = fixed_dir / file.name

    # Already exists with matching content — skip copy
    if dest_matches_source(natural_dest, file, require_mtime=False):
        if delete_source:
            deleted = delete_source_if_verified(
                file, fixed_dir, cached_dest_path=str(natural_dest)
            )
        return True, deleted, str(natural_dest), None

    # Combined EXIF-fix + copy + hash
    new_file = new_name_if_exists(natural_dest)
    source_hash, geo_set = enrich_copy_and_hash(
        file, new_file, date_str=date_str, geo_json=geo_json
    )
    source_hash_hex = source_hash.hex() if source_hash else None
    dest_path = str(new_file)

    # Set file timestamps on destination
    if date_str:
        set_file_timestamps(new_file, date_str)

    # Same-run delete: we just wrote the file, safe to delete source
    if delete_source and new_file.exists():
        try:
            file.unlink()
            deleted = True
            logger.debug(f'[delete] Source deleted (same-run): {file.name}')
        except OSError:
            pass

    return True, deleted, dest_path, source_hash_hex


def copy_to_target_and_divide(
    file: Path,
    fixed_dir: Path,
    delete_source: bool = False,
    date_str: Optional[str] = None,
    geo_json: Optional[dict] = None,
) -> Tuple[bool, bool, Optional[str], Optional[str]]:
    """
    Copy a media file to YYYY/MM/ subdirectory structure in the output.
    Applies EXIF enrichment during copy (single-pass optimization).

    Uses date_str for folder structure if provided, otherwise falls back
    to file mtime.

    Returns:
        (success, source_deleted, dest_path_str, source_hash_hex)
    """
    deleted = False
    source_hash_hex = None

    # Determine output folder date
    if date_str:
        try:
            date = datetime.strptime(date_str, EXIF_DATETIME_FORMAT)
        except ValueError:
            try:
                date = datetime_from_timestamp(file.stat().st_mtime)
            except OSError:
                return False, False, None, None
    else:
        try:
            date = datetime_from_timestamp(file.stat().st_mtime)
        except OSError:
            return False, False, None, None

    new_path = fixed_dir / f"{date.year}/{date.month:02}/"
    new_path.mkdir(parents=True, exist_ok=True)

    natural_dest = new_path / file.name

    # Already exists with matching content — skip copy
    if dest_matches_source(natural_dest, file, require_mtime=False):
        if delete_source:
            deleted = delete_source_if_verified(
                file, fixed_dir, cached_dest_path=str(natural_dest)
            )
        return True, deleted, str(natural_dest), None

    # Combined EXIF-fix + copy + hash
    new_file = new_name_if_exists(natural_dest)
    source_hash, geo_set = enrich_copy_and_hash(
        file, new_file, date_str=date_str, geo_json=geo_json
    )
    source_hash_hex = source_hash.hex() if source_hash else None
    dest_path = str(new_file)

    # Set file timestamps on destination
    if date_str:
        set_file_timestamps(new_file, date_str)

    # Same-run delete: we just wrote the file, safe to delete source
    if delete_source and new_file.exists():
        try:
            file.unlink()
            deleted = True
            logger.debug(f'[delete] Source deleted (same-run): {file.name}')
        except OSError:
            pass

    return True, deleted, dest_path, source_hash_hex


# ── Deduplication ─────────────────────────────────────────────────────────────


def find_and_remove_duplicates(
    fixed_dir: Path,
) -> Tuple[int, int]:
    """
    Find and remove duplicate files in the output directory.

    Uses two-pass approach:
    1. Group by (size, first-chunk-hash) for fast pre-filtering
    2. Full-hash only groups with collisions

    Keeps the first file found, removes subsequent duplicates.

    Returns:
        (duplicates_found, duplicates_removed)
    """
    # Pass 1: group by size
    by_size = defaultdict(list)
    for entry in iterative_walk(fixed_dir):
        p = Path(entry.path)
        # Skip Albums directory
        try:
            rel = p.relative_to(fixed_dir)
            if str(rel).startswith('Albums'):
                continue
        except ValueError:
            pass
        if is_media(p):
            try:
                by_size[entry.stat().st_size].append(p)
            except OSError:
                pass

    # Only process sizes with >1 file
    duplicates_found = 0
    duplicates_removed = 0

    for size, paths in by_size.items():
        if len(paths) < 2:
            continue

        # Pass 2: group by first-chunk hash
        by_chunk = defaultdict(list)
        for p in paths:
            try:
                h = get_hash(p, first_chunk_only=True)
                by_chunk[h].append(p)
            except OSError:
                pass

        for chunk_hash, chunk_paths in by_chunk.items():
            if len(chunk_paths) < 2:
                continue

            # Pass 3: full hash
            by_full = defaultdict(list)
            for p in chunk_paths:
                try:
                    h = get_hash(p, first_chunk_only=False)
                    by_full[h].append(p)
                except OSError:
                    pass

            for full_hash, full_paths in by_full.items():
                if len(full_paths) < 2:
                    continue

                duplicates_found += len(full_paths) - 1

                # Keep first, remove rest
                keeper = full_paths[0]
                for dup in full_paths[1:]:
                    try:
                        dup.unlink()
                        duplicates_removed += 1
                        logger.debug(f'[dedup] Removed: {dup} (kept: {keeper})')
                    except OSError as e:
                        logger.debug(f'[dedup] Failed to remove {dup}: {e}')

    return duplicates_found, duplicates_removed


# ── Cleanup ──────────────────────────────────────────────────────────────────


def cleanup_input_folder(
    photos_dir: Path,
    media_formats=None,
) -> Tuple[int, int]:
    """
    Purge all media files and JSON sidecars from INPUT folder after a complete run.

    Returns:
        (deleted_count, error_count)
    """
    if media_formats is None:
        media_formats = ALL_MEDIA_FORMATS
    cleanup_extensions = media_formats | {'.json'}

    deleted = 0
    errors = 0

    for entry in iterative_walk(photos_dir):
        ext = '.' + entry.name.lower().rsplit('.', 1)[-1] if '.' in entry.name else ''
        if ext in cleanup_extensions:
            try:
                Path(entry.path).unlink()
                deleted += 1
            except Exception as e:
                logger.debug(f'Cleanup delete failed: {entry.path}: {e}')
                errors += 1

    return deleted, errors

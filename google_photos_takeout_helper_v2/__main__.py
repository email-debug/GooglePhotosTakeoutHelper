"""
Google Photos Takeout Helper v2 — Main Orchestrator.

This replaces v1's monolithic 1862-line main() function with a clean,
linear pipeline. Each stage is a function call with explicit inputs/outputs.

Pipeline:
  1. Parse CLI args
  2. Set up logging + cache
  3. Prescan mode (if --prescan)
  4. Build JSON index (scan or load from cache)
  5. Process files: match JSON → fix EXIF → copy to output
  6. Dedup (if --remove-duplicates)
  7. Albums
  8. Local merge (if --merge-local)
  9. Cleanup (if --cleanup)
  10. Final report
"""
import functools
import json
import os
import re
import shutil
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from loguru import logger
from tqdm import tqdm

from .albums import build_album_map, create_albums
from .cache import CacheManager
from .cli import parse_and_validate
from .config import ALL_MEDIA_FORMATS, EXIF_DATETIME_FORMAT
from .exif import (
    get_date_str_from_json,
    get_exif_date,
    set_creation_date_from_exif,
    set_file_exif_date,
    set_file_geo_data,
    set_file_timestamps,
)
from .file_ops import (
    build_dest_index,
    cleanup_input_folder,
    copy_to_target,
    copy_to_target_and_divide,
    delete_source_by_cached_hash,
    delete_source_if_verified,
    find_and_remove_duplicates,
)
from .logging_setup import setup_logging
from .matching import build_json_index_from_cache, match_media_to_json
from .types import EnrichmentInfo, FileState, MatchResult, RunStats
from .utils import (
    datetime_from_timestamp,
    extract_folder_year,
    format_duration,
    guess_date_from_filename,
    has_duplicate_paren,
    is_extra_file,
    is_media,
    is_photo,
    is_video,
    iterative_walk,
    quick_count_media,
)


# ── JSON index building ──────────────────────────────────────────────────────


def scan_and_build_index(photos_dir: Path, limit=None):
    """
    Walk input directory, parse all JSON sidecars, count media files.

    Returns:
        (path_index, title_index, parent_index, basename_sorted,
         basename_to_paths, json_count, media_count)
    """
    path_index = {}
    title_index = defaultdict(list)
    parent_index = defaultdict(list)
    basename_to_paths = defaultdict(list)

    json_count = 0
    media_count = 0
    scan_count = 0

    for entry in iterative_walk(photos_dir):
        scan_count += 1
        if scan_count % 500 == 0:
            print(
                f"  [scanning] {scan_count:,} files | {json_count:,} JSON | {media_count:,} media...",
                end='\r', flush=True,
            )

        name_lower = entry.name.lower()

        if name_lower.endswith('.json'):
            try:
                jp = Path(entry.path)
                with open(jp, 'r', encoding='utf-8', errors='replace') as f:
                    jd = json.load(f)
                path_index[jp] = jd
                if 'title' in jd:
                    title_index[jd['title']].append((jp, jd))
                json_count += 1
            except Exception:
                pass
        elif not limit:
            ext = '.' + name_lower.rsplit('.', 1)[-1] if '.' in name_lower else ''
            if ext in ALL_MEDIA_FORMATS:
                media_count += 1

    print(
        f"  [scanning] {scan_count:,} files | {json_count:,} JSON | {media_count:,} media.   ",
        flush=True,
    )

    # Build remaining indexes
    for jp, jd in path_index.items():
        if jp.suffix.lower() == '.json':
            parent_index[jp.parent].append((jp, jd))
            basename_to_paths[jp.name].append((jp, jd))

    basename_sorted = sorted(
        (jp.name, str(jp)) for jp in path_index if jp.suffix.lower() == '.json'
    )

    if limit:
        media_count = limit

    return (path_index, title_index, parent_index, basename_sorted,
            basename_to_paths, json_count, media_count)


def persist_json_index(cache, path_index, title_index, basename_sorted,
                       input_dir, media_count):
    """Persist the JSON index to cache for fast resumption."""
    index_data = {
        'title_to_paths': {
            t: [str(p) for p, _ in entries]
            for t, entries in title_index.items()
        },
        'path_to_minimal': {
            str(p): {'photoTakenTime': d.get('photoTakenTime')} if isinstance(d, dict) else {}
            for p, d in path_index.items()
        },
        'basename_sorted': basename_sorted,
    }
    cache.set_json_index(index_data, input_dir, media_count)


# ── Album folder metadata ────────────────────────────────────────────────────


@functools.lru_cache(maxsize=None)
def _find_album_meta_json(directory: Path):
    """Find metadata.json with albumData in a directory (cached)."""
    for file in directory.rglob("*.json"):
        try:
            with open(str(file), 'r', encoding="utf-8") as f:
                d = json.load(f)
                if "albumData" in d:
                    return file, d
        except Exception:
            pass
    return None, None


def get_date_from_folder_meta(directory: Path):
    """Get date string from album metadata.json, or None."""
    file, album_dict = _find_album_meta_json(directory)
    if file is None:
        return None
    try:
        ts = int(album_dict["albumData"]["date"]["timestamp"])
        return datetime_from_timestamp(ts).strftime(EXIF_DATETIME_FORMAT)
    except (KeyError, ValueError, TypeError):
        return None


# ── Metadata resolution (no image I/O) ──────────────────────────────────────


def resolve_metadata(
    file: Path,
    path_index, title_index, parent_index, basename_sorted, basename_to_paths,
    args,
    stats: RunStats,
):
    """
    Resolve metadata for a file WITHOUT modifying the image.

    Determines the best date and GPS data from available sources. The actual
    EXIF injection happens during the combined copy pass (enrich_copy_and_hash).

    Priority for date:
    1. JSON sidecar date (highest — overrides existing EXIF)
    2. Existing EXIF date (lightweight read of just the EXIF segment)
    3. Album folder metadata
    4. Filename date patterns
    5. "Photos from YYYY" folder year

    Returns:
        (date_str, date_source, geo_json_dict, json_path_str)
        date_source is one of: 'json', 'existing_exif', 'folder_meta',
                                'filename', 'folder_year', 'none'
    """
    date_str = None
    date_source = 'none'
    geo_json = None
    json_path_str = None

    # 1. Match JSON sidecar (reads .json files only — no image I/O)
    result = match_media_to_json(
        file, path_index, title_index, parent_index, basename_sorted, basename_to_paths
    )
    if result.found:
        stats.json_matched += 1
        try:
            with open(result.json_path, 'r', encoding='utf-8', errors='replace') as f:
                google_json = json.load(f)

            json_path_str = str(result.json_path)
            geo_json = google_json  # Passed to enrich_copy_and_hash for GPS

            json_date = get_date_str_from_json(google_json)
            if json_date:
                date_str = json_date
                date_source = 'json'
        except Exception as e:
            logger.debug(f'Error reading JSON for {file}: {e}')
    else:
        stats.json_not_found += 1
        stats.no_json_files.append(str(file))

    # 2. If no date from JSON, try existing EXIF (lightweight EXIF segment read)
    if date_str is None:
        existing = get_exif_date(file)
        if existing:
            date_str = existing
            date_source = 'existing_exif'

    # 3. Album folder metadata
    if date_str is None:
        folder_date = get_date_from_folder_meta(file.parent)
        if folder_date:
            date_str = folder_date
            date_source = 'folder_meta'

    # 4. Filename date patterns
    if date_str is None and not args.no_guess_timestamp:
        fname_date = guess_date_from_filename(file)
        if fname_date:
            date_str = fname_date
            date_source = 'filename'

    # 5. "Photos from YYYY" folder year fallback
    if date_str is None:
        year = extract_folder_year(file.parent)
        if year:
            date_str = datetime(year, 1, 1, 0, 0, 0).strftime(EXIF_DATETIME_FORMAT)
            date_source = 'folder_year'

    # Update stats (mutually exclusive — no more messy decrement logic)
    if date_source == 'json':
        stats.exif_from_json += 1
    elif date_source == 'existing_exif':
        stats.exif_from_existing += 1
    elif date_source == 'folder_meta':
        stats.exif_from_folder_meta += 1
        stats.date_from_folder_files.append(str(file))
    elif date_source == 'filename':
        stats.exif_from_filename += 1
    elif date_source == 'folder_year':
        stats.exif_from_folder_year += 1
        stats.date_from_folder_files.append(str(file))
    else:
        stats.no_date_at_all += 1
        stats.no_date_files.append(str(file))
        logger.debug(f'No date source found for {file}')

    return date_str, date_source, geo_json, json_path_str


# ── Main entry point ─────────────────────────────────────────────────────────


def main():
    args = parse_and_validate()

    photos_dir = Path(args.input_folder) if args.input_folder else None
    fixed_dir = Path(args.output_folder) if args.output_folder else Path('ALL_PHOTOS')

    # Set up logging
    setup_logging(log_dir=args.final_log_dir_resolved)

    logger.info('Google Photos Takeout Helper v2')
    logger.info('==============================')

    # Set up cache
    cache = CacheManager(
        final_log_dir=args.final_log_dir_resolved,
        temp_log_dir=args.temp_log_dir_resolved,
    )
    cache.load()

    stats = RunStats()

    # ── PRESCAN MODE ──────────────────────────────────────────────────────
    if args.prescan:
        if photos_dir is None:
            logger.error("--prescan requires an INPUT folder.")
            return
        _run_prescan(photos_dir, args, cache)
        return

    if not args.prescan:
        fixed_dir.mkdir(parents=True, exist_ok=True)

    # ── GOOGLE TAKEOUT PROCESSING ─────────────────────────────────────────
    if photos_dir is not None:
        _run_takeout_processing(photos_dir, fixed_dir, args, cache, stats)

    # ── LOCAL MERGE ───────────────────────────────────────────────────────
    if args.merge_local:
        _run_local_merge(Path(args.merge_local), fixed_dir, args, stats)

    # ── DEDUPLICATION ─────────────────────────────────────────────────────
    if args.remove_duplicates:
        logger.info('=====================')
        logger.info('Finding and removing duplicates...')
        found, removed = find_and_remove_duplicates(fixed_dir)
        stats.duplicates_found = found
        stats.duplicates_removed = removed
        logger.info(f'Duplicates found: {found:,}, removed: {removed:,}')

    # ── ALBUMS ────────────────────────────────────────────────────────────
    albums_mode = args.albums.lower() if args.albums else 'none'
    if albums_mode != 'none' and photos_dir is not None:
        logger.info('=====================')
        album_map = build_album_map(photos_dir, fixed_dir)
        entries = create_albums(album_map, fixed_dir, albums_mode, photos_dir)
        stats.albums_created = len(album_map)
        stats.album_entries = entries

    # ── CLEANUP ───────────────────────────────────────────────────────────
    if args.cleanup:
        _run_cleanup(photos_dir, cache, stats)

    # ── FINAL REPORT ──────────────────────────────────────────────────────
    _print_final_report(fixed_dir, stats, photos_dir)


# ── Pipeline stages ──────────────────────────────────────────────────────────


def _run_prescan(photos_dir, args, cache):
    """Run prescan mode — scan and report without modifying files."""
    logger.info(f'PRESCAN: {photos_dir}')
    logger.info('Scanning (read-only, no files modified)...')

    if not args.force_rescan:
        cached_index = cache.get_json_index()
        if cached_index and cache.validate_for_input(str(photos_dir)):
            cached_count = cache.get_media_file_count()
            current_count = quick_count_media(photos_dir)
            if current_count == cached_count:
                logger.info(f'[cache] Source count matches ({cached_count:,}) — using cached index.')
                path_index, title_index, parent_index, basename_sorted, basename_to_paths = \
                    build_json_index_from_cache(cached_index)
                _prescan_report(photos_dir, path_index, title_index, parent_index,
                                basename_sorted, basename_to_paths, cached_count, len(path_index))
                return

    (path_index, title_index, parent_index, basename_sorted,
     basename_to_paths, json_count, media_count) = scan_and_build_index(photos_dir, args.limit)

    # Persist for future runs
    persist_json_index(cache, path_index, title_index, basename_sorted,
                       str(photos_dir), media_count)

    _prescan_report(photos_dir, path_index, title_index, parent_index,
                    basename_sorted, basename_to_paths, media_count, json_count)


def _prescan_report(photos_dir, path_index, title_index, parent_index,
                    basename_sorted, basename_to_paths, media_count, json_count):
    """Print prescan match statistics."""
    matched = 0
    unmatched = 0
    match_types = defaultdict(int)

    for entry in iterative_walk(photos_dir):
        p = Path(entry.path)
        if not is_media(p):
            continue

        result = match_media_to_json(
            p, path_index, title_index, parent_index, basename_sorted, basename_to_paths
        )
        if result.found:
            matched += 1
            match_types[result.match_type] += 1
        else:
            unmatched += 1

    total = matched + unmatched
    rate = (matched / total * 100) if total > 0 else 0

    logger.info('=====================')
    logger.info('PRESCAN RESULTS')
    logger.info('=====================')
    logger.info(f'  Media files:     {media_count:,}')
    logger.info(f'  JSON sidecars:   {json_count:,}')
    logger.info(f'  Matched:         {matched:,} ({rate:.1f}%)')
    logger.info(f'  Unmatched:       {unmatched:,}')
    if match_types:
        logger.info('  Match breakdown:')
        for mt, count in sorted(match_types.items(), key=lambda x: -x[1]):
            logger.info(f'    {mt}: {count:,}')
    logger.info('=====================')


def _run_takeout_processing(photos_dir, fixed_dir, args, cache, stats):
    """Main takeout processing: build index, fix EXIF, copy files."""

    # ── Build or load JSON index ──────────────────────────────────────────
    path_index = {}
    title_index = defaultdict(list)
    parent_index = defaultdict(list)
    basename_sorted = []
    basename_to_paths = defaultdict(list)
    media_count = args.limit or 0
    use_cached_index = False

    cached_index = cache.get_json_index()
    if cached_index and cache.validate_for_input(str(photos_dir)):
        cached_count = cache.get_media_file_count()
        logger.info(f'[cache] Found cached JSON index ({cached_count:,} files) — verifying...')
        current_count = quick_count_media(photos_dir)
        if current_count == cached_count:
            logger.info('[cache] Source count matches — loading from cache.')
            path_index, title_index, parent_index, basename_sorted, basename_to_paths = \
                build_json_index_from_cache(cached_index)
            media_count = args.limit if args.limit else cached_count
            use_cached_index = True

    if not use_cached_index:
        logger.info('Scanning all JSON and counting media files...')
        (path_index, title_index, parent_index, basename_sorted,
         basename_to_paths, json_count, media_count) = scan_and_build_index(photos_dir, args.limit)

        persist_json_index(cache, path_index, title_index, basename_sorted,
                           str(photos_dir), media_count)

    stats.input_json_files = len(path_index)
    stats.input_media_files = media_count
    logger.info(f'Input files: {media_count:,}  |  JSON indexed: {len(path_index):,}')

    # ── Count already-done from cache ─────────────────────────────────────
    already_done_keys = cache.get_already_done_keys()
    already_done = len(already_done_keys)
    already_exif_keys = cache.get_already_exif_keys()
    remaining = max(0, media_count - already_done)

    # Build dest index if needed for source deletion (only when no cached hash)
    dest_index = None
    if already_done > 0 and args.deletesourceimage and fixed_dir.exists():
        # Check if any already-done files lack a cached source_hash
        needs_dest_index = any(
            not (cache.get_file_state(k) and cache.get_file_state(k).source_hash)
            for k in list(already_done_keys)[:100]  # Sample first 100
        )
        if needs_dest_index:
            logger.info('Building destination index for deletion verification...')
            dest_index = build_dest_index(fixed_dir)

    if already_done > 0:
        logger.info(f'[cache] {already_done:,} already done, {remaining:,} remaining.')

    logger.info('=====================')
    logger.info('Resolving metadata and copying files (single-pass)...')

    # ── Single combined pass: resolve metadata → EXIF-fix + copy + hash ──
    t0 = time.perf_counter()
    bar_total = max(media_count, already_done, 1)
    bar = tqdm(
        total=bar_total, unit='files',
        bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]',
    )

    files_done = 0
    files_skipped = 0
    files_deleted = 0
    missing_dest_count = 0
    limit_counter = 0

    for entry in iterative_walk(photos_dir):
        p = Path(entry.path)

        # Filter: only media files
        if not is_media(p):
            continue

        # Skip extras if requested
        if args.skip_extras and is_extra_file(p):
            stats.skipped_extras += 1
            stats.skipped_extra_files.append(str(p))
            continue
        if args.skip_extras_harder and has_duplicate_paren(p):
            stats.skipped_extras += 1
            stats.skipped_extra_files.append(str(p))
            continue

        # Limit mode
        if args.limit is not None:
            if limit_counter >= args.limit:
                break
            limit_counter += 1

        # Relative path for cache key
        try:
            rel = str(p.relative_to(photos_dir))
        except ValueError:
            rel = p.name

        # Already done in prior run?
        if rel in already_done_keys:
            cached_state = cache.get_file_state(rel)
            cached_dest = cached_state.dest_path if cached_state else None

            # Verify destination still exists
            if cached_dest and Path(cached_dest).exists():
                files_skipped += 1
                stats.skipped_already_done += 1

                # Try to delete source if requested
                if args.deletesourceimage:
                    if cached_state and cached_state.source_hash:
                        # Use cached source hash for verification (handles EXIF-modified dest)
                        if delete_source_by_cached_hash(p, cached_state.source_hash):
                            files_deleted += 1
                            stats.source_deleted += 1
                    else:
                        # No cached hash — fall back to old dest-comparison logic
                        if delete_source_if_verified(p, fixed_dir, cached_dest, dest_index):
                            files_deleted += 1
                            stats.source_deleted += 1

                bar.set_postfix_str(
                    f'done:{files_done:,} skip:{files_skipped:,} del:{files_deleted:,}',
                    refresh=False,
                )
                bar.update()
                continue
            else:
                # Dest missing — re-copy
                missing_dest_count += 1
                stats.recopied_missing_dest += 1

        # ── Step A: Resolve metadata (no image I/O) ──────────────────────
        date_str, date_source, geo_json, json_path_str = resolve_metadata(
            p, path_index, title_index, parent_index, basename_sorted, basename_to_paths,
            args, stats,
        )

        # Track GPS from JSON for stats (actual injection happens in copy)
        has_geo = False
        if geo_json:
            from .exif import _build_gps_ifd
            has_geo = _build_gps_ifd(geo_json) is not None

        # ── Step B: Combined EXIF-fix + copy + hash (single I/O pass) ────
        if args.no_divide_to_dates:
            success, deleted, dest_path, source_hash_hex = copy_to_target(
                p, fixed_dir,
                delete_source=args.deletesourceimage,
                date_str=date_str,
                geo_json=geo_json,
            )
        else:
            success, deleted, dest_path, source_hash_hex = copy_to_target_and_divide(
                p, fixed_dir,
                delete_source=args.deletesourceimage,
                date_str=date_str,
                geo_json=geo_json,
            )

        if has_geo:
            stats.geo_set += 1

        # Update cache state
        state = FileState(
            matched=json_path_str is not None,
            match_type='json' if json_path_str else 'none',
            json_path=json_path_str,
            got_date=date_str is not None,
            got_geo=has_geo,
            exif_updated=True,
            moved=True,
            dest_path=dest_path,
            deleted_source=deleted,
            source_hash=source_hash_hex,
        )

        if deleted:
            files_deleted += 1
            stats.source_deleted += 1

        files_done += 1
        stats.copied_new += 1
        cache.mark_file_state(rel, state)
        cache.save_if_needed()

        bar.set_postfix_str(
            f'done:{files_done:,} skip:{files_skipped:,} del:{files_deleted:,}',
            refresh=False,
        )
        bar.update()

    bar.close()

    # Final cache save
    summary = {
        'moved_this_run': files_done,
        'skipped_already_done': files_skipped,
        'total_moved_all_runs': already_done + files_done,
        'deleted_source': files_deleted,
    }
    cache.save_final(summary)
    cache.move_to_final()

    elapsed = time.perf_counter() - t0
    logger.info('=====================')
    logger.info('RUN COMPLETE')
    logger.info(f'  Moved this run:           {files_done:,}')
    logger.info(f'  Already done (skipped):   {files_skipped:,}')
    logger.info(f'  Missing dest (re-copied): {missing_dest_count:,}')
    logger.info(f'  Source files deleted:      {files_deleted:,}')
    logger.info(f'  Total run time:           {format_duration(elapsed)}')
    logger.info('=====================')

    if args.limit:
        logger.info(f'--limit reached: processed {limit_counter} files.')
        logger.info(f'Inspect output in: {fixed_dir}')


def _run_local_merge(local_dir, fixed_dir, args, stats):
    """Merge a non-Google local photo folder into the archive."""
    from .exif import get_exif_date_as_datetime
    from .utils import get_hash

    if not local_dir.is_dir():
        logger.error(f"--merge-local folder not found: {local_dir}")
        return

    logger.info('=====================')
    logger.info(f'LOCAL MERGE: {local_dir}')
    logger.info('=====================')

    copied = 0
    duplicates = 0
    date_exif = 0
    date_fname = 0
    date_mtime = 0
    no_date = []

    # Build hash index of existing archive for dedup
    logger.info('Building hash index of existing archive...')
    existing_hashes = set()
    if fixed_dir.exists():
        for entry in iterative_walk(fixed_dir):
            p = Path(entry.path)
            if is_media(p):
                try:
                    existing_hashes.add(get_hash(p))
                except Exception:
                    pass
    logger.info(f'Existing archive files indexed: {len(existing_hashes):,}')

    # Collect local media files
    local_files = []
    for entry in iterative_walk(local_dir):
        p = Path(entry.path)
        if is_media(p):
            local_files.append(p)

    logger.info(f'Local media files found: {len(local_files):,}')

    bar = tqdm(total=len(local_files), unit='local-files')
    for file in local_files:
        # Dedup check
        try:
            file_hash = get_hash(file)
            if file_hash in existing_hashes:
                duplicates += 1
                bar.update()
                continue
        except Exception:
            pass

        # Date resolution
        date = None
        date_source = None

        # 1. EXIF
        exif_date = get_exif_date_as_datetime(file)
        if exif_date:
            date = exif_date
            date_source = 'exif'
            date_exif += 1

        # 2. Filename
        if date is None and not args.no_guess_timestamp:
            date_str = guess_date_from_filename(file)
            if date_str:
                try:
                    date = datetime.strptime(date_str, EXIF_DATETIME_FORMAT)
                    date_source = 'filename'
                    date_fname += 1
                except ValueError:
                    pass

        # 3. File mtime
        if date is None:
            try:
                date = datetime_from_timestamp(file.stat().st_mtime)
                date_source = 'mtime'
                date_mtime += 1
            except Exception:
                pass

        # Output path
        if not args.local_structure_preserve:
            if date:
                dest_dir = fixed_dir / f"{date.year}" / f"{date.month:02d}"
            else:
                dest_dir = fixed_dir / "0000" / "00"
                no_date.append(str(file))
        else:
            original_folder = file.parent.name or "Photos"
            if date:
                dest_dir = fixed_dir / f"{date.year}" / original_folder
            else:
                year_match = re.search(r'(19|20)\d{2}', original_folder)
                if year_match:
                    dest_dir = fixed_dir / year_match.group(0) / original_folder
                else:
                    dest_dir = fixed_dir / "0000" / "Unknown" / original_folder
                    no_date.append(str(file))

        dest_dir.mkdir(parents=True, exist_ok=True)
        from .utils import new_name_if_exists
        dest_file = new_name_if_exists(dest_dir / file.name)

        try:
            shutil.copy2(file, dest_file)
            if date:
                set_file_timestamps(dest_file, date.strftime(EXIF_DATETIME_FORMAT))
            # Register hash
            try:
                existing_hashes.add(get_hash(dest_file))
            except Exception:
                pass
            # Delete source if requested
            if args.deletesourceimage:
                delete_source_if_verified(file, fixed_dir, cached_dest_path=str(dest_file))
            copied += 1
        except Exception as e:
            logger.warning(f'Failed to copy {file}: {e}')

        bar.update()
    bar.close()

    logger.info('=====================')
    logger.info('LOCAL MERGE COMPLETE')
    logger.info(f'  Files copied:          {copied:,}')
    logger.info(f'  Duplicates skipped:    {duplicates:,}')
    logger.info(f'  Date from EXIF:        {date_exif:,}')
    logger.info(f'  Date from filename:    {date_fname:,}')
    logger.info(f'  Date from mtime:       {date_mtime:,}')
    logger.info(f'  No date (→ 0000/):     {len(no_date):,}')
    logger.info('=====================')


def _run_cleanup(photos_dir, cache, stats):
    """Purge input folder after complete run."""
    if photos_dir is None:
        logger.warning('--cleanup: no INPUT folder provided.')
        return

    # Check all files are moved
    not_moved = sum(
        1 for k, v in cache.file_states.items()
        if not v.moved
    )
    if not_moved > 0:
        logger.warning(
            f'--cleanup: {not_moved:,} files not yet moved. '
            f'INPUT will NOT be purged. Re-run to complete.'
        )
        return

    logger.info('=====================')
    logger.info('CLEANUP: All files moved — purging INPUT...')
    deleted, errors = cleanup_input_folder(photos_dir)
    logger.info(f'  Deleted: {deleted:,}')
    if errors:
        logger.warning(f'  Errors: {errors:,}')
    logger.info('=====================')


def _print_final_report(fixed_dir, stats, photos_dir=None):
    """Print the final summary report with verified counts."""
    # Count actual files in output
    actual_count = 0
    for entry in iterative_walk(fixed_dir):
        p = Path(entry.path)
        try:
            rel = p.relative_to(fixed_dir)
            if str(rel).startswith('Albums'):
                continue
        except ValueError:
            pass
        if is_media(p):
            actual_count += 1
    stats.output_file_count = actual_count

    logger.info('')
    logger.info('=' * 45)
    logger.info('FINAL REPORT')
    logger.info('=' * 45)
    logger.info(f'  Input media files:       {stats.input_media_files:,}')
    logger.info(f'  Files copied (new):      {stats.copied_new:,}')
    logger.info(f'  Already done (skipped):  {stats.skipped_already_done:,}')
    logger.info(f'  Re-copied (missing):     {stats.recopied_missing_dest:,}')
    logger.info(f'  Skipped extras:          {stats.skipped_extras:,}')
    logger.info(f'  Duplicates removed:      {stats.duplicates_removed:,}')
    logger.info(f'  Source files deleted:     {stats.source_deleted:,}')
    logger.info(f'  Total in output:         {actual_count:,}')
    if stats.albums_created:
        logger.info(f'  Albums created:          {stats.albums_created:,}')
        logger.info(f'  Album entries:           {stats.album_entries:,}')
    logger.info('')
    logger.info(f'  JSON matched:            {stats.json_matched:,}')
    logger.info(f'  JSON not found:          {stats.json_not_found:,}')
    logger.info(f'  EXIF from JSON:          {stats.exif_from_json:,}')
    logger.info(f'  EXIF from existing:      {stats.exif_from_existing:,}')
    logger.info(f'  EXIF from folder meta:   {stats.exif_from_folder_meta:,}')
    logger.info(f'  EXIF from filename:      {stats.exif_from_filename:,}')
    logger.info(f'  EXIF from folder year:   {stats.exif_from_folder_year:,}')
    logger.info(f'  No date at all:          {stats.no_date_at_all:,}')
    logger.info(f'  GPS set:                 {stats.geo_set:,}')

    # Cross-check
    warnings = stats.verify_counts()
    if warnings:
        logger.info('')
        logger.warning('COUNT VERIFICATION WARNINGS:')
        for w in warnings:
            logger.warning(f'  {w}')

    logger.info('=' * 45)

    # Write problem file lists
    stats_dir = photos_dir if photos_dir else fixed_dir
    _write_file_list(stats_dir, 'no_json_found.txt', stats.no_json_files,
                     "Files with no JSON sidecar found")
    _write_file_list(stats_dir, 'no_date.txt', stats.no_date_files,
                     "Files where no date could be determined")
    _write_file_list(stats_dir, 'exif_write_failed.txt', stats.exif_failed_files,
                     "Files where EXIF write failed")
    _write_file_list(stats_dir, 'skipped_extras.txt', stats.skipped_extra_files,
                     "Extra files that were skipped")
    _write_file_list(stats_dir, 'date_from_folder.txt', stats.date_from_folder_files,
                     "Files where date was set from folder name")


def _write_file_list(directory, filename, file_list, description):
    """Write a problem file list if non-empty."""
    if not file_list:
        return
    # Deduplicate
    file_list = list(dict.fromkeys(file_list))
    filepath = directory / filename
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(f"# {description}\n")
            f.write(f"# {len(file_list)} files\n\n")
            f.write("\n".join(file_list))
        logger.info(f'  {description}: {len(file_list):,} → {filepath}')
    except Exception:
        pass


if __name__ == '__main__':
    main()

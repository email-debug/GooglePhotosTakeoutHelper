"""
gpth-nas orchestrator.

Pipeline reuses v2's exif and utils modules where they're already correct;
everything that touched RAM-heavy in-memory indexes has been rewritten against
IndexDB.
"""
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from google_photos_takeout_helper_v2.config import (
    ALL_MEDIA_FORMATS,
    EXIF_DATETIME_FORMAT,
)
from google_photos_takeout_helper_v2.exif import (
    get_exif_date,
    set_file_exif_date,
    set_file_geo_data,
    set_file_timestamps,
)
from google_photos_takeout_helper_v2.utils import (
    guess_date_from_filename,
    iterative_walk,
)

from .albums import build_album_map, create_album_shortcuts
from .cli import parse
from .extract import extract_all_zips
from .index_db import IndexDB
from .matching import (
    _build_segment_index,
    cleanup_match_via_index,
    match_media,
)
from .media_db import MediaDB


# ── helpers ──────────────────────────────────────────────────────────────


def _is_json_sidecar(name: str) -> bool:
    nl = name.lower()
    return nl.endswith('.json') and nl != 'metadata.json'


def _is_media(name: str) -> bool:
    if '.' not in name:
        return False
    ext = '.' + name.rsplit('.', 1)[-1].lower()
    return ext in ALL_MEDIA_FORMATS


def _parse_json(path: Path):
    import json
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            return json.load(f)
    except Exception:
        return None


def _date_from_taken_ts(ts: Optional[int]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.utcfromtimestamp(int(ts))
    except (ValueError, OverflowError, OSError):
        return None


def _resolve_date(
    media_path: Path,
    json_info: Optional[dict],
) -> Tuple[Optional[datetime], str]:
    """
    Pick the best date for a media file. Returns (datetime, source_label).
    Order: JSON photoTakenTime → EXIF → filename guess → file mtime.
    """
    if json_info and json_info.get('taken_ts'):
        d = _date_from_taken_ts(json_info['taken_ts'])
        if d:
            return d, 'json'

    exif = get_exif_date(media_path)
    if exif:
        try:
            return datetime.strptime(exif, EXIF_DATETIME_FORMAT), 'exif'
        except ValueError:
            pass

    guess = guess_date_from_filename(media_path)
    if guess:
        try:
            return datetime.strptime(guess, EXIF_DATETIME_FORMAT), 'filename'
        except ValueError:
            pass

    try:
        return datetime.utcfromtimestamp(media_path.stat().st_mtime), 'mtime'
    except OSError:
        return None, 'none'


def _build_dest_path(dst_root: Path, date: Optional[datetime], filename: str) -> Path:
    if date is None:
        return dst_root / 'undated' / filename
    return dst_root / f'{date.year:04d}' / f'{date.month:02d}' / filename


def _unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    stem, suf = p.stem, p.suffix
    i = 1
    while True:
        cand = p.with_name(f'{stem}({i}){suf}')
        if not cand.exists():
            return cand
        i += 1


# ── stages ───────────────────────────────────────────────────────────────


def ingest_jsons(src: Path, db: IndexDB) -> int:
    print(f'[scan] walking {src} for JSON sidecars...')
    n = 0
    db.begin()
    batch = 0
    last_print = time.time()
    for entry in iterative_walk(src):
        if not _is_json_sidecar(entry.name):
            continue
        path = Path(entry.path)
        parsed = _parse_json(path)
        db.upsert_json(path, parsed)
        n += 1
        batch += 1
        if batch >= 2000:
            db.commit()
            db.begin()
            batch = 0
        if time.time() - last_print > 2.0:
            print(f'  [scan] {n:,} JSONs indexed...', end='\r', flush=True)
            last_print = time.time()
    db.commit()
    print(f'[scan] indexed {n:,} JSON sidecars.            ')
    return n


def ingest_media(src: Path, mdb: MediaDB) -> int:
    """Walk src once and persist every media filename to MediaDB.

    Path + parent + ext + first_seg are all that's needed downstream; we
    skip the per-file stat() that DirEntry.stat() would trigger on Linux
    (it's a real syscall there, not a scandir-cached d_type read). Saves
    a stat-per-file on slow NAS storage where the walk dominates.
    """
    print(f'[media-scan] walking {src} for media files...')
    n = 0
    mdb.begin()
    batch = 0
    last_print = time.time()
    for entry in iterative_walk(src):
        if not _is_media(entry.name):
            continue
        mdb.upsert_media(entry.path)
        n += 1
        batch += 1
        if batch >= 2000:
            mdb.commit()
            mdb.begin()
            batch = 0
        if time.time() - last_print > 2.0:
            print(f'  [media-scan] {n:,} media indexed...', end='\r', flush=True)
            last_print = time.time()
    mdb.commit()
    print(f'[media-scan] indexed {n:,} media files.            ')
    return n


def attempt_matches(
    src: Path,
    db: IndexDB,
    limit: Optional[int],
    status: str,
    skip_already_processed: bool = True,
    mdb: Optional[MediaDB] = None,
) -> Tuple[int, int]:
    """
    Match each media file and mark in `processed` with given status.

    If `mdb` is provided, iterate from the MediaDB index (no filesystem walk).
    Otherwise fall back to walking `src` — kept for compatibility, but the
    in-DB path is faster and handles resumes.

    Returns (media_seen, matched).
    """
    seen = 0
    matched = 0
    last_print = time.time()
    db.begin()
    batch = 0

    if mdb is not None:
        source_iter = mdb.iter_all()
    else:
        source_iter = (
            entry.path for entry in iterative_walk(src) if _is_media(entry.name)
        )

    for media_path_str in source_iter:
        media_path = Path(media_path_str)
        if skip_already_processed and db.get_processed(media_path):
            continue
        result = match_media(media_path, db)
        if result.json_path:
            matched += 1
        db.mark_processed(
            media_path=media_path,
            json_path=result.json_path,
            match_type=result.match_type or 'unmatched',
            dest_path=None,
            status=status,
        )
        seen += 1
        batch += 1
        if batch >= 1000:
            db.commit()
            db.begin()
            batch = 0
        if time.time() - last_print > 2.0:
            rate = matched / seen if seen else 0
            print(f'  [match] {seen:,} media | {matched:,} matched ({rate:.1%})', end='\r', flush=True)
            last_print = time.time()
        if limit and seen >= limit:
            break
    db.commit()
    print(f'[match] {seen:,} media | {matched:,} matched              ')
    return seen, matched


def cleanup_unmatched(db: IndexDB, status: str) -> int:
    """
    Re-match every row the main strategies left as 'unmatched' against the
    global first-segment uniqueness index. A media file is rescued only if
    exactly one sidecar shares its first segment — that uniqueness check
    is what makes the wider search safe.

    Returns the number of stragglers newly matched. Skips building the
    segment index entirely when there's nothing left to rescue, so chaining
    prescan → run pays the cost only once.
    """
    rows = db.conn.execute(
        "SELECT media_path FROM processed WHERE status=? AND match_type='unmatched'",
        (status,),
    ).fetchall()
    if not rows:
        return 0
    print(f'[cleanup] building first-segment index...')
    seg_idx = _build_segment_index(db)
    total_jsons = sum(len(v) for v in seg_idx.values())
    print(f'[cleanup] {total_jsons:,} sidecars across {len(seg_idx):,} unique '
          f'first segments; re-matching {len(rows):,} stragglers...')
    db.begin()
    rescued = 0
    for (media_path_str,) in rows:
        result = cleanup_match_via_index(Path(media_path_str), seg_idx)
        if result.json_path:
            db.mark_processed(
                media_path=media_path_str,
                json_path=result.json_path,
                match_type=result.match_type,
                dest_path=None,
                status=status,
            )
            rescued += 1
    db.commit()
    print(f'[cleanup] rescued {rescued:,} of {len(rows):,}.')
    return rescued


def copy_pass(
    src: Path,
    dst: Path,
    db: IndexDB,
    limit: Optional[int],
    delete_source: bool,
    skip_existing: bool,
    skip_exif_write: bool,
    divide_to_dates: bool,
    force_rematch: bool,
) -> Tuple[int, int, int, int]:
    """
    For every media file in src, ensure it's matched (re-match if not), then
    copy to the date-organized destination. Returns (seen, copied, skipped, errors).
    """
    seen = 0
    copied = 0
    skipped = 0
    errors = 0
    last_print = time.time()
    db.begin()
    batch = 0
    for entry in iterative_walk(src):
        if not _is_media(entry.name):
            continue
        media_path = Path(entry.path)
        seen += 1

        prior = db.get_processed(media_path)
        if prior and prior['status'] in ('copied', 'skipped_existing') and not force_rematch:
            continue

        if prior and not force_rematch:
            result_json = prior['json_path']
            result_match_type = prior['match_type']
        else:
            r = match_media(media_path, db)
            result_json = r.json_path
            result_match_type = r.match_type or 'unmatched'

        json_info = db.get_minimal(result_json) if result_json else None
        date, _src_label = _resolve_date(media_path, json_info)

        if divide_to_dates:
            dest = _build_dest_path(dst, date, media_path.name)
        else:
            dest = dst / media_path.name

        # Skip-existing: if a file with this exact dest path already exists,
        # don't touch it. This is the "skip existing" fast path for re-runs
        # against an output tree that's already mostly populated.
        if skip_existing and dest.exists():
            db.mark_processed(
                media_path=media_path,
                json_path=result_json,
                match_type=result_match_type,
                dest_path=dest,
                status='skipped_existing',
            )
            skipped += 1
            batch += 1
            if batch >= 500:
                db.commit()
                db.begin()
                batch = 0
            if time.time() - last_print > 2.0:
                print(f'  [copy] seen {seen:,} | copied {copied:,} | skipped {skipped:,} | errors {errors}', end='\r', flush=True)
                last_print = time.time()
            if limit and copied >= limit:
                break
            continue

        dest.parent.mkdir(parents=True, exist_ok=True)
        if not skip_existing:
            dest = _unique_path(dest)

        try:
            shutil.copy2(media_path, dest)

            if not skip_exif_write and date is not None:
                date_str = date.strftime(EXIF_DATETIME_FORMAT)
                try:
                    set_file_exif_date(dest, date_str)
                except Exception:
                    pass
                if json_info and json_info.get('geo_lat') is not None:
                    try:
                        set_file_geo_data(dest, {
                            'geoData': {
                                'latitude': json_info['geo_lat'],
                                'longitude': json_info['geo_lon'],
                                'altitude': json_info.get('geo_alt') or 0,
                            }
                        })
                    except Exception:
                        pass
                try:
                    set_file_timestamps(dest, date_str)
                except Exception:
                    pass

            db.mark_processed(
                media_path=media_path,
                json_path=result_json,
                match_type=result_match_type,
                dest_path=dest,
                status='copied',
            )
            copied += 1

            if delete_source:
                try:
                    media_path.unlink()
                except OSError:
                    pass

        except Exception as e:
            errors += 1
            db.mark_processed(
                media_path=media_path,
                json_path=result_json,
                match_type=result_match_type,
                dest_path=None,
                status=f'error:{type(e).__name__}',
            )

        batch += 1
        if batch >= 500:
            db.commit()
            db.begin()
            batch = 0
        if time.time() - last_print > 2.0:
            print(f'  [copy] seen {seen:,} | copied {copied:,} | skipped {skipped:,} | errors {errors}', end='\r', flush=True)
            last_print = time.time()
        if limit and copied >= limit:
            break
    db.commit()
    print(f'[copy] seen {seen:,} | copied {copied:,} | skipped {skipped:,} | errors {errors}              ')
    return seen, copied, skipped, errors


def print_report(db: IndexDB) -> None:
    mt = db.match_type_counts()
    st = db.status_counts()
    total = sum(mt.values())
    matched = total - mt.get('unmatched', 0)
    rate = matched / total if total else 0.0

    print()
    print('=== REPORT ===')
    print(f'JSON sidecars indexed:    {db.count_json():,}')
    print(f'Media files processed:    {total:,}')
    print(f'Matched:                  {matched:,}  ({rate:.2%})')
    print(f'Unmatched:                {mt.get("unmatched", 0):,}')
    print()
    print('Match type breakdown:')
    for k in sorted(mt.keys()):
        if k == 'unmatched':
            continue
        print(f'  {k:<22} {mt[k]:>10,}')
    if mt.get('unmatched'):
        print(f'  {"unmatched":<22} {mt["unmatched"]:>10,}')
    print()
    print('Status breakdown:')
    for k, v in sorted(st.items()):
        print(f'  {k:<22} {v:>10,}')


# ── command entry points ─────────────────────────────────────────────────


def cmd_extract(args):
    extract_all_zips(
        zip_dir=args.zips,
        staging=args.staging,
        delete_after=not args.keep_zips,
        dry_run=args.dry_run,
        limit=args.limit,
        skip=args.skip,
    )


def _media_db_path(args) -> Path:
    return args.media_db or Path(str(args.db) + '.media.db')


def cmd_prescan(args):
    mdb_path = _media_db_path(args)
    with IndexDB(args.db) as db, MediaDB(mdb_path) as mdb:
        if args.force_rescan:
            db.conn.execute('DELETE FROM json_files')
            db.conn.execute('DELETE FROM processed')
        if args.force_media_rescan:
            mdb.conn.execute('DELETE FROM media_files')

        if args.force_rescan or db.count_json() == 0:
            ingest_jsons(args.src, db)
        else:
            print(f'[scan] reusing {db.count_json():,} indexed JSONs from {args.db}')
            print('       (pass --force-rescan to rebuild from disk)')

        if args.force_media_rescan or mdb.count() == 0:
            ingest_media(args.src, mdb)
        else:
            print(f'[media-scan] reusing {mdb.count():,} indexed media from {mdb_path}')
            print('             (pass --force-media-rescan to rebuild from disk)')

        db.set_meta('source_root', str(args.src))
        mdb.set_meta('source_root', str(args.src))
        # Wipe prior prescan results so the report reflects this run.
        db.conn.execute("DELETE FROM processed WHERE status='prescan'")
        attempt_matches(args.src, db, args.limit, status='prescan',
                        skip_already_processed=False, mdb=mdb)
        cleanup_unmatched(db, status='prescan')
        print_report(db)


def cmd_run(args):
    skip_existing = not args.overwrite and args.skip_existing
    mdb_path = _media_db_path(args)
    with IndexDB(args.db) as db, MediaDB(mdb_path) as mdb:
        if db.count_json() == 0:
            ingest_jsons(args.src, db)
        else:
            print(f'[scan] reusing {db.count_json():,} indexed JSONs from {args.db}')
        if mdb.count() == 0:
            ingest_media(args.src, mdb)
        else:
            print(f'[media-scan] reusing {mdb.count():,} indexed media from {mdb_path}')
        db.set_meta('source_root', str(args.src))
        mdb.set_meta('source_root', str(args.src))
        db.set_meta('dest_root', str(args.dst))

        cleanup_unmatched(db, status='prescan')

        copy_pass(
            src=args.src,
            dst=args.dst,
            db=db,
            limit=args.limit,
            delete_source=args.delete_source,
            skip_existing=skip_existing,
            skip_exif_write=args.skip_exif_write,
            divide_to_dates=args.divide_to_dates,
            force_rematch=args.force_rematch,
        )

        if args.albums:
            print('[albums] building album map...')
            album_map = build_album_map(args.src, args.dst, ALL_MEDIA_FORMATS)
            n_shortcuts, n_albums = create_album_shortcuts(album_map, args.dst)
            print(f'[albums] {n_albums:,} albums | {n_shortcuts:,} shortcuts created')

        print_report(db)


def cmd_albums(args):
    with IndexDB(args.db) as db:
        db.set_meta('source_root', str(args.src))
    print('[albums] building album map...')
    album_map = build_album_map(args.src, args.dst, ALL_MEDIA_FORMATS)
    n_shortcuts, n_albums = create_album_shortcuts(album_map, args.dst)
    print(f'[albums] {n_albums:,} albums | {n_shortcuts:,} shortcuts created')


def cmd_report(args):
    with IndexDB(args.db) as db:
        print_report(db)


def main(argv=None):
    args = parse(argv)
    {
        'extract': cmd_extract,
        'prescan': cmd_prescan,
        'run':     cmd_run,
        'albums':  cmd_albums,
        'report':  cmd_report,
    }[args.cmd](args)


if __name__ == '__main__':
    main()

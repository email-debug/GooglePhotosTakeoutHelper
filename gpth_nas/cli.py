"""CLI for gpth-nas. Subcommands: extract, prescan, run, albums, report."""
import argparse
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog='gpth-nas',
        description='Google Photos Takeout Helper — Synology NAS fork',
    )
    sub = p.add_subparsers(dest='cmd', required=True)

    # ── extract ────────────────────────────────────────────────────────
    ex = sub.add_parser(
        'extract',
        help='Extract Google Takeout *.zip files into a staging tree, deleting each zip after extraction.',
    )
    ex.add_argument('--zips', required=True, type=Path, help='Folder containing *.zip files')
    ex.add_argument('--staging', required=True, type=Path, help='Folder to extract into (created if missing)')
    ex.add_argument('--keep-zips', action='store_true', help='Do NOT delete each zip after a successful extract.')
    ex.add_argument('--limit', type=int, default=None, help='Only extract the first N zips (sorted by name).')
    ex.add_argument('--skip', type=int, default=0, help='Skip the first N zips (sorted by name) before extracting.')
    ex.add_argument('--dry-run', action='store_true', help='List zips that would be extracted, do nothing.')

    # ── prescan ────────────────────────────────────────────────────────
    pre = sub.add_parser(
        'prescan',
        help='Build / refresh the JSON sidecar index and report match rate. No files copied.',
    )
    pre.add_argument('--src', required=True, type=Path, help='Takeout source root (extracted)')
    pre.add_argument('--db', required=True, type=Path, help='Path to JSON sidecar index (SQLite)')
    pre.add_argument('--media-db', type=Path, default=None,
                     help='Path to media-file index (SQLite). If omitted, defaults to '
                          '<db>.media.db next to --db. Allows separate re-scans of media vs JSON.')
    pre.add_argument('--limit', type=int, default=None,
                     help='Only attempt matching on the first N media files (still scans full tree for JSON).')
    pre.add_argument('--force-rescan', action='store_true',
                     help='Discard existing JSON index and rebuild from scratch.')
    pre.add_argument('--force-media-rescan', action='store_true',
                     help='Discard existing media index and rebuild from scratch.')

    # ── run ────────────────────────────────────────────────────────────
    run = sub.add_parser(
        'run',
        help='Full pipeline: match -> fix EXIF -> copy to dst -> (optional) albums + delete source.',
    )
    run.add_argument('--src', required=True, type=Path)
    run.add_argument('--dst', required=True, type=Path)
    run.add_argument('--db', required=True, type=Path)
    run.add_argument('--media-db', type=Path, default=None,
                     help='Path to media-file index. Defaults to <db>.media.db.')
    run.add_argument('--limit', type=int, default=None,
                     help='Stop after N media files (testing).')
    run.add_argument('--albums', action='store_true',
                     help='After copy phase, generate Albums/<name>/*.lnk shortcuts.')
    run.add_argument('--delete-source', action='store_true',
                     help='Delete each source file after a verified copy.')
    run.add_argument('--skip-existing', action='store_true', default=True,
                     help='If a file already exists at the dest path, skip without copying (default ON).')
    run.add_argument('--overwrite', action='store_true',
                     help='Disable --skip-existing: rewrite files even if dest already exists.')
    run.add_argument('--skip-exif-write', action='store_true',
                     help='Do not write EXIF dates on copied files (faster, less safe).')
    run.add_argument('--divide-to-dates', action='store_true', default=True,
                     help='Organize output into YYYY/MM/ subfolders (default on).')
    run.add_argument('--force-rematch', action='store_true',
                     help='Re-match every file even if already in processed table.')

    # ── ingest-local ───────────────────────────────────────────────────
    il = sub.add_parser(
        'ingest-local',
        help='One-pass walk of a local tree that computes EXIF / mp4-atom '
             'timestamps and stores them in a local SQLite index. Lets '
             'merge-local become pure DB matching with no re-walking.',
    )
    il.add_argument('--src', required=True, type=Path,
                    help='Local tree to ingest.')
    il.add_argument('--local-db', required=True, type=Path,
                    help='Path to the local-file SQLite index.')
    il.add_argument('--limit', type=int, default=None,
                    help='Stop after N files (for testing).')
    il.add_argument('--force-rescan', action='store_true',
                    help='Re-ingest files already in the index.')

    # ── merge-local ────────────────────────────────────────────────────
    ml = sub.add_parser(
        'merge-local',
        help='Match an ingested local-file index against the NAS index and '
             'copy non-dups into YYYY/MM. Requires `ingest-local` first.',
    )
    ml.add_argument('--local-db', required=True, type=Path,
                    help='Local-file index produced by `ingest-local`.')
    ml.add_argument('--dst', required=True, type=Path,
                    help='Destination archive root — typically /volume1/photo.')
    ml.add_argument('--db', required=True, type=Path,
                    help='JSON sidecar index (for EXIF/atom timestamp dedup).')
    ml.add_argument('--media-db', type=Path, default=None,
                    help='NAS media index. Defaults to <db>.media.db.')
    ml.add_argument('--limit', type=int, default=None,
                    help='Stop after N files (for testing).')
    ml.add_argument('--delete-source', action='store_true',
                    help='Delete each source file after a verified action.')
    ml.add_argument('--dry-run', action='store_true',
                    help='Classify and report counts; copy nothing.')

    # ── albums-only ────────────────────────────────────────────────────
    alb = sub.add_parser('albums', help='Generate album .lnk shortcuts from an existing output tree.')
    alb.add_argument('--src', required=True, type=Path, help='Original Takeout root (needed for album metadata.json files)')
    alb.add_argument('--dst', required=True, type=Path)
    alb.add_argument('--db', required=True, type=Path)

    # ── report ─────────────────────────────────────────────────────────
    rep = sub.add_parser('report', help='Print match-rate summary from the SQLite index.')
    rep.add_argument('--db', required=True, type=Path)

    return p


def parse(argv=None):
    return build_parser().parse_args(argv)

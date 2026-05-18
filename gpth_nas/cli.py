"""CLI for gpth-nas. Four subcommands: prescan, run, albums, report."""
import argparse
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog='gpth-nas',
        description='Google Photos Takeout Helper — Synology NAS fork',
    )
    sub = p.add_subparsers(dest='cmd', required=True)

    # ── prescan ────────────────────────────────────────────────────────
    pre = sub.add_parser(
        'prescan',
        help='Build / refresh the JSON sidecar index and report match rate. No files copied.',
    )
    pre.add_argument('--src', required=True, type=Path, help='Takeout source root')
    pre.add_argument('--db', required=True, type=Path, help='Path to SQLite index file')
    pre.add_argument('--limit', type=int, default=None,
                     help='Only attempt matching on the first N media files (still scans full tree for JSON).')
    pre.add_argument('--force-rescan', action='store_true',
                     help='Discard existing index in db and rebuild from scratch.')

    # ── run ────────────────────────────────────────────────────────────
    run = sub.add_parser(
        'run',
        help='Full pipeline: match -> fix EXIF -> copy to dst -> (optional) albums + delete source.',
    )
    run.add_argument('--src', required=True, type=Path)
    run.add_argument('--dst', required=True, type=Path)
    run.add_argument('--db', required=True, type=Path)
    run.add_argument('--limit', type=int, default=None,
                     help='Stop after N media files (testing).')
    run.add_argument('--albums', action='store_true',
                     help='After copy phase, generate Albums/<name>/*.lnk shortcuts.')
    run.add_argument('--delete-source', action='store_true',
                     help='Delete each source file after a verified copy.')
    run.add_argument('--skip-exif-write', action='store_true',
                     help='Do not write EXIF dates on copied files (faster, less safe).')
    run.add_argument('--divide-to-dates', action='store_true', default=True,
                     help='Organize output into YYYY/MM/ subfolders (default on).')
    run.add_argument('--force-rematch', action='store_true',
                     help='Re-match every file even if already in processed table.')

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

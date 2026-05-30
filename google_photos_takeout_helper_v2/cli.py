"""
Command-line argument parser for the v2 takeout helper.

Separated from __main__.py for testability and clarity.
All arguments are identical to v1 for backwards compatibility.
"""
import argparse
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    """Build and return the argument parser."""
    parser = argparse.ArgumentParser(
        prog='Google Photos Takeout Helper v2',
        usage='gpth-v2 [INPUT] [OUTPUT] [OPTIONS]',
        description="""
Google Photos Takeout Helper v2 — Refactored Edition
=====================================================
Takes your Google Photos Takeout export, restores correct EXIF dates and GPS
coordinates from the JSON sidecars, and organizes everything into a clean
chronological archive (YYYY/MM folders).

Key improvements over v1:
  - Hash-verified --deletesourceimage (photos verified by SHA-1, not just size)
  - Accurate output counting (every file accounted for, numbers always add up)
  - Safe incremental runs (crash-resistant cache with periodic saves)
  - Consolidated codebase (no duplicate functions, clear module boundaries)

TYPICAL WORKFLOW:
  Step 1 — Pre-scan (safe, read-only):
    gpth-v2 INPUT OUTPUT --prescan

  Step 2 — Test run (first 500 files):
    gpth-v2 INPUT OUTPUT/Test --limit 500

  Step 3 — Full archive run:
    gpth-v2 INPUT OUTPUT

  Step 4 — Merge local photos (optional):
    gpth-v2 OUTPUT --merge-local LOCAL_FOLDER
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument('--version', action='version', version='%(prog)s 4.0.0')

    parser.add_argument(
        'input_folder',
        type=str,
        nargs='?',
        default=None,
        metavar='INPUT',
        help='Input folder containing extracted Google Photos Takeout data.',
    )
    parser.add_argument(
        'output_folder',
        type=str,
        nargs='?',
        default=None,
        metavar='OUTPUT',
        help='Output folder where organized photos will be placed.',
    )

    # ── Mode flags ────────────────────────────────────────────────────────
    parser.add_argument(
        '--prescan',
        action='store_true',
        help='Scan input and report JSON sidecar match rate WITHOUT moving files.',
    )
    parser.add_argument(
        '--merge-local',
        type=str,
        default=None,
        metavar='LOCAL_FOLDER',
        help='Merge a non-Google local photo folder into the output archive.',
    )
    parser.add_argument(
        '--enrich-local',
        type=str,
        default=None,
        metavar='LOCAL_FOLDER',
        help='Match local photos to archive by filename+date, enrich EXIF.',
    )

    # ── Processing options ────────────────────────────────────────────────
    parser.add_argument(
        '--deletesourceimage',
        action='store_true',
        help='Delete source after copying. Photos: hash-verified. Videos: size-verified.',
    )
    parser.add_argument(
        '--cleanup',
        action='store_true',
        help='After complete run, delete all media+JSON from INPUT folder.',
    )
    parser.add_argument(
        '--remove-duplicates',
        action='store_true',
        help='Remove duplicate files from the output folder.',
    )
    parser.add_argument(
        '--limit',
        type=int,
        default=None,
        metavar='N',
        help='Process only the first N files then stop (test run).',
    )

    # ── Output structure ──────────────────────────────────────────────────
    parser.add_argument(
        '--no-divide-to-dates',
        action='store_true',
        dest='no_divide_to_dates',
        help='Disable YYYY/MM subfolders — copy all files flat.',
    )
    parser.add_argument(
        '--albums',
        type=str,
        default='shortcut',
        help="Album mode: 'shortcut' (default), 'duplicatefiles', 'json', 'none'.",
    )

    # ── Skip options ──────────────────────────────────────────────────────
    parser.add_argument(
        '--skip-extras',
        action='store_true',
        help='Skip files ending in -edited, -effects, etc.',
    )
    parser.add_argument(
        '--skip-extras-harder',
        action='store_true',
        help='Also skip files with (N) duplicate suffix. Includes --skip-extras.',
    )
    parser.add_argument(
        '--no-guess-timestamp-from-filename',
        action='store_true',
        dest='no_guess_timestamp',
        help='Disable filename date guessing.',
    )

    # ── Local merge options ───────────────────────────────────────────────
    parser.add_argument(
        '--local-structure-preserve',
        action='store_true',
        dest='local_structure_preserve',
        help='Keep original folder names when merging local photos.',
    )

    # ── Cache/log options ─────────────────────────────────────────────────
    parser.add_argument(
        '--final-log-dir',
        type=str,
        default=None,
        metavar='DIR',
        help='Where to store takeout_log.json (default: OUTPUT/gpth).',
    )
    parser.add_argument(
        '--temp-log-dir',
        type=str,
        default=None,
        metavar='DIR',
        help='Temp location for takeout_log.json during run.',
    )
    parser.add_argument(
        '--force-rescan',
        action='store_true',
        help='Ignore existing cache and re-scan from scratch.',
    )

    return parser


def parse_and_validate() -> argparse.Namespace:
    """Parse arguments and validate required combinations."""
    parser = build_parser()
    args = parser.parse_args()

    # Validate
    if args.output_folder is None:
        parser.error("OUTPUT folder is required.")
    if (args.input_folder is None and args.merge_local is None
            and not args.prescan and not args.enrich_local):
        parser.error("INPUT folder is required (or use --merge-local / --prescan / --enrich-local).")
    if args.enrich_local and args.output_folder is None:
        parser.error("--enrich-local requires OUTPUT folder.")
    if args.input_folder and not Path(args.input_folder).is_dir():
        parser.error(f"INPUT folder does not exist: {args.input_folder}")
    if args.merge_local and not Path(args.merge_local).is_dir():
        parser.error(f"--merge-local folder does not exist: {args.merge_local}")

    # skip-extras-harder implies skip-extras
    if args.skip_extras_harder:
        args.skip_extras = True

    # Resolve log dirs
    args.final_log_dir_resolved = args.final_log_dir or str(Path(args.output_folder) / 'gpth')
    if args.temp_log_dir and args.temp_log_dir != args.final_log_dir_resolved:
        args.temp_log_dir_resolved = args.temp_log_dir
    else:
        args.temp_log_dir_resolved = None  # same as final = no temp

    return args

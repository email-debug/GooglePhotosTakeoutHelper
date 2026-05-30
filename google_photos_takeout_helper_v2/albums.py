"""
Album creation from Google Takeout folder structure.

Handles building album maps from takeout folder metadata JSON files
and creating album output in various modes: shortcut, symlink, copy, or JSON.
"""
import json
import os
import re
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set

from loguru import logger
from tqdm import tqdm

from .utils import is_media, iterative_walk, sanitize_filename


# ── Album map building ────────────────────────────────────────────────────────


def build_output_name_index(
    fixed_dir: Path,
    skip_dirs: Optional[Set[str]] = None,
) -> Dict[str, List[Path]]:
    """
    Build a filename -> [paths] index of the output folder for O(1) album matching.

    Args:
        fixed_dir: Output directory to index.
        skip_dirs: Set of directory names to skip (e.g. {'Albums', 'gpth'}).

    Returns:
        Dict mapping filename -> list of full paths.
    """
    if skip_dirs is None:
        skip_dirs = {'Albums', 'gpth'}

    skip_paths = {str(fixed_dir / d) for d in skip_dirs}
    name_index = defaultdict(list)

    for root, dirs, files in os.walk(fixed_dir):
        # Skip excluded directories
        if root in skip_paths or any(root.startswith(s + os.sep) for s in skip_paths):
            dirs.clear()
            continue
        for fname in files:
            name_index[fname].append(Path(root) / fname)

    return name_index


def populate_album_map(
    folder: Path,
    album_map: Dict[str, List[Path]],
    fixed_dir_name_index: Optional[Dict[str, List[Path]]] = None,
):
    """
    Check if a folder is a Google Photos album and populate album_map.

    Albums are identified by containing a metadata.json with an 'albumData' key.
    Each media file in the album folder is matched to its output copy.

    Args:
        folder: A takeout folder to check.
        album_map: Dict of album_name -> [output_paths] to populate.
        fixed_dir_name_index: Pre-built name index for O(1) lookups.
    """
    meta_file = folder / 'metadata.json'
    if not meta_file.exists():
        return

    try:
        with open(meta_file, 'r', encoding='utf-8', errors='replace') as f:
            meta = json.load(f)
    except Exception:
        return

    album_data = meta.get('albumData')
    if not album_data:
        return

    album_title = album_data.get('title', folder.name)
    if not album_title:
        album_title = folder.name

    # Collect media files in this album folder
    matched_paths = []
    for entry in folder.iterdir():
        if not entry.is_file():
            continue
        if not is_media(entry):
            continue

        # Find this file in the output via name index
        if fixed_dir_name_index is not None:
            output_paths = fixed_dir_name_index.get(entry.name, [])
            if output_paths:
                matched_paths.append(output_paths[0])

    if matched_paths:
        album_map[album_title] = matched_paths


def build_album_map(
    photos_dir: Path,
    fixed_dir: Path,
    skip_dirs: Optional[Set[str]] = None,
) -> Dict[str, List[Path]]:
    """
    Walk the takeout input and build the complete album map.

    Returns:
        Dict of album_name -> [output file paths].
    """
    logger.info('Building album map...')

    # Index output folder for fast lookups
    logger.info('Indexing output folder for album matching...')
    name_index = build_output_name_index(fixed_dir, skip_dirs)
    logger.info(f'Output index: {sum(len(v) for v in name_index.values()):,} files indexed')

    album_map = {}

    # Walk input to find album folders (folders with metadata.json)
    stack = [str(photos_dir)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                            populate_album_map(
                                Path(entry.path), album_map, name_index
                            )
                    except OSError:
                        continue
        except (PermissionError, OSError):
            continue

    logger.info(
        f'Album map built: {len(album_map):,} albums, '
        f'{sum(len(v) for v in album_map.values()):,} total photos'
    )
    return album_map


# ── Album creation modes ─────────────────────────────────────────────────────


def create_album_shortcuts(
    album_map: Dict[str, List[Path]],
    albums_dir: Path,
) -> int:
    """
    Create album shortcuts/symlinks pointing to output files.

    On Windows with win32com: creates .lnk shortcuts.
    On Windows without win32com: falls back to symlinks then copies.
    On Linux/macOS: creates symlinks.

    Returns count of shortcuts created.
    """
    win32com_available = False
    win32com_client = None
    if os.name == 'nt':
        try:
            import win32com.client as _wc
            win32com_client = _wc
            win32com_available = True
        except ImportError:
            logger.info('win32com not available — falling back to symlinks for album shortcuts')

    count = 0
    total_targets = sum(len(v) for v in album_map.values())

    with tqdm(total=total_targets, unit='shortcuts', desc='Creating album shortcuts') as bar:
        for album_name, paths in album_map.items():
            if not paths:
                continue

            album_dir = albums_dir / sanitize_filename(album_name)
            album_dir.mkdir(parents=True, exist_ok=True)
            name_count = defaultdict(int)

            for target in paths:
                n = name_count[target.name]
                name_count[target.name] += 1

                link_name = target.name if n == 0 else f"{target.stem}({n}){target.suffix}"

                if os.name == 'nt' and win32com_available:
                    shortcut_path = album_dir / (link_name + '.lnk')
                else:
                    shortcut_path = album_dir / link_name

                if not shortcut_path.exists():
                    if _create_shortcut(shortcut_path, target, win32com_client):
                        count += 1
                bar.update(1)

    return count


def _create_shortcut(shortcut_path: Path, target_path: Path, win32com_client=None) -> bool:
    """Create a shortcut/symlink from shortcut_path to target_path."""
    try:
        if os.name == 'nt' and win32com_client is not None:
            shell = win32com_client.Dispatch("WScript.Shell")
            sc = shell.CreateShortCut(str(shortcut_path))
            sc.Targetpath = str(target_path.resolve())
            sc.save()
            return True
        else:
            # Symlink
            shortcut_path.symlink_to(target_path.resolve())
            return True
    except Exception:
        # Last resort: copy the file
        try:
            shutil.copy2(target_path, shortcut_path)
            return True
        except Exception:
            return False


def create_album_copies(
    album_map: Dict[str, List[Path]],
    albums_dir: Path,
) -> int:
    """
    Create album folders with file copies (duplicate files mode).

    Returns count of files copied.
    """
    count = 0

    for album_name, paths in album_map.items():
        if not paths:
            continue

        album_dir = albums_dir / sanitize_filename(album_name)
        album_dir.mkdir(parents=True, exist_ok=True)
        name_count = defaultdict(int)

        for target in paths:
            n = name_count[target.name]
            name_count[target.name] += 1
            dest_name = target.name if n == 0 else f"{target.stem}({n}){target.suffix}"
            dest = album_dir / dest_name

            if not dest.exists():
                try:
                    shutil.copy2(target, dest)
                    count += 1
                except Exception as e:
                    logger.debug(f'Album copy failed: {target} -> {dest}: {e}')

    return count


def create_album_json(
    album_map: Dict[str, List[Path]],
    output_path: Path,
):
    """
    Export album map as a JSON file.

    Args:
        album_map: Dict of album_name -> [paths].
        output_path: File path for the JSON output.
    """
    json_map = {k: [p.name for p in v] for k, v in album_map.items()}
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(json_map, f, indent=2)
    logger.info(f'Albums JSON: {output_path}')


def create_albums(
    album_map: Dict[str, List[Path]],
    fixed_dir: Path,
    mode: str = 'shortcut',
    photos_dir: Optional[Path] = None,
) -> int:
    """
    Create albums in the specified mode.

    Args:
        album_map: Dict of album_name -> [output file paths].
        fixed_dir: Output directory.
        mode: One of 'shortcut', 'duplicatefiles', 'json', 'none'.
        photos_dir: Input directory (needed for json mode).

    Returns:
        Count of album entries created.
    """
    mode = mode.lower()
    if mode == 'none' or not album_map:
        return 0

    albums_dir = fixed_dir / 'Albums'

    if mode == 'shortcut':
        count = create_album_shortcuts(album_map, albums_dir)
        logger.info(f'Album shortcuts: {count:,} in {albums_dir}')
        return count

    elif mode == 'duplicatefiles':
        count = create_album_copies(album_map, albums_dir)
        logger.info(f'Album file copies: {count:,} in {albums_dir}')
        return count

    elif mode == 'json':
        output_path = (photos_dir or fixed_dir) / 'albums.json'
        create_album_json(album_map, output_path)
        return sum(len(v) for v in album_map.values())

    else:
        logger.warning(f'Unknown album mode: {mode}')
        return 0

"""
Album detection and .lnk shortcut creation for the NAS fork.

What changed vs. v2:
  - Album detection no longer REQUIRES the JSON to contain `albumData`. Older
    Takeout exports just have `{"title": "..."}` at the top of the folder's
    metadata.json. The v2 check silently dropped those albums. We now accept
    any folder whose metadata.json has a `title` and that contains ≥ 1 media
    file alongside that metadata. (`albumData` is treated as a stronger hint
    but not required.)
  - Filename-collision resolution: instead of grabbing the first match from
    the output index, prefer (a) a year-folder match against the source album
    folder name, (b) the most recent file by mtime.
  - Shortcut creation uses our own .lnk writer (lnk.py) — no win32com required.
"""
import json
import os
import re
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .lnk import write_lnk


# Filenames that are NOT user-facing albums even if they contain a title.
_NON_ALBUM_TITLES = {
    'photos from',         # Year buckets (e.g. "Photos from 2019")
    'trash', 'bin',
}


def _sanitize_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip().rstrip('. ')


def _is_media_name(name: str, media_exts: Set[str]) -> bool:
    """Cheap suffix check. media_exts contains lowercase '.ext' strings."""
    if '.' not in name:
        return False
    ext = '.' + name.rsplit('.', 1)[-1].lower()
    return ext in media_exts


def _read_album_metadata(folder: Path) -> Optional[dict]:
    """
    Return parsed metadata.json if the folder looks like an album, else None.
    """
    meta = folder / 'metadata.json'
    if not meta.exists():
        return None
    try:
        with open(meta, 'r', encoding='utf-8', errors='replace') as f:
            data = json.load(f)
    except Exception:
        return None

    if not isinstance(data, dict):
        return None

    title = data.get('title') or (data.get('albumData') or {}).get('title')
    if not title:
        return None

    # Skip 'Photos from YYYY' year buckets — those aren't albums.
    title_lower = title.strip().lower()
    if any(title_lower.startswith(p) for p in _NON_ALBUM_TITLES):
        return None

    return {'title': title, '_raw': data}


def build_output_name_index(
    output_root: Path,
    skip_dirnames: Optional[Set[str]] = None,
) -> Dict[str, List[Path]]:
    """
    Walk the output tree and build {basename: [Path, ...]}.

    Excludes 'Albums' and the work dir to avoid self-references.
    """
    if skip_dirnames is None:
        skip_dirnames = {'Albums', '.gpth'}

    out: Dict[str, List[Path]] = defaultdict(list)
    for root, dirs, files in os.walk(output_root):
        dirs[:] = [d for d in dirs if d not in skip_dirnames]
        for fname in files:
            out[fname].append(Path(root) / fname)
    return out


def _pick_output_path(
    candidates: List[Path],
    album_folder: Path,
) -> Optional[Path]:
    """
    Pick the right output file when the same basename exists multiple times.

    Heuristics:
      1. If album folder name (or its parent's "Photos from YYYY" hint) contains
         a year, prefer the candidate whose path contains that year.
      2. Otherwise prefer the most recently modified (file mtime).
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    # Try to extract year hint from album folder path.
    m = re.search(r'(?:19|20)\d{2}', str(album_folder))
    year_hint = m.group(0) if m else None

    if year_hint:
        for c in candidates:
            if year_hint in str(c):
                return c

    # Fallback: most recently modified.
    try:
        return max(candidates, key=lambda p: p.stat().st_mtime)
    except OSError:
        return candidates[0]


def build_album_map(
    source_root: Path,
    output_root: Path,
    media_exts: Set[str],
) -> Dict[str, List[Path]]:
    """
    Walk the source for album folders and map album_title -> [output paths].
    """
    name_index = build_output_name_index(output_root)
    album_map: Dict[str, List[Path]] = defaultdict(list)

    # Walk source iteratively.
    stack = [str(source_root)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                    except OSError:
                        continue
        except (PermissionError, OSError):
            continue

        folder = Path(current)
        meta = _read_album_metadata(folder)
        if meta is None:
            continue

        # Confirm the folder has at least one media file.
        media_in_folder = []
        try:
            for f in folder.iterdir():
                if f.is_file() and _is_media_name(f.name, media_exts):
                    media_in_folder.append(f.name)
        except OSError:
            continue

        if not media_in_folder:
            continue

        album_title = meta['title']

        for fname in media_in_folder:
            paths = name_index.get(fname, [])
            chosen = _pick_output_path(paths, folder)
            if chosen is not None:
                album_map[album_title].append(chosen)

    return dict(album_map)


def create_album_shortcuts(
    album_map: Dict[str, List[Path]],
    output_root: Path,
) -> Tuple[int, int]:
    """
    Materialize album_map into output_root/Albums/<name>/*.lnk files.

    Returns (shortcuts_created, albums_created).
    """
    albums_dir = output_root / 'Albums'
    albums_dir.mkdir(parents=True, exist_ok=True)

    total = 0
    album_count = 0
    for album_name, paths in album_map.items():
        if not paths:
            continue
        album_dir = albums_dir / _sanitize_filename(album_name)
        album_dir.mkdir(parents=True, exist_ok=True)
        album_count += 1

        name_counter: Dict[str, int] = defaultdict(int)
        for target in paths:
            n = name_counter[target.name]
            name_counter[target.name] += 1
            if n == 0:
                link_name = target.name + '.lnk'
            else:
                link_name = f"{target.stem}({n}){target.suffix}.lnk"

            shortcut = album_dir / link_name
            if shortcut.exists():
                continue
            try:
                write_lnk(shortcut, target)
                total += 1
            except Exception:
                # As a last resort, copy the file. Should be rare.
                try:
                    shutil.copy2(target, album_dir / target.name)
                    total += 1
                except Exception:
                    pass

    return total, album_count

"""
JSON sidecar matching engine.

Consolidates v1's duplicated matching logic into a single module.
Implements all 7 matching strategies:

Same-folder:
  1-3: Exact name / stem / parenthetical variants
  4-5: Fuzzy prefix matching (≥15 char prefix)
  6:   Title field match

Cross-folder:
  1-3: Same as above but across all indexed folders
  4-5: Binary search prefix matching across all folders
  7:   Title field match across all folders
"""
import bisect
import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .types import MatchResult


def _sidecar_candidates(media_path: Path, cname: str) -> List[Path]:
    """
    Return candidate JSON sidecar Paths for a media file + candidate name.

    Handles Google's various naming conventions:
    - filename.jpg.json
    - filename.jpg.supplemental-metadata.json
    - filename.json (stem only)
    - filename(1).jpg.json -> filename.jpg(1).json (parenthetical reordering)
    """
    candidates = []
    parens = re.findall(r'\([0-9]+\)', cname)
    stem = Path(cname).stem

    # Parenthetical reordering: IMG_1234(1).jpg -> IMG_1234.jpg(1).json
    if len(parens) == 1:
        stripped = re.sub(r'\([0-9]+\)', '', cname)
        bwp = stripped + parens[0]
        candidates.append(media_path.parent / (bwp + '.json'))
        candidates.append(media_path.parent / (bwp + '.supplemental-metadata.json'))

    # Standard: filename.ext.json
    candidates.append(media_path.parent / (cname + '.json'))
    candidates.append(media_path.parent / (cname + '.supplemental-metadata.json'))

    # Stem only: filename.json (without original extension)
    if stem != cname:
        candidates.append(media_path.parent / (stem + '.json'))
        candidates.append(media_path.parent / (stem + '.supplemental-metadata.json'))

    return candidates


def _sidecar_candidate_basenames(cname: str) -> List[str]:
    """
    Return candidate JSON basenames (filename only) for cross-folder matching.
    """
    seen = set()
    out = []
    parens = re.findall(r'\([0-9]+\)', cname)
    stem = Path(cname).stem

    if len(parens) == 1:
        stripped = re.sub(r'\([0-9]+\)', '', cname)
        bwp = stripped + parens[0]
        for suf in ['.json', '.supplemental-metadata.json']:
            b = bwp + suf
            if b not in seen:
                seen.add(b)
                out.append(b)

    for suf in ['.json', '.supplemental-metadata.json']:
        b = cname + suf
        if b not in seen:
            seen.add(b)
            out.append(b)

    if stem != cname:
        for suf in ['.json', '.supplemental-metadata.json']:
            b = stem + suf
            if b not in seen:
                seen.add(b)
                out.append(b)

    return out


def _next_prefix(p: str) -> str:
    """Smallest string > p that does not start with p. For binary-search upper bound."""
    if not p:
        return ''
    return p[:-1] + chr(ord(p[-1]) + 1)


def _build_candidates(name: str) -> List[str]:
    """Build the list of candidate names for a media file."""
    candidates = []
    if '-edited' in name:
        candidates.append(name.replace('-edited', ''))
    candidates.append(name)
    candidates.append(re.sub(r'(\s*\(\d+\)|~\d+)', '', name))
    candidates.append(re.sub(r'(\s*\(\d+\)|~\d+)', '', name.replace('-edited', '')))
    return candidates


def match_media_to_json(
    media_path: Path,
    path_index: Dict[Path, dict],
    title_index: Dict[str, List[Tuple[Path, dict]]],
    parent_index: Dict[Path, List[Tuple[Path, dict]]],
    basename_sorted: List[Tuple[str, str]],
    basename_to_paths: Dict[str, List[Tuple[Path, dict]]],
) -> MatchResult:
    """
    Match a media file to its JSON sidecar using all 7 strategies.

    Args:
        media_path: The media file to match.
        path_index: Dict mapping JSON Path -> parsed dict.
        title_index: Dict mapping title string -> [(json_path, json_dict), ...].
        parent_index: Dict mapping parent dir -> [(json_path, json_dict), ...].
        basename_sorted: Sorted list of (basename, path_str) for binary search.
        basename_to_paths: Dict mapping json basename -> [(json_path, json_dict), ...].

    Returns:
        MatchResult with json_path and match_type set if found.
    """
    name = media_path.name
    candidates = _build_candidates(name)

    # ── Same folder: Strategy 1-3 (exact/stem/paren) ─────────────────────
    seen_paths = set()
    for cand in candidates:
        for jp in _sidecar_candidates(media_path, cand):
            if jp in seen_paths:
                continue
            seen_paths.add(jp)
            if jp in path_index:
                return MatchResult(jp, 'same_exact')

    # ── Same folder: Strategy 4-5 (fuzzy/prefix) ─────────────────────────
    for jp, jd in parent_index.get(media_path.parent, []):
        if jp.name.startswith(name) or (len(name) >= 15 and jp.name.startswith(name[:15])):
            return MatchResult(jp, 'same_fuzzy')

    # ── Same folder: Strategy 6 (title) ──────────────────────────────────
    if name in title_index:
        for jp, jd in title_index[name]:
            if jp.parent == media_path.parent:
                return MatchResult(jp, 'same_title')

    # ── Cross-folder: Strategy 1-3 ───────────────────────────────────────
    if basename_to_paths:
        for cand in candidates:
            for basename in _sidecar_candidate_basenames(cand):
                if basename in basename_to_paths:
                    jp, jd = basename_to_paths[basename][0]
                    return MatchResult(jp, 'cross_exact')

    # ── Cross-folder: Strategy 4-5 (binary search prefix) ────────────────
    if basename_sorted:
        prefix = name if len(name) < 15 else name[:15]
        hi = _next_prefix(prefix)
        lo_idx = bisect.bisect_left(basename_sorted, (prefix, ''))
        hi_idx = bisect.bisect_left(basename_sorted, (hi, ''))
        for i in range(lo_idx, hi_idx):
            bname, pstr = basename_sorted[i]
            if bname.startswith(name) or (len(name) >= 15 and bname.startswith(name[:15])):
                return MatchResult(Path(pstr), 'cross_fuzzy')

    # ── Cross-folder: Strategy 7 (title) ─────────────────────────────────
    if name in title_index:
        jp, jd = title_index[name][0]
        return MatchResult(jp, 'cross_title')

    return MatchResult()


def build_json_index_from_cache(cached_index: dict):
    """
    Rebuild all index structures from persisted json_index in the cache.

    Returns:
        Tuple of (path_index, title_index, parent_index, basename_sorted, basename_to_paths).
    """
    path_index = {}
    title_index = defaultdict(list)
    path_to_minimal = cached_index.get('path_to_minimal', {})
    title_to_paths = cached_index.get('title_to_paths', {})
    basename_sorted_raw = cached_index.get('basename_sorted', [])

    for pstr, minimal in path_to_minimal.items():
        p = Path(pstr)
        path_index[p] = minimal

    for title, paths in title_to_paths.items():
        for pstr in paths:
            p = Path(pstr)
            path_index.setdefault(p, path_to_minimal.get(pstr, {}))
            title_index[title].append((p, path_index[p]))

    parent_index = defaultdict(list)
    basename_to_paths = defaultdict(list)
    for p, d in path_index.items():
        if p.suffix.lower() == '.json':
            parent_index[p.parent].append((p, d))
            basename_to_paths[p.name].append((p, d))

    basename_sorted = [(b, p) for b, p in basename_sorted_raw]

    return path_index, title_index, parent_index, basename_sorted, basename_to_paths

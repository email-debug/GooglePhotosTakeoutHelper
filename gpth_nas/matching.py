"""
JSON sidecar matching for the NAS fork. Backed by SQLite (index_db).

Eight strategies, tried in order of confidence. Each is independent of how the
index is stored — they all go through IndexDB's query API.

  same_exact      Sidecar in the same folder, exact basename match.
  same_paren      Same folder, Google's IMG(1).jpg <-> IMG.jpg(1).json reorder.
  same_edited     Same folder, '-edited' variant of the media name.
  same_truncated  Same folder, Google truncated the JSON stem (e.g. 46 chars).
  cross_exact     Any folder, exact basename match.
  cross_truncated Any folder, truncation strategy.
  cross_stem_pref Any folder, stem-prefix match (≥ 25 chars) for long unique names.
  title           JSON 'title' field equals the media filename.

Collision resolution: when multiple candidates exist, prefer (1) same parent
folder, (2) same year folder ("Photos from YYYY"), (3) closest stem length,
(4) lexicographic. The first three avoid grabbing a stranger sidecar with the
same name that lives next to a different photo.
"""
import re
from collections import namedtuple
from pathlib import Path
from typing import List, Optional, Tuple

from .index_db import IndexDB


MatchResult = namedtuple('MatchResult', ['json_path', 'match_type'])
NO_MATCH = MatchResult(None, None)

# Google's known JSON-stem truncation lengths. Empirically Google truncates the
# embedded media filename to 46 chars (most common) or 51 in some exports.
# We try a few common lengths plus a couple defensive ones.
_TRUNC_LENS = (46, 47, 51, 50, 45, 44, 43, 42, 41, 40)


_YEAR_RE = re.compile(r'\bfrom\s+((?:19|20)\d{2})\b', re.IGNORECASE)


def _year_in_path(p: str) -> Optional[str]:
    m = _YEAR_RE.search(p)
    return m.group(1) if m else None


def _build_candidate_names(media_name: str) -> List[str]:
    """Generate name variants (without .json suffix) to try for sidecar lookup."""
    candidates = [media_name]
    if '-edited' in media_name:
        candidates.append(media_name.replace('-edited', ''))
    # Strip "(1)" and "~1" duplicate markers.
    stripped = re.sub(r'(\s*\(\d+\)|~\d+)', '', media_name)
    if stripped not in candidates:
        candidates.append(stripped)
    return candidates


def _sidecar_basenames(cname: str) -> List[str]:
    """All JSON basenames to try for a given media-name candidate."""
    out = []
    parens = re.findall(r'\([0-9]+\)', cname)
    stem = Path(cname).stem

    # Paren reorder: foo(1).jpg -> foo.jpg(1).json
    if len(parens) == 1:
        without = re.sub(r'\([0-9]+\)', '', cname)
        with_paren_after = without + parens[0]
        out.append(with_paren_after + '.json')
        out.append(with_paren_after + '.supplemental-metadata.json')

    # Standard
    out.append(cname + '.json')
    out.append(cname + '.supplemental-metadata.json')

    # Stem-only (filename without media extension)
    if stem and stem != cname:
        out.append(stem + '.json')
        out.append(stem + '.supplemental-metadata.json')

    # Deduplicate, preserve order
    seen = set()
    deduped = []
    for b in out:
        if b not in seen:
            seen.add(b)
            deduped.append(b)
    return deduped


def _pick_best(candidates: List[str], media_parent: str, media_name: str) -> Optional[str]:
    """
    Choose the single best JSON path from a candidate list using locality heuristics.

    Returns None for empty input. Returns the only entry for single-element lists.
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    media_year = _year_in_path(media_parent)
    media_stem_len = len(Path(media_name).stem)

    def score(jp: str) -> tuple:
        j_parent = str(Path(jp).parent)
        same_parent = (j_parent == media_parent)
        j_year = _year_in_path(j_parent)
        same_year = (media_year is not None and j_year == media_year)
        j_stem = IndexDB._stem_of_json(Path(jp).name)
        len_diff = abs(len(j_stem) - media_stem_len)
        # Sort key: same_parent first (True > False so negate), then same_year, then small len_diff, then lex.
        return (
            0 if same_parent else 1,
            0 if same_year else 1,
            len_diff,
            jp,
        )

    return sorted(candidates, key=score)[0]


def match_media(media_path: Path, db: IndexDB) -> MatchResult:
    """Run the full strategy ladder for a single media file. Returns a MatchResult."""
    media_name = media_path.name
    media_parent = str(media_path.parent)
    candidate_names = _build_candidate_names(media_name)

    # ── same-folder: exact / paren / edited (Strategies 1-3 collapsed) ───
    for cname in candidate_names:
        for bn in _sidecar_basenames(cname):
            hit = db.in_parent_with_basename(media_parent, bn)
            if hit:
                mt = 'same_exact'
                if cname != media_name:
                    mt = 'same_edited' if '-edited' in media_name else 'same_paren'
                return MatchResult(hit, mt)

    # ── same-folder: truncation (Google often truncates the embedded name) ─
    media_stem = Path(media_name).stem
    media_ext = Path(media_name).suffix
    if len(media_stem) > min(_TRUNC_LENS):
        for tl in _TRUNC_LENS:
            if tl >= len(media_stem):
                continue
            truncated_stem = media_stem[:tl]
            for try_basename in (
                truncated_stem + media_ext + '.json',
                truncated_stem + media_ext + '.supplemental-metadata.json',
                truncated_stem + '.json',
            ):
                hit = db.in_parent_with_basename(media_parent, try_basename)
                if hit:
                    return MatchResult(hit, 'same_truncated')

    # ── cross-folder: exact / paren / edited ─────────────────────────────
    for cname in candidate_names:
        for bn in _sidecar_basenames(cname):
            hits = db.by_basename(bn)
            best = _pick_best(hits, media_parent, media_name)
            if best:
                return MatchResult(best, 'cross_exact')

    # ── cross-folder: truncation ─────────────────────────────────────────
    if len(media_stem) > min(_TRUNC_LENS):
        for tl in _TRUNC_LENS:
            if tl >= len(media_stem):
                continue
            truncated_stem = media_stem[:tl]
            for try_basename in (
                truncated_stem + media_ext + '.json',
                truncated_stem + media_ext + '.supplemental-metadata.json',
                truncated_stem + '.json',
            ):
                hits = db.by_basename(try_basename)
                best = _pick_best(hits, media_parent, media_name)
                if best:
                    return MatchResult(best, 'cross_truncated')

    # ── cross-folder: stem-prefix for long unique names ──────────────────
    # Only safe for stems ≥ 25 chars (otherwise prefix collisions explode).
    if len(media_stem) >= 25:
        prefix = media_stem[:25]
        rows = db.by_stem_prefix(prefix, limit=64)
        # Filter to ones that the media stem actually starts with their JSON stem
        # (truncation case) OR that start with the media stem (extension variants).
        candidate_paths = []
        for jp, jstem in rows:
            j_stem_core = Path(jstem).stem
            if media_stem.startswith(j_stem_core) or j_stem_core.startswith(media_stem):
                candidate_paths.append(jp)
        best = _pick_best(candidate_paths, media_parent, media_name)
        if best:
            return MatchResult(best, 'cross_stem_pref')

    # ── title field match ────────────────────────────────────────────────
    title_hits = db.by_title(media_name)
    if title_hits:
        # Prefer same parent.
        same_parent_hits = [p for p, par in title_hits if par == media_parent]
        if same_parent_hits:
            return MatchResult(same_parent_hits[0], 'same_title')
        return MatchResult(title_hits[0][0], 'cross_title')

    return NO_MATCH

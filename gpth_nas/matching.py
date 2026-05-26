"""
JSON sidecar matching for the NAS fork. Backed by SQLite (index_db).

Strategies, tried in order of confidence. Each is independent of how the
index is stored — they all go through IndexDB's query API.

  same_exact      Sidecar in the same folder, exact basename match.
  same_paren      Same folder, Google's IMG(1).jpg <-> IMG.jpg(1).json reorder.
  same_edited     Same folder, '-edited'/'-EFFECTS'/etc. variant of the media name.
  same_truncated  Same folder, Google truncated the JSON filename to 51 chars.
  cross_exact     Any folder, exact basename match.
  cross_truncated Any folder, truncation strategy.
  cross_stem_pref Any folder, stem-prefix match (≥ 25 chars) for long unique names.
  title           JSON 'title' field equals the media filename.

Truncation model (CRITICAL — verified against real Takeout exports):
Google builds the sidecar name as `{media_filename}.supplemental-metadata.json`
and then truncates the WHOLE name (before the trailing '.json') to 46 chars,
re-appending '.json' — capping the full basename at 51 characters. The cut can
land anywhere: in the '.supplemental-metadata' suffix ('.supplemental-me.json'),
mid-extension ('.jp.json'), or even right after the media name ('foo.'). This
is reconstructed exactly by `_expected_sidecar()`; we do NOT guess stem lengths.

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

# Google caps the sidecar basename at this many characters (incl. ".json").
_MAX_JSON_LEN = 51
_SUPP = '.supplemental-metadata'

# Editor-generated variants that share the ORIGINAL photo's sidecar. Google
# appends these before the extension: foo-edited.jpg, foo-EFFECTS.jpg, etc.
_EDIT_SUFFIXES = (
    '-edited', '-EFFECTS', '-COLLAGE', '-ANIMATION',
    '-PANO', '-MIX', '-SMILE', '-MOTION',
)

_YEAR_RE = re.compile(r'\bfrom\s+((?:19|20)\d{2})\b', re.IGNORECASE)
_PAREN_RE = re.compile(r'\((\d+)\)$')


def _year_in_path(p: str) -> Optional[str]:
    m = _YEAR_RE.search(p)
    return m.group(1) if m else None


def _expected_sidecar(media_name: str) -> Tuple[str, bool]:
    """
    Reconstruct Google's sidecar basename for a media filename.

    Returns (basename, was_truncated). The full form is
    `{media_name}.supplemental-metadata.json`; if that exceeds 51 chars Google
    truncates the pre-'.json' portion to 46 chars, yielding a 51-char basename.
    """
    full = media_name + _SUPP + '.json'
    if len(full) <= _MAX_JSON_LEN:
        return full, False
    return (media_name + _SUPP)[: _MAX_JSON_LEN - len('.json')] + '.json', True


def _build_candidate_names(media_name: str) -> List[str]:
    """Generate media-name variants whose sidecar we should look for."""
    candidates = [media_name]
    stem, ext = _splitext(media_name)

    # Editor variants share the original photo's sidecar.
    for suf in _EDIT_SUFFIXES:
        if stem.endswith(suf):
            candidates.append(stem[: -len(suf)] + ext)

    # Strip "(1)" / "~1" duplicate markers.
    stripped = re.sub(r'(\s*\(\d+\)|~\d+)$', '', stem) + ext
    if stripped not in candidates:
        candidates.append(stripped)

    return candidates


def _splitext(name: str) -> Tuple[str, str]:
    p = Path(name)
    return p.stem, p.suffix


def _sidecar_basenames(cname: str) -> List[Tuple[str, str]]:
    """
    All (json_basename, kind) pairs to try for a media-name candidate.

    kind is 'exact' | 'truncated' | 'paren'. The caller prefixes 'same_'/'cross_'.
    """
    out: List[Tuple[str, str]] = []
    stem, ext = _splitext(cname)

    # Paren reorder: foo(1).jpg -> foo.jpg(1).json
    m = _PAREN_RE.search(stem)
    if m:
        reordered = stem[: m.start()] + ext + '(' + m.group(1) + ')'
        out.append((reordered + '.json', 'paren'))
        out.append((reordered + _SUPP + '.json', 'paren'))

    # Primary: Google's exact (possibly truncated) sidecar name.
    expected, truncated = _expected_sidecar(cname)
    out.append((expected, 'truncated' if truncated else 'exact'))

    # Legacy / alternate forms.
    out.append((cname + '.json', 'exact'))
    if stem and stem != cname:
        out.append((stem + '.json', 'exact'))
        out.append((stem + _SUPP + '.json', 'exact'))

    # Deduplicate, preserve order.
    seen = set()
    deduped = []
    for bn, kind in out:
        if bn not in seen:
            seen.add(bn)
            deduped.append((bn, kind))
    return deduped


def _pick_best(candidates: List[str], media_parent: str, media_name: str) -> Optional[str]:
    """Choose the single best JSON path from a candidate list using locality heuristics."""
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

    def label(kind: str, cname: str, scope: str) -> str:
        # scope is 'same' or 'cross'.
        if cname != media_name:
            stem, _ = _splitext(media_name)
            if any(stem.endswith(s) for s in _EDIT_SUFFIXES):
                return scope + '_edited'
            return scope + '_paren'
        if kind == 'truncated':
            return scope + '_truncated'
        if kind == 'paren':
            return scope + '_paren'
        return scope + '_exact'

    # ── same-folder ──────────────────────────────────────────────────────
    for cname in candidate_names:
        for bn, kind in _sidecar_basenames(cname):
            hit = db.in_parent_with_basename(media_parent, bn)
            if hit:
                return MatchResult(hit, label(kind, cname, 'same'))

    # ── cross-folder ─────────────────────────────────────────────────────
    for cname in candidate_names:
        for bn, kind in _sidecar_basenames(cname):
            hits = db.by_basename(bn)
            best = _pick_best(hits, media_parent, media_name)
            if best:
                return MatchResult(best, label(kind, cname, 'cross'))

    # ── cross-folder: stem-prefix for long unique names ──────────────────
    # Only safe for stems ≥ 25 chars (otherwise prefix collisions explode).
    media_stem = Path(media_name).stem
    if len(media_stem) >= 25:
        prefix = media_stem[:25]
        rows = db.by_stem_prefix(prefix, limit=64)
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
        same_parent_hits = [p for p, par in title_hits if par == media_parent]
        if same_parent_hits:
            return MatchResult(same_parent_hits[0], 'same_title')
        return MatchResult(title_hits[0][0], 'cross_title')

    return NO_MATCH

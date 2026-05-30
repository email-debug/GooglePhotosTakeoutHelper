"""
JSON sidecar matching for the NAS fork. Backed by SQLite (index_db).

Strategies, tried in order of confidence. Each is independent of how the
index is stored — they all go through IndexDB's query API.

  same_exact      Sidecar in the same folder, exact basename match.
  same_paren      Same folder, Google's IMG(1).jpg <-> IMG.jpg(1).json reorder,
                  OR the sidecar-side collision form IMG.jpg.supplemental-metadata(1).json
                  (used when two media names yield the same sidecar stem).
  same_edited     Same folder, '-edited'/'-EFFECTS'/etc. variant of the media name.
                  Composes with paren-stripping: foo-edited(1).jpg falls back to foo.jpg.
  same_truncated  Same folder, Google truncated the JSON filename to 51 chars.
  same_sibling_ext Same folder, sidecar's title matches the media stem but
                  with a different extension. Covers Live Photos where only
                  the .HEIC has a sidecar and the .MP4 motion clip inherits it.
  cross_exact     Any folder, exact basename match.
  cross_truncated Any folder, truncation strategy.
  cross_stem_pref Any folder, stem-prefix match (≥ 25 chars) for long unique names.
  title           JSON 'title' field equals the media filename.

Cleanup pass — applied ONLY to rows the main matcher returned as 'unmatched':
  cleanup_seg_unique  Global search over ALL JSON sidecars: if exactly one
                      sidecar's title first-segment matches the media's
                      first-segment, claim it. The uniqueness constraint is
                      what prevents false matches; sharing a sidecar with
                      another (already-matched) media is OK — Google's
                      '-edited' variants legitimately share the base sidecar.

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
    """Generate media-name variants whose sidecar we should look for.

    Composes paren-stripping and edit-suffix-stripping so that
    `foo-edited(1).jpg` also tries `foo.jpg` (Google's original-photo sidecar).
    """
    candidates: List[str] = []
    stem, ext = _splitext(media_name)

    def add(name: str) -> None:
        if name not in candidates:
            candidates.append(name)

    add(media_name)

    # Detect a trailing "(N)" / "~N" duplicate marker so it can be re-applied
    # after edit-suffix stripping (otherwise foo-edited(1).jpg only reaches
    # foo.jpg and loses the (1) — picking the wrong sidecar in a folder with
    # both foo.jpg.supplemental-metadata.json and ...(1).json).
    m = re.search(r'(\s*\(\d+\)|~\d+)$', stem)
    paren_marker = m.group(1) if m else ''
    base = stem[: m.start()] if m else stem

    seeds = [(stem, '')]
    if base != stem:
        seeds.append((base, paren_marker))

    for seed, marker in seeds:
        add(seed + ext)
        for suf in _EDIT_SUFFIXES:
            if seed.endswith(suf):
                stripped = seed[: -len(suf)]
                if marker:
                    add(stripped + marker + ext)  # more specific first
                add(stripped + ext)
                break
        else:
            # Truncated edit-suffix: filename was cut mid-'-edited'/etc by
            # Google's ~47-char cap. Trim any '-<prefix-of-edit-suffix>' (≥3 chars).
            for suf in _EDIT_SUFFIXES:
                for k in range(len(suf) - 1, 2, -1):
                    partial = suf[:k]
                    if seed.endswith(partial):
                        stripped = seed[: -len(partial)]
                        if marker:
                            add(stripped + marker + ext)
                        add(stripped + ext)
                        break
                else:
                    continue
                break

    # Case-swap extension. Google preserves the source's ORIGINAL casing
    # (often '.JPG' from older cameras) on the base photo while lowercasing
    # the '-edited.jpg' variant — so the edit-stripped candidate misses the
    # uppercase sidecar.
    for cn in list(candidates):
        s, e = _splitext(cn)
        if e and e != e.swapcase():
            add(s + e.swapcase())

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
        n = m.group(1)
        reordered = stem[: m.start()] + ext + '(' + n + ')'
        out.append((reordered + '.json', 'paren'))
        out.append((reordered + _SUPP + '.json', 'paren'))

        # Sidecar-side collision: Google appends (N) AFTER .supplemental-metadata
        # when two media in the same folder yield the same sidecar stem.
        #   foo(1).jpg -> foo.jpg.supplemental-metadata(1).json
        base_cname = stem[: m.start()] + ext
        paren_suf = '(' + n + ').json'
        full_b = base_cname + _SUPP + paren_suf
        if len(full_b) <= _MAX_JSON_LEN:
            out.append((full_b, 'paren'))
        else:
            keep = _MAX_JSON_LEN - len(paren_suf)
            out.append(((base_cname + _SUPP)[:keep] + paren_suf, 'paren'))

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

    # ── same-folder sibling-extension (Live Photo: .MP4 ↔ .HEIC sidecar) ──
    # Google often only sidecars the .HEIC of a Live Photo; the paired .MP4
    # motion clip has no JSON of its own. Look for a sidecar in this folder
    # whose 'title' is the same stem with a different extension.
    #
    # Also try the paren-stripped stem so that foo(1).MP4 can borrow
    # foo.jpg's (1) sidecar via Pattern B (...supplemental-metadata(1).json)
    # — handled below by retrying through _sidecar_basenames on cnames built
    # from the title.
    media_stem = Path(media_name).stem
    if media_stem:
        m_par = re.search(r'(\s*\(\d+\)|~\d+)$', media_stem)
        paren_marker = m_par.group(1) if m_par else ''
        base_stem = media_stem[: m_par.start()] if m_par else media_stem

        # Try base stem first; if media has a (N) marker, prefer a sidecar
        # whose basename carries the same marker (so foo(1).MP4 picks up
        # foo.jpg.supplemental-metadata(1).json, not the base one).
        siblings = db.in_parent_with_title_stem(media_parent, base_stem)
        if siblings:
            if paren_marker:
                matching = [p for p in siblings if paren_marker + '.json' in Path(p).name]
                if matching:
                    return MatchResult(matching[0], 'same_sibling_ext')
            return MatchResult(siblings[0], 'same_sibling_ext')

        if media_stem != base_stem:
            siblings = db.in_parent_with_title_stem(media_parent, media_stem)
            if siblings:
                return MatchResult(siblings[0], 'same_sibling_ext')

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


_FIRST_SEG_RE = re.compile(r'^([^-(~]+)')
_MIN_FIRST_SEG = 4


def _first_segment(stem: str) -> str:
    """Stem up to the first '-', '(' or '~' — the part Google preserves
    intact across edit variants, paren dupes, and tilde suffixes."""
    m = _FIRST_SEG_RE.match(stem)
    return m.group(1) if m else stem


def _build_segment_index(db: IndexDB) -> dict:
    """Map lowercase first-segment → list of JSON paths.

    Built over ALL indexed JSONs (not just orphans). The cleanup pass relies
    on segment uniqueness, not orphan-status — a JSON already claimed by a
    base photo can also legitimately serve its '-edited' / variant siblings.
    """
    rows = db.conn.execute(
        "SELECT path, title, stem FROM json_files"
    ).fetchall()
    idx: dict = {}
    for path, title, stem in rows:
        target = (title or stem or '')
        target_stem = target.rsplit('.', 1)[0] if '.' in target else target
        seg = _first_segment(target_stem).lower()
        if len(seg) < _MIN_FIRST_SEG:
            continue
        idx.setdefault(seg, []).append(path)
    return idx


# Back-compat alias (existing __main__ import).
_build_orphan_segment_index = _build_segment_index


def cleanup_match_via_index(media_path: Path, seg_idx: dict) -> MatchResult:
    """
    First-segment uniqueness cleanup. If the WHOLE JSON index has exactly ONE
    sidecar whose title's first segment matches the media's first segment,
    claim it — even if another media already points to that sidecar (a base
    photo and its '-edited' variant share one JSON in Google's export).

    Global search (not folder-scoped) — the main matcher already exhausted
    same-folder options; the uniqueness constraint is what prevents false
    matches.
    """
    seg = _first_segment(media_path.stem)
    if len(seg) < _MIN_FIRST_SEG:
        return NO_MATCH
    matches = seg_idx.get(seg.lower(), ())
    if len(matches) == 1:
        return MatchResult(matches[0], 'cleanup_seg_unique')
    return NO_MATCH


def cleanup_match(media_path: Path, db: IndexDB) -> MatchResult:
    """Single-file convenience wrapper. For bulk use, build the index once
    via `_build_segment_index` and call `cleanup_match_via_index`."""
    return cleanup_match_via_index(media_path, _build_segment_index(db))

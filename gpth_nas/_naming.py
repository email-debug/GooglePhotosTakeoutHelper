"""
Shared filename-decomposition helpers.

Lives in its own module because both the matcher and the media-index
compute identical things over filenames (first segment, paren markers,
extension splitting) — keeping the regexes here removes the drift risk
of two copies, which would silently desync any cleanup match that joins
across them.
"""
import re
from pathlib import Path
from typing import Tuple

# A filename's "first segment" is its stem up to the first '-', '(' or '~'.
# Google preserves this prefix verbatim across every variant it generates
# (-edited, (N) dupe, ~N tilde marker, hard-truncated -e), which makes it
# the most stable structural anchor for matching unrelated-looking siblings
# back to their base photo's sidecar.
_FIRST_SEG_RE = re.compile(r'^([^-(~]+)')

# Cleanup matching requires this much first-segment to consider a media file
# distinctive enough for a global uniqueness check. Shorter prefixes collide
# too often across unrelated photos (IMG_, DSC_, etc.) to be safely matched.
MIN_FIRST_SEG = 4


def first_segment(stem: str) -> str:
    """Return the stem up to the first '-', '(' or '~' — or the whole stem
    if no such delimiter exists."""
    m = _FIRST_SEG_RE.match(stem)
    return m.group(1) if m else stem


def splitext(name: str) -> Tuple[str, str]:
    """Path-aware stem/suffix split. Kept as a single-line helper so callers
    don't have to construct a Path twice when they need both halves."""
    p = Path(name)
    return p.stem, p.suffix

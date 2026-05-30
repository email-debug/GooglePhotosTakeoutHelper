"""
Unit tests for gpth_nas.matching against a synthetic SQLite index.

Covers the three matcher gaps fixed in this change:
  - Live Photo MP4 borrowing its HEIC sibling's sidecar (same_sibling_ext)
  - Sidecar-side collision form `name.ext.supplemental-metadata(N).json` (same_paren)
  - Composed paren+edit stripping (foo-edited(1).jpg → foo.jpg)

Plus regression coverage of the existing strategies.
"""
from pathlib import Path

import pytest

from gpth_nas.index_db import IndexDB
from gpth_nas.matching import (
    _build_orphan_segment_index,
    cleanup_match,
    cleanup_match_via_index,
    match_media,
)


@pytest.fixture
def db(tmp_path):
    with IndexDB(tmp_path / 'index.db') as d:
        yield d


def _insert(db, full_path: str, title: str):
    p = Path(full_path)
    basename = p.name
    parent = str(p.parent)
    stem = IndexDB._stem_of_json(basename)
    db.conn.execute(
        "INSERT OR REPLACE INTO json_files"
        "(path,basename,parent,stem,title,taken_ts,raw_json)"
        " VALUES(?,?,?,?,?,?,?)",
        (full_path, basename, parent, stem, title, 1700000000, '{}'),
    )


def test_same_exact(db):
    _insert(db, '/a/b/IMG_1.jpg.supplemental-metadata.json', 'IMG_1.jpg')
    r = match_media(Path('/a/b/IMG_1.jpg'), db)
    assert r.json_path == '/a/b/IMG_1.jpg.supplemental-metadata.json'
    assert r.match_type == 'same_exact'


def test_same_truncated(db):
    # Long media name → Google truncates sidecar to 51 chars total.
    long_media = 'main-image-star-forming-region-carina-nircam-f.avif'
    sidecar = '/a/b/main-image-star-forming-region-carina-nircam-f.json'
    _insert(db, sidecar, 'main-image-star-forming-region-carina-nircam-final-1280.avif')
    r = match_media(Path(f'/a/b/{long_media}'), db)
    assert r.json_path == sidecar
    assert r.match_type == 'same_truncated'


def test_same_paren_sidecar_collision(db):
    """Two media in same folder produce same sidecar stem -> Google appends (N)
    AFTER .supplemental-metadata. The (1) sidecar must be reachable from the
    (1) media."""
    _insert(db, '/a/b/20141023_162005.jpg.supplemental-metadata.json', '20141023_162005.jpg')
    _insert(db, '/a/b/20141023_162005.jpg.supplemental-metadata(1).json', '20141023_162005.jpg')

    # Base media gets the base sidecar.
    r1 = match_media(Path('/a/b/20141023_162005.jpg'), db)
    assert r1.json_path == '/a/b/20141023_162005.jpg.supplemental-metadata.json'

    # (1) media must reach the (1) sidecar, NOT fall through to the base one.
    r2 = match_media(Path('/a/b/20141023_162005(1).jpg'), db)
    assert r2.json_path == '/a/b/20141023_162005.jpg.supplemental-metadata(1).json'
    assert r2.match_type == 'same_paren'


def test_same_edited_composed_with_paren(db):
    """foo-edited(1).jpg should fall back to foo.jpg's sidecar (compose paren
    strip and edit-suffix strip)."""
    _insert(db, '/a/b/20141023_162005.jpg.supplemental-metadata.json', '20141023_162005.jpg')
    _insert(db, '/a/b/20141023_162005.jpg.supplemental-metadata(1).json', '20141023_162005.jpg')

    # -edited(1) prefers the (1) sidecar (paren still applies).
    r = match_media(Path('/a/b/20141023_162005-edited(1).jpg'), db)
    assert r.json_path == '/a/b/20141023_162005.jpg.supplemental-metadata(1).json'


def test_same_sibling_ext_live_photo(db):
    """Live Photo: folder has IMG_4066.HEIC + IMG_4066.HEIC.supplemental-metadata.json
    + IMG_4066.MP4. The MP4 should match the HEIC's sidecar."""
    _insert(db, '/a/b/IMG_4066.HEIC.supplemental-metadata.json', 'IMG_4066.HEIC')
    r = match_media(Path('/a/b/IMG_4066.MP4'), db)
    assert r.json_path == '/a/b/IMG_4066.HEIC.supplemental-metadata.json'
    assert r.match_type == 'same_sibling_ext'


def test_sibling_ext_does_not_cross_folders(db):
    """The sibling-extension fallback is same-folder only — must not return
    a sidecar from a different folder."""
    _insert(db, '/x/y/IMG_4066.HEIC.supplemental-metadata.json', 'IMG_4066.HEIC')
    r = match_media(Path('/a/b/IMG_4066.MP4'), db)
    # Falls through to title-based cross-folder strategies, which won't fire
    # here because the title is "IMG_4066.HEIC", not "IMG_4066.MP4".
    assert r.match_type != 'same_sibling_ext'


def test_cross_folder_exact(db):
    _insert(db, '/x/y/IMG_1.jpg.supplemental-metadata.json', 'IMG_1.jpg')
    r = match_media(Path('/a/b/IMG_1.jpg'), db)
    assert r.json_path == '/x/y/IMG_1.jpg.supplemental-metadata.json'
    assert r.match_type == 'cross_exact'


def test_no_match(db):
    r = match_media(Path('/a/b/FB_IMG_999.jpg'), db)
    assert r.json_path is None


def test_case_swapped_ext(db):
    """Edited variant is foo-edited.jpg (lowercase) but original sidecar
    is foo.JPG.supplemental-metadata.json (uppercase ext from old camera).
    Matcher must try both ext cases on the edit-stripped candidate."""
    _insert(db, '/a/b/IMG_1930.JPG.supplemental-metadata.json', 'IMG_1930.JPG')
    r = match_media(Path('/a/b/IMG_1930-edited.jpg'), db)
    assert r.json_path == '/a/b/IMG_1930.JPG.supplemental-metadata.json'


def test_sibling_ext_with_paren_marker(db):
    """foo(1).MP4 should match foo.jpg.supplemental-metadata(1).json
    via sibling-ext, preferring the (1) variant over the base."""
    _insert(db, '/a/b/20210714_111659.jpg.supplemental-metadata.json', '20210714_111659.jpg')
    _insert(db, '/a/b/20210714_111659.jpg.supplemental-metadata(1).json', '20210714_111659.jpg')
    r = match_media(Path('/a/b/20210714_111659(1).MP4'), db)
    assert r.json_path == '/a/b/20210714_111659.jpg.supplemental-metadata(1).json'


def test_cleanup_extreme_truncation(db):
    """Google hard-truncated '-edited' to just '-e'. The main matcher's
    partial-suffix detector stops at 3 chars; cleanup tries down to 1.
    Real case from Tim's takeout."""
    base_sidecar = '/a/b/bigstockphoto_Woman_Real_Estate_Agent_1926781..json'
    _insert(db, base_sidecar, 'bigstockphoto_Woman_Real_Estate_Agent_1926781.jpg')

    r = cleanup_match(Path('/a/b/bigstockphoto_Woman_Real_Estate_Agent_1926781-e.jpg'), db)
    assert r.json_path == base_sidecar
    assert r.match_type == 'cleanup_seg_unique'


def test_cleanup_does_not_match_random_dash(db):
    """An unmatched 'totally-different-X.jpg' must not claim an unrelated
    orphan just because '-X' looks edit-suffix-ish — the cleanup pass keys
    off the FIRST segment ('totally'), and 'unrelated' doesn't share it."""
    _insert(db, '/a/b/unrelated.jpg.supplemental-metadata.json', 'unrelated.jpg')
    # Pretend the orphan is unclaimed by leaving 'processed' empty.
    r = cleanup_match(Path('/a/b/totally-different-X.jpg'), db)
    assert r.json_path is None


def test_cleanup_ambiguous_rejected(db):
    """If TWO orphans share the media's first segment, refuse to guess."""
    _insert(db, '/a/b/IMG_4404.JPG.supplemental-metadata.json', 'IMG_4404.JPG')
    _insert(db, '/x/y/IMG_4404.HEIC.supplemental-metadata.json', 'IMG_4404.HEIC')
    r = cleanup_match(Path('/c/d/IMG_4404-e.png'), db)
    assert r.json_path is None  # ambiguous → no match


def test_cleanup_shares_with_non_orphan(db):
    """The sidecar may already be claimed by a base photo — cleanup must
    still let the '-edited' variant share it. Real case: bigstockphoto
    base + sidecar were already matched, the '-e' truncated variant should
    inherit the same sidecar."""
    base_sidecar = '/a/b/long_unique_prefix_xyz..json'
    _insert(db, base_sidecar, 'long_unique_prefix_xyz.jpg')

    # Simulate base photo already claiming the sidecar.
    db.mark_processed(
        media_path='/a/b/long_unique_prefix_xyz.jpg',
        json_path=base_sidecar,
        match_type='same_truncated',
        dest_path=None,
        status='prescan',
    )

    # The '-e' variant should still cleanup-match the shared sidecar.
    r = cleanup_match(Path('/a/b/long_unique_prefix_xyz-e.jpg'), db)
    assert r.json_path == base_sidecar
    assert r.match_type == 'cleanup_seg_unique'


def test_cleanup_global_not_folder_scoped(db):
    """Cleanup uses the WHOLE orphan set — a unique match in a different
    folder still wins. The main matcher already exhausted same-folder
    options, so a globally-unique first-segment match is the correct call."""
    _insert(db, '/x/y/uniqueprefix_1234.jpg.supplemental-metadata.json', 'uniqueprefix_1234.jpg')
    r = cleanup_match(Path('/a/b/uniqueprefix_1234-e.jpg'), db)
    assert r.json_path == '/x/y/uniqueprefix_1234.jpg.supplemental-metadata.json'
    assert r.match_type == 'cleanup_seg_unique'


def test_truncated_edit_suffix(db):
    """Google sometimes truncates the filename mid-'-edited' to fit the
    ~47-char cap (e.g. 'foo-edite.jpg'). Matcher must recognize the partial
    suffix and fall back to the base photo's sidecar."""
    _insert(
        db,
        '/a/b/bigstockphoto_Human_Resource_Forms_756416.jpg.supplemental-metadata.json',
        'bigstockphoto_Human_Resource_Forms_756416.jpg',
    )
    r = match_media(Path('/a/b/bigstockphoto_Human_Resource_Forms_756416-edite.jpg'), db)
    assert r.json_path == '/a/b/bigstockphoto_Human_Resource_Forms_756416.jpg.supplemental-metadata.json'

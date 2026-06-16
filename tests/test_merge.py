"""Unit tests for the merge-local matcher.

Focused on the structural rules — TZ-aware matching, year/size tolerance,
junk filtering — without exercising actual EXIF/QuickTime parsing libs
(those are integration concerns covered by manual testing on the live
corpus).
"""
from datetime import datetime
from pathlib import Path

import pytest

from gpth_nas.index_db import IndexDB
from gpth_nas.media_db import MediaDB
from gpth_nas.merge import (
    JUNK_EXTS, JUNK_PATH_TOKENS,
    classify_local, infer_year, is_junk,
    name_year_size_match, resolve_date, tz_aware_match,
)


def _seed_json(idx, path, taken_ts):
    """Bypass upsert_json — same Windows pathlib normalisation issue
    documented in test_matcher_unit._insert."""
    p = Path(path)
    idx.conn.execute(
        "INSERT OR REPLACE INTO json_files"
        "(path,basename,parent,stem,title,taken_ts,raw_json)"
        " VALUES(?,?,?,?,?,?,?)",
        (path, p.name, str(p.parent), p.stem, p.stem,
         taken_ts, '{}'),
    )


def _seed_media(media, path, size):
    p = Path(path)
    media.conn.execute(
        "INSERT OR REPLACE INTO media_files"
        "(path,basename,parent,stem,ext,first_seg,size_bytes,mtime)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (path, p.name, str(p.parent), p.stem, p.suffix,
         p.stem.split('-')[0].split('(')[0].split('~')[0], size, 0),
    )


@pytest.fixture
def dbs(tmp_path):
    with IndexDB(tmp_path / 'i.db') as i, MediaDB(tmp_path / 'm.db') as m:
        yield i, m


def test_junk_path():
    assert is_junk(Path('/x/Icons/foo.png'))
    assert is_junk(Path('/x/Equilliance/y.jpg'))
    assert is_junk(Path('/x/Other/Resize Test/y.jpg'))
    assert is_junk(Path('/x/y/foo.ico'))
    assert is_junk(Path('/x/y/Thumbs.db'))
    assert is_junk(Path('/x/.tmp.drivedownload/z.jpg'))
    assert not is_junk(Path('/x/2014 Italy/IMG_1.jpg'))


def test_infer_year():
    assert infer_year(Path('/a/2014 Italy/IMG_1.jpg')) == 2014
    assert infer_year(Path('/a/Photos from 2022/foo.jpg')) == 2022
    assert infer_year(Path('/a/2014-08-27/foo.jpg')) == 2014
    assert infer_year(Path('/a/no-year/foo.jpg')) is None


def test_tz_aware_exact_offset(dbs):
    """Local EXIF says 2014-05-24 18:11:58 (naive); NAS taken_ts is the
    same wall-clock at UTC+6 → 22:11:58 UTC. Matcher must accept."""
    idx, _ = dbs
    local_dt = datetime(2014, 5, 24, 18, 11, 58)
    local_ts = int((local_dt - datetime(1970, 1, 1)).total_seconds())
    nas_ts = local_ts + 6 * 3600
    _seed_json(idx, '/n/jp1.json', nas_ts)

    hit = tz_aware_match(local_ts, idx)
    assert hit is not None
    assert hit[0] == '/n/jp1.json'
    assert hit[1] == 6


def test_tz_aware_rejects_non_hour_offset(dbs):
    """A 4500s offset isn't a whole-hour delta — must NOT match (would
    false-positive on unrelated photos)."""
    idx, _ = dbs
    local_ts = 1_400_000_000
    _seed_json(idx, '/n/jp1.json', local_ts + 4500)  # 1h15min, not a TZ
    hit = tz_aware_match(local_ts, idx)
    assert hit is None


def test_tz_aware_5s_residual_ok(dbs):
    """Camera clock drift of a few seconds shouldn't kill the match."""
    idx, _ = dbs
    local_ts = 1_400_000_000
    _seed_json(idx, '/n/jp1.json', local_ts + 6 * 3600 + 3)  # 3s drift
    hit = tz_aware_match(local_ts, idx)
    assert hit is not None
    assert hit[1] == 6


def test_tz_aware_residual_too_big(dbs):
    """A 10s residual on top of a 6h offset is past the threshold."""
    idx, _ = dbs
    local_ts = 1_400_000_000
    _seed_json(idx, '/n/jp1.json', local_ts + 6 * 3600 + 10)
    hit = tz_aware_match(local_ts, idx)
    assert hit is None


def test_tz_match_requires_size_match_when_media_db_given(dbs):
    """Same EXIF but different bytes (an edited photo) must NOT match.
    Otherwise the merge silently drops the user's edit."""
    idx, media = dbs
    local_ts = 1_400_000_000
    _seed_json(idx, '/n/IMG_1.JPG.supplemental-metadata.json', local_ts)
    # The JSON's `title` is 'IMG_1.JPG.supplemental-metadata'; the matcher
    # uses title as the basename to look up NAS media. Seed a media row
    # whose basename matches.
    idx.conn.execute(
        "UPDATE json_files SET title=?, parent=? WHERE path=?",
        ('IMG_1.JPG', '/n', '/n/IMG_1.JPG.supplemental-metadata.json'),
    )
    _seed_media(media, '/n/IMG_1.JPG', size=4_000_000)

    # Local file is 2x larger — clearly edited / higher quality. Must not
    # be skipped.
    hit = tz_aware_match(local_ts, idx, local_size=8_000_000, media_db=media)
    assert hit is None


def test_tz_match_accepts_close_size(dbs):
    """Same EXIF + size within 5% → confident dup. Catches Google's
    same-format recompression without false-positives on edits."""
    idx, media = dbs
    local_ts = 1_400_000_000
    _seed_json(idx, '/n/IMG_1.JPG.supplemental-metadata.json', local_ts)
    idx.conn.execute(
        "UPDATE json_files SET title=?, parent=? WHERE path=?",
        ('IMG_1.JPG', '/n', '/n/IMG_1.JPG.supplemental-metadata.json'),
    )
    _seed_media(media, '/n/IMG_1.JPG', size=4_100_000)  # 2.5% smaller

    hit = tz_aware_match(local_ts, idx, local_size=4_000_000, media_db=media)
    assert hit is not None
    assert hit[0] == '/n/IMG_1.JPG.supplemental-metadata.json'


def test_tz_match_orphan_json_skipped(dbs):
    """If a NAS JSON has matching taken_ts but the corresponding media
    file isn't in MediaDB (orphan sidecar), we can't confirm the photo
    is in the archive. Treat as not-a-dup."""
    idx, media = dbs
    local_ts = 1_400_000_000
    _seed_json(idx, '/n/IMG_1.JPG.supplemental-metadata.json', local_ts)
    idx.conn.execute(
        "UPDATE json_files SET title=?, parent=? WHERE path=?",
        ('IMG_1.JPG', '/n', '/n/IMG_1.JPG.supplemental-metadata.json'),
    )
    # No media seeded — sidecar is orphan.
    hit = tz_aware_match(local_ts, idx, local_size=4_000_000, media_db=media)
    assert hit is None


def test_name_year_size_match(dbs):
    _, media = dbs
    _seed_media(media, '/n/2014/06/IMG_4404.JPG', 4_000_000)
    nas = name_year_size_match(
        'IMG_4404.JPG', year=2014, size=4_200_000, media_db=media,
    )
    assert nas == '/n/2014/06/IMG_4404.JPG'


def test_name_year_size_rejects_year_mismatch(dbs):
    _, media = dbs
    _seed_media(media, '/n/2010/05/IMG_4404.JPG', 4_000_000)
    nas = name_year_size_match(
        'IMG_4404.JPG', year=2014, size=4_200_000, media_db=media,
    )
    assert nas is None


def test_name_year_size_rejects_size_mismatch(dbs):
    _, media = dbs
    _seed_media(media, '/n/2014/06/IMG_4404.JPG', 4_000_000)
    nas = name_year_size_match(
        'IMG_4404.JPG', year=2014, size=8_000_000, media_db=media,
    )
    assert nas is None


def test_classify_junk(dbs):
    idx, media = dbs
    decision, _ = classify_local(Path('/x/Icons/foo.png'), idx, media)
    assert decision == 'skipped_junk'


def test_resolve_date_prefers_exif():
    ts = int((datetime(2014, 5, 24, 18, 11, 58) - datetime(1970, 1, 1)).total_seconds())
    dt = resolve_date(Path('/x/2020/foo.jpg'), exif_ts=ts, mp4_ts=None)
    assert dt.year == 2014 and dt.month == 5


def test_resolve_date_falls_back_to_folder():
    dt = resolve_date(Path('/x/2014 Italy/foo.jpg'), exif_ts=None, mp4_ts=None)
    assert dt.year == 2014

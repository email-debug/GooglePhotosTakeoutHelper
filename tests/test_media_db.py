"""
Unit tests for the MediaDB index and its integration with the matcher.
"""
from pathlib import Path

import pytest

from gpth_nas.index_db import IndexDB
from gpth_nas.media_db import MediaDB, _first_segment


def test_first_segment():
    assert _first_segment('IMG_1929') == 'IMG_1929'
    assert _first_segment('IMG_1929-edited') == 'IMG_1929'
    assert _first_segment('IMG_1929(1)') == 'IMG_1929'
    assert _first_segment('IMG_1929~2') == 'IMG_1929'
    assert _first_segment('bigstockphoto_Woman_Real_Estate_Agent_1926781-e') == \
        'bigstockphoto_Woman_Real_Estate_Agent_1926781'


def test_media_db_roundtrip(tmp_path):
    f1 = tmp_path / 'a' / 'IMG_1.jpg'
    f2 = tmp_path / 'a' / 'IMG_2-edited.png'
    with MediaDB(tmp_path / 'm.db') as m:
        m.upsert_media(f1, size_bytes=12345, mtime=1700000000)
        m.upsert_media(f2)
        m.commit()
        assert m.count() == 2

        paths = list(m.iter_all())
        assert str(f1) in paths
        assert str(f2) in paths

        seg_paths = list(m.iter_by_first_seg('IMG_2'))
        assert seg_paths == [str(f2)]


def test_media_db_idempotent_upsert(tmp_path):
    """Re-ingesting the same path mustn't duplicate rows."""
    f = tmp_path / 'IMG_1.jpg'
    with MediaDB(tmp_path / 'm.db') as m:
        for _ in range(3):
            m.upsert_media(f, size_bytes=100, mtime=1)
        m.commit()
        assert m.count() == 1


def test_in_parent_with_basename(tmp_path):
    f1 = tmp_path / 'a' / 'IMG_1.jpg'
    f2 = tmp_path / 'x' / 'IMG_1.jpg'
    with MediaDB(tmp_path / 'm.db') as m:
        m.upsert_media(f1)
        m.upsert_media(f2)
        m.commit()
        assert m.in_parent_with_basename(str(f1.parent), 'IMG_1.jpg') == str(f1)
        assert m.in_parent_with_basename(str(f1.parent), 'IMG_2.jpg') is None

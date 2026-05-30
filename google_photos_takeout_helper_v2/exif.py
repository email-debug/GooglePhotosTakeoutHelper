"""
EXIF metadata operations: read, write, and GPS injection.

Consolidates v1's scattered EXIF functions (duplicated across main() and
run_enrich_local) into a single module with clear error handling.
"""
import math
import os
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Optional

import piexif
from loguru import logger

from .config import EXIF_DATETIME_FORMAT
from .utils import datetime_from_timestamp, timestamp_from_datetime

# EXIF tag constants
TAG_DATE_TIME_ORIGINAL = piexif.ExifIFD.DateTimeOriginal
TAG_DATE_TIME_DIGITIZED = piexif.ExifIFD.DateTimeDigitized
TAG_DATE_TIME = 306  # IFD0 DateTime


# ── Reading EXIF ──────────────────────────────────────────────────────────────

def get_exif_date(file: Path) -> Optional[str]:
    """
    Read DateTimeOriginal / DateTimeDigitized / DateTime from EXIF.

    Returns date string in EXIF format (YYYY:MM:DD HH:MM:SS) or None.
    Does not raise — returns None on any failure.
    """
    try:
        exif_dict = piexif.load(str(file))
    except Exception:
        return None

    tags = [
        ('Exif', TAG_DATE_TIME_ORIGINAL),
        ('Exif', TAG_DATE_TIME_DIGITIZED),
        ('0th', TAG_DATE_TIME),
    ]
    for ifd, tag in tags:
        try:
            raw = exif_dict[ifd][tag]
            if isinstance(raw, bytes):
                raw = raw.decode('UTF-8')
            # Normalize separators: YYYY-MM-DD -> YYYY:MM:DD
            raw = raw.replace('-', ':').replace('/', ':').replace('.', ':') \
                      .replace('\\', ':').replace(': ', ':0')[:19]
            # Validate by parsing
            datetime.strptime(raw, EXIF_DATETIME_FORMAT)
            return raw
        except (KeyError, ValueError, AttributeError, UnicodeDecodeError):
            continue
    return None


def get_exif_date_as_datetime(file: Path) -> Optional[datetime]:
    """Read EXIF date and return as datetime object, or None."""
    date_str = get_exif_date(file)
    if date_str:
        try:
            return datetime.strptime(date_str, EXIF_DATETIME_FORMAT)
        except ValueError:
            pass
    return None


# ── Writing EXIF ──────────────────────────────────────────────────────────────

def set_file_exif_date(file: Path, date_str: str) -> bool:
    """
    Write date string to all three EXIF date tags.

    Args:
        file: Image file to modify.
        date_str: Date in EXIF format (YYYY:MM:DD HH:MM:SS).

    Returns:
        True if successful, False if EXIF write failed.
    """
    try:
        exif_dict = piexif.load(str(file))
    except Exception:
        exif_dict = {'0th': {}, 'Exif': {}}

    encoded = date_str.encode('UTF-8')
    exif_dict['0th'][TAG_DATE_TIME] = encoded
    exif_dict['Exif'][TAG_DATE_TIME_ORIGINAL] = encoded
    exif_dict['Exif'][TAG_DATE_TIME_DIGITIZED] = encoded

    try:
        piexif.insert(piexif.dump(exif_dict), str(file))
        return True
    except Exception as e:
        logger.debug(f"Couldn't insert EXIF date for {file}: {e}")
        return False


def set_file_timestamps(file: Path, date_str: str) -> bool:
    """
    Set file system timestamps (mtime, atime, and ctime on Windows) from a date string.

    Args:
        file: File to modify.
        date_str: Date in EXIF format or similar (will be normalized).

    Returns:
        True if successful, False on error.
    """
    try:
        # Normalize various date separators to EXIF format
        normalized = date_str.replace('-', ':').replace('/', ':').replace('.', ':') \
                             .replace('\\', ':').replace(': ', ':0')[:19]
        timestamp = timestamp_from_datetime(
            datetime.strptime(normalized, EXIF_DATETIME_FORMAT)
        )
        os.utime(file, (timestamp, timestamp))
        if os.name == 'nt':
            try:
                import win32_setctime
                win32_setctime.setctime(str(file), timestamp)
            except ImportError:
                pass  # win32_setctime not available on non-Windows
        return True
    except Exception as e:
        logger.debug(f"Error setting timestamps for {file}: {e}")
        return False


def set_creation_date_from_exif(file: Path) -> bool:
    """
    Read EXIF date from file and set file system timestamps to match.

    Returns True if date was found and set, False otherwise.
    """
    date_str = get_exif_date(file)
    if date_str is None:
        return False
    return set_file_timestamps(file, date_str)


# ── JSON sidecar date ─────────────────────────────────────────────────────────

def get_date_str_from_json(json_dict: dict) -> Optional[str]:
    """
    Extract photoTakenTime from a Google JSON sidecar and format as EXIF date string.

    Returns None if the timestamp is missing or invalid.
    """
    try:
        ts = int(json_dict['photoTakenTime']['timestamp'])
        return datetime_from_timestamp(ts).strftime(EXIF_DATETIME_FORMAT)
    except (KeyError, ValueError, TypeError):
        return None


# ── GPS ───────────────────────────────────────────────────────────────────────

def _change_to_rational(number):
    """Convert a number to rational tuple (numerator, denominator)."""
    f = Fraction(str(number))
    return f.numerator, f.denominator


def _deg_to_dms_rational(deg_float):
    """Convert decimal degrees to DMS rational format for EXIF GPS."""
    min_float = deg_float % 1 * 60
    sec_float = min_float % 1 * 60
    deg = math.floor(deg_float)
    deg_min = math.floor(min_float)
    sec = round(sec_float * 100)
    return [(deg, 1), (deg_min, 1), (sec, 100)]


def _safe_float(val):
    """Convert to float, returning 0.0 for string values (Google's null marker)."""
    if isinstance(val, str):
        return 0.0
    return float(val)


def _build_gps_ifd(json_dict: dict):
    """
    Build GPS EXIF IFD dict from a Google JSON sidecar.

    Returns the GPS IFD dict, or None if no location data.
    """
    geo = json_dict.get('geoData', {})
    longitude = _safe_float(geo.get('longitude', 0))
    latitude = _safe_float(geo.get('latitude', 0))
    altitude = _safe_float(geo.get('altitude', 0))

    if longitude == 0 and latitude == 0:
        geo_exif = json_dict.get('geoDataExif', {})
        if geo_exif:
            longitude = _safe_float(geo_exif.get('longitude', 0))
            latitude = _safe_float(geo_exif.get('latitude', 0))
            altitude = _safe_float(geo_exif.get('altitude', 0))

    if latitude == 0 and longitude == 0:
        return None

    lat_ref = 'N' if latitude >= 0 else 'S'
    lon_ref = 'E' if longitude >= 0 else 'W'
    latitude = abs(latitude)
    longitude = abs(longitude)

    gps_ifd = {
        piexif.GPSIFD.GPSVersionID: (2, 0, 0, 0),
        piexif.GPSIFD.GPSLatitudeRef: lat_ref,
        piexif.GPSIFD.GPSLatitude: _deg_to_dms_rational(latitude),
        piexif.GPSIFD.GPSLongitudeRef: lon_ref,
        piexif.GPSIFD.GPSLongitude: _deg_to_dms_rational(longitude),
    }

    if altitude != 0:
        gps_ifd[piexif.GPSIFD.GPSAltitudeRef] = 1
        gps_ifd[piexif.GPSIFD.GPSAltitude] = _change_to_rational(round(altitude))

    return gps_ifd


def set_file_geo_data(file: Path, json_dict: dict) -> bool:
    """
    Read geoData from a Google JSON sidecar and write to EXIF GPS tags.

    Prioritizes geoData (edited in Google Photos) over geoDataExif (camera original).
    Skips if both lat and lon are 0 (no location data).

    Args:
        file: Image file to modify.
        json_dict: Parsed Google JSON sidecar.

    Returns:
        True if GPS data was written, False otherwise.
    """
    try:
        exif_dict = piexif.load(str(file))
    except Exception:
        exif_dict = {'0th': {}, 'Exif': {}}

    gps_ifd = _build_gps_ifd(json_dict)
    if gps_ifd is None:
        return False

    exif_dict['GPS'] = gps_ifd

    try:
        piexif.insert(piexif.dump(exif_dict), str(file))
        return True
    except Exception as e:
        logger.debug(f"Couldn't insert GPS EXIF for {file}: {e}")
        return False


# ── In-memory EXIF operations (combined copy optimization) ───────────────────


def get_exif_date_from_bytes(data: bytes) -> Optional[str]:
    """
    Read DateTimeOriginal / DateTimeDigitized / DateTime from JPEG bytes in memory.

    Returns date string in EXIF format (YYYY:MM:DD HH:MM:SS) or None.
    """
    try:
        exif_dict = piexif.load(data)
    except Exception:
        return None

    tags = [
        ('Exif', TAG_DATE_TIME_ORIGINAL),
        ('Exif', TAG_DATE_TIME_DIGITIZED),
        ('0th', TAG_DATE_TIME),
    ]
    for ifd, tag in tags:
        try:
            raw = exif_dict[ifd][tag]
            if isinstance(raw, bytes):
                raw = raw.decode('UTF-8')
            raw = raw.replace('-', ':').replace('/', ':').replace('.', ':') \
                      .replace('\\', ':').replace(': ', ':0')[:19]
            datetime.strptime(raw, EXIF_DATETIME_FORMAT)
            return raw
        except (KeyError, ValueError, AttributeError, UnicodeDecodeError):
            continue
    return None


def modify_image_bytes(
    data: bytes,
    date_str: Optional[str] = None,
    geo_json: Optional[dict] = None,
) -> tuple:
    """
    Apply EXIF date and GPS modifications to JPEG bytes in memory.

    This is the core of the combined-pass optimization: instead of writing EXIF
    to the source file then copying, we modify the bytes in memory and write
    the result directly to the destination.

    Args:
        data: Raw JPEG bytes.
        date_str: EXIF date string to inject (optional).
        geo_json: Google JSON sidecar dict for GPS extraction (optional).

    Returns:
        (modified_bytes, geo_was_set: bool)
        Returns original data unchanged if no modifications needed or on failure.
    """
    modified = False
    geo_was_set = False

    try:
        exif_dict = piexif.load(data)
    except Exception:
        exif_dict = {'0th': {}, 'Exif': {}, 'GPS': {}, '1st': {}, 'thumbnail': None}

    if date_str:
        encoded = date_str.encode('UTF-8')
        exif_dict.setdefault('0th', {})[TAG_DATE_TIME] = encoded
        exif_dict.setdefault('Exif', {})[TAG_DATE_TIME_ORIGINAL] = encoded
        exif_dict.setdefault('Exif', {})[TAG_DATE_TIME_DIGITIZED] = encoded
        modified = True

    if geo_json:
        gps_ifd = _build_gps_ifd(geo_json)
        if gps_ifd:
            exif_dict['GPS'] = gps_ifd
            modified = True
            geo_was_set = True

    if not modified:
        return data, geo_was_set

    try:
        exif_bytes = piexif.dump(exif_dict)
        return piexif.insert(exif_bytes, data), geo_was_set
    except Exception as e:
        logger.debug(f"modify_image_bytes failed: {e}")
        return data, False

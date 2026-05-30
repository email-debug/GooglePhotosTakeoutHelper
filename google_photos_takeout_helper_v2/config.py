"""
Centralized configuration: file formats, constants, and shared settings.
"""

PHOTO_FORMATS = frozenset([
    '.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tif', '.tiff', '.svg', '.heic',
    '.avif',
])

JPEG_EXTENSIONS = frozenset(['.jpg', '.jpeg'])

VIDEO_FORMATS = frozenset([
    '.mp4', '.gif', '.mov', '.webm', '.avi', '.wmv', '.rm', '.mpg', '.mpe',
    '.mpeg', '.mkv', '.m4v', '.mts', '.m2ts', '.3gp', '.3g2',
])

ALL_MEDIA_FORMATS = PHOTO_FORMATS | VIDEO_FORMATS

EXTRA_SUFFIXES = [
    '-edited', '-effects', '-smile', '-mix',  # EN/US
    '-edytowane',  # PL
    '-bearbeitet',  # DE
]

EXIF_DATETIME_FORMAT = '%Y:%m:%d %H:%M:%S'

LOG_FILENAME = 'takeout_log.json'

# How often to save incremental progress (every N files)
CACHE_SAVE_INTERVAL = 500

import sys as _sys

from loguru import logger
from tqdm import tqdm as _tqdm

_sys.stdout.reconfigure(encoding='utf-8', errors='backslashreplace')
logger.remove()  # removes the default console logger provided by Loguru.
# I find it to be too noisy with details more appropriate for file logging.
# INFO and messages of higher priority only shown on the console.
logger.add(lambda msg: _tqdm.write(msg, end=""), format="{message}", level="INFO")
# This creates a logging sink and handler that puts all messages at or above the TRACE level into a logfile for each run.
logger.add("file_{time}.log", level="TRACE", encoding="utf8")  # Unicode instructions needed to avoid file write errors.


@logger.catch(
    message=
    "WHHoopssiee! Looks like script crashed! This shouldn't happen, although it often does haha :P\n"
    "Most of the times, you should cut out the last printed file (it should be down there somehwere) "
    "to some other folder, and continue\n"
    "\n"
    "If this doesn't help, and it keeps doing this after many cut-outs, you can check out issues tab:\n"
    "https://github.com/TheLastGimbus/GooglePhotosTakeoutHelper/issues \n"
    "to see if anyone has similar issue, or contact me other way:\n"
    "https://github.com/TheLastGimbus/GooglePhotosTakeoutHelper/blob/master/README.md#contacterrors \n",
    # Still tell the system that something bad happened
    onerror=lambda e: _sys.exit(1)

)  # wraps entire function in a trap to display enhanced error tracebaks after an exception occurs.
def main():
    import argparse as _argparse
    import json as _json
    import os as _os
    import re as _re
    import shutil as _shutil
    import hashlib as _hashlib
    import functools as _functools
    from collections import defaultdict as  _defaultdict
    from datetime import datetime as _datetime
    from datetime import timedelta as _timedelta
    from pathlib import Path as Path

    try:
        from google_photos_takeout_helper.__version__ import __version__
    except ModuleNotFoundError:
        from __version__ import __version__

    import piexif as _piexif
    from fractions import Fraction  # piexif requires some values to be stored as rationals
    import math
    if _os.name == 'nt':
        import win32_setctime as _windoza_setctime

    parser = _argparse.ArgumentParser(
        prog='Google Photos Takeout Helper',
        usage='google-photos-takeout-helper -i [TAKEOUT FOLDER] -o [OUTPUT FOLDER] [OPTIONS]',
        description="""
Google Photos Takeout Helper — Enhanced Edition
================================================
Takes your Google Photos Takeout export, restores correct EXIF dates and GPS
coordinates from the JSON sidecars, and organises everything into a clean
chronological archive (YYYY/MM folders).

ENHANCEMENTS OVER BASE 2.3.0:
  --prescan         Safely scan your Takeout folder BEFORE running the
                    destructive archive operation. Reports JSON sidecar match
                    rate, file counts, date range, and file types. No files
                    are moved or modified. Run this first.

  Cross-folder      Builds a global JSON index across ALL folders before
  JSON matching     matching, so sidecars that Google scattered into different
                    folders/zips are still found. Base gpth only looks in the
                    same folder as each photo — this catches everything else.

  --limit N         Process only the first N files then stop cleanly. Use
                    with a test output folder (e.g. -o "P:\\Test") to inspect
                    real output before committing to the full 900GB run.

  --merge-local     Merge a non-Google local photo folder (camera imports,
                    iPhone backups, etc.) into the same archive. No JSON
                    sidecars required — uses EXIF, filename patterns, and
                    file modified date. Deduplicates against existing archive
                    by SHA-1 hash so Google-verified files are never
                    overwritten.

  --local-structure Control output folder layout for local files:
    dates           Sort into YYYY/MM (default, matches Google archive style)
    preserve        Keep original folder names nested under year

TYPICAL WORKFLOW:
  Step 1 — Pre-scan (safe, read-only):
    python -m google_photos_takeout_helper \\
        -i "M:\\Takeout_Extracted\\Google Photos" --prescan

  Step 2 — Test run (first 500 files into test folder):
    python -m google_photos_takeout_helper \\
        -i "M:\\Takeout_Extracted\\Google Photos" \\
        -o "P:\\Test" --divide-to-dates --limit 500

  Step 3 — Full Google archive run:
    python -m google_photos_takeout_helper \\
        -i "M:\\Takeout_Extracted\\Google Photos" \\
        -o "P:\\" --divide-to-dates --guess-timestamp-from-filename \\
        --albums shortcut --deletesourceimage

  Step 4 — Merge local photos (after Step 3 completes):
    python -m google_photos_takeout_helper \\
        --merge-local "M:\\My_Local_Photos" \\
        -o "P:\\" --local-structure dates \\
        --guess-timestamp-from-filename --deletesourceimage
""",
        formatter_class=_argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--version', action='version', version=f"%(prog)s {__version__}")
    parser.add_argument(
        '-i', '--input-folder',
        type=str,
        required=False,
        default=None,
        help='Input folder with all stuff from Google Photos takeout zip(s). '
             'Optional if --merge-local is provided.'
    )
    parser.add_argument(
        '--merge-local',
        type=str,
        default=None,
        metavar='LOCAL_FOLDER',
        help='Merge a non-Google local photo folder into the output archive. '
             'Files are processed using EXIF data only (no JSON sidecars). '
             'Duplicates already in the archive are skipped by hash. '
             'Use --local-structure to control output folder organisation.'
    )
    parser.add_argument(
        '--local-structure',
        type=str,
        default='dates',
        choices=['dates', 'preserve'],
        help="How to organise non-Google local photos in the output:\n"
             "'dates'    - Sort into YYYY/MM folders using EXIF date (default).\n"
             "             Falls back to filename pattern, then file modified date.\n"
             "             Files with no readable date go to 0000/00/.\n"
             "'preserve' - Keep original folder names, nested under year if date\n"
             "             is readable (e.g. 2019/Scotland Trip/). Files with no\n"
             "             date go to 0000/Unknown/<original-folder-name>/."
    )
    parser.add_argument(
        '-o', '--output-folder',
        type=str,
        required=False,
        default='ALL_PHOTOS',
        help='Output folders which in all photos will be placed in'
    )
    parser.add_argument(
        '--skip-extras',
        action='store_true',
        help='EXPERIMENTAL: Skips the extra photos like photos that end in "edited" or "EFFECTS".'
    )
    parser.add_argument(
        '--skip-extras-harder',  # Oh yeah, skip my extras harder daddy
        action='store_true',
        help='EXPERIMENTAL: Skips the extra photos like photos like pic(1). Also includes --skip-extras.'
    )
    parser.add_argument(
        '--guess-timestamp-from-filename',
        action='store_true',
        help="EXPERIMENTAL: If all reliable methods of identifying a timestamp for a photo fail, also search the filename for common date/time patterns (e.g. 20220101_123456)."
    )
    parser.add_argument(
        "--divide-to-dates",
        action='store_true',
        help="Create folders and subfolders based on the date the photos were taken"
    )
    parser.add_argument(
        '--albums',
        type=str,
        help="EXPERIMENTAL, MAY NOT WORK FOR EVERYONE: What kind of 'albums solution' you would like:\n"
             "'json' - written in a json file\n"
    )
    parser.add_argument(
        '--deletesourceimage',
        action='store_true',
        help='Delete the original image from the input folder after successfully processing and copying it to the output folder.'
    )
    parser.add_argument(
        '--prescan',
        action='store_true',
        help='Scan input folder and report JSON sidecar match rate WITHOUT moving or modifying any files. '
             'Builds a global cross-folder JSON index to catch sidecars Google placed in different folders. '
             'Use before running the full archive operation to understand your match rate.'
    )
    parser.add_argument(
        '--limit',
        type=int,
        default=None,
        metavar='N',
        help='Used with --prescan: copy the first N media files (plus their sidecars) to a staging '
             'folder for a safe test run. Staging folder is created at <input>/../gpth_test_batch/. '
             'Cross-folder sidecars are placed alongside their photo so gpth can find them.'
    )
    args = parser.parse_args()

    # Validate: must have -i or --merge-local (or both)
    if args.input_folder is None and args.merge_local is None and not args.prescan:
        parser.error("At least one of -i/--input-folder or --merge-local is required.")

    logger.info('Heeeere we go!')

    PHOTOS_DIR = Path(args.input_folder) if args.input_folder else None
    FIXED_DIR = Path(args.output_folder)

    TAG_DATE_TIME_ORIGINAL = _piexif.ExifIFD.DateTimeOriginal
    TAG_DATE_TIME_DIGITIZED = _piexif.ExifIFD.DateTimeDigitized
    TAG_DATE_TIME = 306

    EXIF_DATETIME_FORMAT = '%Y:%m:%d %H:%M:%S'

    photo_formats = ['.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tif', '.tiff', '.svg', '.heic']
    video_formats = ['.mp4', '.gif', '.mov', '.webm', '.avi', '.wmv', '.rm', '.mpg', '.mpe', '.mpeg', '.mkv', '.m4v',
                     '.mts', '.m2ts']
    extra_formats = [
        '-edited', '-effects', '-smile', '-mix',  # EN/US
        '-edytowane',  # PL
        '-bearbeitet', # DE
        # Add more "edited" flags in more languages if you want. They need to be lowercase.
    ]

    # Album Multimap
    album_mmap = _defaultdict(list)

    # Duplicate by full hash multimap
    files_by_full_hash = _defaultdict(list)

    # holds all the renamed files that clashed from their
    rename_map = dict()

    _all_jsons_dict = _defaultdict(dict)

    # Statistics:
    s_removed_duplicates_count = 0
    s_copied_files = 0
    s_cant_insert_exif_files = []  # List of files where inserting exif failed
    s_date_from_folder_files = []  # List of files where date was set from folder name
    s_skipped_extra_files = []  # List of extra files ("-edited" etc) which were skipped
    s_no_json_found = []  # List of files where we couldn't find json
    s_no_date_at_all = []  # List of files where there was absolutely no option to set correct date

    FIXED_DIR.mkdir(parents=True, exist_ok=True)

    def for_all_files_recursive(
      dir: Path,
      file_function=lambda fi: True,
      folder_function=lambda fo: True,
      filter_fun=lambda file: True
    ):
        for file in dir.rglob("*"):
            if file.is_dir():
                folder_function(file)
                continue
            elif file.is_file():
                if filter_fun(file):
                    file_function(file)
            else:
                logger.debug(f'Found something weird... {file}')

    # This is required, because windoza crashes when timestamp is negative
    # https://github.com/joke2k/faker/issues/460#issuecomment-308897287
    # This (dynamic assigning a function) mayyy be a little faster than comparing it every time (?)
    datetime_from_timestamp = (lambda t: _datetime(1970, 1, 1) + _timedelta(seconds=int(t))) \
        if _os.name == 'nt' \
        else _datetime.fromtimestamp
    timestamp_from_datetime = (lambda dt: (dt - _datetime(1970, 1, 1)).total_seconds()) \
        if _os.name == 'nt' \
        else _datetime.timestamp

    def is_photo(file: Path):
        if file.suffix.lower() not in photo_formats:
            return False
        # skips the extra photo file, like edited or effects. They're kinda useless.
        nonlocal s_skipped_extra_files
        if args.skip_extras or args.skip_extras_harder:  # if the file name includes something under the extra_formats, it skips it.
            for extra in extra_formats:
                if extra in file.name.lower():
                    s_skipped_extra_files.append(str(file.resolve()))
                    return False
        if args.skip_extras_harder:
            search = r"\(\d+\)\."  # we leave the period in so it doesn't catch folders.
            if bool(_re.search(search, file.name)):
                # PICT0003(5).jpg -> PICT0003.jpg      The regex would match "(5).", and replace it with a "."
                plain_file = file.with_name(_re.sub(search, '.', file.name))
                # if the original exists, it will ignore the (1) file, ensuring there is only one copy of each file.
                if plain_file.is_file():
                    s_skipped_extra_files.append(str(file.resolve()))
                    return False
        return True

    def is_video(file: Path):
        if file.suffix.lower() not in video_formats:
            return False
        return True

    def chunk_reader(fobj, chunk_size=1024):
        """ Generator that reads a file in chunks of bytes """
        while True:
            chunk = fobj.read(chunk_size)
            if not chunk:
                return
            yield chunk

    def get_hash(file: Path, first_chunk_only=False, hash_algo=_hashlib.sha1):
        hashobj = hash_algo()
        with open(file, "rb") as f:
            if first_chunk_only:
                hashobj.update(f.read(1024))
            else:
                for chunk in chunk_reader(f):
                    hashobj.update(chunk)
        return hashobj.digest()

    def populate_album_map(path: Path, filter_fun=lambda f: (is_photo(f) or is_video(f))):
        if not path.is_dir():
            raise NotADirectoryError('populate_album_map only handles directories not files')

        meta_file_exists = find_album_meta_json_file(path)
        if meta_file_exists is None or not meta_file_exists.exists():
            return False

        # means that we are processing an album so process
        for file in path.rglob("*"):
            if not (file.is_file() and filter_fun(file)):
                continue
            file_name = file.name
            # If it's not in the output folder
            if not (FIXED_DIR / file.name).is_file():
                full_hash = None
                try:
                    full_hash = get_hash(file, first_chunk_only=False)
                except Exception as e:
                    logger.debug(e)
                    logger.debug(f"populate_album_map - couldn't get hash of {file}")
                if full_hash is not None and full_hash in files_by_full_hash:
                    full_hash_files = files_by_full_hash[full_hash]
                    if len(full_hash_files) != 1:
                        logger.error("full_hash_files list should only be one after duplication removal, bad state")
                        exit(-5)
                        return False
                    file_name = full_hash_files[0].name

            # check rename map in case there was an overlap namechange
            if str(file) in rename_map:
                file_name = rename_map[str(file)].name

            album_mmap[file.parent.name].append(file_name)

    # PART 3: removing duplicates

    # THIS IS PARTLY COPIED FROM STACKOVERFLOW
    # https://stackoverflow.com/questions/748675/finding-duplicate-files-and-removing-them
    #
    # We now use an optimized version linked from tfeldmann
    # https://gist.github.com/tfeldmann/fc875e6630d11f2256e746f67a09c1ae
    #
    # THANK YOU Todor Minakov (https://github.com/tminakov) and Thomas Feldmann (https://github.com/tfeldmann)
    #
    # NOTE: defaultdict(list) is a multimap, all init array handling is done internally 
    # See: https://en.wikipedia.org/wiki/Multimap#Python
    #
    def find_duplicates(path: Path, filter_fun=lambda file: True):
        files_by_size = _defaultdict(list)
        files_by_small_hash = _defaultdict(list)

        for file in path.rglob("*"):
            if file.is_file() and filter_fun(file):
                try:
                    file_size = file.stat().st_size
                except (OSError, FileNotFoundError):
                    # not accessible (permissions, etc) - pass on
                    continue
                files_by_size[file_size].append(file)

        # For all files with the same file size, get their hash on the first 1024 bytes
        logger.info('Calculating small hashes...')
        for file_size, files in _tqdm(files_by_size.items(), unit='files-by-size'):
            if len(files) < 2:
                continue  # this file size is unique, no need to spend cpu cycles on it

            for file in files:
                try:
                    small_hash = get_hash(file, first_chunk_only=True)
                except OSError:
                    # the file access might've changed till the exec point got here
                    continue
                files_by_small_hash[(file_size, small_hash)].append(file)

        # For all files with the hash on the first 1024 bytes, get their hash on the full
        # file - if more than one file is inserted on a hash here they are certinly duplicates
        logger.info('Calculating full hashes...')
        for files in _tqdm(files_by_small_hash.values(), unit='files-by-small-hash'):
            if len(files) < 2:
                # the hash of the first 1k bytes is unique -> skip this file
                continue

            for file in files:
                try:
                    full_hash = get_hash(file, first_chunk_only=False)
                except OSError:
                    # the file access might've changed till the exec point got here
                    continue

                files_by_full_hash[full_hash].append(file)

    # Removes all duplicates in folder
    # ONLY RUN AFTER RUNNING find_duplicates()
    def remove_duplicates():
        nonlocal s_removed_duplicates_count
        # Now we have populated the final multimap of absolute dups, We now can attempt to find the original file
        # and remove all the other duplicates
        for files in _tqdm(files_by_full_hash.values(), unit='duplicates'):
            if len(files) < 2:
                continue  # this file size is unique, no need to spend cpu cycles on it

            s_removed_duplicates_count += len(files) - 1
            for file in files:
                # TODO reconsider which dup we delete these now that we're searching globally?
                if len(files) > 1:
                    file.unlink()
                    files.remove(file)
        return True

    # PART 1: Fixing metadata and date-related stuff

    # Returns json dict
    def find_json_for_file(file: Path):
        parenthesis_regexp = r'\([0-9]+\)'

        def json_paths_for_filename(name: str):
            """Build all possible paths to the JSON sidecar for a given image filename."""
            paths_to_try = []
            parenthesis = _re.findall(parenthesis_regexp, name)
            stem = Path(name).stem

            if len(parenthesis) == 1:
                # Fix for files that have as image/video IMG_1234(1).JPG with a json IMG_1234.JPG(1).json
                stripped_filename = _re.sub(parenthesis_regexp, '', name)
                base_with_paren = stripped_filename + parenthesis[0]
                paths_to_try.append(file.with_name(base_with_paren + '.json'))
                paths_to_try.append(file.with_name(base_with_paren + '.supplemental-metadata.json'))
            paths_to_try.append(file.with_name(name + '.json'))
            paths_to_try.append(file.with_name(name + '.supplemental-metadata.json'))
            if stem != name:
                paths_to_try.append(file.with_name(stem + '.json'))
                paths_to_try.append(file.with_name(stem + '.supplemental-metadata.json'))
            return paths_to_try

        def try_load_json(path: Path):
            if path.is_file():
                try:
                    with open(path, 'r', encoding="utf-8") as f:
                        return _json.load(f)
                except Exception:
                    pass
            return None

        # If file contains -edited, try original filename (strip -edited) first
        numbering_regexp = r'(\s*\(\d+\)|~\d+)'
        candidates = []
        if '-edited' in file.name:
            candidates.append(file.name.replace('-edited', ''))
        candidates.append(file.name)
        candidates.append(_re.sub(numbering_regexp, '', file.name))
        candidates.append(_re.sub(numbering_regexp, '', file.name.replace('-edited', '')))

        seen_paths = set()
        for candidate_name in candidates:
            for potential_json in json_paths_for_filename(candidate_name):
                if potential_json in seen_paths:
                    continue
                seen_paths.add(potential_json)
                json_dict = try_load_json(potential_json)
                if json_dict is not None:
                    return json_dict

        # Fuzzy: any JSON in folder that starts with image filename and ends with .json (Google truncates long names)
        for json_file in file.parent.iterdir():
            if json_file.suffix.lower() == '.json' and json_file.name.startswith(file.name):
                json_dict = try_load_json(json_file)
                if json_dict is not None:
                    return json_dict

        # Last resort: match on first 15 chars (e.g. date prefix 20241029) to get at least the right day
        if len(file.name) >= 15:
            prefix = file.name[:15]
            for json_file in file.parent.iterdir():
                if json_file.suffix.lower() == '.json' and json_file.name.startswith(prefix):
                    json_dict = try_load_json(json_file)
                    if json_dict is not None:
                        return json_dict

        nonlocal _all_jsons_dict
        # Check if we need to load this folder
        if file.parent not in _all_jsons_dict:
            for json_file in file.parent.rglob("*.json"):
                try:
                    with json_file.open('r', encoding="utf-8") as f:
                        json_dict = _json.load(f)
                        if "title" in json_dict:
                            # We found a JSON file with a proper title, store the file name
                            _all_jsons_dict[file.parent][json_dict["title"]] = json_dict
                except:
                    logger.debug(f"Couldn't open json file {json_file}")

        # Check if we have found the JSON file among all the loaded ones in the folder
        if file.parent in _all_jsons_dict and file.name in _all_jsons_dict[file.parent]:
            # Great we found a valid JSON file in this folder corresponding to this file
            return _all_jsons_dict[file.parent][file.name]
        else:
            nonlocal s_no_json_found
            s_no_json_found.append(str(file.resolve()))
            raise FileNotFoundError(f"Couldn't find json for file: {file}")

    # Returns date in 2019:01:01 23:59:59 format
    def get_date_from_folder_meta(dir: Path):
        file = find_album_meta_json_file(dir)
        if not file:
            logger.debug("Couldn't pull datetime from album meta")
            return None
        try:
            with open(str(file), 'r', encoding="utf-8") as fi:
                album_dict = _json.load(fi)
                # find_album_meta_json_file *should* give us "safe" file
                time = int(album_dict["albumData"]["date"]["timestamp"])
                return datetime_from_timestamp(time).strftime(EXIF_DATETIME_FORMAT)
        except KeyError:
            logger.error(
                "get_date_from_folder_meta - json doesn't have required stuff "
                "- that probably means that either google fucked us again, or find_album_meta_json_file"
                "is seriously broken"
            )

        return None

    @_functools.lru_cache(maxsize=None)
    def find_album_meta_json_file(dir: Path):
        for file in dir.rglob("*.json"):
            try:
                with open(str(file), 'r', encoding="utf-8") as f:
                    dict = _json.load(f)
                    if "albumData" in dict:
                        return file
            except Exception as e:
                logger.debug(e)
                logger.debug(f"find_album_meta_json_file - Error opening file: {file}")

        return None

    def set_creation_date_from_str(file: Path, str_datetime):
        try:
            # Turns out exif can have different formats - YYYY:MM:DD, YYYY/..., YYYY-... etc
            # God wish that americans won't have something like MM-DD-YYYY
            # The replace ': ' to ':0' fixes issues when it reads the string as 2006:11:09 10:54: 1.
            # It replaces the extra whitespace with a 0 for proper parsing
            str_datetime = str_datetime.replace('-', ':').replace('/', ':').replace('.', ':') \
                               .replace('\\', ':').replace(': ', ':0')[:19]
            timestamp = timestamp_from_datetime(
                _datetime.strptime(
                    str_datetime,
                    EXIF_DATETIME_FORMAT
                )
            )
            _os.utime(file, (timestamp, timestamp))
            if _os.name == 'nt':
                _windoza_setctime.setctime(str(file), timestamp)
        except Exception as e:
            raise ValueError(f"Error setting creation date from string: {str_datetime}")

    def set_creation_date_from_exif(file: Path):
        try:
            # Why do you need to be like that, Piexif...
            exif_dict = _piexif.load(str(file))
        except Exception as e:
            raise IOError("Can't read file's exif!")
        tags = [['0th', TAG_DATE_TIME], ['Exif', TAG_DATE_TIME_ORIGINAL], ['Exif', TAG_DATE_TIME_DIGITIZED]]
        datetime_str = ''
        date_set_success = False
        for tag in tags:
            try:
                datetime_str = exif_dict[tag[0]][tag[1]].decode('UTF-8')
                set_creation_date_from_str(file, datetime_str)
                date_set_success = True
                break
            except KeyError:
                pass  # No such tag - continue searching :/
            except ValueError:
                logger.debug("Wrong date format in exif!")
                logger.debug(datetime_str)
                logger.debug(f"does not match {EXIF_DATETIME_FORMAT}")
        if not date_set_success:
            raise IOError('No correct DateTime in given exif')

    def set_file_exif_date(file: Path, creation_date):
        try:
            exif_dict = _piexif.load(str(file))
        except:  # Sorry but Piexif is too unpredictable
            exif_dict = {'0th': {}, 'Exif': {}}

        creation_date = creation_date.encode('UTF-8')
        exif_dict['0th'][TAG_DATE_TIME] = creation_date
        exif_dict['Exif'][TAG_DATE_TIME_ORIGINAL] = creation_date
        exif_dict['Exif'][TAG_DATE_TIME_DIGITIZED] = creation_date

        try:
            _piexif.insert(_piexif.dump(exif_dict), str(file))
        except Exception as e:
            logger.debug("Couldn't insert exif!")
            logger.debug(e)
            nonlocal s_cant_insert_exif_files
            s_cant_insert_exif_files.append(str(file.resolve()))

    def get_date_str_from_json(json):
        return datetime_from_timestamp(
            int(json['photoTakenTime']['timestamp'])
        ).strftime(EXIF_DATETIME_FORMAT)

    # ========= THIS IS ALL GPS STUFF =========

    def change_to_rational(number):
        """convert a number to rational
        Keyword arguments: number
        return: tuple like (1, 2), (numerator, denominator)
        """
        f = Fraction(str(number))
        return f.numerator, f.denominator

    # got this here https://github.com/hMatoba/piexifjs/issues/1#issuecomment-260176317
    def degToDmsRational(degFloat):
        min_float = degFloat % 1 * 60
        sec_float = min_float % 1 * 60
        deg = math.floor(degFloat)
        deg_min = math.floor(min_float)
        sec = round(sec_float * 100)

        return [(deg, 1), (deg_min, 1), (sec, 100)]

    def set_file_geo_data(file: Path, json):
        """
        Reads the geoData from google and saves it to the EXIF. This works assuming that the geodata looks like -100.12093, 50.213143. Something like that.

        Written by DalenW.
        :param file:
        :param json:
        :return:
        """

        # prevents crashes
        try:
            exif_dict = _piexif.load(str(file))
        except:
            exif_dict = {'0th': {}, 'Exif': {}}

        # converts a string input into a float. If it fails, it returns 0.0
        def _str_to_float(num):
            if type(num) == str:
                return 0.0
            else:
                return float(num)

        # fallbacks to GeoData Exif if it wasn't set in the photos editor.
        # https://github.com/TheLastGimbus/GooglePhotosTakeoutHelper/pull/5#discussion_r531792314
        longitude = _str_to_float(json['geoData']['longitude'])
        latitude = _str_to_float(json['geoData']['latitude'])
        altitude = _str_to_float(json['geoData']['altitude'])

        # Prioritise geoData set from GPhotos editor. If it's blank, fall back to geoDataExif
        try:
            if longitude == 0 and latitude == 0:
                longitude = _str_to_float(json['geoDataExif']['longitude'])
                latitude = _str_to_float(json['geoDataExif']['latitude'])
                altitude = _str_to_float(json['geoDataExif']['altitude'])
        except KeyError:
            pass

        # latitude >= 0: North latitude -> "N"
        # latitude < 0: South latitude -> "S"
        # longitude >= 0: East longitude -> "E"
        # longitude < 0: West longitude -> "W"

        if longitude >= 0:
            longitude_ref = 'E'
        else:
            longitude_ref = 'W'
            longitude = longitude * -1

        if latitude >= 0:
            latitude_ref = 'N'
        else:
            latitude_ref = 'S'
            latitude = latitude * -1

        # referenced from https://gist.github.com/c060604/8a51f8999be12fc2be498e9ca56adc72
        gps_ifd = {
            _piexif.GPSIFD.GPSVersionID: (2, 0, 0, 0)
        }

        # skips it if it's empty
        if latitude != 0 or longitude != 0:
            gps_ifd.update({
                _piexif.GPSIFD.GPSLatitudeRef: latitude_ref,
                _piexif.GPSIFD.GPSLatitude: degToDmsRational(latitude),

                _piexif.GPSIFD.GPSLongitudeRef: longitude_ref,
                _piexif.GPSIFD.GPSLongitude: degToDmsRational(longitude)
            })

        if altitude != 0:
            gps_ifd.update({
                _piexif.GPSIFD.GPSAltitudeRef: 1,
                _piexif.GPSIFD.GPSAltitude: change_to_rational(round(altitude))
            })

        gps_exif = {"GPS": gps_ifd}
        exif_dict.update(gps_exif)

        try:
            _piexif.insert(_piexif.dump(exif_dict), str(file))
        except Exception as e:
            logger.debug("Couldn't insert geo exif!")
            # local variable 'new_value' referenced before assignment means that one of the GPS values is incorrect
            logger.debug(e)

    # ============ END OF GPS STUFF ============

    COMMON_DATETIME_PATTERNS = (
        # example: Screenshot_20190919-053857_Camera-edited.jpg
        (_re.compile(r'(?P<date>20\d{2}(01|02|03|04|05|06|07|08|09|10|11|12)[0-3]\d-\d{6})'),
            lambda m: _datetime.strptime(m.group('date'), '%Y%m%d-%H%M%S'),),
        # example: IMG_20190509_154733-edited.jpg, MVIMG_20190215_193501.MP4, IMG_20190221_112112042_BURST000_COVER_TOP.MP4
        (_re.compile(r'(?P<date>20\d{2}(01|02|03|04|05|06|07|08|09|10|11|12)[0-3]\d_\d{6})'),
            lambda m: _datetime.strptime(m.group('date'), '%Y%m%d_%H%M%S'),),
        # example: Screenshot_2019-04-16-11-19-37-232_com.google.a.jpg
        (_re.compile(r'(?P<date>20\d{2}-(01|02|03|04|05|06|07|08|09|10|11|12)-[0-3]\d-\d{2}-?\d{2}-?\d{2})'),
            lambda m: _datetime.strptime(m.group('date'), '%Y-%m-%d-%H-%M-%S'),),
    )

    def guess_date_from_filename(file: Path):
        for regex, extractor in COMMON_DATETIME_PATTERNS:
            m = regex.search(file.name)
            if m:
                return extractor(m).strftime(EXIF_DATETIME_FORMAT)

    # Fixes ALL metadata, takes just file and dir and figures it out
    def fix_metadata(file: Path):
        # logger.info(file)

        has_nice_date = False
        try:
            set_creation_date_from_exif(file)
            has_nice_date = True
        except (_piexif.InvalidImageDataError, ValueError, IOError) as e:
            logger.debug(e)
            logger.debug(f'No exif for {file}')
        except IOError:
            logger.debug('No creation date found in exif!')

        try:
            google_json = find_json_for_file(file)
            date = get_date_str_from_json(google_json)
            set_file_geo_data(file, google_json)
            set_file_exif_date(file, date)
            set_creation_date_from_str(file, date)
            has_nice_date = True
            return
        except FileNotFoundError as e:
            logger.debug(e)

        if has_nice_date:
            return True

        logger.debug(f'Try copying folder meta as date for {file}')
        date = get_date_from_folder_meta(file.parent)
        if date is not None:
            set_file_exif_date(file, date)
            set_creation_date_from_str(file, date)
            nonlocal s_date_from_folder_files
            s_date_from_folder_files.append(str(file.resolve()))
            return True

        if args.guess_timestamp_from_filename:
            logger.debug(f'Search the filename for common date/time patterns for {file}')
            date = guess_date_from_filename(file)
            if date is not None:
                set_file_exif_date(file, date)
                set_creation_date_from_str(file, date)
                return True

        logger.warning(f'There was literally no option to set date on {file}')
        nonlocal s_no_date_at_all
        s_no_date_at_all.append(str(file.resolve()))
        return False

    # PART 2: Copy all photos and videos to target folder

    # Makes a new name like 'photo(1).jpg'
    def new_name_if_exists(file: Path):
        new_name = file
        i = 1
        while True:
            if not new_name.is_file():
                return new_name
            else:
                new_name = file.with_name(f"{file.stem}({i}){file.suffix}")
                rename_map[str(file)] = new_name
                i += 1

    def copy_to_target(file: Path):
        if is_photo(file) or is_video(file):
            new_file = new_name_if_exists(FIXED_DIR / file.name)
            _shutil.copy2(file, new_file)
            if new_file.exists() and new_file.stat().st_size == file.stat().st_size and args.deletesourceimage:
                file.unlink()
                print(f"Reclaimed space: Deleted {file}")
            nonlocal s_copied_files
            s_copied_files += 1
        return True

    def copy_to_target_and_divide(file: Path):
        creation_date = file.stat().st_mtime
        date = datetime_from_timestamp(creation_date)

        new_path = FIXED_DIR / f"{date.year}/{date.month:02}/"
        new_path.mkdir(parents=True, exist_ok=True)

        new_file = new_name_if_exists(new_path / file.name)
        _shutil.copy2(file, new_file)
        if new_file.exists() and new_file.stat().st_size == file.stat().st_size and args.deletesourceimage:
            file.unlink()
            print(f"Reclaimed space: Deleted {file}")
        nonlocal s_copied_files
        s_copied_files += 1
        return True

    # xD python lambdas are shit - this is only because we can't do 2 commands, so we do them in arguments
    def _walk_with_tqdm(res, bar: _tqdm):
        bar.update()
        return res

    # ── PRESCAN MODE ──────────────────────────────────────────────────────────
    # Builds a global cross-folder JSON index and reports match rate before
    # any files are moved or modified. Safe to run at any time.
    if args.prescan:
        import sys as _sys
        from collections import defaultdict as _prescan_defaultdict
        from datetime import datetime as _prescan_datetime

        _prescan_PHOTO_FORMATS = set(photo_formats)
        _prescan_VIDEO_FORMATS = set(video_formats)
        _prescan_ALL_MEDIA     = _prescan_PHOTO_FORMATS | _prescan_VIDEO_FORMATS

        if PHOTOS_DIR is None:
            parser.error("--prescan requires -i/--input-folder to be set.")

        logger.info("=== PRESCAN MODE — no files will be moved or modified ===")
        logger.info(f"Input: {PHOTOS_DIR}")

        # Step 1: Build global JSON index across entire tree
        logger.info("Building global JSON index (all folders)...")
        _prescan_path_index  = {}          # Path -> json dict
        _prescan_title_index = _prescan_defaultdict(list)  # title -> [(Path, dict)]
        _prescan_json_count  = 0
        _prescan_json_errors = 0

        for _pj in PHOTOS_DIR.rglob("*.json"):
            try:
                with open(_pj, 'r', encoding='utf-8', errors='replace') as _f:
                    _pd = _json.load(_f)
                _prescan_path_index[_pj] = _pd
                if "title" in _pd:
                    _prescan_title_index[_pd["title"]].append((_pj, _pd))
                _prescan_json_count += 1
            except Exception:
                _prescan_json_errors += 1

        logger.info(f"JSON files indexed: {_prescan_json_count:,} ({_prescan_json_errors} unreadable)")

        # Step 2: Collect all media files
        logger.info("Collecting media files...")
        _prescan_media = [
            f for f in PHOTOS_DIR.rglob("*")
            if f.is_file() and f.suffix.lower() in _prescan_ALL_MEDIA
        ]
        _prescan_total      = len(_prescan_media)
        _prescan_total_size = sum(f.stat().st_size for f in _prescan_media if f.exists())

        # Step 3: Match each file using same logic as find_json_for_file
        # plus cross-folder title match (strategy 7)
        _prescan_same_exact  = []
        _prescan_same_fuzzy  = []
        _prescan_same_title  = []
        _prescan_cross_title = []
        _prescan_no_match    = []
        _prescan_by_type     = _prescan_defaultdict(int)
        _prescan_timestamps  = []

        logger.info(f"Matching {_prescan_total:,} media files to JSON sidecars...")

        for _pm in _prescan_media:
            _prescan_by_type[_pm.suffix.lower()] += 1
            _name = _pm.name
            _matched = None
            _mtype   = 'no_match'

            # Build candidate sidecar paths (mirrors find_json_for_file)
            def _pcands(cname):
                _pp = []
                _parens = _re.findall(r'\([0-9]+\)', cname)
                _stem   = Path(cname).stem
                if len(_parens) == 1:
                    _stripped = _re.sub(r'\([0-9]+\)', '', cname)
                    _bwp = _stripped + _parens[0]
                    _pp.append(_pm.parent / (_bwp + '.json'))
                    _pp.append(_pm.parent / (_bwp + '.supplemental-metadata.json'))
                _pp.append(_pm.parent / (cname + '.json'))
                _pp.append(_pm.parent / (cname + '.supplemental-metadata.json'))
                if _stem != cname:
                    _pp.append(_pm.parent / (_stem + '.json'))
                    _pp.append(_pm.parent / (_stem + '.supplemental-metadata.json'))
                return _pp

            _candidates = []
            if '-edited' in _name:
                _candidates.append(_name.replace('-edited', ''))
            _candidates.append(_name)
            _candidates.append(_re.sub(r'(\s*\(\d+\)|~\d+)', '', _name))
            _candidates.append(_re.sub(r'(\s*\(\d+\)|~\d+)', '', _name.replace('-edited', '')))

            # Strategies 1-3: exact/stem/parenthesis in same folder
            _seen_paths = set()
            for _cand in _candidates:
                for _jp in _pcands(_cand):
                    if _jp in _seen_paths:
                        continue
                    _seen_paths.add(_jp)
                    if _jp in _prescan_path_index:
                        _matched = _prescan_path_index[_jp]
                        _mtype   = 'same_exact'
                        break
                if _matched:
                    break

            # Strategy 4-5: fuzzy/prefix in same folder
            if not _matched:
                for _jp, _jd in _prescan_path_index.items():
                    if _jp.parent == _pm.parent and _jp.suffix.lower() == '.json':
                        if _jp.name.startswith(_name) or (len(_name) >= 15 and _jp.name.startswith(_name[:15])):
                            _matched = _jd
                            _mtype   = 'same_fuzzy'
                            break

            # Strategy 6: title match same folder
            if not _matched and _name in _prescan_title_index:
                for _jp, _jd in _prescan_title_index[_name]:
                    if _jp.parent == _pm.parent:
                        _matched = _jd
                        _mtype   = 'same_title'
                        break

            # Strategy 7 (NEW): cross-folder title match
            if not _matched and _name in _prescan_title_index:
                _matched = _prescan_title_index[_name][0][1]
                _mtype   = 'cross_title'

            if _mtype == 'same_exact':
                _prescan_same_exact.append(_pm)
            elif _mtype == 'same_fuzzy':
                _prescan_same_fuzzy.append(_pm)
            elif _mtype == 'same_title':
                _prescan_same_title.append(_pm)
            elif _mtype == 'cross_title':
                _prescan_cross_title.append(_pm)
            else:
                _prescan_no_match.append(_pm)

            if _matched and 'photoTakenTime' in _matched:
                try:
                    _prescan_timestamps.append(int(_matched['photoTakenTime']['timestamp']))
                except Exception:
                    pass

        # Step 4: Report
        _ps_matched_same  = len(_prescan_same_exact) + len(_prescan_same_fuzzy) + len(_prescan_same_title)
        _ps_matched_cross = len(_prescan_cross_title)
        _ps_total_matched = _ps_matched_same + _ps_matched_cross
        _ps_no_match      = len(_prescan_no_match)
        _ps_match_pct     = (_ps_total_matched / _prescan_total * 100) if _prescan_total else 0
        _ps_cross_pct     = (_ps_matched_cross / _prescan_total * 100) if _prescan_total else 0
        _ps_size_gb       = _prescan_total_size / (1024 ** 3)

        _ps_date_range = ''
        if _prescan_timestamps:
            _ps_earliest = _prescan_datetime.utcfromtimestamp(min(_prescan_timestamps)).strftime('%Y-%m-%d')
            _ps_latest   = _prescan_datetime.utcfromtimestamp(max(_prescan_timestamps)).strftime('%Y-%m-%d')
            _ps_date_range = f"{_ps_earliest}  →  {_ps_latest}"

        logger.info("=" * 56)
        logger.info("  GPTH PRE-SCAN REPORT")
        logger.info("=" * 56)
        logger.info(f"  Total media files:           {_prescan_total:>10,}")
        logger.info(f"  Total size:                  {_ps_size_gb:>9.1f} GB")
        if _ps_date_range:
            logger.info(f"  Date range (from JSON):      {_ps_date_range}")
        logger.info(f"")
        logger.info(f"  JSON SIDECAR MATCH RESULTS:")
        logger.info(f"  {'Matched (same folder):':<34} {_ps_matched_same:>8,}  ({_ps_matched_same/_prescan_total*100:.1f}%)")
        logger.info(f"  {'Matched (cross-folder):':<34} {_ps_matched_cross:>8,}  ({_ps_cross_pct:.1f}%)")
        logger.info(f"  {'Total matched:':<34} {_ps_total_matched:>8,}  ({_ps_match_pct:.1f}%)")
        logger.info(f"  {'No JSON found:':<34} {_ps_no_match:>8,}  ({_ps_no_match/_prescan_total*100:.1f}%)")
        logger.info(f"")
        logger.info(f"  MATCH BREAKDOWN:")
        logger.info(f"    Exact/stem match:            {len(_prescan_same_exact):>8,}")
        logger.info(f"    Fuzzy/prefix match:          {len(_prescan_same_fuzzy):>8,}")
        logger.info(f"    Title match (same folder):   {len(_prescan_same_title):>8,}")
        logger.info(f"    Title match (cross-folder):  {len(_prescan_cross_title):>8,}  <- gpth would miss these")
        logger.info(f"")
        logger.info(f"  FILE TYPES:")
        for _ext, _cnt in sorted(_prescan_by_type.items(), key=lambda x: -x[1]):
            logger.info(f"    {_ext:<12} {_cnt:>8,}")
        if _ps_no_match > 0:
            logger.info(f"")
            logger.info(f"  WARNING: {_ps_no_match:,} files have no JSON sidecar.")
            logger.info(f"  These will use --guess-timestamp-from-filename only.")
            logger.info(f"  They will NOT have GPS coordinates in the archive.")
        if _ps_matched_cross > 0:
            logger.info(f"")
            logger.info(f"  INFO: {_ps_matched_cross:,} sidecars found in different folders.")
            logger.info(f"  Base gpth 2.3.0 would miss these. This build finds them.")
        logger.info("=" * 56)

        # Step 5: Optional --limit staging copy
        if args.limit:
            _staging_dir = PHOTOS_DIR.parent / 'gpth_test_batch'
            _staging_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"Copying test batch of {args.limit} files to {_staging_dir} ...")

            _batch = (_prescan_same_exact + _prescan_same_fuzzy +
                      _prescan_same_title + _prescan_cross_title +
                      _prescan_no_match)[:args.limit]

            _copied_media = 0
            _copied_json  = 0
            _cross_copied = 0

            for _bfile in _batch:
                try:
                    _brel  = _bfile.relative_to(PHOTOS_DIR)
                except ValueError:
                    _brel  = Path(_bfile.name)
                _bdest = _staging_dir / _brel
                _bdest.parent.mkdir(parents=True, exist_ok=True)
                _shutil.copy2(_bfile, _bdest)
                _copied_media += 1

                # Find and copy sidecar — rebuild match for this file
                _bname = _bfile.name
                _bjson_path = None
                _bjson_cross = False

                # Quick lookup: same-folder exact
                for _cand in [_bname, Path(_bname).stem]:
                    for _jsuf in ['.json', '.supplemental-metadata.json']:
                        _try = _bfile.parent / (_cand + _jsuf)
                        if _try in _prescan_path_index:
                            _bjson_path = _try
                            break
                    if _bjson_path:
                        break

                # Cross-folder fallback
                if not _bjson_path and _bname in _prescan_title_index:
                    _bjson_path = _prescan_title_index[_bname][0][0]
                    _bjson_cross = True

                if _bjson_path:
                    if _bjson_cross:
                        # Place alongside photo in staging so gpth can find it
                        _jdest = _bdest.parent / _bjson_path.name
                        _cross_copied += 1
                    else:
                        try:
                            _jrel  = _bjson_path.relative_to(PHOTOS_DIR)
                        except ValueError:
                            _jrel  = Path(_bjson_path.name)
                        _jdest = _staging_dir / _jrel
                    _jdest.parent.mkdir(parents=True, exist_ok=True)
                    _shutil.copy2(_bjson_path, _jdest)
                    _copied_json += 1

            logger.info(f"  Media files copied:        {_copied_media:,}")
            logger.info(f"  JSON sidecars copied:      {_copied_json:,}")
            logger.info(f"    of which cross-folder:   {_cross_copied:,} (placed alongside photo)")
            logger.info(f"  Staging folder: {_staging_dir}")
            logger.info(f"")
            logger.info(f"  Now run gpth against the test batch:")
            logger.info(f'  python -m google_photos_takeout_helper -i "{_staging_dir}" -o "P:\\Test" --divide-to-dates --guess-timestamp-from-filename --albums shortcut')

        logger.info("Prescan complete. No files were moved or modified.")
        logger.info("Remove --prescan flag to run the full archive operation.")
        return  # Exit before any destructive operations

    # ── END PRESCAN MODE ──────────────────────────────────────────────────────

    # ── LIMIT MODE: cap number of files processed for test runs ──────────────
    # When --limit N is set (without --prescan), gpth processes only N files
    # then exits cleanly. No staging copy needed — just run normally with a
    # small output folder like -o "P:\Test" to inspect results.
    _limit_counter = [0]  # mutable so lambdas can increment it

    def _limit_filter(f):
        """Returns True only while we are under the --limit cap."""
        if args.limit is None:
            return is_photo(f) or is_video(f)
        if _limit_counter[0] >= args.limit:
            return False
        if is_photo(f) or is_video(f):
            _limit_counter[0] += 1
            return True
        return False

    if args.limit:
        logger.info(f"--limit {args.limit}: will process first {args.limit} files then stop.")

    # ── GOOGLE TAKEOUT PROCESSING (only if -i was provided) ──────────────────
    if PHOTOS_DIR is not None:
        # Count *all* photo and video files - this is hacky, and we should use .rglob altogether instead of is_photo
        logger.info("Counting how many input files we have ahead...")
        _input_files_count = args.limit if args.limit else 0
        if not args.limit:
            for ext in _tqdm(photo_formats + video_formats, unit='formats'):
                _input_files_count += len(list(PHOTOS_DIR.rglob(f'**/*{ext}')))
        logger.info(f'Input files: {_input_files_count}')

        logger.info('=====================')
        logger.info('Fixing files metadata and creation dates...')
        _metadata_bar = _tqdm(total=_input_files_count, unit='files')

        for_all_files_recursive(
            dir=PHOTOS_DIR,
            file_function=lambda f: _walk_with_tqdm(fix_metadata(f), _metadata_bar),
            filter_fun=_limit_filter
        )
        _metadata_bar.close()
        logger.info('=====================')

        # Reset counter so copy phase processes the same files
        _limit_counter[0] = 0

        logger.info('=====================')
        _copy_bar = _tqdm(total=_input_files_count, unit='files')
        if args.divide_to_dates:
            logger.info('Creating subfolders and dividing files based on date...')
            for_all_files_recursive(
                dir=PHOTOS_DIR,
                file_function=lambda f: _walk_with_tqdm(copy_to_target_and_divide(f), _copy_bar),
                filter_fun=_limit_filter
            )
        else:
            logger.info('Copying all files to one folder...')
            logger.info('(If you want, you can get them organized in folders based on year and month.'
                        ' Run with --divide-to-dates to do this)')
            for_all_files_recursive(
                dir=PHOTOS_DIR,
                file_function=lambda f: _walk_with_tqdm(copy_to_target(f), _copy_bar),
                filter_fun=_limit_filter
            )
        _copy_bar.close()

        if args.limit:
            logger.info(f"--limit reached: processed {_limit_counter[0]} files. Skipping duplicate removal.")
            logger.info(f"Inspect output in: {FIXED_DIR}")
            logger.info(f"When ready for the full run, remove --limit from your command.")
            return  # Skip dedup and album steps for test runs
    # ── LOCAL PHOTO MERGE (--merge-local) ────────────────────────────────────
    # Processes a non-Google photo folder into the same output archive.
    # No JSON sidecars expected — uses EXIF, filename patterns, and file
    # modified date as fallbacks. Deduplicates against the existing archive
    # using SHA-1 hashing so Google-verified files are never overwritten.
    if args.merge_local:
        from datetime import datetime as _ml_datetime

        LOCAL_DIR = Path(args.merge_local)
        if not LOCAL_DIR.is_dir():
            logger.error(f"--merge-local folder not found: {LOCAL_DIR}")
        else:
            logger.info('=====================')
            logger.info(f'LOCAL MERGE: {LOCAL_DIR}')
            logger.info(f'Structure mode: {args.local_structure}')
            logger.info('=====================')

            # Statistics for local merge
            s_local_copied       = 0
            s_local_duplicates   = 0
            s_local_date_exif    = 0
            s_local_date_fname   = 0
            s_local_date_mtime   = 0
            s_local_no_date      = []

            # Build hash index of files already in output (dedup against Google archive)
            logger.info('Building hash index of existing archive (for dedup)...')
            _existing_hashes = set()
            if FIXED_DIR.exists():
                for _ef in _tqdm(list(FIXED_DIR.rglob('*')), unit='existing-files'):
                    if _ef.is_file() and _ef.suffix.lower() in ALL_MEDIA_FORMATS:
                        try:
                            _existing_hashes.add(get_hash(_ef, first_chunk_only=False))
                        except Exception:
                            pass
            logger.info(f'Existing archive files indexed for dedup: {len(_existing_hashes):,}')

            def _get_date_from_exif(file: Path):
                """
                Read DateTimeOriginal or DateTime from EXIF.
                Returns datetime object or None.
                """
                try:
                    exif_dict = _piexif.load(str(file))
                    for tag in [['Exif', TAG_DATE_TIME_ORIGINAL],
                                ['Exif', TAG_DATE_TIME_DIGITIZED],
                                ['0th', TAG_DATE_TIME]]:
                        try:
                            raw = exif_dict[tag[0]][tag[1]].decode('UTF-8')
                            raw = raw.replace('-', ':').replace('/', ':')[:19]
                            return _datetime.strptime(raw, EXIF_DATETIME_FORMAT)
                        except (KeyError, ValueError, AttributeError):
                            pass
                except Exception:
                    pass
                return None

            def _get_date_from_filename(file: Path):
                """
                Try to extract a date from the filename using gpth's existing
                COMMON_DATETIME_PATTERNS. Returns datetime or None.
                """
                for regex, extractor in COMMON_DATETIME_PATTERNS:
                    m = regex.search(file.name)
                    if m:
                        try:
                            return extractor(m)
                        except Exception:
                            pass
                return None

            def _copy_local_file(file: Path):
                """
                Resolve date for a local (non-Google) photo/video file and
                copy it to the output folder using the chosen --local-structure.
                Skips exact duplicates already in the archive.
                """
                nonlocal s_local_copied, s_local_duplicates
                nonlocal s_local_date_exif, s_local_date_fname, s_local_date_mtime

                # Dedup check
                try:
                    file_hash = get_hash(file, first_chunk_only=False)
                    if file_hash in _existing_hashes:
                        s_local_duplicates += 1
                        logger.debug(f'Local duplicate skipped: {file.name}')
                        return
                except Exception:
                    pass

                # --- Date resolution ---
                date = None
                date_source = None

                # 1. EXIF DateTimeOriginal / DateTime
                exif_date = _get_date_from_exif(file)
                if exif_date:
                    date = exif_date
                    date_source = 'exif'
                    s_local_date_exif += 1

                # 2. Filename pattern
                if date is None and args.guess_timestamp_from_filename:
                    fname_date = _get_date_from_filename(file)
                    if fname_date:
                        date = fname_date
                        date_source = 'filename'
                        s_local_date_fname += 1

                # 3. File modified time (last resort)
                if date is None:
                    try:
                        mtime = file.stat().st_mtime
                        date = datetime_from_timestamp(mtime)
                        date_source = 'mtime'
                        s_local_date_mtime += 1
                    except Exception:
                        pass

                # --- Output path resolution ---
                if args.local_structure == 'dates':
                    # YYYY/MM structure, same as Google archive
                    if date:
                        dest_dir = FIXED_DIR / f"{date.year}" / f"{date.month:02d}"
                    else:
                        dest_dir = FIXED_DIR / "0000" / "00"
                        s_local_no_date.append(str(file.resolve()))

                else:  # preserve
                    # Keep original folder name, nested under year
                    # e.g. /Local/Scotland Trip/img.jpg → 2019/Scotland Trip/img.jpg
                    original_folder = file.parent.name or "Photos"
                    if date:
                        dest_dir = FIXED_DIR / f"{date.year}" / original_folder
                    else:
                        # Try to get year from parent folder name
                        year_match = _re.search(r'(19|20)\d{2}', original_folder)
                        if year_match:
                            dest_dir = FIXED_DIR / year_match.group(0) / original_folder
                        else:
                            dest_dir = FIXED_DIR / "0000" / "Unknown" / original_folder
                            s_local_no_date.append(str(file.resolve()))

                dest_dir.mkdir(parents=True, exist_ok=True)
                dest_file = new_name_if_exists(dest_dir / file.name)

                try:
                    _shutil.copy2(file, dest_file)
                    # Set file timestamps from resolved date
                    if date:
                        date_str = date.strftime(EXIF_DATETIME_FORMAT)
                        try:
                            set_creation_date_from_str(dest_file, date_str)
                        except Exception:
                            pass
                    # Register hash so subsequent files don't duplicate this one
                    try:
                        _existing_hashes.add(get_hash(dest_file, first_chunk_only=False))
                    except Exception:
                        pass
                    # Delete source if requested
                    if args.deletesourceimage:
                        if dest_file.exists() and dest_file.stat().st_size == file.stat().st_size:
                            file.unlink()
                    s_local_copied += 1
                    logger.debug(f'[{date_source}] {file.name} → {dest_file}')
                except Exception as e:
                    logger.warning(f'Failed to copy {file}: {e}')

            # Walk and process all media files in the local folder
            ALL_MEDIA_FORMATS = set(photo_formats) | set(video_formats)
            _local_files = [
                f for f in LOCAL_DIR.rglob('*')
                if f.is_file() and f.suffix.lower() in ALL_MEDIA_FORMATS
            ]
            logger.info(f'Local media files found: {len(_local_files):,}')

            _local_bar = _tqdm(total=len(_local_files), unit='local-files')
            for _lf in _local_files:
                _copy_local_file(_lf)
                _local_bar.update()
            _local_bar.close()

            # Local merge summary
            logger.info('=====================')
            logger.info('LOCAL MERGE COMPLETE')
            logger.info(f'  Files copied:              {s_local_copied:,}')
            logger.info(f'  Duplicates skipped:        {s_local_duplicates:,}')
            logger.info(f'  Date from EXIF:            {s_local_date_exif:,}')
            logger.info(f'  Date from filename:        {s_local_date_fname:,}')
            logger.info(f'  Date from file mtime:      {s_local_date_mtime:,}')
            logger.info(f'  No date found (→ 0000/):   {len(s_local_no_date):,}')
            if s_local_no_date:
                _no_date_log = LOCAL_DIR / 'local_no_date.txt'
                with open(_no_date_log, 'w', encoding='utf-8') as _f:
                    _f.write("# Local files where no date could be determined\n")
                    _f.write("# These were copied to 0000/00/ or 0000/Unknown/ — review manually\n")
                    _f.write("\n".join(s_local_no_date))
                logger.info(f'  No-date file list:         {_no_date_log}')
            logger.info('=====================')

    # ── END LOCAL MERGE ───────────────────────────────────────────────────────

    logger.info('=====================')
    logger.info('=====================')
    logger.info('Finding duplicates...')
    find_duplicates(FIXED_DIR, lambda f: (is_photo(f) or is_video(f)))
    logger.info('Removing duplicates...')
    remove_duplicates()
    logger.info('=====================')
    if args.albums is not None:
        if args.albums.lower() == 'json':
            logger.info('=====================')
            logger.info('Populate json file with albums...')
            logger.info('=====================')
            for_all_files_recursive(
                dir=PHOTOS_DIR,
                folder_function=populate_album_map
            )
            file = PHOTOS_DIR / 'albums.json'
            with open(file, 'w', encoding="utf-8") as outfile:
                _json.dump(album_mmap, outfile)
            logger.info(str(file))

    logger.info('')
    logger.info('DONE! FREEEEEDOOOOM!!!')
    logger.info('')
    logger.info("Final statistics:")
    logger.info(f"Files copied to target folder: {s_copied_files}")
    logger.info(f"Removed duplicates: {s_removed_duplicates_count}")
    logger.info(f"Files for which we couldn't find json: {len(s_no_json_found)}")
    if len(s_no_json_found) > 0:
        with open(PHOTOS_DIR / 'no_json_found.txt', 'w', encoding="utf-8") as f:
            f.write("# This file contains list of files for which there was no corresponding .json file found\n")
            f.write("# You might find it useful, but you can safely delete this :)\n")
            f.write("\n".join(s_no_json_found))
            logger.info(f" - you have full list in {f.name}")
    logger.info(f"Files where inserting new exif failed: {len(s_cant_insert_exif_files)}")
    if len(s_cant_insert_exif_files) > 0:
        logger.info("(This is not necessary bad thing - pretty much all videos fail, "
                    "and your photos probably have their original exif already")
        with open(PHOTOS_DIR / 'failed_inserting_exif.txt', 'w', encoding="utf-8") as f:
            f.write("# This file contains list of files where setting right exif date failed\n")
            f.write("# You might find it useful, but you can safely delete this :)\n")
            f.write("\n".join(s_cant_insert_exif_files))
            logger.info(f" - you have full list in {f.name}")
    logger.info(f"Files where date was set from name of the folder: {len(s_date_from_folder_files)}")
    if len(s_date_from_folder_files) > 0:
        with open(PHOTOS_DIR / 'date_from_folder_name.txt', 'w', encoding="utf-8") as f:
            f.write("# This file contains list of files where date was set from name of the folder\n")
            f.write("# You might find it useful, but you can safely delete this :)\n")
            f.write("\n".join(s_date_from_folder_files))
            logger.info(f" - you have full list in {f.name}")
    if args.skip_extras or args.skip_extras_harder:
        # Remove duplicates: https://www.w3schools.com/python/python_howto_remove_duplicates.asp
        s_skipped_extra_files = list(dict.fromkeys(s_skipped_extra_files))
        logger.info(f"Extra files that were skipped: {len(s_skipped_extra_files)}")
        with open(PHOTOS_DIR / 'skipped_extra_files.txt', 'w', encoding="utf-8") as f:
            f.write("# This file contains list of extra files (ending with '-edited' etc) which were skipped because "
                    "you've used either --skip-extras or --skip-extras-harder\n")
            f.write("# You might find it useful, but you can safely delete this :)\n")
            f.write("\n".join(s_skipped_extra_files))
            logger.info(f" - you have full list in {f.name}")
    if len(s_no_date_at_all) > 0:
        logger.info('')
        logger.info(f"!!! There were {len(s_no_date_at_all)} files where there was absolutely no way to set "
                    f"a correct date! They will probably appear at the top of the others, as their 'last modified' "
                    f"value is set to moment of downloading your takeout :/")
        with open(PHOTOS_DIR / 'unsorted.txt', 'w', encoding="utf-8") as f:
            f.write("# This file contains list of files where there was no way to set correct date!\n")
            f.write("# You probably want to set their dates manually - but you can delete this if you want\n")
            f.write("\n".join(s_no_date_at_all))
            logger.info(f" - you have full list in {f.name}")

    logger.info('')
    logger.info('Sooo... what now? You can see README.md for what nice G Photos alternatives I found and recommend')
    logger.info('')
    logger.info('If I helped you, you can consider donating me: https://www.paypal.me/TheLastGimbus')
    logger.info('Have a nice day!')


if __name__ == '__main__':
    main()

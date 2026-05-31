# Google Photos Takeout Helper — Enhanced Fork

> **Fork of [TheLastGimbus/GooglePhotosTakeoutHelper](https://github.com/TheLastGimbus/GooglePhotosTakeoutHelper)** with significant new capabilities for large-scale photo archive management.

---

## Why This Tool Is Needed

Google Takeout exports photos and videos alongside JSON sidecar files that contain metadata (dates, GPS, etc.). Two challenges make matching difficult:

1. **Scattered JSON** — In large exports, JSON sidecars often end up in different folders (or different zip archives) than their corresponding media files. Tools that only look in the same folder as each photo miss many matches.

2. **Names don't match** — JSON filenames don't always match media filenames. Variants like `-edited`, `(1)`, or different extensions require multiple matching strategies beyond simple exact-name lookup.

This fork adds cross-folder matching and several strategies to handle name mismatches, improving match rates on large Takeout exports. On a 143K-file export, GPTH PRO increased the match rate from **76%** (original gpth) to **100%** (143,510 matched).

---

## What's New in v3.0.0

This fork adds several major enhancements on top of the excellent v2.3.0 base:

| Feature | Flag | Purpose |
|---|---|---|
| Cross-folder JSON matching | *(automatic)* | Handles scattered sidecars |
| Safe prescan & match report | `--prescan` | Non-destructive match rate analysis |
| Test-batch limiter | `--limit N` | Cap any live run at N files |
| Local (non-Google) photo merge | `--merge-local FOLDER` | Integrate phone/camera photos with SHA-1 dedup |
| Browsable album shortcuts | `--albums shortcut` | `OUTPUT/Albums/<name>/` with .lnk/symlinks to photos |
| Local folder structure | `--local-structure` | `dates` (YYYY/MM) or `preserve` (original folders) |

### 1. Cross-Folder JSON Sidecar Matching (automatic)

In large Takeout exports, JSON sidecars often end up in different folders than their media files. The original gpth only searched `file.parent` for sidecars (Strategies 1–6). This often left thousands of photos unmatched.

**v3.0.0 fix:** When you pass `--prescan` or when sidecar strategies 1–6 all fail, gpth now builds a global title index across the *entire* input tree and attempts a cross-folder match (Strategy 7). This dramatically improves match rates on large Takeout exports.

### 2. `--prescan` — Safe Read-Only Match Rate Report

Run this **before** any live operation to understand how well your Takeout export will match. It scans the entire input folder, builds a global JSON index, attempts all 7 matching strategies for every photo/video, and prints a detailed report — **without copying or moving any files**.

```
gpth --prescan -i "D:\Takeout"
```

Example output:
```
=== PRESCAN REPORT ===
Input folder: D:\Takeout
Photos/videos found:  45,231
JSON sidecars found:   44,876
  Strategy 1 (same-dir exact):       38,441  (85.0%)
  Strategy 2 (same-dir +edited):        892  ( 2.0%)
  Strategy 3 (same-dir truncated):    3,201  ( 7.1%)
  Strategy 4-6 (name variants):         890  ( 2.0%)
  Strategy 7 (cross-folder title):    1,180  ( 2.6%)
  Unmatched (no sidecar):               627  ( 1.4%)
Match rate: 98.6%  —  unmatched will use filename/EXIF date
```

### 3. `--limit N` — Test Batch Limiter

Process only the first N photos/videos and exit before the deduplication phase. Perfect for testing your output folder and date-guessing settings without waiting hours.

```
gpth -i "D:\Takeout" -o "P:\Test" --limit 200
```

Combine with `--prescan` first to understand your data, then `--limit 200` for a small live test, then the full run.

### 4. `--merge-local` — Integrate Non-Google Photos

Merge photos from any local folder (phone backups, camera SD cards, old hard drives) into an existing archive. Since these files have no Google JSON sidecars, dates are resolved from:

1. EXIF `DateTimeOriginal` / `DateTime`
2. Filename patterns (`IMG_20230415_...`, `2023-04-15_...`, etc.)
3. File modified time (last resort)

SHA-1 deduplication is performed against the existing archive — already-archived files are skipped automatically.

```
gpth --merge-local "E:\OldPhone\DCIM" -o "P:\Photos"
```

You can run this without touching your Google Takeout archive at all (omit `-i`).

### 5. `--albums shortcut` — Browsable Album Folders

Creates `OUTPUT/Albums/<album_name>/` with shortcuts (`.lnk` on Windows, symlinks on Unix) pointing to the actual photos in the date-organized archive. **Browse albums directly in Explorer** — no custom viewer needed. Duplicates in an album become multiple shortcuts to the same file (correct ordering).

```
gpth -i "D:\Takeout" -o "P:\Photos" --albums shortcut
```

**Fix an existing extract:** If you already copied without `--albums shortcut`, run `--albums-only` to add album shortcuts after the fact. **Requires the source (INPUT) to still exist** — if you used `--deletesourceimage` and removed the Takeout folder, album structure cannot be recovered.

```
gpth -i "M:\googlephotos" -o "P:\" --albums-only
```

### 6. `--local-structure` — Output Folder Layout for Local Files

Controls how merged local files are organised in the output folder:

- **`dates`** (default): `YYYY/MM/filename.jpg` — matches the Google archive layout
- **`preserve`**: `YYYY/OriginalFolderName/filename.jpg` — keeps the original folder name nested under the year

```
gpth --merge-local "E:\SD_Card" -o "P:\Photos" --local-structure preserve
```

---

## Recommended Workflow for Large Archives

```
# Step 1: Safe prescan — understand your match rate, no files touched
gpth --prescan -i "D:\Takeout"

# Step 2: Small live test — 200 files to a test folder
gpth -i "D:\Takeout" -o "P:\Test" --limit 200

# Step 3: Full Google Takeout run (--albums shortcut = browsable Albums/<name>/ folders)
gpth -i "D:\Takeout" -o "P:\Photos" --deletesourceimage --albums shortcut

# Step 4: Merge local (non-Google) photos
gpth --merge-local "E:\OldPhone" -o "P:\Photos" --local-structure dates

# Step 5: Merge more local sources (dedup prevents re-copying)
gpth --merge-local "E:\Camera_SD" -o "P:\Photos" --local-structure preserve
```

---

## Installation

### From PyPI (recommended)

```
pip install google-photos-takeout-helper-enhanced
```

### From source

```
git clone https://github.com/YOUR_USERNAME/GooglePhotosTakeoutHelper.git
cd GooglePhotosTakeoutHelper
pip install -e .
```

---

## All Flags (v3.0.0)

```
usage: gpth [-h] [-i INPUT_FOLDER] [-o OUTPUT_FOLDER]
            [--merge-local LOCAL_FOLDER] [--local-structure {dates,preserve}]
            [--prescan] [--limit N]
            [--divide-to-dates] [--albums {shortcut,duplicate-copy,json,reverse-shortcut,nothing}]
            [--guess-timestamp-from-filename] [--dont-move] [--deletesourceimage]
            [--skip-extras] [--skip-extras-harder] [--google-translate-titles]

  -i INPUT_FOLDER       Google Takeout folder (optional if using --merge-local only)
  -o OUTPUT_FOLDER      Output folder for organised photos
  --merge-local FOLDER  Folder of non-Google photos to merge into output
  --local-structure     'dates' (YYYY/MM) or 'preserve' (YYYY/orig-folder) [default: dates]
  --prescan             Safe read-only match rate report — no files copied
  --limit N             Stop after N photos (for testing, exits before dedup)
  --divide-to-dates     Divide output into YYYY/MM subfolders (Google photos)
  --albums MODE         How to handle Google Albums
  --deletesourceimage   Delete source files after successful copy
  --skip-extras         Skip 'edited', '-effects' variants
  --skip-extras-harder  More aggressive extras skipping
  --guess-timestamp-from-filename  Try to extract date from filename
  --dont-move           Copy files (don't move) from input
```

---

## `gpth_nas` — SQLite-Backed Variant for Low-Memory Hosts

`gpth_nas/` is a parallel orchestrator built for hosts where the in-memory
JSON index won't fit — e.g. a Synology DS220j with 512 MB of RAM and a
250K-file Takeout. The standard `gpth` holds its sidecar index in Python
dicts; `gpth_nas` persists JSON metadata to a SQLite file on disk and
queries it incrementally. Same matching strategies, no OOM.

### When to use it

- Host RAM is < 1 GB and your Takeout has > 100K files.
- You want crash-resume: the JSON ingest and media walk are both
  idempotent and indexed in their own SQLite databases.
- You need fast matcher iteration: change a strategy, re-run match
  against the existing indexes without re-walking the filesystem.

### Quick start

```
# 1. Extract zips (idempotent, deletes each zip after success)
python -m gpth_nas extract --zips /path/to/zips --staging /photo/staging

# 2. Prescan: build JSON + media indexes, attempt all strategies, report
python -m gpth_nas prescan \
    --src /photo/staging \
    --db  /photo/gpth/index.db

# 3. Live run: copy to /photo/YYYY/MM/, EXIF fixes, optional --albums
python -m gpth_nas run \
    --src /photo/staging \
    --dst /photo \
    --db  /photo/gpth/index.db \
    --albums --delete-source --skip-existing
```

### Matcher strategies (in confidence order)

| Strategy | Catches |
|---|---|
| `same_exact` | Sidecar in same folder, exact basename match |
| `same_paren` | `IMG(1).jpg ↔ IMG.jpg(1).json` reorder + sidecar-side collision form `IMG.jpg.supplemental-metadata(1).json` |
| `same_edited` | `-edited`/`-EFFECTS`/etc. variant — composes with paren stripping so `foo-edited(1).jpg` finds the right `(1)` sidecar |
| `same_truncated` | Google's hard 51-char filename cap on long sidecar names |
| `same_sibling_ext` | Live Photo MP4 borrowing the HEIC sibling's sidecar |
| `cross_*` | Same strategies extended across folders for scattered exports |
| `cleanup_seg_unique` | Final pass: globally unique first-segment match for any straggler |

Real corpus result (143,975 media + 107,199 JSONs from a 17-zip Takeout):
**100.00% sidecar match**, 0 unmatched — driven on a 512 MB Synology box.

### Indexes

- `index.db` — JSON sidecars: `path`, `basename`, `parent`, `stem`,
  `title`, `taken_ts`, GPS columns, raw JSON blob. Indexed on basename
  and parent.
- `index.db.media.db` — media files: `path`, `basename`, `parent`,
  `stem`, `ext`, `first_seg`, optional size/mtime. Allows separate
  re-scans of media vs JSON when you change extension config or
  matcher logic.

The matcher operates entirely against these indexes — no filesystem
walks during the matching or cleanup phases. The walks happen once,
at ingest, and survive resume.

---

## Credits

**Thank you to [TheLastGimbus](https://github.com/TheLastGimbus)** for the original [Google Photos Takeout Helper](https://github.com/TheLastGimbus/GooglePhotosTakeoutHelper) — the core logic, deduplication, album handling, and EXIF workflow that made this fork possible.

GPTH PRO extends it with cross-folder JSON matching, `--prescan`, `--limit`, `--merge-local`, `--local-structure`, and other enhancements.

The original project is licensed under Apache 2.0. This fork extends it under the same license.

---

## Original README

See [ORIGINAL_README.md](ORIGINAL_README.md) for the original project documentation.

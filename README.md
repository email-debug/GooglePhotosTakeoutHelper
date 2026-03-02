# Google Photos Takeout Helper — Enhanced Fork

> **Fork of [TheLastGimbus/GooglePhotosTakeoutHelper](https://github.com/TheLastGimbus/GooglePhotosTakeoutHelper)** with significant new capabilities for large-scale photo archive management.

---

## What's New in v3.0.0

This fork adds four major enhancements on top of the excellent v2.3.0 base:

| Feature | Flag | Purpose |
|---|---|---|
| Cross-folder JSON matching | *(automatic)* | Fixes Google's scattered sidecar bug |
| Safe prescan & match report | `--prescan` | Non-destructive match rate analysis |
| Test-batch limiter | `--limit N` | Cap any live run at N files |
| Local (non-Google) photo merge | `--merge-local FOLDER` | Integrate phone/camera photos with SHA-1 dedup |
| Local folder structure | `--local-structure` | `dates` (YYYY/MM) or `preserve` (original folders) |

### 1. Cross-Folder JSON Sidecar Matching (automatic)

Google Takeout intentionally scatters JSON sidecar files across different folders and zip archives in large exports. The original gpth only searched `file.parent` for sidecars (Strategies 1–6). This often left thousands of photos unmatched.

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

### 5. `--local-structure` — Output Folder Layout for Local Files

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

# Step 3: Full Google Takeout run
gpth -i "D:\Takeout" -o "P:\Photos" --deletesourceimage

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

## Credits

- **Original author**: [TheLastGimbus](https://github.com/TheLastGimbus) — all core gpth logic (Strategies 1–6, dedup, album handling)
- **v3.0.0 enhancements**: Cross-folder Strategy 7 matching, `--prescan`, `--limit`, `--merge-local`, `--local-structure`

The original project is licensed under Apache 2.0. This fork extends it under the same license.

---

## Original README

See [ORIGINAL_README.md](ORIGINAL_README.md) for the original project documentation.

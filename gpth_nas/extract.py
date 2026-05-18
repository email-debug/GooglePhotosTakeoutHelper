"""
Zip extraction stage.

Google Takeout exports as a series of zip files. We must extract them all to a
single staging tree before matching, because Google scatters JSON sidecars and
their media files across zips arbitrarily.

We extract one zip at a time, delete each as soon as it's done, so peak disk
usage = (remaining zips) + (extracted so far) + (one currently extracting).
"""
import time
import zipfile
from pathlib import Path
from typing import List, Tuple


def _list_zips(zip_dir: Path) -> List[Path]:
    return sorted(p for p in zip_dir.iterdir()
                  if p.is_file() and p.suffix.lower() == '.zip')


def _extract_one(zip_path: Path, staging: Path) -> int:
    """Extract zip_path into staging. Returns count of files extracted."""
    count = 0
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            if member.is_dir():
                continue
            # Extract preserving the path structure inside the zip.
            zf.extract(member, staging)
            count += 1
    return count


def extract_all_zips(
    zip_dir: Path,
    staging: Path,
    delete_after: bool = True,
    dry_run: bool = False,
) -> Tuple[int, int]:
    """
    Extract every *.zip in zip_dir into staging.

    Returns (zips_processed, total_files_extracted).
    """
    zips = _list_zips(zip_dir)
    if not zips:
        print(f'[extract] no .zip files in {zip_dir}')
        return (0, 0)

    staging.mkdir(parents=True, exist_ok=True)
    total_size = sum(z.stat().st_size for z in zips)
    print(f'[extract] found {len(zips)} zips, total {total_size/1e9:.2f} GB')
    print(f'[extract] staging: {staging}')
    print(f'[extract] delete-after-extract: {delete_after}')
    if dry_run:
        for i, z in enumerate(zips, 1):
            print(f'  [dry-run] would extract [{i}/{len(zips)}] {z.name} ({z.stat().st_size/1e9:.2f} GB)')
        return (len(zips), 0)

    total_files = 0
    t0 = time.time()
    for i, z in enumerate(zips, 1):
        size_gb = z.stat().st_size / 1e9
        print(f'[extract] [{i}/{len(zips)}] {z.name} ({size_gb:.2f} GB) ...', flush=True)
        try:
            n = _extract_one(z, staging)
        except zipfile.BadZipFile as e:
            print(f'  ERROR: bad zip — {e}. Skipping {z.name}.')
            continue
        elapsed = time.time() - t0
        total_files += n
        print(f'  [extract] +{n:,} files (cumulative {total_files:,}, '
              f'{elapsed/60:.1f} min elapsed)')
        if delete_after:
            try:
                z.unlink()
                print(f'  [extract] deleted {z.name}')
            except OSError as e:
                print(f'  [extract] could not delete {z.name}: {e}')

    print(f'[extract] done: {len(zips)} zips, {total_files:,} files in {(time.time()-t0)/60:.1f} min')
    return (len(zips), total_files)
